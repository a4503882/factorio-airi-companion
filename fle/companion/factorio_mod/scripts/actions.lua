local State = require("scripts.state")
local Character = require("scripts.character")
local Movement = require("scripts.movement")
local Observation = require("scripts.observation")
local Tasks = require("scripts.tasks")
local Wiki = require("scripts.wiki")
local Upstream = require("scripts.upstream")

local Actions = {}

local function distance(a, b)
    local dx = a.x - b.x
    local dy = a.y - b.y
    return math.sqrt((dx * dx) + (dy * dy))
end

local CARDINAL_DIRECTIONS = {
    north = defines.direction.north,
    up = defines.direction.north,
    east = defines.direction.east,
    right = defines.direction.east,
    south = defines.direction.south,
    down = defines.direction.south,
    west = defines.direction.west,
    left = defines.direction.west
}

local DIRECTION_NAMES = {
    [defines.direction.north] = "north",
    [defines.direction.east] = "east",
    [defines.direction.south] = "south",
    [defines.direction.west] = "west"
}

local function parse_direction(arguments)
    local raw = arguments.direction
    if raw == nil then raw = arguments.direction_value end
    if raw == nil then return defines.direction.north, "north" end
    if type(raw) == "string" then
        local direction = CARDINAL_DIRECTIONS[string.lower(raw)]
        if not direction then
            return nil, "direction must be north, east, south, or west"
        end
        return direction, DIRECTION_NAMES[direction]
    end
    local direction = tonumber(raw)
    if not DIRECTION_NAMES[direction] then
        return nil, "direction must be a cardinal Factorio direction (0, 4, 8, or 12)"
    end
    return direction, DIRECTION_NAMES[direction]
end

local function resolve_entity_name(item_name)
    local item = prototypes.item[item_name]
    local place_result = item and item.place_result
    local entity_name = place_result and place_result.name or item_name
    if not prototypes.entity[entity_name] then return nil end
    return entity_name
end

local function placement_check(arguments)
    local character = Character.get()
    if not character then return nil, "AIRI has no character body" end
    local item_name = arguments.item or arguments.name
    local x = tonumber(arguments.x)
    local y = tonumber(arguments.y)
    if type(item_name) ~= "string" or not x or not y then
        return nil, "placement requires item/name, x, and y"
    end
    local entity_name = resolve_entity_name(item_name)
    if not entity_name then return nil, "The supplied item does not place an entity" end
    local direction, direction_name = parse_direction(arguments)
    if not direction then return nil, direction_name end
    local position = {x = x, y = y}
    local build_distance = character.build_distance or 10
    local in_reach = distance(character.position, position) <= build_distance
    local can_place = in_reach and character.surface.can_place_entity({
        name = entity_name,
        position = position,
        direction = direction,
        force = character.force,
        build_check_type = defines.build_check_type.manual
    }) or false
    local result = {
        item = item_name,
        entity = entity_name,
        position = position,
        direction = direction,
        direction_name = direction_name,
        in_reach = in_reach,
        distance = distance(character.position, position),
        build_distance = build_distance,
        item_count = character.get_item_count(item_name),
        can_place = can_place,
        blockers = can_place and {} or Observation.placement_blockers(entity_name, position)
    }
    if not in_reach then
        result.reason = "out_of_reach"
    elseif not can_place and #result.blockers > 0 then
        result.reason = "blocked_by_entities"
    elseif not can_place then
        result.reason = "terrain_or_collision"
    end
    return result
end

local function status()
    return {
        tick = game.tick,
        character = Character.status(),
        movement = Movement.status(),
        task = Tasks.status(),
        bridge = State.ensure().bridge
    }
end

local function craft_item(arguments)
    local character = Character.get()
    if not character then
        return false, "AIRI has no character body"
    end

    local recipe = arguments.recipe or arguments.name
    local count = math.floor(tonumber(arguments.count) or 0)
    if type(recipe) ~= "string" or recipe == "" then
        return false, "craft_item requires a recipe name"
    end
    if count < 1 or count > 10000 then
        return false, "craft_item count must be between 1 and 10000"
    end

    local force_recipe = character.force.recipes[recipe]
    if not force_recipe then
        return false, "The recipe does not exist; raw resources must be gathered"
    end
    if not force_recipe.enabled then
        return false, "The recipe is not unlocked for AIRI's force"
    end
    local output_per_craft = 1
    for _, product in pairs(force_recipe.products or {}) do
        if product.type == "item" and product.name == recipe then
            output_per_craft = product.amount or product.amount_min or 1
            break
        end
    end
    local crafts = math.ceil(count / output_per_craft)
    local queued = character.begin_crafting({count = crafts, recipe = recipe})
    if queued == 0 then
        return false, "The recipe could not be queued; check ingredients and technology"
    end
    return true, {
        recipe = recipe,
        requested = count,
        queued = queued,
        expected_items = queued * output_per_craft,
        output_per_craft = output_per_craft,
        energy = force_recipe.energy
    }
end

local WATER_TILE_NAMES = {
    water = true,
    deepwater = true,
    ["water-green"] = true,
    ["deepwater-green"] = true,
    ["water-shallow"] = true,
    ["water-mud"] = true
}

local function copy_arguments(arguments)
    local copy = {}
    for key, value in pairs(arguments) do copy[key] = value end
    return copy
end

local function nearby_placement(arguments, entity_name)
    local character = Character.get()
    if not character then return nil end
    local origin = {x = tonumber(arguments.x), y = tonumber(arguments.y)}
    if not origin.x or not origin.y then return nil end

    if entity_name == "offshore-pump" then
        local diagnostics = {checked = 0, sample = {}}
        local shores = {
            {dx = 0, dy = 1, direction = defines.direction.south},
            {dx = 1, dy = 0, direction = defines.direction.east},
            {dx = 0, dy = -1, direction = defines.direction.north},
            {dx = -1, dy = 0, direction = defines.direction.west}
        }
        for radius = 0, 20 do
            for offset_y = -radius, radius do
                for offset_x = -radius, radius do
                    if radius == 0 or math.abs(offset_x) == radius
                        or math.abs(offset_y) == radius then
                        local land = {
                            x = origin.x + offset_x,
                            y = origin.y + offset_y
                        }
                        if not WATER_TILE_NAMES[character.surface.get_tile(land.x, land.y).name] then
                            for _, shore in ipairs(shores) do
                                local water = {
                                    x = land.x + shore.dx,
                                    y = land.y + shore.dy
                                }
                                if WATER_TILE_NAMES[
                                    character.surface.get_tile(water.x, water.y).name
                                ] then
                                    local candidate = copy_arguments(arguments)
                                    candidate.x = land.x
                                    candidate.y = land.y
                                    candidate.direction = shore.direction
                                    candidate.direction_value = shore.direction
                                    local check = placement_check(candidate)
                                    if check and check.can_place then return check end
                                    diagnostics.checked = diagnostics.checked + 1
                                    if #diagnostics.sample < 4 then
                                        table.insert(diagnostics.sample, {
                                            position = {x = land.x, y = land.y},
                                            water = water,
                                            direction = shore.direction,
                                            in_reach = check and check.in_reach or nil,
                                            reason = check and check.reason or nil
                                        })
                                    end
                                end
                            end
                        end
                    end
                end
            end
        end
        return nil, diagnostics
    end

    for radius = 1, 10 do
        for offset_y = -radius, radius do
            for offset_x = -radius, radius do
                if math.abs(offset_x) == radius or math.abs(offset_y) == radius then
                    local candidate = copy_arguments(arguments)
                    candidate.x = origin.x + offset_x
                    candidate.y = origin.y + offset_y
                    local check = placement_check(candidate)
                    if check and check.can_place then return check end
                end
            end
        end
    end
    return nil
end

local function place_entity(arguments)
    local character = Character.get()
    if not character then
        return false, "AIRI has no character body"
    end
    local check, check_error = placement_check(arguments)
    if not check then return false, check_error end
    local item_name = check.item
    local entity_name = check.entity
    if character.get_item_count(item_name) < 1 then
        return false, "AIRI does not have " .. item_name .. " in her inventory"
    end
    local exact = arguments.exact ~= false
    if not check.can_place and (not exact or entity_name == "offshore-pump") then
        local nearby, diagnostics = nearby_placement(arguments, entity_name)
        check = nearby or check
        if not nearby and diagnostics then
            check.alternative_search = diagnostics
        end
    end
    if not check.can_place then
        return false, {
            error = "The entity cannot be placed at the requested position",
            check = check
        }
    end

    local created = character.surface.create_entity({
        name = entity_name,
        position = check.position,
        direction = check.direction,
        force = character.force,
        build_check_type = defines.build_check_type.manual,
        raise_built = true
    })
    if not created then
        return false, "Factorio did not create the requested entity"
    end

    character.remove_item({name = item_name, count = 1})
    local result = Observation.describe_entity(created)
    result.item = item_name
    result.entity = entity_name
    return true, result
end

local function insert_item(arguments)
    local character = Character.get()
    if not character then return false, "AIRI has no character body" end
    local item_name = arguments.item or arguments.name
    local requested = math.floor(tonumber(arguments.count or arguments.quantity) or 0)
    if type(item_name) ~= "string" or item_name == "" then
        return false, "insert_item requires an item name"
    end
    if requested < 1 or requested > 10000 then
        return false, "insert_item count must be between 1 and 10000"
    end
    if not prototypes.item[item_name] then
        return false, "Unknown item prototype: " .. item_name
    end
    local target, target_error = Observation.find_entity({
        x = arguments.x,
        y = arguments.y,
        name = arguments.target_name or arguments.entity
    })
    if not target then return false, target_error end
    local reach_distance = character.reach_distance or character.build_distance or 10
    if distance(character.position, target.position) > reach_distance then
        return false, "The target is out of reach; move_to it first"
    end
    local available = character.get_item_count(item_name)
    if available < 1 then
        return false, "AIRI does not have " .. item_name .. " in her inventory"
    end
    local count = math.min(requested, available)
    local inserted = 0

    if target.type == "transport-belt"
        or target.type == "underground-belt"
        or target.type == "splitter" then
        for _ = 1, count do
            local placed = false
            for line_index = 1, 8 do
                local line_ok, line = pcall(function()
                    return target.get_transport_line(line_index)
                end)
                if not line_ok or not line then break end
                local insert_ok, accepted = pcall(function()
                    return line.insert_at_back({name = item_name, count = 1})
                end)
                if insert_ok and accepted then
                    inserted = inserted + 1
                    placed = true
                    break
                end
            end
            if not placed then break end
        end
    else
        local burner_ok, burner = pcall(function() return target.burner end)
        local item = prototypes.item[item_name]
        if burner_ok and burner and item and (item.fuel_value or 0) > 0 then
            inserted = burner.inventory.insert({name = item_name, count = count})
        else
            local insert_ok, accepted = pcall(function()
                return target.insert({name = item_name, count = count})
            end)
            if insert_ok then inserted = accepted or 0 end
        end
    end
    if inserted < 1 then
        return false, "The target could not accept " .. item_name
    end
    character.remove_item({name = item_name, count = inserted})
    return true, {
        item = item_name,
        requested = requested,
        inserted = inserted,
        target = Observation.describe_entity(target)
    }
end

local function rotate_entity(arguments)
    local target, target_error = Observation.find_entity(arguments)
    if not target then return false, target_error end
    local direction, direction_name = parse_direction(arguments)
    if not direction then return false, direction_name end
    local rotate_ok, rotate_error = pcall(function()
        target.direction = direction
    end)
    if not rotate_ok then return false, tostring(rotate_error) end
    if target.direction ~= direction then
        return false, "Factorio did not apply the requested direction"
    end
    return true, Observation.describe_entity(target)
end

local function pickup_entity(arguments)
    local character = Character.get()
    if not character then return false, "AIRI has no character body" end
    local target, target_error = Observation.find_entity(arguments)
    if not target then return false, target_error end
    local reach_distance = character.reach_distance or character.build_distance or 10
    if distance(character.position, target.position) > reach_distance then
        return false, "The target is out of reach; move_to it first"
    end
    local name = target.name
    local position = {x = target.position.x, y = target.position.y}
    local inventory = character.get_main_inventory()
    local mine_ok, mined = pcall(function()
        return target.mine({
            inventory = inventory,
            force = true,
            raise_destroyed = true
        })
    end)
    if not mine_ok then return false, tostring(mined) end
    if not mined then return false, "Factorio could not pick up the target entity" end
    return true, {picked_up = true, entity = name, position = position}
end

function Actions.execute(action, arguments, request_id, owner_player_index)
    arguments = arguments or {}
    action = tostring(action or "")

    if action == "spawn" then
        local ok, result = Character.spawn(owner_player_index)
        return ok, result, false
    elseif action == "dismiss" then
        Tasks.cancel("AIRI was dismissed")
        Movement.stop("AIRI was dismissed", true)
        local ok, result = Character.dismiss()
        return ok, result, false
    elseif action == "follow" then
        Tasks.cancel("Replaced by follow mode")
        local ok, result = Movement.follow(owner_player_index)
        return ok, result, false
    elseif action == "move_to" or action == "move" then
        Tasks.cancel("Replaced by move_to")
        local ok, result = Movement.go_to({
            x = tonumber(arguments.x),
            y = tonumber(arguments.y)
        }, request_id)
        return ok, result, ok
    elseif action == "stop" or action == "cancel" then
        local _, task_result = Tasks.cancel("Stopped by command")
        local _, movement_result = Movement.stop("Stopped by command", true)
        return true, {
            task = task_result,
            movement = movement_result
        }, false
    elseif action == "observe" then
        return true, Observation.capture(arguments.radius), false
    elseif action == "get_entities" then
        local result, err = Observation.get_entities(arguments)
        return result ~= nil, result or err, false
    elseif action == "inspect_entity" or action == "get_entity" then
        return true, Observation.inspect_entity(arguments), false
    elseif action == "find_resource" or action == "locate_resource" then
        local result, err = Observation.find_resource(
            arguments.resource or arguments.name,
            arguments.radius
        )
        return result ~= nil, result or err, false
    elseif action == "inventory" or action == "inspect_inventory" then
        local inventory, err
        if arguments.x ~= nil and arguments.y ~= nil then
            inventory, err = Observation.entity_inventory(arguments)
        else
            inventory, err = Observation.inventory()
        end
        return inventory ~= nil, inventory or err, false
    elseif action == "status" then
        return true, status(), false
    elseif action == "wiki" or action == "prototype_info" then
        local character = Character.get()
        local force = character and character.force or nil
        if not force and owner_player_index then
            local owner = game.get_player(owner_player_index)
            force = owner and owner.force or nil
        end
        local result, err = Wiki.lookup(arguments.query or arguments.name, force)
        return result ~= nil, result or err, false
    elseif action == "mine" or action == "mine_resource" then
        Tasks.cancel("Replaced by a new mining task")
        Movement.stop("Preparing to mine", true)
        local ok, result = Tasks.start_mining(
            arguments.resource or arguments.name,
            arguments.count,
            request_id
        )
        return ok, result, ok
    elseif action == "craft" or action == "craft_item" then
        local ok, result = craft_item(arguments)
        return ok, result, false
    elseif action == "place" or action == "place_entity" then
        local ok, result = place_entity(arguments)
        return ok, result, false
    elseif action == "can_place_entity" then
        local result, err = placement_check(arguments)
        return result ~= nil, result or err, false
    elseif action == "insert_item" then
        local ok, result = insert_item(arguments)
        return ok, result, false
    elseif action == "rotate_entity" then
        local ok, result = rotate_entity(arguments)
        return ok, result, false
    elseif action == "pickup_entity" then
        local ok, result = pickup_entity(arguments)
        return ok, result, false
    elseif action == "say" then
        local text = tostring(arguments.text or "")
        if text == "" then
            return false, "say requires non-empty text", false
        end
        State.ensure().last_message = text
        State.append_chat_message("assistant", text)
        game.print({"", "[AIRI] ", text})
        return true, {text = text}, false
    end

    local handled, ok, result, asynchronous = Upstream.execute(action, arguments)
    if handled then return ok, result, asynchronous end

    return false, "Unknown AIRI action: " .. action, false
end

return Actions
