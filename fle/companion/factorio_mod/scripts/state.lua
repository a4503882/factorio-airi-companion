local State = {}

local CHAT_HISTORY_LIMIT = 80

local function current_tick()
    return game and game.tick or 0
end

local function new_movement_state()
    return {
        mode = "idle",
        status = "idle",
        destination = nil,
        path = nil,
        path_index = 1,
        path_request_id = nil,
        command_request_id = nil,
        task_id = nil,
        last_request_tick = -100000,
        last_progress_tick = 0,
        last_position = nil,
        stuck_count = 0,
        message = nil
    }
end

function State.ensure()
    if not storage.airi_companion then
        storage.airi_companion = {
            protocol_version = 1,
            sequence = 0,
            owner_player_index = nil,
            character = nil,
            map_tag = nil,
            name_render = nil,
            marker_name = nil,
            marker_force_index = nil,
            respawn_tick = nil,
            movement = new_movement_state(),
            task = nil,
            outbox = {},
            bridge = {
                connected = false,
                last_packet_tick = nil,
                last_error = nil
            },
            recent_requests = {},
            recent_request_order = {},
            last_message = nil,
            plan = nil,
            chat_history = {},
            chat_message_sequence = 0,
            chat_processing = false,
            chat_processing_since_tick = nil,
            chat_request_id = nil
        }
    end

    local data = storage.airi_companion
    data.protocol_version = 1
    data.sequence = data.sequence or 0
    data.movement = data.movement or new_movement_state()
    data.outbox = data.outbox or {}
    data.bridge = data.bridge or {
        connected = false,
        last_packet_tick = nil,
        last_error = nil
    }
    data.recent_requests = data.recent_requests or {}
    data.recent_request_order = data.recent_request_order or {}
    data.chat_history = data.chat_history or {}
    data.chat_message_sequence = data.chat_message_sequence or 0
    if data.chat_processing == nil then
        data.chat_processing = false
    end
    if not data.chat_history_migrated then
        if #data.chat_history == 0 and data.last_message then
            data.chat_message_sequence = data.chat_message_sequence + 1
            table.insert(data.chat_history, {
                id = data.chat_message_sequence,
                role = "assistant",
                text = data.last_message,
                tick = current_tick()
            })
        end
        data.chat_history_migrated = true
    end
    return data
end

function State.new_movement_state()
    return new_movement_state()
end

function State.next_id(prefix)
    local data = State.ensure()
    data.sequence = data.sequence + 1
    return string.format("%s-%d-%d", prefix or "airi", game.tick, data.sequence)
end

function State.push_event(event_type, payload)
    local data = State.ensure()
    table.insert(data.outbox, {
        type = event_type,
        payload = payload or {}
    })

    if event_type == "result" and payload and payload.request_id then
        State.remember_request(payload.request_id, {
            status = "complete",
            ok = payload.ok,
            result = payload.result
        })
    end
end

function State.remember_request(request_id, record)
    if not request_id then return end

    local data = State.ensure()
    if not data.recent_requests[request_id] then
        table.insert(data.recent_request_order, request_id)
    end

    data.recent_requests[request_id] = record

    while #data.recent_request_order > 128 do
        local oldest = table.remove(data.recent_request_order, 1)
        data.recent_requests[oldest] = nil
    end
end

function State.get_request(request_id)
    if not request_id then return nil end
    return State.ensure().recent_requests[request_id]
end

function State.append_chat_message(role, text, speaker)
    local data = State.ensure()
    if type(text) ~= "string" and type(text) ~= "table" then
        text = tostring(text or "")
    end
    if type(text) == "string" and text == "" then
        return nil
    end

    data.chat_message_sequence = data.chat_message_sequence + 1
    local message = {
        id = data.chat_message_sequence,
        role = role or "system",
        text = text,
        speaker = speaker,
        tick = current_tick()
    }
    table.insert(data.chat_history, message)
    while #data.chat_history > CHAT_HISTORY_LIMIT do
        table.remove(data.chat_history, 1)
    end
    return message
end

function State.set_chat_processing(active, request_id)
    local data = State.ensure()
    active = not not active
    if active and not data.chat_processing then
        data.chat_processing_since_tick = current_tick()
    elseif not active then
        data.chat_processing_since_tick = nil
        data.chat_request_id = nil
    end
    if active and type(request_id) == "string" and request_id ~= "" then
        data.chat_request_id = request_id
    end
    data.chat_processing = active
end

return State
