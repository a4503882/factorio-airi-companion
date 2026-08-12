local function require_value(condition, message)
    if not condition then
        error("[airi-companion-smoke] FAIL: " .. message)
    end
end

local function distance(a, b)
    local dx = a.x - b.x
    local dy = a.y - b.y
    return math.sqrt((dx * dx) + (dy * dy))
end

local function resource_by_name(resources, name)
    for _, resource in pairs(resources or {}) do
        if resource.name == name then
            return resource
        end
    end
    return nil
end

local function contains_value(values, target)
    for _, value in pairs(values or {}) do
        if value == target then return true end
    end
    return false
end

local function find_descendant(root, name)
    if not root or not root.valid then return nil end
    if root.name == name then return root end
    for _, child in pairs(root.children) do
        local found = find_descendant(child, name)
        if found then return found end
    end
    return nil
end

local function execute_action(action, arguments, request_id)
    local ok, result = remote.call(
        "airi_companion",
        "execute",
        action,
        arguments or {},
        1,
        request_id
    )
    require_value(ok, action .. " failed: " .. helpers.table_to_json(result))
    return result
end

local function find_coal_line_layout(character)
    local surface = character.surface
    local origin = character.position
    for dy = -5, 5 do
        for dx = -5, 0 do
            local drill = {
                x = math.floor(origin.x) + dx,
                y = math.floor(origin.y) + dy
            }
            local belt1 = {x = drill.x + 1.5, y = drill.y - 0.5}
            local belt2 = {x = belt1.x + 1, y = belt1.y}
            local inserter = {x = belt2.x + 1, y = belt2.y}
            local chest = {x = inserter.x + 1, y = inserter.y}
            local has_coal = surface.count_entities_filtered({
                area = {
                    {drill.x - 1, drill.y - 1},
                    {drill.x + 1, drill.y + 1}
                },
                name = "coal"
            }) > 0
            local in_reach = distance(origin, chest) <= character.build_distance
            if has_coal and in_reach
                and surface.can_place_entity({
                    name = "burner-mining-drill",
                    position = drill,
                    direction = defines.direction.east,
                    force = character.force,
                    build_check_type = defines.build_check_type.manual
                })
                and surface.can_place_entity({
                    name = "transport-belt",
                    position = belt1,
                    direction = defines.direction.east,
                    force = character.force,
                    build_check_type = defines.build_check_type.manual
                })
                and surface.can_place_entity({
                    name = "transport-belt",
                    position = belt2,
                    direction = defines.direction.east,
                    force = character.force,
                    build_check_type = defines.build_check_type.manual
                })
                and surface.can_place_entity({
                    name = "burner-inserter",
                    position = inserter,
                    direction = defines.direction.east,
                    force = character.force,
                    build_check_type = defines.build_check_type.manual
                })
                and surface.can_place_entity({
                    name = "wooden-chest",
                    position = chest,
                    force = character.force,
                    build_check_type = defines.build_check_type.manual
                }) then
                return {
                    drill = drill,
                    belt1 = belt1,
                    belt2 = belt2,
                    inserter = inserter,
                    chest = chest
                }
            end
        end
    end
    return nil
end

local function run_checks()
    if storage.airi_companion_smoke_done then return end
    storage.airi_companion_smoke_done = true

    local status_ok, status = remote.call(
        "airi_companion",
        "execute",
        "status",
        {},
        1,
        "smoke-status"
    )
    require_value(status_ok, "status action failed")
    require_value(status.character and status.character.present, "character is missing")
    require_value(type(status.character.max_health) == "number", "max health is invalid")
    require_value(status.character.force == game.get_player(1).force.name, "character is not on the owner's force")
    require_value(status.character.same_force_as_owner, "character does not report teammate force membership")
    require_value(status.character.display_name == "团子", "companion display name is wrong")
    require_value(status.character.name_label_present, "world-space name label is missing")
    require_value(status.character.map_tag and status.character.map_tag.present, "map tag is missing")
    require_value(status.character.map_tag.text == "团子", "map tag name is wrong")
    require_value(status.character.map_tag.force == game.get_player(1).force.name, "map tag is not force-scoped")

    local iron_wiki = execute_action(
        "wiki",
        {query = "iron-plate"},
        "smoke-wiki-iron-plate"
    )
    require_value(
        iron_wiki.source == "current-game-prototypes",
        "wiki source is not the live prototype registry"
    )
    require_value(
        iron_wiki.item and iron_wiki.item.name == "iron-plate",
        "wiki did not describe the iron-plate item"
    )
    require_value(
        iron_wiki.recipe and iron_wiki.recipe.category == "smelting",
        "wiki did not identify iron-plate as a smelting recipe"
    )
    require_value(
        iron_wiki.recipe.force_enabled,
        "wiki did not report the current force's iron-plate recipe as enabled"
    )
    local iron_ingredient = resource_by_name(
        iron_wiki.recipe.ingredients,
        "iron-ore"
    )
    local iron_product = resource_by_name(
        iron_wiki.recipe.products,
        "iron-plate"
    )
    require_value(
        iron_ingredient and iron_ingredient.amount == 1,
        "wiki returned the wrong iron-plate ingredient"
    )
    require_value(
        iron_product and iron_product.amount == 1,
        "wiki returned the wrong iron-plate product"
    )
    require_value(
        pcall(helpers.table_to_json, iron_wiki),
        "wiki result is not JSON-safe"
    )

    local furnace_wiki = execute_action(
        "wiki",
        {query = "stone-furnace"},
        "smoke-wiki-stone-furnace"
    )
    require_value(
        furnace_wiki.entity and
            contains_value(furnace_wiki.entity.crafting_categories, "smelting"),
        "wiki did not report the stone furnace smelting category"
    )
    require_value(
        furnace_wiki.entity.burner and
            contains_value(furnace_wiki.entity.burner.fuel_categories, "chemical"),
        "wiki did not report the stone furnace chemical fuel category"
    )

    local chat_input = prototypes.custom_input["airi-companion-toggle-chat"]
    require_value(chat_input ~= nil, "chat custom input prototype is missing")
    require_value(chat_input.key_sequence == "G", "chat custom input does not default to G")

    local player = game.get_player(1)
    local toggled = remote.call("airi_companion", "toggle_chat", player.index)
    require_value(toggled, "chat panel could not be toggled through the runtime interface")
    local chat_panel = player.gui.screen["airi_companion_chat_panel"]
    require_value(chat_panel and chat_panel.valid, "chat hotkey did not open the panel")
    require_value(
        find_descendant(chat_panel, "airi_companion_history") ~= nil,
        "chat history scroll pane is missing"
    )
    require_value(
        find_descendant(chat_panel, "airi_companion_input") ~= nil,
        "chat input field is missing"
    )
    require_value(
        find_descendant(chat_panel, "airi_companion_send") ~= nil,
        "chat send button is missing"
    )

    remote.call("airi_companion", "set_activity", "SECRET_COT_SHOULD_NOT_RENDER")
    local activity = find_descendant(chat_panel, "airi_companion_activity")
    require_value(activity ~= nil, "chat processing indicator is missing")
    local activity_caption_json = helpers.table_to_json(activity.caption)
    require_value(
        activity.caption[1] == "gui.airi-processing" or
            activity.caption[1] == "gui.airi-processing-disconnected",
        "chat processing indicator does not use the generic activity caption: " ..
            activity_caption_json
    )
    require_value(
        not string.find(activity_caption_json, "SECRET_COT_SHOULD_NOT_RENDER", 1, true),
        "plan/reasoning text leaked into the chat UI"
    )
    remote.call("airi_companion", "set_activity", "")

    local character = remote.call("airi_companion", "get_character")
    require_value(character and character.valid, "remote character handle is invalid")

    local dense_count = 0
    for x = -8, 8 do
        for y = -8, 8 do
            if x ~= 0 or y ~= 0 then
                local coal = character.surface.create_entity({
                    name = "coal",
                    position = {
                        x = character.position.x + x + 0.5,
                        y = character.position.y + y + 0.5
                    },
                    amount = 100
                })
                if coal and coal.valid then
                    dense_count = dense_count + 1
                end
            end
        end
    end
    require_value(dense_count > 256, "dense coal fixture did not exceed the old shared limit")

    local iron_resource = character.surface.create_entity({
        name = "iron-ore",
        position = {
            x = character.position.x + 0.5,
            y = character.position.y + 0.5
        },
        amount = 100
    })
    require_value(iron_resource and iron_resource.valid, "test iron resource was not created")

    local water_tiles = {}
    local water_origin = {
        x = math.floor(character.position.x) + 7,
        y = math.floor(character.position.y)
    }
    for x = 0, 4 do
        for y = 0, 4 do
            table.insert(water_tiles, {
                name = "water",
                position = {x = water_origin.x + x, y = water_origin.y + y}
            })
        end
    end
    character.surface.set_tiles(water_tiles, true, true, true, false)

    require_value(
        character.surface.can_place_entity({
            name = "offshore-pump",
            position = {x = water_origin.x - 1, y = water_origin.y},
            direction = defines.direction.east,
            force = character.force,
            build_check_type = defines.build_check_type.manual
        }),
        "controlled water fixture has no valid offshore-pump position"
    )

    local nearest_iron = execute_action(
        "nearest",
        {name = "iron-ore", radius = 32},
        "smoke-upstream-nearest-iron"
    )
    require_value(nearest_iron.name == "iron-ore", "upstream nearest returned wrong ore")
    require_value(nearest_iron.position ~= nil, "upstream nearest omitted ore position")

    local nearest_water = execute_action(
        "nearest",
        {name = "water", radius = 32},
        "smoke-upstream-nearest-water"
    )
    require_value(nearest_water.name == "water", "upstream nearest did not find water")
    local water_patch = execute_action(
        "get_resource_patch",
        {
            resource = "water",
            x = water_origin.x + 2,
            y = water_origin.y + 2,
            radius = 6
        },
        "smoke-upstream-water-patch"
    )
    require_value(water_patch.name == "water", "water patch returned the wrong name")
    require_value(water_patch.size >= 25, "water patch omitted created water tiles")
    require_value(
        water_patch.bounding_box and water_patch.bounding_box.left_top
            and water_patch.bounding_box.right_bottom,
        "water patch omitted its bounding box"
    )

    character.insert({name = "offshore-pump", count = 1})
    local offshore_pump = execute_action(
        "place_entity",
        {
            item = "offshore-pump",
            x = water_origin.x + 2,
            y = water_origin.y + 2,
            direction = "north",
            exact = false
        },
        "smoke-upstream-place-offshore-pump"
    )
    require_value(
        offshore_pump.name == "offshore-pump",
        "place_entity exact=false did not place an offshore pump on the shore"
    )
    local picked_pump = execute_action(
        "pickup_entity",
        {
            name = "offshore-pump",
            x = offshore_pump.position.x,
            y = offshore_pump.position.y
        },
        "smoke-upstream-pickup-offshore-pump"
    )
    require_value(picked_pump.picked_up, "offshore pump could not be reclaimed")

    local distant_target = {
        x = character.position.x + 80,
        y = character.position.y
    }
    character.surface.request_to_generate_chunks(distant_target, 1)
    character.surface.force_generate_chunk_requests()
    local distant_position = character.surface.find_non_colliding_position(
        "wooden-chest",
        distant_target,
        10,
        0.5
    )
    require_value(distant_position ~= nil, "no distant get_entities fixture position")
    require_value(
        distance(character.position, distant_position) > 64,
        "distant get_entities fixture remained inside passive perception"
    )
    local distant_chest = character.surface.create_entity({
        name = "wooden-chest",
        position = distant_position,
        force = character.force
    })
    require_value(distant_chest and distant_chest.valid, "distant chest was not created")
    local distant_entities = execute_action(
        "get_entities",
        {
            names = {"wooden-chest"},
            radius = 100,
            upstream_api = true
        },
        "smoke-upstream-get-entities-radius"
    )
    local found_distant_chest = false
    for _, candidate in pairs(distant_entities) do
        if candidate.name == "wooden-chest"
            and distance(candidate.position, distant_chest.position) < 0.1 then
            found_distant_chest = true
            break
        end
    end
    require_value(
        found_distant_chest,
        "upstream get_entities omitted the distant chest: "
            .. helpers.table_to_json({
                character = character.position,
                requested = distant_position,
                created = distant_chest.position,
                returned = distant_entities
            })
    )
    distant_chest.destroy()

    local buildable = execute_action(
        "nearest_buildable",
        {
            item = "stone-furnace",
            building_box = {width = 2, height = 2},
            x = character.position.x,
            y = character.position.y,
            max_radius = 20
        },
        "smoke-upstream-nearest-buildable"
    )
    require_value(
        buildable.left_top and buildable.right_bottom,
        "nearest_buildable omitted its bounding box"
    )
    require_value(
        buildable.right_bottom.x - buildable.left_top.x == 2
            and buildable.right_bottom.y - buildable.left_top.y == 2,
        "nearest_buildable returned the wrong dimensions"
    )

    local automation_progress = execute_action(
        "get_research_progress",
        {technology = "automation"},
        "smoke-upstream-research-progress"
    )
    require_value(
        type(automation_progress) == "table",
        "get_research_progress did not return an ingredient list"
    )

    local observation = remote.call("airi_companion", "observe", 16)
    require_value(type(observation) == "table", "observation is not a table")
    for _, key in pairs({"character", "owner", "movement", "resources", "buildings"}) do
        require_value(observation[key] ~= nil, "observation is missing " .. key)
    end
    require_value(pcall(helpers.table_to_json, observation), "observation is not JSON-safe")
    local coal_overview = resource_by_name(observation.resources, "coal")
    local iron_overview = resource_by_name(observation.resources, "iron-ore")
    require_value(coal_overview ~= nil, "dense coal is missing from resource overview")
    require_value(iron_overview ~= nil, "iron was hidden by the dense coal patch")
    require_value(coal_overview.entity_count > 256, "coal entity count was capped")
    require_value(coal_overview.truncated, "large coal patch was not marked truncated")
    require_value(coal_overview.sampled_entity_count == 256, "coal sample size is wrong")
    require_value(coal_overview.amount == nil, "sampled coal amount was exposed as a total")

    local find_ok, find_result = remote.call(
        "airi_companion",
        "execute",
        "find_resource",
        {resource = "iron-ore", radius = 16},
        1,
        "smoke-find-resource"
    )
    require_value(find_ok, "find_resource action failed")
    require_value(find_result.found, "find_resource did not find nearby iron")
    require_value(find_result.name == "iron-ore", "find_resource returned wrong resource")
    require_value(find_result.entity_count >= 1, "find_resource entity count is invalid")
    require_value(find_result.nearest ~= nil, "find_resource omitted nearest position")

    local missing_ok, missing_result = remote.call(
        "airi_companion",
        "execute",
        "find_resource",
        {resource = "uranium-ore", radius = 8},
        1,
        "smoke-find-missing-resource"
    )
    require_value(missing_ok, "missing find_resource query failed structurally")
    require_value(not missing_result.found, "missing resource was falsely reported present")

    local position = status.character.position
    local move_ok, move_result, move_async = remote.call(
        "airi_companion",
        "execute",
        "move_to",
        {x = position.x, y = position.y},
        1,
        "smoke-move"
    )
    require_value(move_ok, "move_to action failed")
    require_value(move_async, "move_to was not marked asynchronous")
    require_value(type(move_result.path_request_id) == "number", "path request was not created")

    remote.call("airi_companion", "execute", "stop", {}, 1, "smoke-stop")

    character.insert({name = "iron-plate", count = 4})
    local craft_ok, craft_result = remote.call(
        "airi_companion",
        "execute",
        "craft_item",
        {recipe = "iron-gear-wheel", count = 1},
        1,
        "smoke-craft"
    )
    require_value(craft_ok, "craft_item failed: " .. tostring(craft_result))
    require_value(craft_result.queued == 1, "craft_item did not queue one recipe")

    character.insert({name = "stone-furnace", count = 1})
    local desired_build_position = {
        x = character.position.x + 3,
        y = character.position.y
    }
    local build_position = character.surface.find_non_colliding_position(
        "stone-furnace",
        desired_build_position,
        6,
        0.5
    )
    require_value(build_position ~= nil, "no nearby furnace placement position")
    require_value(
        distance(character.position, build_position) <= character.build_distance,
        "furnace placement position is out of reach"
    )
    local placement_check = execute_action(
        "can_place_entity",
        {
            item = "stone-furnace",
            x = build_position.x,
            y = build_position.y,
            direction = "south"
        },
        "smoke-can-place"
    )
    require_value(placement_check.can_place, "can_place_entity rejected a valid position")
    require_value(placement_check.direction_name == "south", "string direction was not parsed")
    local place_ok, place_result = remote.call(
        "airi_companion",
        "execute",
        "place_entity",
        {
            item = "stone-furnace",
            x = build_position.x,
            y = build_position.y,
            direction = "south"
        },
        1,
        "smoke-place"
    )
    require_value(place_ok, "place_entity failed: " .. tostring(place_result))
    require_value(place_result.entity == "stone-furnace", "wrong entity was placed")

    character.insert({name = "coal", count = 10})
    local insertion = execute_action(
        "insert_item",
        {
            item = "coal",
            count = 5,
            target_name = "stone-furnace",
            x = place_result.position.x,
            y = place_result.position.y
        },
        "smoke-insert"
    )
    require_value(insertion.inserted == 5, "insert_item inserted the wrong count")
    require_value(
        insertion.target.inventories.fuel.coal == 5,
        "insert_item did not report furnace fuel inventory"
    )

    local inspected = execute_action(
        "inspect_entity",
        {
            name = "stone-furnace",
            x = place_result.position.x,
            y = place_result.position.y
        },
        "smoke-inspect-entity"
    )
    require_value(inspected.found, "inspect_entity did not find the furnace")
    require_value(
        ((inspected.inventory.coal or 0) >= 1 and inspected.inventory.coal <= 5)
            or (inspected.burner and inspected.burner.currently_burning == "coal"),
        "inspect_entity omitted stored and burning fuel: " .. helpers.table_to_json(inspected)
    )

    local target_inventory = execute_action(
        "inspect_inventory",
        {
            name = "stone-furnace",
            x = place_result.position.x,
            y = place_result.position.y
        },
        "smoke-inspect-inventory"
    )
    require_value(
        ((target_inventory.contents.coal or 0) >= 1
            and target_inventory.contents.coal <= 5)
            or (target_inventory.burner
                and target_inventory.burner.currently_burning == "coal"),
        "target inventory query omitted stored and burning fuel"
    )

    local extracted = execute_action(
        "extract_item",
        {
            item = "coal",
            count = 1,
            source_name = "stone-furnace",
            x = place_result.position.x,
            y = place_result.position.y
        },
        "smoke-upstream-extract-item"
    )
    require_value(extracted.extracted == 1, "extract_item returned the wrong count")
    require_value(
        extracted.source and extracted.source.name == "stone-furnace",
        "extract_item omitted the source entity"
    )

    local nearby_furnaces = execute_action(
        "get_entities",
        {
            names = {"stone-furnace"},
            x = place_result.position.x,
            y = place_result.position.y,
            radius = 8
        },
        "smoke-get-entities"
    )
    require_value(#nearby_furnaces >= 1, "get_entities did not return the furnace")

    local pickup_result = execute_action(
        "pickup_entity",
        {
            name = "stone-furnace",
            x = place_result.position.x,
            y = place_result.position.y
        },
        "smoke-pickup"
    )
    require_value(pickup_result.picked_up, "pickup_entity did not mine the furnace")

    character.insert({name = "burner-inserter", count = 1})
    local inserter_position = character.surface.find_non_colliding_position(
        "burner-inserter",
        {x = character.position.x - 3, y = character.position.y},
        6,
        0.5
    )
    require_value(inserter_position ~= nil, "no nearby inserter placement position")
    local placed_inserter = execute_action(
        "place_entity",
        {
            item = "burner-inserter",
            x = inserter_position.x,
            y = inserter_position.y,
            direction = "north"
        },
        "smoke-place-inserter"
    )
    local filtered_inserter = execute_action(
        "set_entity_recipe",
        {
            recipe = "iron-plate",
            target_name = "burner-inserter",
            x = placed_inserter.position.x,
            y = placed_inserter.position.y
        },
        "smoke-upstream-set-inserter-filter"
    )
    require_value(
        filtered_inserter.filter == "iron-plate",
        "set_entity_recipe did not expose the inserter filter"
    )
    local rotated_inserter = execute_action(
        "rotate_entity",
        {
            name = "burner-inserter",
            x = placed_inserter.position.x,
            y = placed_inserter.position.y,
            direction = "east"
        },
        "smoke-rotate-inserter"
    )
    require_value(rotated_inserter.direction == defines.direction.east, "rotate_entity direction is wrong")
    require_value(rotated_inserter.direction_name == "east", "rotate_entity name is wrong")
    local picked_inserter = execute_action(
        "pickup_entity",
        {
            name = "burner-inserter",
            x = placed_inserter.position.x,
            y = placed_inserter.position.y
        },
        "smoke-pickup-inserter"
    )
    require_value(picked_inserter.picked_up, "rotated inserter could not be picked up")

    local clear_area = {
        {character.position.x - 12, character.position.y - 12},
        {character.position.x + 12, character.position.y + 12}
    }
    for _, entity in pairs(character.surface.find_entities_filtered({area = clear_area})) do
        if entity.valid and entity.type == "tree" then entity.destroy() end
    end
    local coal_area = {
        {character.position.x - 6, character.position.y - 6},
        {character.position.x + 1, character.position.y + 6}
    }
    for _, entity in pairs(character.surface.find_entities_filtered({area = coal_area})) do
        if entity.valid and entity.name == "coal" then entity.destroy() end
    end
    for x = math.floor(character.position.x) - 5, math.floor(character.position.x) do
        for y = math.floor(character.position.y) - 5, math.floor(character.position.y) + 5 do
            local coal = character.surface.create_entity({
                name = "coal",
                position = {x = x + 0.5, y = y + 0.5},
                amount = 1000
            })
            require_value(coal and coal.valid, "coal production fixture could not be created")
        end
    end
    local layout = find_coal_line_layout(character)
    require_value(layout ~= nil, "no valid automated coal-line layout was found")
    character.insert({name = "burner-mining-drill", count = 1})
    character.insert({name = "transport-belt", count = 2})
    character.insert({name = "burner-inserter", count = 1})
    character.insert({name = "wooden-chest", count = 1})
    character.insert({name = "coal", count = 20})

    local drill = execute_action(
        "place_entity",
        {
            item = "burner-mining-drill",
            x = layout.drill.x,
            y = layout.drill.y,
            direction = "east"
        },
        "smoke-coal-drill"
    )
    local connection_arguments = {
        waypoints = {
            {x = layout.belt1.x, y = layout.belt1.y, kind = "position"},
            {x = layout.belt2.x, y = layout.belt2.y, kind = "position"}
        },
        connection_types = {"transport-belt"}
    }
    connection_arguments.dry_run = true
    local connection_plan = execute_action(
        "connect_entities",
        connection_arguments,
        "smoke-upstream-connect-belts-dry-run"
    )
    require_value(
        connection_plan.number_of_entities_required == 2,
        "connect_entities dry run returned the wrong belt count"
    )
    connection_arguments.dry_run = false
    local belt_group = execute_action(
        "connect_entities",
        connection_arguments,
        "smoke-upstream-connect-belts"
    )
    require_value(belt_group.name == "belt-group", "connect_entities returned wrong group")
    require_value(#belt_group.entities == 2, "connect_entities returned wrong members")
    local line_inserter = execute_action(
        "place_entity",
        {item = "burner-inserter", x = layout.inserter.x, y = layout.inserter.y, direction = "west"},
        "smoke-coal-inserter"
    )
    local line_chest = execute_action(
        "place_entity",
        {item = "wooden-chest", x = layout.chest.x, y = layout.chest.y, direction = "north"},
        "smoke-coal-chest"
    )
    execute_action(
        "insert_item",
        {item = "coal", count = 10, target_name = drill.name, x = drill.position.x, y = drill.position.y},
        "smoke-fuel-coal-drill"
    )
    execute_action(
        "insert_item",
        {
            item = "coal",
            count = 5,
            target_name = line_inserter.name,
            x = line_inserter.position.x,
            y = line_inserter.position.y
        },
        "smoke-fuel-coal-inserter"
    )
    local chest_before = execute_action(
        "inspect_inventory",
        {name = line_chest.name, x = line_chest.position.x, y = line_chest.position.y},
        "smoke-coal-chest-before"
    )
    storage.airi_companion_smoke_coal_line = {
        chest = line_chest.position,
        coal_before = chest_before.contents.coal or 0
    }

    local mine_ok, mine_result, mine_async = remote.call(
        "airi_companion",
        "execute",
        "mine_resource",
        {resource = "iron-ore", count = 1},
        1,
        "smoke-mine"
    )
    require_value(mine_ok, "mine_resource failed: " .. tostring(mine_result))
    require_value(mine_async, "mine_resource was not marked asynchronous")

    storage.airi_companion_smoke_final_tick = game.tick + 900
end

local function schedule_checks()
    if not storage.airi_companion_smoke_done then
        storage.airi_companion_smoke_start_tick = game.tick + 60
    end
end

script.on_init(schedule_checks)
script.on_configuration_changed(schedule_checks)

script.on_event(defines.events.on_tick, function(event)
    local start_tick = storage.airi_companion_smoke_start_tick
    if start_tick and event.tick >= start_tick then
        storage.airi_companion_smoke_start_tick = nil
        run_checks()
    end

    local final_tick = storage.airi_companion_smoke_final_tick
    if not final_tick or event.tick < final_tick then return end
    storage.airi_companion_smoke_final_tick = nil

    local character = remote.call("airi_companion", "get_character")
    require_value(character and character.valid, "character vanished during action checks")
    local final_status_ok, final_status = remote.call(
        "airi_companion",
        "execute",
        "status",
        {},
        1,
        "smoke-final-status"
    )
    require_value(final_status_ok, "final status action failed")
    require_value(final_status.character.name_label_present, "name label vanished")
    require_value(final_status.character.map_tag.present, "map tag vanished")
    require_value(
        distance(final_status.character.map_tag.position, character.position) < 0.1,
        "map tag did not follow the companion"
    )
    require_value(character.get_item_count("iron-gear-wheel") >= 1, "crafting did not finish")
    require_value(character.get_item_count("iron-ore") >= 1, "mining did not collect iron ore")

    local coal_line = storage.airi_companion_smoke_coal_line
    require_value(coal_line ~= nil, "coal production line state is missing")
    local coal_chest_inventory = execute_action(
        "inspect_inventory",
        {
            name = "wooden-chest",
            x = coal_line.chest.x,
            y = coal_line.chest.y
        },
        "smoke-coal-chest-after"
    )
    local coal_after = coal_chest_inventory.contents.coal or 0
    local coal_line_entities = execute_action(
        "get_entities",
        {
            names = {
                "burner-mining-drill",
                "transport-belt",
                "burner-inserter",
                "wooden-chest"
            },
            x = coal_line.chest.x - 2,
            y = coal_line.chest.y,
            radius = 8
        },
        "smoke-coal-line-diagnostics"
    )
    require_value(
        coal_after > coal_line.coal_before,
        "automated coal line did not increase chest coal: before=" ..
            coal_line.coal_before .. ", after=" .. coal_after ..
            ", entities=" .. helpers.table_to_json(coal_line_entities)
    )

    log(
        "[airi-companion-smoke] PASS: chat UI/hotkey, teammate identity, " ..
        "resource overview/query including water patches, upstream layout/connection, " ..
        "live prototype wiki, movement, crafting, placement, mining, entity correction, " ..
        "inventory transfer, and automated coal output"
    )
end)
