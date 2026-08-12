local State = require("scripts.state")
local Character = require("scripts.character")
local Movement = require("scripts.movement")
local Tasks = require("scripts.tasks")

local Gui = {}

local TOGGLE_NAME = "airi_companion_toggle"
local PANEL_NAME = "airi_companion_chat_panel"
local LEGACY_PANEL_NAME = "airi_companion_panel"
local TITLE_FLOW_NAME = "airi_companion_title_flow"
local CLOSE_NAME = "airi_companion_close"
local STATUS_NAME = "airi_companion_status"
local ACTIVITY_NAME = "airi_companion_activity"
local HISTORY_NAME = "airi_companion_history"
local MESSAGES_NAME = "airi_companion_messages"
local INPUT_FLOW_NAME = "airi_companion_input_flow"
local INPUT_NAME = "airi_companion_input"
local SEND_NAME = "airi_companion_send"
local CONTROLS_NAME = "airi_companion_controls"
local SPAWN_NAME = "airi_companion_spawn"
local FOLLOW_NAME = "airi_companion_follow"
local STOP_NAME = "airi_companion_stop"

local MAX_INPUT_LENGTH = 4000
local BRIDGE_FRESH_TICKS = 600
local pending_latest_scroll = {}

local function current_tick()
    return game and game.tick or 0
end

local function display_name()
    return settings.global["airi-companion-display-name"].value
end

local function bridge_is_connected()
    local bridge = State.ensure().bridge
    return bridge.connected
        and bridge.last_packet_tick ~= nil
        and current_tick() - bridge.last_packet_tick <= BRIDGE_FRESH_TICKS
end

local function status_caption()
    local character = Character.get()
    local movement = Movement.status()
    local task = Tasks.status()
    local body = character and {"gui.airi-body-present"} or {"gui.airi-body-absent"}
    local bridge = bridge_is_connected() and {"gui.airi-bridge-connected"}
        or {"gui.airi-bridge-disconnected"}
    local activity = task.active and (task.kind .. "/" .. task.phase)
        or (movement.mode .. "/" .. movement.status)

    return {"", {"gui.airi-status-prefix"}, " ", body, " · ", bridge, " · ", activity}
end

local function toggle_caption()
    return {"", display_name(), " · ", {"gui.airi-chat"}}
end

local function panel(player)
    return player and player.valid and player.gui.screen[PANEL_NAME] or nil
end

local function scroll_history_to_latest(player)
    local root = panel(player)
    local history = root and root[HISTORY_NAME]
    if not history or not history.valid then return false end
    history.scroll_to_bottom()
    return true
end

local function schedule_latest_scroll(player)
    if not player or not player.valid then return end
    pending_latest_scroll[player.index] = current_tick() + 1
end

local function input_element(root)
    local input_flow = root and root[INPUT_FLOW_NAME]
    return input_flow and input_flow[INPUT_NAME] or nil
end

local function send_element(root)
    local input_flow = root and root[INPUT_FLOW_NAME]
    return input_flow and input_flow[SEND_NAME] or nil
end

local function message_header(message)
    if message.role == "user" then
        return message.speaker or {"gui.airi-message-user"}
    elseif message.role == "assistant" then
        return display_name()
    end
    return {"gui.airi-message-system"}
end

local function role_color(role)
    if role == "user" then
        return {r = 0.45, g = 0.75, b = 1}
    elseif role == "assistant" then
        return {r = 1, g = 0.78, b = 0.42}
    end
    return {r = 0.72, g = 0.72, b = 0.72}
end

local function render_history(player)
    local root = panel(player)
    local history = root and root[HISTORY_NAME]
    local messages = history and history[MESSAGES_NAME]
    if not messages then return end

    messages.clear()
    local chat_history = State.ensure().chat_history
    if #chat_history == 0 then
        local empty = messages.add({
            type = "label",
            caption = {"gui.airi-chat-empty"}
        })
        empty.style.font_color = {r = 0.65, g = 0.65, b = 0.65}
        empty.style.top_margin = 12
        empty.style.left_margin = 8
    else
        for index, message in ipairs(chat_history) do
            local block = messages.add({type = "flow", direction = "vertical"})
            block.style.horizontally_stretchable = true
            block.style.padding = {6, 8}

            local header = block.add({
                type = "label",
                caption = message_header(message)
            })
            header.style.font = "default-bold"
            header.style.font_color = role_color(message.role)

            local body = block.add({type = "label", caption = message.text})
            body.style.single_line = false
            body.style.maximal_width = 600
            body.style.horizontally_stretchable = true

            if index < #chat_history then
                messages.add({type = "line", direction = "horizontal"})
            end
        end
    end
    scroll_history_to_latest(player)
    -- GUI dimensions settle after the current event. Retry on the next tick so
    -- a newly opened or rebuilt history reliably lands on its newest message.
    schedule_latest_scroll(player)
end

local function activity_caption()
    local data = State.ensure()
    if data.chat_processing then
        local tick = current_tick()
        local since = data.chat_processing_since_tick or tick
        local elapsed = math.max(0, math.floor((tick - since) / 60))
        local dots = string.rep("·", (math.floor(tick / 30) % 3) + 1)
        if not bridge_is_connected() then
            return {"gui.airi-processing-disconnected", dots, elapsed}, "disconnected"
        end
        return {"gui.airi-processing", dots, elapsed}, "processing"
    end
    if bridge_is_connected() then
        return {"gui.airi-chat-ready"}, "ready"
    end
    return {"gui.airi-chat-offline"}, "disconnected"
end

function Gui.ensure(player)
    if not player or not player.valid then return end
    local legacy_panel = player.gui.screen[LEGACY_PANEL_NAME]
    if legacy_panel then legacy_panel.destroy() end
    local button = player.gui.top[TOGGLE_NAME]
    if not button then
        button = player.gui.top.add({
            type = "button",
            name = TOGGLE_NAME,
            caption = toggle_caption(),
            tooltip = {"gui.airi-toggle-tooltip"}
        })
    else
        button.caption = toggle_caption()
    end
end

function Gui.ensure_all()
    for _, player in pairs(game.players) do
        Gui.ensure(player)
    end
end

function Gui.open(player)
    if not player or not player.valid then return end
    local existing = panel(player)
    if existing then
        scroll_history_to_latest(player)
        schedule_latest_scroll(player)
        local existing_input = input_element(existing)
        if existing_input then existing_input.focus() end
        return
    end

    local frame = player.gui.screen.add({
        type = "frame",
        name = PANEL_NAME,
        direction = "vertical"
    })
    frame.auto_center = true
    frame.style.width = 680

    local title_flow = frame.add({
        type = "flow",
        name = TITLE_FLOW_NAME,
        direction = "horizontal"
    })
    title_flow.style.horizontally_stretchable = true
    local title = title_flow.add({
        type = "label",
        caption = {"", display_name(), " · ", {"gui.airi-chat"}}
    })
    title.style.font = "default-large-bold"
    local dragger = title_flow.add({type = "empty-widget", style = "draggable_space"})
    dragger.style.horizontally_stretchable = true
    dragger.style.height = 28
    dragger.drag_target = frame
    title_flow.add({
        type = "button",
        name = CLOSE_NAME,
        caption = "×",
        tooltip = {"gui.airi-close"}
    })

    local status = frame.add({
        type = "label",
        name = STATUS_NAME,
        caption = status_caption()
    })
    status.style.single_line = false
    status.style.maximal_width = 640

    local activity = frame.add({
        type = "label",
        name = ACTIVITY_NAME,
        caption = {"gui.airi-chat-offline"}
    })
    activity.style.font = "default-bold"
    activity.style.bottom_margin = 4

    local history = frame.add({
        type = "scroll-pane",
        name = HISTORY_NAME,
        direction = "vertical"
    })
    history.style.width = 640
    history.style.height = 360
    history.vertical_scroll_policy = "auto"
    history.horizontal_scroll_policy = "never"
    local messages = history.add({
        type = "flow",
        name = MESSAGES_NAME,
        direction = "vertical"
    })
    messages.style.horizontally_stretchable = true

    local input_flow = frame.add({
        type = "flow",
        name = INPUT_FLOW_NAME,
        direction = "horizontal"
    })
    input_flow.style.horizontally_stretchable = true
    input_flow.style.top_margin = 6
    local input = input_flow.add({
        type = "textfield",
        name = INPUT_NAME,
        text = "",
        tooltip = {"gui.airi-input-tooltip"}
    })
    input.style.width = 545
    input.lose_focus_on_confirm = false
    local send = input_flow.add({
        type = "button",
        name = SEND_NAME,
        caption = {"gui.airi-send"}
    })
    send.style.horizontally_stretchable = true

    local controls = frame.add({
        type = "flow",
        name = CONTROLS_NAME,
        direction = "horizontal"
    })
    controls.style.horizontally_stretchable = true
    controls.add({type = "button", name = SPAWN_NAME, caption = {"gui.airi-spawn"}})
    controls.add({type = "button", name = FOLLOW_NAME, caption = {"gui.airi-follow"}})
    controls.add({type = "button", name = STOP_NAME, caption = {"gui.airi-stop"}})
    local spacer = controls.add({type = "empty-widget"})
    spacer.style.horizontally_stretchable = true
    local shortcut = controls.add({type = "label", caption = {"gui.airi-shortcut-hint"}})
    shortcut.style.font_color = {r = 0.65, g = 0.65, b = 0.65}

    player.opened = frame
    render_history(player)
    Gui.refresh(player)
    input.focus()
end

function Gui.close(player)
    if not player or not player.valid then return end
    pending_latest_scroll[player.index] = nil
    local root = panel(player)
    if root then root.destroy() end
end

function Gui.tick(tick)
    for player_index, due_tick in pairs(pending_latest_scroll) do
        if tick >= due_tick then
            pending_latest_scroll[player_index] = nil
            local player = game.get_player(player_index)
            if player then scroll_history_to_latest(player) end
        end
    end
end

function Gui.toggle(player)
    if panel(player) then
        Gui.close(player)
    else
        Gui.open(player)
    end
end

function Gui.refresh(player)
    if not player or not player.valid then return end
    Gui.ensure(player)
    local root = panel(player)
    if not root then return end

    local status = root[STATUS_NAME]
    if status then status.caption = status_caption() end

    local activity = root[ACTIVITY_NAME]
    local caption, state = activity_caption()
    if activity then
        activity.caption = caption
        if state == "processing" then
            activity.style.font_color = {r = 1, g = 0.72, b = 0.25}
        elseif state == "ready" then
            activity.style.font_color = {r = 0.45, g = 0.9, b = 0.5}
        else
            activity.style.font_color = {r = 1, g = 0.45, b = 0.4}
        end
    end

    local send = send_element(root)
    if send then
        send.enabled = bridge_is_connected() and not State.ensure().chat_processing
    end
end

function Gui.refresh_all(history_changed)
    for _, player in pairs(game.players) do
        Gui.refresh(player)
        if history_changed then render_history(player) end
    end
end

function Gui.add_user_message(player, text)
    State.append_chat_message("user", text, player and player.name or nil)
    Gui.refresh_all(true)
end

function Gui.show_message(text)
    text = tostring(text or "")
    local data = State.ensure()
    data.last_message = text
    State.append_chat_message("assistant", text)
    State.set_chat_processing(false)
    Gui.refresh_all(true)
end

function Gui.show_system_message(text)
    State.append_chat_message("system", text)
    Gui.refresh_all(true)
end

function Gui.set_plan(text)
    local rendered = tostring(text or "")
    State.ensure().plan = rendered
    -- An empty plan is sent immediately before the final chat response. Keep
    -- the turn active until that correlated response arrives (or Stop cancels
    -- it), otherwise the response could be mistaken for a stale packet.
    if rendered ~= "" then
        State.set_chat_processing(true)
    end
    Gui.refresh_all()
end

function Gui.begin_processing(request_id)
    State.set_chat_processing(true, request_id)
    Gui.refresh_all()
end

function Gui.end_processing()
    State.ensure().plan = nil
    State.set_chat_processing(false)
    Gui.refresh_all()
end

local function submit_text(player)
    local root = panel(player)
    if not root then return nil end
    local input = input_element(root)
    if not input then return nil end

    local text = input.text
    if not text or text:match("^%s*$") then
        input.focus()
        return nil
    end
    if not bridge_is_connected() then
        Gui.show_system_message({"gui.airi-bridge-unavailable"})
        input.focus()
        return nil
    end
    if #text > MAX_INPUT_LENGTH then
        Gui.show_system_message({"gui.airi-message-too-long", MAX_INPUT_LENGTH})
        input.focus()
        return nil
    end
    if State.ensure().chat_processing then
        Gui.show_system_message({"gui.airi-wait-current"})
        input.focus()
        return nil
    end

    input.text = ""
    input.focus()
    return {kind = "chat", text = text}
end

function Gui.handle_click(event)
    local element = event.element
    if not element or not element.valid then return nil end
    local player = game.get_player(event.player_index)
    if not player then return nil end

    if element.name == TOGGLE_NAME then
        Gui.toggle(player)
        return {kind = "toggle"}
    elseif element.name == CLOSE_NAME then
        Gui.close(player)
        return {kind = "toggle"}
    elseif element.name == SEND_NAME then
        return submit_text(player)
    elseif element.name == SPAWN_NAME then
        return {kind = "action", action = "spawn", arguments = {}}
    elseif element.name == FOLLOW_NAME then
        return {kind = "action", action = "follow", arguments = {}}
    elseif element.name == STOP_NAME then
        return {kind = "stop"}
    end
    return nil
end

function Gui.handle_confirmed(event)
    local element = event.element
    if not element or not element.valid or element.name ~= INPUT_NAME then
        return nil
    end
    local player = game.get_player(event.player_index)
    if not player then return nil end
    return submit_text(player)
end

function Gui.handle_closed(event)
    local element = event.element
    if not element or not element.valid or element.name ~= PANEL_NAME then
        return false
    end
    local player = game.get_player(event.player_index)
    if not player then return false end
    Gui.close(player)
    return true
end

return Gui
