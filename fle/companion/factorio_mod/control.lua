local State = require("scripts.state")
local Character = require("scripts.character")
local Movement = require("scripts.movement")
local Tasks = require("scripts.tasks")
local Actions = require("scripts.actions")
local Observation = require("scripts.observation")
local Transport = require("scripts.transport")
local Gui = require("scripts.gui")

local function encode_for_display(value)
    if type(value) == "string" then return value end
    local ok, encoded = pcall(function()
        return helpers.table_to_json(value)
    end)
    return ok and encoded or tostring(value)
end

local function execute_local(player, action, arguments)
    local call_ok, ok, result, asynchronous = pcall(
        Actions.execute,
        action,
        arguments or {},
        nil,
        player and player.index or nil
    )
    if not call_ok then
        if player then player.print({"", "[AIRI] ", tostring(ok)}) end
        return
    end

    if player then
        if ok then
            local prefix = asynchronous and {"gui.airi-task-accepted"} or {"gui.airi-command-complete"}
            player.print({"", "[AIRI] ", prefix, ": ", encode_for_display(result)})
        else
            player.print({"", "[AIRI] ", {"gui.airi-command-failed"}, ": ", encode_for_display(result)})
        end
    end
    Gui.refresh_all()
end

local function process_remote_command(packet)
    local cached = State.get_request(packet.id)
    if cached then
        Transport.ack(packet, cached.status == "complete" and "complete" or "accepted")
        if cached.status == "complete" then
            Transport.result(packet, cached.ok, cached.result)
        end
        return
    end

    local payload = packet.payload
    local action = payload.action
    local arguments = payload.arguments or payload.args or {}
    State.remember_request(packet.id, {status = "pending"})

    local call_ok, ok, result, asynchronous = pcall(
        Actions.execute,
        action,
        arguments,
        packet.id,
        payload.owner_player_index
    )
    if not call_ok then
        local call_error = ok
        ok = false
        result = tostring(call_error)
        asynchronous = false
    end

    Transport.ack(packet, ok and "accepted" or "rejected")
    if not ok or not asynchronous then
        State.remember_request(packet.id, {
            status = "complete",
            ok = ok,
            result = result
        })
        Transport.result(packet, ok, result)
    end
    Gui.refresh_all()
end

local function packet_matches_active_chat(packet)
    local data = State.ensure()
    local request_id = packet.payload.request_id
    return data.chat_processing
        and type(request_id) == "string"
        and request_id ~= ""
        and request_id == data.chat_request_id
end

local function handle_udp(event)
    local packet, err = Transport.decode(event)
    if not packet then
        State.ensure().bridge.last_error = err
        log("[airi-companion] rejected UDP packet: " .. tostring(err))
        return
    end

    log(string.format(
        "[airi-companion] received UDP packet type=%s id=%s instance=%d",
        packet.type,
        packet.id,
        event.player_index
    ))

    if packet.type == "ping" then
        Transport.send("pong", {request_id = packet.id})
    elseif packet.type == "command" then
        process_remote_command(packet)
    elseif packet.type == "chat_response" then
        if packet_matches_active_chat(packet) then
            Gui.show_message(packet.payload.text)
            Transport.ack(packet, "displayed")
        else
            log(string.format(
                "[airi-companion] ignored stale chat_response id=%s request_id=%s active_request_id=%s",
                tostring(packet.id),
                tostring(packet.payload.request_id),
                tostring(State.ensure().chat_request_id)
            ))
            Transport.ack(packet, "ignored-stale")
        end
    elseif packet.type == "plan" then
        if packet_matches_active_chat(packet) then
            Gui.set_plan(packet.payload.text)
            Transport.ack(packet, "displayed")
        else
            log(string.format(
                "[airi-companion] ignored stale plan id=%s request_id=%s active_request_id=%s",
                tostring(packet.id),
                tostring(packet.payload.request_id),
                tostring(State.ensure().chat_request_id)
            ))
            Transport.ack(packet, "ignored-stale")
        end
    else
        Transport.result(packet, false, "Unknown inbound packet type: " .. packet.type)
    end
end

local function handle_gui_result(player_index, gui_result)
    if not gui_result or gui_result.kind == "toggle" then return end
    local player = game.get_player(player_index)
    if not player then return end

    if gui_result.kind == "action" then
        execute_local(player, gui_result.action, gui_result.arguments)
    elseif gui_result.kind == "stop" then
        local data = State.ensure()
        local was_processing = data.chat_processing
        local active_request_id = data.chat_request_id
        if was_processing then
            -- Clear the UI first so the player is never held hostage by a
            -- blocked provider request. The request ID lets both sides reject
            -- any response that was already in flight.
            Gui.end_processing()
            local cancel_ok = Transport.send("cancel_chat", {
                request_id = active_request_id,
                player_index = player.index
            })
            if cancel_ok then
                Gui.show_system_message({"gui.airi-turn-cancelled"})
            else
                Gui.show_system_message({"gui.airi-turn-cancelled-offline"})
            end
        end
        execute_local(player, "stop", {})
    elseif gui_result.kind == "chat" then
        Gui.add_user_message(player, gui_result.text)
        local ok, request_id = Transport.send("chat", {
            text = gui_result.text,
            player_index = player.index,
            context = Observation.capture()
        })
        if ok then
            Gui.begin_processing(request_id)
        else
            Gui.end_processing()
            Gui.show_system_message({"gui.airi-bridge-unavailable"})
        end
    end
end

local function initialize()
    local data = State.ensure()
    data.plan = nil
    State.set_chat_processing(false)
    Gui.ensure_all()
    if settings.global["airi-companion-auto-spawn"].value then
        Character.spawn()
    end
    Transport.hello()
    log(string.format(
        "[airi-companion] runtime initialized; multiplayer=%s; connected_players=%d",
        tostring(game.is_multiplayer()),
        #game.connected_players
    ))
end

remote.add_interface("airi_companion", {
    execute = function(action, arguments, owner_player_index, request_id)
        return Actions.execute(
            action,
            arguments or {},
            request_id,
            owner_player_index
        )
    end,
    observe = function(radius)
        return Observation.capture(radius)
    end,
    find_resource = function(resource_name, radius)
        return Observation.find_resource(resource_name, radius)
    end,
    toggle_chat = function(player_index)
        local player = game.get_player(player_index)
        if not player then return false end
        Gui.toggle(player)
        return true
    end,
    set_activity = function(text)
        Gui.set_plan(text)
        return true
    end,
    get_character = function()
        return Character.get()
    end
})

script.on_init(initialize)
script.on_configuration_changed(initialize)

script.on_event(defines.events.on_player_created, function(event)
    local player = game.get_player(event.player_index)
    Gui.ensure(player)
    if settings.global["airi-companion-auto-spawn"].value and not Character.get() then
        Character.spawn(event.player_index)
    end
end)

script.on_event(defines.events.on_player_joined_game, function(event)
    local player = game.get_player(event.player_index)
    Gui.ensure(player)
    if settings.global["airi-companion-auto-spawn"].value and not Character.get() then
        Character.spawn(event.player_index)
    end
end)

script.on_event(defines.events.on_player_changed_force, function(event)
    if event.player_index == State.ensure().owner_player_index then
        Character.refresh_identity(true)
    end
end)

script.on_event(defines.events.on_gui_click, function(event)
    handle_gui_result(event.player_index, Gui.handle_click(event))
end)

script.on_event(defines.events.on_gui_confirmed, function(event)
    handle_gui_result(event.player_index, Gui.handle_confirmed(event))
end)

script.on_event(defines.events.on_gui_closed, function(event)
    Gui.handle_closed(event)
end)

script.on_event("airi-companion-toggle-chat", function(event)
    local player = game.get_player(event.player_index)
    if player then Gui.toggle(player) end
end)

script.on_event(defines.events.on_entity_died, function(event)
    if Character.handle_entity_died(event.entity, event.tick) then
        Gui.refresh_all()
    end
end)

script.on_event(defines.events.on_script_path_request_finished, function(event)
    Movement.handle_path_finished(event)
end)

script.on_event(defines.events.on_udp_packet_received, handle_udp)

script.on_event(defines.events.on_runtime_mod_setting_changed, function(event)
    if event.setting:match("^airi%-companion%-") then
        if event.setting == "airi-companion-display-name" then
            Character.refresh_identity(true)
        end
        Transport.hello()
        Gui.refresh_all()
    end
end)

script.on_event(defines.events.on_tick, function(event)
    Gui.tick(event.tick)

    if event.tick % 2 == 0 then
        Transport.poll()
    end

    Character.tick(event.tick)
    Tasks.tick(event.tick)
    Movement.tick(event.tick)
    Transport.flush_outbox()

    if event.tick % 60 == 0 then
        Gui.refresh_all()
    end
    if event.tick % 300 == 0 then
        Transport.send("heartbeat", {
            character = Character.status(),
            movement = Movement.status(),
            task = Tasks.status()
        })
    end
end)

local function parse_command(parameter)
    local text = parameter or ""
    local words = {}
    for word in text:gmatch("%S+") do
        table.insert(words, word)
    end
    local command = string.lower(words[1] or "")

    if command == "spawn" or command == "summon" then
        return "action", "spawn", {}
    elseif command == "dismiss" then
        return "action", "dismiss", {}
    elseif command == "follow" then
        return "action", "follow", {}
    elseif command == "stop" or command == "cancel" then
        return "action", "stop", {}
    elseif command == "status" then
        return "action", "status", {}
    elseif command == "observe" then
        return "action", "observe", {radius = tonumber(words[2])}
    elseif command == "find" or command == "find_resource" then
        return "action", "find_resource", {
            resource = words[2],
            radius = tonumber(words[3])
        }
    elseif command == "move" or command == "move_to" then
        return "action", "move_to", {x = tonumber(words[2]), y = tonumber(words[3])}
    elseif command == "mine" then
        return "action", "mine_resource", {
            resource = words[2],
            count = tonumber(words[3])
        }
    elseif command == "craft" then
        return "action", "craft_item", {
            recipe = words[2],
            count = tonumber(words[3])
        }
    elseif command == "" then
        return "toggle", nil, nil
    end
    return "chat", text, nil
end

commands.add_command("airi", {"command-help.airi"}, function(command)
    local player = command.player_index and game.get_player(command.player_index)
        or Character.get_owner()
    if not player then return end

    local kind, action_or_text, arguments = parse_command(command.parameter)
    if kind == "toggle" then
        Gui.toggle(player)
    elseif kind == "action" then
        execute_local(player, action_or_text, arguments)
    elseif kind == "chat" then
        handle_gui_result(player.index, {kind = "chat", text = action_or_text})
    end
end)
