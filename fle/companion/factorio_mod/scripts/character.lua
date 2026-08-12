local State = require("scripts.state")

local Character = {}
local MARKER_UPDATE_INTERVAL = 30
local DEFAULT_DISPLAY_NAME = "团子"

local function is_valid(object)
    return object and object.valid
end

local function display_name()
    local configured = settings.global["airi-companion-display-name"]
    local value = configured and configured.value or DEFAULT_DISPLAY_NAME
    if type(value) ~= "string" or value:match("^%s*$") then
        return DEFAULT_DISPLAY_NAME
    end
    return value
end

local function destroy_if_valid(object)
    if is_valid(object) then
        object.destroy()
    end
end

function Character.clear_identity_markers()
    local data = State.ensure()
    destroy_if_valid(data.map_tag)
    destroy_if_valid(data.name_render)
    data.map_tag = nil
    data.name_render = nil
    data.marker_name = nil
    data.marker_force_index = nil
end

local function create_identity_markers(character, owner, name)
    local data = State.ensure()
    data.name_render = rendering.draw_text({
        text = name,
        surface = character.surface,
        target = {
            type = "entity",
            entity = character,
            offset = {0, -2.25}
        },
        color = {r = 0.30, g = 0.82, b = 1.00, a = 1.00},
        alignment = "center",
        vertical_alignment = "bottom",
        scale = 1.0,
        scale_with_zoom = true,
        forces = {character.force},
        render_mode = "game"
    })

    local tag_specification = {
        position = character.position,
        icon = {type = "virtual", name = "signal-info"},
        text = name
    }
    if owner then
        tag_specification.last_user = owner.index
    end
    data.map_tag = character.force.add_chart_tag(
        character.surface,
        tag_specification
    )
    data.marker_name = name
    data.marker_force_index = character.force.index
end

function Character.refresh_identity(force_rebuild)
    local data = State.ensure()
    local character = Character.get()
    if not character then
        Character.clear_identity_markers()
        return false
    end

    local owner = Character.get_owner(data.owner_player_index)
    local force_changed = false
    if owner and character.force ~= owner.force then
        character.force = owner.force
        force_changed = true
    end

    local name = display_name()
    local surface_changed = is_valid(data.map_tag)
        and data.map_tag.surface ~= character.surface
    local rebuild = force_rebuild
        or force_changed
        or surface_changed
        or data.marker_name ~= name
        or data.marker_force_index ~= character.force.index
        or not is_valid(data.map_tag)
        or not is_valid(data.name_render)

    if rebuild then
        Character.clear_identity_markers()
        create_identity_markers(character, owner, name)
    else
        data.map_tag.position = character.position
        data.map_tag.text = name
    end
    return true
end

function Character.get()
    local character = State.ensure().character
    if is_valid(character) then
        return character
    end
    return nil
end

function Character.get_owner(preferred_player_index)
    local data = State.ensure()
    local player_index = preferred_player_index or data.owner_player_index

    if player_index then
        local player = game.get_player(player_index)
        if is_valid(player) then
            return player
        end
    end

    for _, player in pairs(game.connected_players) do
        if is_valid(player) then
            return player
        end
    end

    for _, player in pairs(game.players) do
        if is_valid(player) then
            return player
        end
    end

    return nil
end

local function spawn_position_for(owner)
    local desired = {
        x = owner.position.x + 2,
        y = owner.position.y
    }

    return owner.surface.find_non_colliding_position(
        "character",
        desired,
        32,
        0.5
    )
end

function Character.spawn(owner_player_index)
    local data = State.ensure()
    local current = Character.get()
    if current then
        local current_owner = Character.get_owner(owner_player_index)
        if current_owner then
            data.owner_player_index = current_owner.index
        end
        Character.refresh_identity(false)
        return true, {
            already_present = true,
            display_name = display_name(),
            force = current.force.name,
            position = {x = current.position.x, y = current.position.y},
            surface = current.surface.name
        }
    end

    local owner = Character.get_owner(owner_player_index)
    if not owner then
        return false, "No Factorio player is available to own the companion"
    end

    local position = spawn_position_for(owner)
    if not position then
        return false, "No non-colliding position was found near the owner"
    end

    local character = owner.surface.create_entity({
        name = "character",
        position = position,
        force = owner.force
    })

    if not character then
        return false, "Factorio did not create the companion character"
    end

    character.color = {r = 0.30, g = 0.72, b = 1.00, a = 1.00}
    data.character = character
    data.owner_player_index = owner.index
    data.respawn_tick = nil
    data.movement = State.new_movement_state()
    data.task = nil
    Character.refresh_identity(true)

    State.push_event("character_spawned", {
        owner_player_index = owner.index,
        display_name = display_name(),
        force = character.force.name,
        position = {x = character.position.x, y = character.position.y},
        surface = character.surface.name
    })
    log(string.format(
        "[airi-companion] spawned character for player %d at %.2f, %.2f on %s",
        owner.index,
        character.position.x,
        character.position.y,
        character.surface.name
    ))

    return true, {
        already_present = false,
        owner_player_index = owner.index,
        display_name = display_name(),
        force = character.force.name,
        position = {x = character.position.x, y = character.position.y},
        surface = character.surface.name
    }
end

function Character.dismiss()
    local data = State.ensure()
    local character = Character.get()
    if not character then
        return true, {already_absent = true}
    end

    Character.clear_identity_markers()
    character.destroy()
    data.character = nil
    data.respawn_tick = nil
    data.movement = State.new_movement_state()
    data.task = nil
    State.push_event("character_dismissed", {})
    log("[airi-companion] dismissed character")
    return true, {already_absent = false}
end

function Character.handle_entity_died(entity, tick)
    local data = State.ensure()
    if not data.character or entity ~= data.character then
        return false
    end

    Character.clear_identity_markers()
    data.character = nil
    data.movement = State.new_movement_state()
    data.task = nil

    local respawn_seconds = settings.global["airi-companion-respawn-seconds"].value
    data.respawn_tick = tick + (respawn_seconds * 60)
    State.push_event("character_died", {
        respawn_tick = data.respawn_tick
    })
    log(string.format(
        "[airi-companion] character died; respawn scheduled for tick %d",
        data.respawn_tick
    ))
    return true
end

function Character.teleport_near_owner(owner)
    local character = Character.get()
    if not character or not owner then
        return false
    end

    local position = spawn_position_for(owner)
    if not position then
        return false
    end

    local teleported = character.teleport(position, owner.surface)
    if teleported then
        Character.refresh_identity(true)
    end
    return teleported
end

function Character.tick(tick)
    local data = State.ensure()
    if Character.get() then
        if tick % MARKER_UPDATE_INTERVAL == 0 then
            Character.refresh_identity(false)
        end
        return
    end

    if data.map_tag or data.name_render then
        Character.clear_identity_markers()
    end

    if data.respawn_tick and tick >= data.respawn_tick then
        data.respawn_tick = nil
        Character.spawn(data.owner_player_index)
        return
    end

    if settings.global["airi-companion-auto-spawn"].value and not data.respawn_tick then
        local owner = Character.get_owner()
        if owner then
            Character.spawn(owner.index)
        end
    end
end

function Character.status()
    local data = State.ensure()
    local character = Character.get()
    if not character then
        return {
            present = false,
            owner_player_index = data.owner_player_index,
            respawn_tick = data.respawn_tick
        }
    end

    local owner = Character.get_owner(data.owner_player_index)
    local map_tag_present = is_valid(data.map_tag)

    return {
        present = true,
        owner_player_index = data.owner_player_index,
        display_name = display_name(),
        force = character.force.name,
        same_force_as_owner = owner and character.force == owner.force or false,
        position = {x = character.position.x, y = character.position.y},
        surface = character.surface.name,
        health = character.health,
        max_health = character.max_health,
        name_label_present = is_valid(data.name_render),
        map_tag = {
            present = map_tag_present,
            text = map_tag_present and data.map_tag.text or nil,
            force = map_tag_present and data.map_tag.force.name or nil,
            surface = map_tag_present and data.map_tag.surface.name or nil,
            position = map_tag_present and {
                x = data.map_tag.position.x,
                y = data.map_tag.position.y
            } or nil
        }
    }
end

return Character
