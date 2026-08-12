local State = require("scripts.state")
local Character = require("scripts.character")

local Movement = {}

local function distance(a, b)
    local dx = a.x - b.x
    local dy = a.y - b.y
    return math.sqrt((dx * dx) + (dy * dy))
end

local function direction_to(from_position, to_position)
    local dx = to_position.x - from_position.x
    local dy = to_position.y - from_position.y
    local margin = 0.20

    if math.abs(dx) < margin and math.abs(dy) < margin then
        return nil
    elseif math.abs(dx) < margin then
        return dy > 0 and defines.direction.south or defines.direction.north
    elseif math.abs(dy) < margin then
        return dx > 0 and defines.direction.east or defines.direction.west
    elseif dx > 0 then
        return dy > 0 and defines.direction.southeast or defines.direction.northeast
    else
        return dy > 0 and defines.direction.southwest or defines.direction.northwest
    end
end

local function emit_completion(movement, ok, message)
    if movement.command_request_id then
        State.push_event("result", {
            request_id = movement.command_request_id,
            ok = ok,
            result = {
                action = "move_to",
                task_id = movement.task_id,
                message = message,
                position = Character.get() and {
                    x = Character.get().position.x,
                    y = Character.get().position.y
                } or nil
            }
        })
    end
end

local function finish(ok, message, suppress_result)
    local data = State.ensure()
    local movement = data.movement
    local character = Character.get()

    if character then
        character.walking_state = {walking = false}
    end

    if not suppress_result then
        emit_completion(movement, ok, message)
    end

    movement.mode = "idle"
    movement.status = ok and "completed" or "failed"
    movement.path = nil
    movement.path_index = 1
    movement.path_request_id = nil
    movement.message = message
    movement.last_progress_tick = game.tick
end

local function request_path()
    local data = State.ensure()
    local movement = data.movement
    local character = Character.get()
    if not character or not movement.destination then
        return false, "No character or destination is available"
    end

    local prototype = character.prototype
    local request_id = character.surface.request_path({
        bounding_box = prototype.collision_box,
        collision_mask = prototype.collision_mask,
        start = character.position,
        goal = movement.destination,
        force = character.force,
        radius = 0.75,
        entity_to_ignore = character,
        can_open_gates = true,
        pathfind_flags = {
            cache = true,
            no_break = true,
            prefer_straight_paths = true,
            allow_paths_through_own_entities = true
        }
    })

    if not request_id then
        return false, "Factorio did not accept the path request"
    end

    movement.path_request_id = request_id
    movement.status = "requesting"
    movement.last_request_tick = game.tick
    return true, request_id
end

local function cancel_existing(reason)
    local movement = State.ensure().movement
    if movement.mode == "move" and movement.status ~= "completed" and movement.status ~= "failed" then
        finish(false, reason or "Movement replaced by a new command", false)
    end
end

function Movement.go_to(position, command_request_id, options)
    options = options or {}
    local character = Character.get()
    if not character then
        return false, "AIRI has no character body"
    end
    if type(position) ~= "table" or type(position.x) ~= "number" or type(position.y) ~= "number" then
        return false, "move_to requires numeric x and y coordinates"
    end

    cancel_existing("Movement replaced by a new command")

    local movement = State.new_movement_state()
    movement.mode = "move"
    movement.status = "requesting"
    movement.destination = {x = position.x, y = position.y}
    movement.command_request_id = command_request_id
    movement.task_id = options.task_id or State.next_id("move")
    movement.last_progress_tick = game.tick
    movement.last_position = {x = character.position.x, y = character.position.y}
    State.ensure().movement = movement

    local ok, request_or_error = request_path()
    if not ok then
        finish(false, request_or_error, options.suppress_result)
        return false, request_or_error
    end

    return true, {
        task_id = movement.task_id,
        destination = movement.destination,
        path_request_id = request_or_error
    }
end

function Movement.follow(owner_player_index)
    local character = Character.get()
    local owner = Character.get_owner(owner_player_index)
    if not character then
        return false, "AIRI has no character body"
    end
    if not owner then
        return false, "No owner player is available"
    end

    cancel_existing("Movement replaced by follow mode")
    local movement = State.new_movement_state()
    movement.mode = "follow"
    movement.status = "following"
    movement.task_id = State.next_id("follow")
    movement.last_progress_tick = game.tick
    movement.last_position = {x = character.position.x, y = character.position.y}
    State.ensure().movement = movement
    State.ensure().owner_player_index = owner.index

    return true, {
        task_id = movement.task_id,
        owner_player_index = owner.index
    }
end

function Movement.stop(reason, suppress_result)
    local movement = State.ensure().movement
    local was_active = movement.mode ~= "idle"
    if was_active and movement.mode == "move" then
        finish(false, reason or "Stopped", suppress_result)
    else
        local character = Character.get()
        if character then
            character.walking_state = {walking = false}
        end
        movement.mode = "idle"
        movement.status = "stopped"
        movement.path = nil
        movement.path_index = 1
        movement.path_request_id = nil
        movement.message = reason or "Stopped"
    end
    return true, {was_active = was_active, reason = reason or "Stopped"}
end

function Movement.handle_path_finished(event)
    local movement = State.ensure().movement
    if event.id ~= movement.path_request_id then
        return false
    end

    movement.path_request_id = nil
    movement.path = nil
    movement.path_index = 1

    if event.path then
        movement.path = {}
        for _, waypoint in ipairs(event.path) do
            local position = waypoint.position or waypoint
            table.insert(movement.path, {x = position.x, y = position.y})
        end

        if #movement.path == 0 then
            if movement.mode == "move" then
                finish(false, "Factorio returned an empty path", false)
            else
                movement.status = "waiting"
            end
        else
            movement.status = "walking"
        end
    elseif event.try_again_later then
        movement.status = "waiting"
        movement.message = "Pathfinder busy; retrying"
    else
        if movement.mode == "move" then
            finish(false, "No walkable path was found", false)
        else
            movement.status = "waiting"
            movement.message = "No follow path was found"
        end
    end
    return true
end

local function update_follow_destination(tick)
    local data = State.ensure()
    local movement = data.movement
    local character = Character.get()
    local owner = Character.get_owner(data.owner_player_index)
    if not character or not owner then
        Movement.stop("Owner or companion is unavailable", true)
        return
    end

    if character.surface ~= owner.surface then
        if Character.teleport_near_owner(owner) then
            movement.path = nil
            movement.path_index = 1
            movement.path_request_id = nil
        else
            movement.status = "waiting"
            movement.message = "Could not join the owner's surface"
            return
        end
    end

    local follow_distance = settings.global["airi-companion-follow-distance"].value
    local owner_distance = distance(character.position, owner.position)
    if owner_distance <= follow_distance then
        character.walking_state = {walking = false}
        movement.path = nil
        movement.path_index = 1
        movement.status = "following"
        return
    end

    local destination_changed = not movement.destination
        or distance(movement.destination, owner.position) > 2
    if destination_changed then
        movement.destination = {x = owner.position.x, y = owner.position.y}
        movement.path = nil
        movement.path_index = 1
    end

    if not movement.path and not movement.path_request_id and tick - movement.last_request_tick >= 30 then
        local ok, message = request_path()
        if not ok then
            movement.status = "waiting"
            movement.message = message
        end
    end
end

local function walk_path(tick)
    local movement = State.ensure().movement
    local character = Character.get()
    if not character or not movement.path then
        return
    end

    local target = movement.path[movement.path_index]
    while target and distance(character.position, target) < 0.65 do
        movement.path_index = movement.path_index + 1
        target = movement.path[movement.path_index]
    end

    if not target then
        movement.path = nil
        movement.path_index = 1
        character.walking_state = {walking = false}

        if movement.mode == "move" then
            if distance(character.position, movement.destination) <= 1.25 then
                finish(true, "Destination reached", false)
            elseif tick - movement.last_request_tick >= 15 then
                request_path()
            else
                movement.status = "waiting"
            end
        else
            movement.status = "following"
        end
        return
    end

    local direction = direction_to(character.position, target)
    if direction then
        character.walking_state = {
            walking = true,
            direction = direction
        }
    end

    if tick - movement.last_progress_tick >= 120 then
        local previous = movement.last_position or character.position
        if distance(previous, character.position) < 0.25 then
            movement.stuck_count = (movement.stuck_count or 0) + 1
            movement.path = nil
            movement.path_index = 1
            movement.path_request_id = nil
            character.walking_state = {walking = false}

            if movement.stuck_count >= 3 and movement.mode == "move" then
                finish(false, "AIRI remained stuck after three path retries", false)
                return
            end
        else
            movement.stuck_count = 0
        end

        movement.last_position = {x = character.position.x, y = character.position.y}
        movement.last_progress_tick = tick
    end
end

function Movement.tick(tick)
    local movement = State.ensure().movement
    if movement.mode == "idle" then
        return
    end

    local character = Character.get()
    if not character then
        movement.mode = "idle"
        movement.status = "failed"
        movement.message = "AIRI has no character body"
        return
    end

    if movement.mode == "follow" then
        update_follow_destination(tick)
    elseif movement.mode == "move" and movement.destination then
        if distance(character.position, movement.destination) <= 0.85 then
            finish(true, "Destination reached", false)
            return
        elseif not movement.path and not movement.path_request_id
            and tick - movement.last_request_tick >= 30 then
            local ok, message = request_path()
            if not ok then
                finish(false, message, false)
                return
            end
        end
    end

    walk_path(tick)
end

function Movement.status()
    local movement = State.ensure().movement
    return {
        mode = movement.mode,
        status = movement.status,
        destination = movement.destination,
        task_id = movement.task_id,
        message = movement.message
    }
end

return Movement
