local Character = require("scripts.character")
local Observation = require("scripts.observation")

-- Compatibility actions for the public tools discovered from
-- fle/env/tools/agent by upstream FLE.  The Companion keeps its own character
-- and UDP transport, but these functions preserve the upstream action names
-- and game semantics instead of inventing replacement verbs.
local Upstream = {}

local WATER_TILES = {
    "water",
    "deepwater",
    "water-green",
    "deepwater-green",
    "water-shallow",
    "water-mud"
}

local function distance_squared(a, b)
    local dx = a.x - b.x
    local dy = a.y - b.y
    return (dx * dx) + (dy * dy)
end

local function distance(a, b)
    return math.sqrt(distance_squared(a, b))
end

local function normalize_name(name)
    if name == "copper" then return "copper-ore" end
    if name == "iron" then return "iron-ore" end
    if name == "uranium" then return "uranium-ore" end
    return name
end

local function explicit_radius(requested, default, maximum)
    return math.min(math.max(tonumber(requested) or default, 0), maximum)
end

local function resolve_entity_name(item_name)
    local item = prototypes.item[item_name]
    local place_result = item and item.place_result
    local entity_name = place_result and place_result.name or item_name
    if not prototypes.entity[entity_name] then return nil end
    return entity_name
end

local function nearest_entity(entities, position)
    local nearest = nil
    local nearest_distance = nil
    for _, entity in pairs(entities) do
        if entity.valid then
            local squared = distance_squared(entity.position, position)
            if nearest_distance == nil or squared < nearest_distance then
                nearest = entity
                nearest_distance = squared
            end
        end
    end
    return nearest, nearest_distance
end

local function nearest(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    local name = normalize_name(arguments.name or arguments.resource)
    if type(name) ~= "string" or name == "" then
        return nil, "nearest requires a prototype or resource name"
    end
    -- Upstream nearest deliberately searches a 500-tile square.  This explicit
    -- query is not the passive observe radius and must be able to locate water
    -- outside the Companion's local observation bubble.
    local radius = explicit_radius(arguments.radius, 500, 500)
    local position = character.position

    if name == "water" then
        local tiles = character.surface.find_tiles_filtered({
            area = {
                {position.x - radius, position.y - radius},
                {position.x + radius, position.y + radius}
            },
            name = WATER_TILES
        })
        local selected = nil
        local selected_distance = nil
        for _, tile in pairs(tiles) do
            local squared = distance_squared(tile.position, position)
            if selected_distance == nil or squared < selected_distance then
                selected = tile
                selected_distance = squared
            end
        end
        if not selected then return nil, "Could not find water in the visible area" end
        return {
            name = name,
            position = {x = selected.position.x, y = selected.position.y},
            distance = math.sqrt(selected_distance)
        }
    end

    local filter = {
        position = position,
        radius = radius
    }
    if name == "wood" then
        filter.type = "tree"
    else
        filter.name = name
    end
    local selected, squared = nearest_entity(
        character.surface.find_entities_filtered(filter),
        position
    )
    if not selected then
        return nil, "Could not find an entity called " .. name .. " in the visible area"
    end
    return {
        name = name,
        entity = selected.name,
        position = {x = selected.position.x, y = selected.position.y},
        distance = math.sqrt(squared)
    }
end

local function new_bounds(position)
    return {
        left_top = {x = position.x, y = position.y},
        right_bottom = {x = position.x, y = position.y}
    }
end

local function expand_bounds(bounds, position)
    bounds.left_top.x = math.min(bounds.left_top.x, position.x)
    bounds.left_top.y = math.min(bounds.left_top.y, position.y)
    bounds.right_bottom.x = math.max(bounds.right_bottom.x, position.x)
    bounds.right_bottom.y = math.max(bounds.right_bottom.y, position.y)
end

local function resource_patch(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    local name = normalize_name(arguments.resource or arguments.name)
    local x, y = tonumber(arguments.x), tonumber(arguments.y)
    if type(name) ~= "string" or not x or not y then
        return nil, "get_resource_patch requires resource, x, and y"
    end
    local center = {x = x, y = y}
    local radius = explicit_radius(arguments.radius, 30, 500)
    local bounds = new_bounds(center)

    if name == "water" then
        local tiles = character.surface.find_tiles_filtered({
            position = center,
            radius = radius,
            name = WATER_TILES
        })
        if #tiles == 0 then return nil, "No water at the specified location" end
        for _, tile in pairs(tiles) do expand_bounds(bounds, tile.position) end
        bounds.left_top.x = bounds.left_top.x - 0.5
        bounds.left_top.y = bounds.left_top.y - 0.5
        bounds.right_bottom.x = bounds.right_bottom.x + 0.5
        bounds.right_bottom.y = bounds.right_bottom.y + 0.5
        return {name = name, size = #tiles, bounding_box = bounds}
    end

    if name == "wood" then
        local trees = character.surface.find_entities_filtered({
            position = center,
            radius = radius,
            type = "tree"
        })
        if #trees == 0 then return nil, "No trees at the specified location" end
        local total = 0
        for _, tree in pairs(trees) do
            expand_bounds(bounds, tree.position)
            local products = tree.prototype.mineable_properties
                and tree.prototype.mineable_properties.products or {}
            local added = false
            for _, product in pairs(products) do
                if product.type == "item" and product.name == "wood" then
                    total = total + (product.amount or product.amount_min or 1)
                    added = true
                end
            end
            if not added then total = total + 1 end
        end
        return {name = name, size = total, bounding_box = bounds}
    end

    local candidates = character.surface.find_entities_filtered({
        position = center,
        radius = radius,
        type = "resource",
        name = name
    })
    if #candidates == 0 then
        return nil, "No resource of type " .. name .. " at the specified location"
    end
    local start = nearest_entity(candidates, center)
    local queue = {start}
    local head = 1
    local visited = {}
    local queued = {
        [tostring(start.position.x) .. "," .. tostring(start.position.y)] = true
    }
    local total = 0
    while head <= #queue do
        local entity = queue[head]
        head = head + 1
        local key = tostring(entity.position.x) .. "," .. tostring(entity.position.y)
        if not visited[key] then
            visited[key] = true
            total = total + (entity.amount or 0)
            expand_bounds(bounds, entity.position)
            local neighbours = character.surface.find_entities_filtered({
                area = {
                    {entity.position.x - 1, entity.position.y - 1},
                    {entity.position.x + 1, entity.position.y + 1}
                },
                type = "resource",
                name = name
            })
            for _, neighbour in pairs(neighbours) do
                local neighbour_key = tostring(neighbour.position.x)
                    .. "," .. tostring(neighbour.position.y)
                if not visited[neighbour_key] and not queued[neighbour_key] then
                    queued[neighbour_key] = true
                    table.insert(queue, neighbour)
                end
            end
        end
    end
    return {name = name, size = total, bounding_box = bounds}
end

local function entity_item_count(entity, item_name)
    local ok, count = pcall(function() return entity.get_item_count(item_name) end)
    return ok and tonumber(count) or 0
end

local function extract_item(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    local item_name = arguments.item or arguments.name
    local requested = math.floor(tonumber(arguments.count or arguments.quantity) or 0)
    local x, y = tonumber(arguments.x), tonumber(arguments.y)
    if type(item_name) ~= "string" or not x or not y then
        return nil, "extract_item requires item, x, and y"
    end
    if requested < 1 or requested > 10000 then
        return nil, "extract_item count must be between 1 and 10000"
    end
    local center = {x = x, y = y}
    local filter = {position = center, radius = 10, limit = 256}
    if arguments.source_name then filter.name = arguments.source_name end
    local candidates = character.surface.find_entities_filtered(filter)
    local selected = nil
    local selected_distance = nil
    for _, candidate in pairs(candidates) do
        if candidate.valid and candidate ~= character
            and entity_item_count(candidate, item_name) > 0 then
            local squared = distance_squared(candidate.position, center)
            if selected_distance == nil or squared < selected_distance then
                selected = candidate
                selected_distance = squared
            end
        end
    end
    if not selected then
        return nil, "Could not find a nearby entity containing " .. item_name
    end
    local reach = character.reach_distance or character.build_distance or 10
    if distance(character.position, selected.position) > reach then
        return nil, "The source is out of reach; move_to it first"
    end
    local inventory = character.get_main_inventory()
    if not inventory then return nil, "AIRI inventory is unavailable" end
    local available = entity_item_count(selected, item_name)
    local extract_count = math.min(requested, available)
    if not inventory.can_insert({name = item_name, count = extract_count}) then
        return nil, "AIRI inventory cannot accept the requested items"
    end
    local removed = selected.remove_item({name = item_name, count = extract_count})
    if removed < 1 then return nil, "The source did not release " .. item_name end
    local inserted = character.insert({name = item_name, count = removed})
    if inserted < removed then
        selected.insert({name = item_name, count = removed - inserted})
    end
    return {
        item = item_name,
        extracted = inserted,
        source = Observation.describe_entity(selected)
    }
end

local function harvest_resource(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    local x, y = tonumber(arguments.x), tonumber(arguments.y)
    local requested = math.floor(tonumber(arguments.count or arguments.quantity) or 1)
    local radius = explicit_radius(arguments.radius, 10, 64)
    if not x or not y then return nil, "harvest_resource requires x and y" end
    if requested < 1 or requested > 10000 then
        return nil, "harvest_resource count must be between 1 and 10000"
    end
    local center = {x = x, y = y}
    local reach = character.resource_reach_distance or 2.7
    if distance(character.position, center) > reach then
        return nil, "Nothing at the requested position is within reach"
    end
    local targets = character.surface.find_entities_filtered({
        position = center,
        radius = math.min(radius, reach),
        type = {"tree", "resource", "simple-entity"}
    })
    table.sort(targets, function(a, b)
        return distance_squared(a.position, center) < distance_squared(b.position, center)
    end)
    if #targets == 0 then return nil, "Nothing within reach to harvest" end

    local first = targets[1]
    local same_kind = {}
    for _, target in pairs(targets) do
        if target.valid and target.minable
            and target.type == first.type and target.name == first.name then
            table.insert(same_kind, target)
        end
    end
    local inventory = character.get_main_inventory()
    local harvested = 0
    for _, target in pairs(same_kind) do
        if harvested >= requested then break end
        local before = {}
        local products = target.prototype.mineable_properties
            and target.prototype.mineable_properties.products or {}
        for _, product in pairs(products) do
            if product.type == "item" then
                before[product.name] = character.get_item_count(product.name)
            end
        end
        character.update_selected_entity(target.position)
        local ok, mined = pcall(function()
            return target.mine({
                inventory = inventory,
                force = true,
                raise_destroyed = true
            })
        end)
        if ok and mined then
            for name, count_before in pairs(before) do
                harvested = harvested
                    + math.max(0, character.get_item_count(name) - count_before)
            end
        end
    end
    if harvested < 1 then return nil, "Factorio could not harvest the target" end
    return {harvested = harvested, entity = first.name, type = first.type}
end

local function research_ingredients(technology, remaining_units)
    local result = {}
    for _, ingredient in pairs(technology.research_unit_ingredients or {}) do
        table.insert(result, {
            name = ingredient.name,
            count = ingredient.amount * remaining_units,
            type = ingredient.type
        })
    end
    return result
end

local function get_research_progress(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    local force = character.force
    local name = arguments.technology or arguments.name
    if not name then
        if not force.current_research then return nil, "No research currently in progress" end
        name = force.current_research.name
    end
    local technology = force.technologies[name]
    if not technology then return nil, "Technology " .. name .. " does not exist" end
    if technology.researched then return {} end
    local is_current = force.current_research
        and force.current_research.name == technology.name
    local progress = is_current and force.research_progress or 0
    local remaining = math.ceil(technology.research_unit_count * (1 - progress))
    return research_ingredients(technology, remaining)
end

local function set_research(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    local name = arguments.technology or arguments.name
    if type(name) ~= "string" or name == "" then
        return nil, "set_research requires a technology name"
    end
    local force = character.force
    local technology = force.technologies[name]
    if not technology then return nil, "Technology " .. name .. " does not exist" end
    if technology.researched then return nil, "Technology " .. name .. " is already researched" end
    if not technology.enabled then return nil, "Technology " .. name .. " is not enabled" end
    if force.current_research then force.cancel_current_research() end
    if not force.add_research(name) then
        return nil, "Failed to start research for " .. name
    end
    return research_ingredients(technology, technology.research_unit_count)
end

local function set_entity_recipe(arguments)
    local recipe_name = arguments.recipe or arguments.name
    if type(recipe_name) ~= "string" or recipe_name == "" then
        return nil, "set_entity_recipe requires a recipe or filter name"
    end
    local target, target_error = Observation.find_entity({
        x = arguments.x,
        y = arguments.y,
        name = arguments.target_name
    })
    if not target then return nil, target_error end
    local result
    if target.type == "inserter" then
        local ok, err = pcall(function()
            target.use_filters = true
            target.set_filter(1, recipe_name)
        end)
        if not ok then return nil, tostring(err) end
        result = Observation.describe_entity(target)
        result.filter = recipe_name
    else
        local character = Character.get()
        local recipe = character and character.force.recipes[recipe_name] or nil
        if not recipe then return nil, "Recipe " .. recipe_name .. " does not exist" end
        local ok, err = pcall(function() target.set_recipe(recipe_name) end)
        if not ok then return nil, tostring(err) end
        result = Observation.describe_entity(target)
        result.recipe = recipe_name
    end
    return result
end

local function launch_rocket(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    local x, y = tonumber(arguments.x), tonumber(arguments.y)
    if not x or not y then return nil, "launch_rocket requires x and y" end
    local silos = character.surface.find_entities_filtered({
        position = {x = x, y = y},
        radius = 1000,
        name = "rocket-silo",
        force = character.force,
        limit = 32
    })
    local silo = nearest_entity(silos, {x = x, y = y})
    if not silo then return nil, "No rocket silo found near the specified position" end
    if silo.rocket_silo_status ~= defines.rocket_silo_status.rocket_ready then
        return nil, "Rocket is not ready for launch"
    end
    local ok, launched = pcall(function() return silo.launch_rocket() end)
    if not ok or launched == false then
        return nil, "Factorio could not launch the rocket: " .. tostring(launched)
    end
    local result = Observation.describe_entity(silo)
    result.launched = true
    return result
end

local function has_resource_categories(prototype)
    if not prototype or not prototype.resource_categories then return false end
    for _ in pairs(prototype.resource_categories) do return true end
    return false
end

local function nearest_buildable(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    local item_name = arguments.item or arguments.name
    local entity_name = resolve_entity_name(item_name)
    if not entity_name then return nil, "The supplied item does not place an entity" end
    local prototype = prototypes.entity[entity_name]
    local box = arguments.building_box or {}
    local width = math.max(1, math.floor(tonumber(box.width) or 1))
    local height = math.max(1, math.floor(tonumber(box.height) or 1))
    local start = {
        x = tonumber(arguments.x) or character.position.x,
        y = tonumber(arguments.y) or character.position.y
    }
    local max_radius = math.min(math.max(tonumber(arguments.max_radius) or 30, 0), 60)
    local needs_resources = has_resource_categories(prototype)
    local needs_oil = entity_name == "pumpjack"

    local function buildable(origin)
        local left_top = {x = origin.x, y = origin.y}
        local right_bottom = {x = origin.x + width, y = origin.y + height}
        if character.surface.count_tiles_filtered({
            area = {left_top, right_bottom},
            name = WATER_TILES
        }) > 0 then
            return false
        end
        if character.surface.count_entities_filtered({
            area = {left_top, right_bottom},
            type = {"character", "resource"},
            invert = true
        }) > 0 then
            return false
        end
        if needs_resources then
            if needs_oil then
                if character.surface.count_entities_filtered({
                    area = {left_top, right_bottom},
                    name = "crude-oil"
                }) < 1 then return false end
            else
                local min_x = math.floor(left_top.x)
                local min_y = math.floor(left_top.y)
                local max_x = math.ceil(right_bottom.x) - 1
                local max_y = math.ceil(right_bottom.y) - 1
                local covered = {}
                local resources = character.surface.find_entities_filtered({
                    area = {left_top, right_bottom},
                    type = "resource"
                })
                for _, resource in pairs(resources) do
                    covered[math.floor(resource.position.x) .. ","
                        .. math.floor(resource.position.y)] = true
                end
                for x = min_x, max_x do
                    for y = min_y, max_y do
                        if not covered[x .. "," .. y] then return false end
                    end
                end
            end
        end
        return true, left_top, right_bottom
    end

    local dx, dy = 0, 0
    local segment_length, segment_passed, direction = 1, 0, 0
    while math.max(math.abs(dx), math.abs(dy)) <= max_radius do
        local origin = {x = start.x + dx, y = start.y + dy}
        local ok, left_top, right_bottom = buildable(origin)
        if ok then return {left_top = left_top, right_bottom = right_bottom} end
        segment_passed = segment_passed + 1
        if direction == 0 then dx = dx + 1
        elseif direction == 1 then dy = dy + 1
        elseif direction == 2 then dx = dx - 1
        else dy = dy - 1 end
        if segment_passed == segment_length then
            segment_passed = 0
            direction = (direction + 1) % 4
            if direction % 2 == 0 then segment_length = segment_length + 1 end
        end
    end
    return nil, "Could not find a buildable area for " .. entity_name
end

local function round_half(value)
    return math.floor((value * 2) + 0.5) / 2
end

local function point_key(position)
    return string.format("%.1f,%.1f", position.x, position.y)
end

local function direction_between(source, target)
    local dx, dy = target.x - source.x, target.y - source.y
    if math.abs(dx) >= math.abs(dy) then
        return dx >= 0 and defines.direction.east or defines.direction.west
    end
    return dy >= 0 and defines.direction.south or defines.direction.north
end

local function connection_entity_at(surface, entity_name, position, force)
    local entities = surface.find_entities_filtered({
        position = position,
        radius = 0.35,
        name = entity_name,
        force = force,
        limit = 1
    })
    return entities[1]
end

local function connection_endpoint(waypoint, other, connection_name, is_source)
    if string.find(connection_name, "belt", 1, true) then
        local preferred = is_source and waypoint.drop_position or waypoint.pickup_position
        if preferred then return {x = preferred.x, y = preferred.y} end
    end
    if string.find(connection_name, "pipe", 1, true)
        and type(waypoint.connections) == "table" and #waypoint.connections > 0 then
        local selected = waypoint.connections[1]
        local selected_distance = distance_squared(selected, other)
        for _, connection in pairs(waypoint.connections) do
            local squared = distance_squared(connection, other)
            if squared < selected_distance then
                selected = connection
                selected_distance = squared
            end
        end
        return {x = selected.x, y = selected.y}
    end
    local result = {x = waypoint.x, y = waypoint.y}
    if waypoint.kind ~= "entity" then return result end
    local character = Character.get()
    local candidates = character.surface.find_entities_filtered({
        position = result,
        radius = 0.8,
        force = character.force,
        limit = 16
    })
    local occupied = nil
    for _, candidate in pairs(candidates) do
        if candidate.valid and candidate ~= character then occupied = candidate break end
    end
    if not occupied or occupied.name == connection_name then return result end
    local box = occupied.bounding_box
    local horizontal = math.abs(other.x - result.x) >= math.abs(other.y - result.y)
    if horizontal then
        local half = math.abs(box.right_bottom.x - box.left_top.x) / 2
        result.x = result.x + (other.x >= result.x and 1 or -1) * (half + 0.5)
    else
        local half = math.abs(box.right_bottom.y - box.left_top.y) / 2
        result.y = result.y + (other.y >= result.y and 1 or -1) * (half + 0.5)
    end
    return result
end

local function find_connection_path(surface, entity_name, item_name, force, start, goal)
    start = {x = round_half(start.x), y = round_half(start.y)}
    goal = {x = round_half(goal.x), y = round_half(goal.y)}
    local minimum_x = math.min(start.x, goal.x) - 12
    local maximum_x = math.max(start.x, goal.x) + 12
    local minimum_y = math.min(start.y, goal.y) - 12
    local maximum_y = math.max(start.y, goal.y) + 12
    local queue = {start}
    local head = 1
    local parents = {[point_key(start)] = false}
    local positions = {[point_key(start)] = start}
    local function traversable(position)
        if connection_entity_at(surface, entity_name, position, force) then return true end
        return surface.can_place_entity({
            name = entity_name,
            position = position,
            force = force,
            build_check_type = defines.build_check_type.manual
        })
    end
    while head <= #queue and head <= 12000 do
        local current = queue[head]
        head = head + 1
        if point_key(current) == point_key(goal) then
            local path = {}
            local key = point_key(current)
            while key do
                table.insert(path, 1, positions[key])
                key = parents[key]
            end
            return path
        end
        local horizontal = goal.x >= current.x and 1 or -1
        local vertical = goal.y >= current.y and 1 or -1
        local neighbours = {
            {x = current.x + horizontal, y = current.y},
            {x = current.x, y = current.y + vertical},
            {x = current.x - horizontal, y = current.y},
            {x = current.x, y = current.y - vertical}
        }
        for _, candidate in ipairs(neighbours) do
            if candidate.x >= minimum_x and candidate.x <= maximum_x
                and candidate.y >= minimum_y and candidate.y <= maximum_y then
                local key = point_key(candidate)
                if parents[key] == nil and traversable(candidate) then
                    parents[key] = point_key(current)
                    positions[key] = candidate
                    table.insert(queue, candidate)
                end
            end
        end
    end
    return nil
end

local function connection_kind(entity_name)
    local prototype = prototypes.entity[entity_name]
    if not prototype then return nil end
    if prototype.type == "transport-belt" or prototype.type == "underground-belt" then
        return "transport"
    elseif prototype.type == "pipe" or prototype.type == "pipe-to-ground" then
        return "fluid"
    elseif prototype.type == "electric-pole" then
        return "power"
    elseif prototype.type == "wall" then
        return "wall"
    end
    return nil
end

local function connect_entities(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    local waypoints = arguments.waypoints
    local connection_types = arguments.connection_types
    if type(waypoints) ~= "table" or #waypoints < 2 then
        return nil, "connect_entities requires at least two waypoints"
    end
    if type(connection_types) ~= "table" or #connection_types < 1 then
        return nil, "connect_entities requires at least one connection type"
    end
    local expected_kind = nil
    local surface_candidates = {}
    for _, candidate in ipairs(connection_types) do
        local candidate_entity = resolve_entity_name(candidate)
        local kind = candidate_entity and connection_kind(candidate_entity) or nil
        if not kind then
            return nil, "Unsupported connection prototype: " .. tostring(candidate)
        end
        if expected_kind and expected_kind ~= kind then
            return nil, "All connection prototypes must belong to one network kind; "
                .. "received both " .. expected_kind .. " and " .. kind
        end
        expected_kind = kind
        if not string.find(candidate, "underground", 1, true)
            and candidate ~= "pipe-to-ground" then
            table.insert(surface_candidates, candidate)
        end
    end
    if #surface_candidates == 0 then
        return nil, "The Companion connector currently requires a surface belt or "
            .. "pipe prototype; underground-only routing is not implemented"
    end
    local item_name = surface_candidates[1]
    local best_available = character.get_item_count(item_name)
    for index = 2, #surface_candidates do
        local available = character.get_item_count(surface_candidates[index])
        if available > best_available then
            item_name = surface_candidates[index]
            best_available = available
        end
    end
    local entity_name = resolve_entity_name(item_name)
    if not entity_name then return nil, "Unsupported connection prototype: " .. item_name end

    local full_path = {}
    local seen = {}
    for index = 1, #waypoints - 1 do
        local source_raw, target_raw = waypoints[index], waypoints[index + 1]
        local source = connection_endpoint(source_raw, target_raw, entity_name, true)
        local target = connection_endpoint(target_raw, source_raw, entity_name, false)
        local segment = find_connection_path(
            character.surface,
            entity_name,
            item_name,
            character.force,
            source,
            target
        )
        if not segment then
            return nil, "No buildable connection path was found between waypoints "
                .. index .. " and " .. (index + 1)
        end
        for _, position in ipairs(segment) do
            local key = point_key(position)
            if not seen[key] then
                seen[key] = true
                table.insert(full_path, position)
            end
        end
    end

    local required = 0
    for _, position in ipairs(full_path) do
        if not connection_entity_at(
            character.surface,
            entity_name,
            position,
            character.force
        ) then required = required + 1 end
    end
    local available = character.get_item_count(item_name)
    if arguments.dry_run then
        return {
            number_of_entities_required = required,
            number_of_entities_available = available,
            path = full_path,
            connection_type = item_name
        }
    end
    if available < required then
        return nil, "Not enough " .. item_name .. ": requires " .. required
            .. " but AIRI has " .. available
    end

    local placed = {}
    for index, position in ipairs(full_path) do
        local target = full_path[math.min(index + 1, #full_path)]
        local previous = full_path[math.max(index - 1, 1)]
        local direction = direction_between(position, target)
        if index == #full_path then direction = direction_between(previous, position) end
        local existing = connection_entity_at(
            character.surface,
            entity_name,
            position,
            character.force
        )
        if existing then
            if string.find(entity_name, "belt", 1, true) then existing.direction = direction end
            table.insert(placed, Observation.describe_entity(existing))
        else
            local created = character.surface.create_entity({
                name = entity_name,
                position = position,
                direction = direction,
                force = character.force,
                build_check_type = defines.build_check_type.manual,
                raise_built = true
            })
            if not created then
                return nil, "Factorio failed while creating the preflighted connection"
            end
            character.remove_item({name = item_name, count = 1})
            table.insert(placed, Observation.describe_entity(created))
        end
    end
    local group_name = "entity-group"
    if string.find(entity_name, "belt", 1, true) then group_name = "belt-group"
    elseif string.find(entity_name, "pipe", 1, true) then group_name = "pipe-group"
    elseif string.find(entity_name, "pole", 1, true) then group_name = "electricity-group"
    elseif string.find(entity_name, "wall", 1, true) then group_name = "wall-group" end
    local middle = placed[math.max(1, math.ceil(#placed / 2))]
    return {
        name = group_name,
        connection_type = item_name,
        number_of_entities_required = required,
        number_of_entities_available = available,
        position = middle and middle.position or full_path[1],
        entities = placed,
        path = full_path
    }
end

local ACTIONS = {
    nearest = nearest,
    get_resource_patch = resource_patch,
    harvest_resource = harvest_resource,
    extract_item = extract_item,
    get_research_progress = get_research_progress,
    set_research = set_research,
    set_entity_recipe = set_entity_recipe,
    launch_rocket = launch_rocket,
    nearest_buildable = nearest_buildable,
    connect_entities = connect_entities
}

function Upstream.execute(action, arguments)
    local handler = ACTIONS[action]
    if not handler then return false, false, nil, false end
    local result, err = handler(arguments or {})
    if result == nil then return true, false, err, false end
    return true, true, result, false
end

return Upstream
