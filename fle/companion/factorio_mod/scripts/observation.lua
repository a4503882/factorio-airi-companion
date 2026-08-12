local State = require("scripts.state")
local Character = require("scripts.character")
local Movement = require("scripts.movement")

local Observation = {}

local RESOURCE_SAMPLE_LIMIT = 256
local RESOURCE_NEAREST_INITIAL_RADIUS = 4
local ENTITY_RESULT_LIMIT = 96

local DIRECTION_NAMES = {
    [defines.direction.north] = "north",
    [defines.direction.east] = "east",
    [defines.direction.south] = "south",
    [defines.direction.west] = "west"
}

local ENTITY_STATUS_NAMES = {}
for name, value in pairs(defines.entity_status) do
    ENTITY_STATUS_NAMES[value] = name
end

local INVENTORY_DEFINITIONS = {
    chest = defines.inventory.chest,
    fuel = defines.inventory.fuel,
    burnt_result = defines.inventory.burnt_result,
    furnace_source = defines.inventory.furnace_source,
    furnace_result = defines.inventory.furnace_result,
    furnace_modules = defines.inventory.furnace_modules,
    assembling_machine_input = defines.inventory.assembling_machine_input,
    assembling_machine_output = defines.inventory.assembling_machine_output,
    assembling_machine_modules = defines.inventory.assembling_machine_modules,
    lab_input = defines.inventory.lab_input,
    lab_modules = defines.inventory.lab_modules,
    mining_drill_modules = defines.inventory.mining_drill_modules,
    turret_ammo = defines.inventory.turret_ammo
}

local function inventory_labels_for_entity(entity)
    local entity_type = entity.type
    if entity_type == "container" or entity_type == "logistic-container" then
        return {"chest"}
    elseif entity_type == "furnace" then
        return {
            "furnace_source",
            "furnace_result",
            "fuel",
            "burnt_result",
            "furnace_modules"
        }
    elseif entity_type == "assembling-machine" or entity_type == "rocket-silo" then
        return {
            "assembling_machine_input",
            "assembling_machine_output",
            "assembling_machine_modules"
        }
    elseif entity_type == "mining-drill" then
        return {"fuel", "burnt_result", "mining_drill_modules"}
    elseif entity_type == "inserter" or entity_type == "boiler"
        or entity_type == "burner-generator" then
        return {"fuel", "burnt_result"}
    elseif entity_type == "lab" then
        return {"lab_input", "lab_modules"}
    elseif entity_type == "ammo-turret" or entity_type == "artillery-turret" then
        return {"turret_ammo"}
    end
    return {}
end

local function distance_squared(a, b)
    local dx = a.x - b.x
    local dy = a.y - b.y
    return (dx * dx) + (dy * dy)
end

local function inventory_contents(inventory)
    local result = {}
    if not inventory then
        return result
    end

    for key, value in pairs(inventory.get_contents()) do
        if type(value) == "table" and value.name then
            result[value.name] = (result[value.name] or 0) + value.count
        elseif type(key) == "string" and type(value) == "number" then
            result[key] = value
        end
    end
    return result
end

local function entity_inventories(entity)
    local result = {}
    local seen = {}
    for _, label in ipairs(inventory_labels_for_entity(entity)) do
        local inventory_id = INVENTORY_DEFINITIONS[label]
        if inventory_id then
            local ok, inventory = pcall(function()
                return entity.get_inventory(inventory_id)
            end)
            if ok and inventory and inventory.valid and not seen[inventory] then
                seen[inventory] = true
                local contents = inventory_contents(inventory)
                if next(contents) ~= nil then
                    result[label] = contents
                end
            end
        end
    end
    return result
end

local function merged_inventory_contents(inventories)
    local merged = {}
    for _, contents in pairs(inventories or {}) do
        for name, count in pairs(contents) do
            merged[name] = (merged[name] or 0) + count
        end
    end
    return merged
end

local function serializable_box(box)
    if not box or not box.left_top or not box.right_bottom then return nil end
    return {
        left_top = {x = box.left_top.x, y = box.left_top.y},
        right_bottom = {x = box.right_bottom.x, y = box.right_bottom.y}
    }
end

local function transport_line_contents(entity)
    if entity.type ~= "transport-belt"
        and entity.type ~= "underground-belt"
        and entity.type ~= "splitter" then
        return nil
    end
    local result = {}
    for index = 1, 8 do
        local ok, line = pcall(function()
            return entity.get_transport_line(index)
        end)
        if not ok or not line then break end
        local contents = inventory_contents(line)
        if next(contents) ~= nil then result[tostring(index)] = contents end
    end
    return next(result) and result or nil
end

local function describe_entity(entity)
    if not entity or not entity.valid then return nil end
    local inventories = entity_inventories(entity)
    local result = {
        name = entity.name,
        type = entity.type,
        position = {x = entity.position.x, y = entity.position.y},
        direction = entity.direction,
        direction_name = DIRECTION_NAMES[entity.direction],
        health = entity.health,
        bounding_box = serializable_box(entity.bounding_box),
        inventories = inventories,
        inventory = merged_inventory_contents(inventories)
    }

    local status_ok, entity_status = pcall(function() return entity.status end)
    if status_ok and entity_status ~= nil then
        result.status = entity_status
        result.status_name = ENTITY_STATUS_NAMES[entity_status]
    end
    local burner_ok, burner = pcall(function() return entity.burner end)
    if burner_ok and burner then
        result.burner = {
            currently_burning = burner.currently_burning
                and burner.currently_burning.name or nil,
            remaining_burning_fuel = burner.remaining_burning_fuel,
            heat = burner.heat
        }
    end
    local pickup_ok, pickup_position = pcall(function()
        return entity.pickup_position
    end)
    if pickup_ok and pickup_position then
        result.pickup_position = {x = pickup_position.x, y = pickup_position.y}
    end
    local drop_ok, drop_position = pcall(function()
        return entity.drop_position
    end)
    if drop_ok and drop_position then
        result.drop_position = {x = drop_position.x, y = drop_position.y}
    end
    local held_ok, held_stack = pcall(function()
        return entity.held_stack
    end)
    if held_ok and held_stack and held_stack.valid_for_read then
        result.held_stack = {name = held_stack.name, count = held_stack.count}
    end
    local recipe_ok, recipe = pcall(function() return entity.get_recipe() end)
    if recipe_ok and recipe then result.recipe = recipe.name end
    local filter_ok, filter = pcall(function() return entity.get_filter(1) end)
    if filter_ok and filter then
        result.filter = type(filter) == "table" and filter.name or filter
    end
    local fluidbox_ok, fluidbox = pcall(function() return entity.fluidbox end)
    if fluidbox_ok and fluidbox and #fluidbox > 0 then
        local connections = {}
        local seen_connections = {}
        for index = 1, #fluidbox do
            local points_ok, points = pcall(function()
                return fluidbox.get_pipe_connections(index)
            end)
            if points_ok and points then
                for _, connection in pairs(points) do
                    local position = connection.position
                    if position then
                        local key = tostring(position.x) .. "," .. tostring(position.y)
                        if not seen_connections[key] then
                            seen_connections[key] = true
                            table.insert(connections, {x = position.x, y = position.y})
                        end
                    end
                end
            end
        end
        if #connections > 0 then result.connections = connections end
        local segment_ok, segment_id = pcall(function()
            return fluidbox.get_fluid_segment_id(1)
        end)
        if segment_ok and segment_id ~= nil then
            result.fluid_system_id = segment_id
        end
    end
    local network_ok, network_id = pcall(function()
        return entity.electric_network_id
    end)
    if network_ok and network_id ~= nil then result.electrical_id = network_id end
    result.transport_lines = transport_line_contents(entity)
    return result
end

local function perception_radius(requested_radius)
    local configured_radius = settings.global["airi-companion-perception-radius"].value
    return math.min(
        math.max(tonumber(requested_radius) or configured_radius, 8),
        configured_radius
    )
end

local function resource_prototype_names()
    local names = {}
    local resource_prototypes = prototypes.get_entity_filtered({
        {filter = "type", type = "resource"}
    })
    for name in pairs(resource_prototypes) do
        table.insert(names, name)
    end
    table.sort(names)
    return names
end

local function find_nearest_resource_entity(character, resource_name, radius)
    local probe_radius = math.min(RESOURCE_NEAREST_INITIAL_RADIUS, radius)
    while true do
        local probe = character.surface.find_entities_filtered({
            position = character.position,
            radius = probe_radius,
            type = "resource",
            name = resource_name,
            limit = RESOURCE_SAMPLE_LIMIT
        })
        if #probe > 0 then
            local nearest = nil
            local nearest_distance_squared = nil
            for _, entity in pairs(probe) do
                local squared = distance_squared(character.position, entity.position)
                if not nearest_distance_squared or squared < nearest_distance_squared then
                    nearest = entity
                    nearest_distance_squared = squared
                end
            end
            return nearest, nearest_distance_squared
        end
        if probe_radius >= radius then
            return nil, nil
        end
        probe_radius = math.min(probe_radius * 2, radius)
    end
end

local function inspect_resource(character, resource_name, radius)
    local filter = {
        position = character.position,
        radius = radius,
        type = "resource",
        name = resource_name
    }
    local entity_count = character.surface.count_entities_filtered(filter)
    if entity_count == 0 then
        return {
            name = resource_name,
            found = false,
            searched_radius = radius
        }
    end

    local samples = character.surface.find_entities_filtered({
        position = character.position,
        radius = radius,
        type = "resource",
        name = resource_name,
        limit = RESOURCE_SAMPLE_LIMIT
    })
    local sampled_amount = 0
    for _, entity in pairs(samples) do
        sampled_amount = sampled_amount + (entity.amount or 0)
    end

    local nearest, nearest_distance_squared = find_nearest_resource_entity(
        character,
        resource_name,
        radius
    )
    if not nearest then
        return {
            name = resource_name,
            found = false,
            searched_radius = radius,
            error = "Resource count changed during inspection"
        }
    end
    local truncated = entity_count > #samples
    local result = {
        name = resource_name,
        found = true,
        searched_radius = radius,
        entity_count = entity_count,
        nearest = {x = nearest.position.x, y = nearest.position.y},
        nearest_distance = math.sqrt(nearest_distance_squared),
        truncated = truncated
    }
    if truncated then
        result.sampled_entity_count = #samples
        result.sampled_amount = sampled_amount
        result.sample_limit = RESOURCE_SAMPLE_LIMIT
    else
        result.amount = sampled_amount
    end
    return result
end

local function observe_resources(character, radius)
    local result = {}
    for _, resource_name in pairs(resource_prototype_names()) do
        local entry = inspect_resource(character, resource_name, radius)
        if entry.found then
            entry.found = nil
            entry.searched_radius = nil
            table.insert(result, entry)
        end
    end
    table.sort(result, function(a, b)
        if a.nearest_distance == b.nearest_distance then
            return a.name < b.name
        end
        return a.nearest_distance < b.nearest_distance
    end)
    return result
end

local function observe_enemies(character, radius)
    local enemies = character.surface.find_entities_filtered({
        position = character.position,
        radius = radius,
        force = game.forces.enemy,
        limit = 32
    })
    local result = {}
    for _, entity in pairs(enemies) do
        table.insert(result, {
            name = entity.name,
            type = entity.type,
            position = {x = entity.position.x, y = entity.position.y},
            health = entity.health
        })
    end
    return result
end

local function observe_buildings(character, radius)
    local entities = character.surface.find_entities_filtered({
        position = character.position,
        radius = radius,
        force = character.force,
        limit = 96
    })
    local result = {}
    for _, entity in pairs(entities) do
        if entity ~= character
            and entity.type ~= "character"
            and entity.type ~= "resource"
            and entity.type ~= "item-entity"
            and #result < 48 then
            table.insert(result, describe_entity(entity))
        end
    end
    return result
end

local function requested_names(arguments)
    local names = arguments.names
    if type(names) == "string" then return {names} end
    if type(names) == "table" and #names > 0 then return names end
    if type(arguments.name) == "string" and arguments.name ~= "" then
        return {arguments.name}
    end
    return nil
end

function Observation.find_entity(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    arguments = arguments or {}
    local x = tonumber(arguments.x)
    local y = tonumber(arguments.y)
    if not x or not y then return nil, "entity query requires x and y" end

    local filter = {
        position = {x = x, y = y},
        radius = math.min(math.max(tonumber(arguments.entity_radius) or 0.8, 0.1), 3),
        force = character.force,
        limit = 32
    }
    local names = requested_names(arguments)
    if names then filter.name = names end
    local candidates = character.surface.find_entities_filtered(filter)
    local nearest = nil
    local nearest_distance = nil
    local target = {x = x, y = y}
    for _, entity in pairs(candidates) do
        if entity.valid and entity ~= character and entity.type ~= "character" then
            local squared = distance_squared(target, entity.position)
            if nearest_distance == nil or squared < nearest_distance then
                nearest = entity
                nearest_distance = squared
            end
        end
    end
    return nearest, nearest and nil or "No matching entity at the requested position"
end

function Observation.describe_entity(entity)
    return describe_entity(entity)
end

function Observation.get_entities(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    arguments = arguments or {}
    local upstream_api = arguments.upstream_api == true
    local radius
    if upstream_api then
        radius = math.min(math.max(tonumber(arguments.radius) or 1000, 0), 1000)
    else
        radius = perception_radius(arguments.radius)
    end
    local center = character.position
    if tonumber(arguments.x) and tonumber(arguments.y) then
        center = {x = tonumber(arguments.x), y = tonumber(arguments.y)}
    end
    local filter = {
        force = character.force,
        limit = upstream_api and (ENTITY_RESULT_LIMIT + 1) or ENTITY_RESULT_LIMIT
    }
    if upstream_api then
        -- Upstream get_entities uses a square search area rather than the
        -- passive Companion perception circle.
        filter.area = {
            {center.x - radius, center.y - radius},
            {center.x + radius, center.y + radius}
        }
    else
        filter.position = center
        filter.radius = radius
    end
    local names = requested_names(arguments)
    if names then filter.name = names end
    local entities = character.surface.find_entities_filtered(filter)
    if upstream_api and #entities > ENTITY_RESULT_LIMIT then
        return nil, "get_entities matched more than " .. ENTITY_RESULT_LIMIT
            .. " entities; narrow the prototype filter, position, or radius "
            .. "so the complete result fits in one Companion UDP response"
    end
    local result = {}
    for _, entity in pairs(entities) do
        if entity.valid
            and entity ~= character
            and entity.type ~= "character"
            and entity.type ~= "resource"
            and entity.type ~= "item-entity" then
            table.insert(result, describe_entity(entity))
        end
    end
    table.sort(result, function(a, b)
        local a_distance = distance_squared(center, a.position)
        local b_distance = distance_squared(center, b.position)
        if a_distance == b_distance then return a.name < b.name end
        return a_distance < b_distance
    end)
    return result
end

function Observation.inspect_entity(arguments)
    local entity, err = Observation.find_entity(arguments)
    if not entity then
        return {found = false, error = err}
    end
    local result = describe_entity(entity)
    result.found = true
    return result
end

function Observation.entity_inventory(arguments)
    local entity, err = Observation.find_entity(arguments)
    if not entity then return nil, err end
    local inventories = entity_inventories(entity)
    local described = describe_entity(entity)
    return {
        entity = entity.name,
        position = {x = entity.position.x, y = entity.position.y},
        contents = merged_inventory_contents(inventories),
        inventories = inventories,
        transport_lines = transport_line_contents(entity),
        burner = described.burner
    }
end

function Observation.placement_blockers(entity_name, position)
    local character = Character.get()
    if not character then return {} end
    local prototype = prototypes.entity[entity_name]
    local collision_box = prototype and prototype.collision_box
    if not collision_box then return {} end
    local area = {
        {
            position.x + collision_box.left_top.x,
            position.y + collision_box.left_top.y
        },
        {
            position.x + collision_box.right_bottom.x,
            position.y + collision_box.right_bottom.y
        }
    }
    local blockers = character.surface.find_entities_filtered({area = area, limit = 16})
    local result = {}
    for _, blocker in pairs(blockers) do
        if blocker.valid and blocker.type ~= "resource" and blocker.type ~= "item-entity" then
            table.insert(result, {
                name = blocker.name,
                type = blocker.type,
                position = {x = blocker.position.x, y = blocker.position.y},
                is_airi = blocker == character
            })
        end
    end
    return result
end

function Observation.capture(requested_radius)
    local character = Character.get()
    local data = State.ensure()
    if not character then
        return {
            tick = game.tick,
            character = Character.status(),
            movement = Movement.status(),
            task = data.task and {
                active = true,
                id = data.task.id,
                kind = data.task.kind,
                phase = data.task.phase,
                resource = data.task.resource_name,
                requested = data.task.requested_count,
                collected = data.task.collected_count
            } or {active = false},
            error = "AIRI has no character body"
        }
    end

    local radius = perception_radius(requested_radius)
    local owner = Character.get_owner(data.owner_player_index)

    return {
        tick = game.tick,
        radius = radius,
        character = {
            present = true,
            position = {x = character.position.x, y = character.position.y},
            surface = character.surface.name,
            health = character.health,
            max_health = character.max_health,
            inventory = inventory_contents(character.get_main_inventory()),
            crafting_queue_size = character.crafting_queue_size
        },
        owner = owner and {
            player_index = owner.index,
            name = owner.name,
            position = {x = owner.position.x, y = owner.position.y},
            surface = owner.surface.name
        } or nil,
        movement = Movement.status(),
        task = data.task and {
            active = true,
            id = data.task.id,
            kind = data.task.kind,
            phase = data.task.phase,
            resource = data.task.resource_name,
            requested = data.task.requested_count,
            collected = data.task.collected_count
        } or {active = false},
        resources = observe_resources(character, radius),
        enemies = observe_enemies(character, radius),
        buildings = observe_buildings(character, math.min(radius, 32))
    }
end

function Observation.find_resource(resource_name, requested_radius)
    local character = Character.get()
    if not character then
        return nil, "AIRI has no character body"
    end
    if type(resource_name) ~= "string" or resource_name == "" then
        return nil, "find_resource requires a resource name"
    end

    local prototype = prototypes.entity[resource_name]
    if not prototype or prototype.type ~= "resource" then
        return nil, "Unknown resource prototype: " .. resource_name
    end

    return inspect_resource(
        character,
        resource_name,
        perception_radius(requested_radius)
    )
end

function Observation.inventory()
    local character = Character.get()
    if not character then
        return nil, "AIRI has no character body"
    end
    return inventory_contents(character.get_main_inventory())
end

return Observation
