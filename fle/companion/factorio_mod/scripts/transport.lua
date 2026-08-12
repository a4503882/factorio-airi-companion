local State = require("scripts.state")

local Transport = {}

local function bridge_port()
    return settings.global["airi-companion-bridge-port"].value
end

local function session_token()
    return settings.global["airi-companion-session-token"].value
end

-- UDP is enabled per Factorio instance. This companion intentionally binds to
-- the owner's graphical client, matching its single-client design. Factorio
-- 2.0.77 can crash inside TickClosure when a dedicated server consumes a UDP
-- packet through instance 0, so a server without the owner simply leaves the
-- bridge disabled instead of falling back to that unsafe endpoint.
local function udp_instance()
    -- In single-player the optional for_player argument must be omitted. The
    -- `false` sentinel below means "use the sole local instance".
    if not game.is_multiplayer() then
        return false
    end

    local owner_index = State.ensure().owner_player_index
    for _, player in pairs(game.connected_players) do
        if player.index == owner_index then
            return player.index
        end
    end

    return nil
end

function Transport.send(packet_type, payload, packet_id)
    local data = State.ensure()
    local instance = udp_instance()
    if instance == nil then
        data.bridge.last_error = "AIRI UDP is waiting for the owner player client"
        return false, data.bridge.last_error
    end

    local packet = {
        version = data.protocol_version,
        id = packet_id or State.next_id("event"),
        type = packet_type,
        tick = game.tick,
        token = session_token(),
        payload = payload or {}
    }

    local encoded
    local encoded_ok, encoded_error = pcall(function()
        encoded = helpers.table_to_json(packet)
    end)
    if not encoded_ok then
        data.bridge.last_error = "JSON encoding failed: " .. tostring(encoded_error)
        return false, data.bridge.last_error
    end

    local sent_ok, sent_error = pcall(function()
        if instance == false then
            helpers.send_udp(bridge_port(), encoded)
        else
            helpers.send_udp(bridge_port(), encoded, instance)
        end
    end)
    if not sent_ok then
        data.bridge.last_error = "UDP send failed: " .. tostring(sent_error)
        return false, data.bridge.last_error
    end

    return true, packet.id
end

function Transport.poll()
    local data = State.ensure()
    local instance = udp_instance()
    if instance == nil then
        data.bridge.last_error = "AIRI UDP is waiting for the owner player client"
        return false
    end

    local ok, err = pcall(function()
        if instance == false then
            helpers.recv_udp()
        else
            helpers.recv_udp(instance)
        end
    end)
    if not ok then
        local message = "UDP receive unavailable: " .. tostring(err)
        if data.bridge.last_error ~= message then
            log("[airi-companion] " .. message)
        end
        data.bridge.last_error = message
        return false
    end
    return true
end

function Transport.decode(event)
    local data = State.ensure()
    if event.source_port ~= bridge_port() then
        return nil, "Packet source port is not the configured AIRI bridge port"
    end
    if #event.payload > 60000 then
        return nil, "Packet exceeds the AIRI protocol size limit"
    end

    local packet
    local ok, err = pcall(function()
        packet = helpers.json_to_table(event.payload)
    end)
    if not ok or type(packet) ~= "table" then
        return nil, "Invalid JSON packet: " .. tostring(err or "not an object")
    end
    if packet.version ~= data.protocol_version then
        return nil, "Unsupported protocol version: " .. tostring(packet.version)
    end
    if type(packet.id) ~= "string" or packet.id == "" then
        return nil, "Packet id is required"
    end
    if type(packet.type) ~= "string" or packet.type == "" then
        return nil, "Packet type is required"
    end

    local expected_token = session_token()
    if expected_token ~= "" and packet.token ~= expected_token then
        return nil, "Packet session token did not match"
    end

    packet.payload = type(packet.payload) == "table" and packet.payload or {}
    data.bridge.connected = true
    data.bridge.last_packet_tick = game.tick
    data.bridge.last_error = nil
    return packet, nil
end

function Transport.ack(packet, status)
    return Transport.send("ack", {
        request_id = packet.id,
        status = status or "accepted"
    })
end

function Transport.result(packet_or_id, ok, result)
    local request_id = type(packet_or_id) == "table" and packet_or_id.id or packet_or_id
    return Transport.send("result", {
        request_id = request_id,
        ok = ok,
        result = result
    })
end

function Transport.flush_outbox()
    local outbox = State.ensure().outbox
    while #outbox > 0 do
        local event = outbox[1]
        local ok = Transport.send(event.type, event.payload)
        if not ok then
            break
        end
        table.remove(outbox, 1)
    end

    while #outbox > 128 do
        table.remove(outbox, 1)
    end
end

function Transport.hello()
    return Transport.send("hello", {
        mod = "airi-companion",
        mod_version = "0.1.0",
        factorio_version = script.active_mods.base,
        character_present = State.ensure().character and State.ensure().character.valid or false
    })
end

return Transport
