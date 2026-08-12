local State = require("scripts.state")
local Character = require("scripts.character")
local Movement = require("scripts.movement")

local Tasks = {}

local function distance(a, b)
    local dx = a.x - b.x
    local dy = a.y - b.y
    return math.sqrt((dx * dx) + (dy * dy))
end

local function stop_mining(character)
    if character then
        character.mining_state = {mining = false}
    end
end

local function push_result(task, ok, message, extra)
    if not task.request_id then
        return
    end

    local result = extra or {}
    result.action = task.kind
    result.task_id = task.id
    result.message = message
    State.push_event("result", {
        request_id = task.request_id,
        ok = ok,
        result = result
    })
end

local function finish(ok, message, extra)
    local data = State.ensure()
    local task = data.task
    if not task then return end

    local character = Character.get()
    stop_mining(character)
    Movement.stop(message, true)
    push_result(task, ok, message, extra)
    data.task = nil
end

local function first_item_product(entity)
    if not entity or not entity.valid then return nil end
    local properties = entity.prototype.mineable_properties
    if not properties or not properties.products then return nil end

    for _, product in pairs(properties.products) do
        if product.type == "item" then
            return product.name
        end
    end
    return nil
end

local function nearest_resource(character, resource_name)
    local radius = settings.global["airi-companion-perception-radius"].value
    local entities = character.surface.find_entities_filtered({
        position = character.position,
        radius = radius,
        type = "resource",
        name = resource_name,
        limit = 256
    })

    local nearest = nil
    local nearest_distance = nil
    for _, entity in pairs(entities) do
        if entity.valid and entity.minable then
            local entity_distance = distance(character.position, entity.position)
            if not nearest_distance or entity_distance < nearest_distance then
                nearest = entity
                nearest_distance = entity_distance
            end
        end
    end
    return nearest, nearest_distance
end

function Tasks.start_mining(resource_name, count, request_id)
    local character = Character.get()
    if not character then
        return false, "AIRI has no character body"
    end
    if type(resource_name) ~= "string" or resource_name == "" then
        return false, "mine_resource requires a resource name"
    end

    count = math.floor(tonumber(count) or 0)
    if count < 1 or count > 10000 then
        return false, "mine_resource count must be between 1 and 10000"
    end

    if State.ensure().task then
        Tasks.cancel("Replaced by a new mining task")
    end

    local task = {
        id = State.next_id("mine"),
        kind = "mine_resource",
        request_id = request_id,
        resource_name = resource_name,
        requested_count = count,
        phase = "seeking",
        target = nil,
        product_name = nil,
        baseline_count = nil,
        collected_count = 0,
        started_tick = game.tick,
        last_progress_tick = game.tick
    }
    State.ensure().task = task
    return true, {
        task_id = task.id,
        resource = resource_name,
        count = count
    }
end

function Tasks.cancel(reason)
    local data = State.ensure()
    local task = data.task
    if not task then
        return true, {was_active = false}
    end

    stop_mining(Character.get())
    Movement.stop(reason or "Task cancelled", true)
    push_result(task, false, reason or "Task cancelled", {
        collected = task.collected_count or 0,
        requested = task.requested_count
    })
    data.task = nil
    return true, {was_active = true, task_id = task.id}
end

local function update_collected(task, character)
    if not task.product_name or task.baseline_count == nil then
        return
    end

    local current = character.get_item_count(task.product_name)
    local collected = math.max(0, current - task.baseline_count)
    if collected > task.collected_count then
        task.collected_count = collected
        task.last_progress_tick = game.tick
    end
end

local function seek_target(task, character)
    local target = nearest_resource(character, task.resource_name)
    if not target then
        finish(false, "No matching resource is visible within the configured perception radius", {
            resource = task.resource_name,
            collected = task.collected_count,
            requested = task.requested_count
        })
        return
    end

    local product_name = first_item_product(target)
    if not product_name then
        finish(false, "The selected resource has no hand-mineable item product", {
            resource = task.resource_name
        })
        return
    end

    if not task.product_name then
        task.product_name = product_name
        task.baseline_count = character.get_item_count(product_name)
    end

    task.target = target
    character.update_selected_entity(target.position)
    local reach = character.resource_reach_distance or 2.7
    if distance(character.position, target.position) <= reach then
        task.phase = "mining"
    else
        task.phase = "moving"
        local ok, result = Movement.go_to(target.position, nil, {
            task_id = task.id,
            suppress_result = true
        })
        if not ok then
            finish(false, result, {
                resource = task.resource_name,
                collected = task.collected_count,
                requested = task.requested_count
            })
        end
    end
end

local function mine_target(task, character)
    update_collected(task, character)
    if task.collected_count >= task.requested_count then
        finish(true, "Requested resources collected", {
            resource = task.resource_name,
            product = task.product_name,
            collected = task.collected_count,
            requested = task.requested_count
        })
        return
    end

    if not task.target or not task.target.valid then
        task.target = nil
        task.phase = "seeking"
        return
    end

    local reach = character.resource_reach_distance or 2.7
    if distance(character.position, task.target.position) > reach then
        stop_mining(character)
        task.phase = "moving"
        local ok, result = Movement.go_to(task.target.position, nil, {
            task_id = task.id,
            suppress_result = true
        })
        if not ok then
            finish(false, result, {
                resource = task.resource_name,
                collected = task.collected_count,
                requested = task.requested_count
            })
        end
        return
    end

    character.update_selected_entity(task.target.position)
    character.mining_state = {
        mining = true,
        position = task.target.position
    }

    if game.tick - task.last_progress_tick > 1800 then
        finish(false, "Mining made no progress for 30 seconds", {
            resource = task.resource_name,
            collected = task.collected_count,
            requested = task.requested_count
        })
    end
end

function Tasks.tick()
    local task = State.ensure().task
    if not task then return end

    local character = Character.get()
    if not character then
        finish(false, "AIRI lost her character body", {})
        return
    end

    if task.kind ~= "mine_resource" then
        finish(false, "Unsupported task type: " .. tostring(task.kind), {})
        return
    end

    update_collected(task, character)
    if task.collected_count >= task.requested_count then
        finish(true, "Requested resources collected", {
            resource = task.resource_name,
            product = task.product_name,
            collected = task.collected_count,
            requested = task.requested_count
        })
        return
    end

    if task.phase == "seeking" then
        seek_target(task, character)
    elseif task.phase == "moving" then
        if not task.target or not task.target.valid then
            Movement.stop("Mining target disappeared", true)
            task.target = nil
            task.phase = "seeking"
            return
        end

        local reach = character.resource_reach_distance or 2.7
        if distance(character.position, task.target.position) <= reach then
            Movement.stop("Mining target reached", true)
            task.phase = "mining"
        else
            local movement = Movement.status()
            if movement.task_id == task.id and movement.status == "failed" then
                finish(false, movement.message or "Could not reach the mining target", {
                    resource = task.resource_name,
                    collected = task.collected_count,
                    requested = task.requested_count
                })
            end
        end
    elseif task.phase == "mining" then
        mine_target(task, character)
    end
end

function Tasks.status()
    local task = State.ensure().task
    if not task then
        return {active = false}
    end
    return {
        active = true,
        id = task.id,
        kind = task.kind,
        phase = task.phase,
        resource = task.resource_name,
        requested = task.requested_count,
        collected = task.collected_count
    }
end

return Tasks
