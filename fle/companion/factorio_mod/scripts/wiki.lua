local Wiki = {}

local PRODUCING_RECIPE_LIMIT = 24

local function safe_property(object, property)
    if not object then return nil end
    local ok, value = pcall(function() return object[property] end)
    if not ok then return nil end
    return value
end

local function sorted_true_keys(values)
    local result = {}
    for key, enabled in pairs(values or {}) do
        if enabled then table.insert(result, tostring(key)) end
    end
    table.sort(result)
    return result
end

local function copy_position(position)
    if not position then return nil end
    return {x = position.x or position[1], y = position.y or position[2]}
end

local function copy_bounding_box(box)
    if not box then return nil end
    local left_top = box.left_top or box[1]
    local right_bottom = box.right_bottom or box[2]
    if not left_top or not right_bottom then return nil end
    return {
        left_top = copy_position(left_top),
        right_bottom = copy_position(right_bottom)
    }
end

local function describe_ingredient(ingredient)
    local result = {
        type = ingredient.type,
        name = ingredient.name,
        amount = ingredient.amount
    }
    if ingredient.temperature ~= nil then result.temperature = ingredient.temperature end
    if ingredient.minimum_temperature ~= nil then
        result.minimum_temperature = ingredient.minimum_temperature
    end
    if ingredient.maximum_temperature ~= nil then
        result.maximum_temperature = ingredient.maximum_temperature
    end
    return result
end

local function describe_product(product)
    local result = {
        type = product.type,
        name = product.name,
        probability = product.probability
    }
    if product.amount ~= nil then result.amount = product.amount end
    if product.amount_min ~= nil then result.amount_min = product.amount_min end
    if product.amount_max ~= nil then result.amount_max = product.amount_max end
    if product.temperature ~= nil then result.temperature = product.temperature end
    if product.extra_count_fraction ~= nil then
        result.extra_count_fraction = product.extra_count_fraction
    end
    return result
end

local function describe_recipe(recipe, force)
    local ingredients = {}
    for _, ingredient in pairs(recipe.ingredients or {}) do
        table.insert(ingredients, describe_ingredient(ingredient))
    end
    local products = {}
    for _, product in pairs(recipe.products or {}) do
        table.insert(products, describe_product(product))
    end

    local force_recipe = force and force.recipes[recipe.name] or nil
    local force_enabled = nil
    if force_recipe then force_enabled = force_recipe.enabled end
    return {
        name = recipe.name,
        type = recipe.type,
        hidden = recipe.hidden,
        category = recipe.category,
        energy = recipe.energy,
        prototype_enabled = recipe.enabled,
        force_enabled = force_enabled,
        ingredients = ingredients,
        products = products
    }
end

local function describe_item(item)
    local place_result = safe_property(item, "place_result")
    local burnt_result = safe_property(item, "burnt_result")
    return {
        name = item.name,
        type = item.type,
        hidden = item.hidden,
        stack_size = item.stack_size,
        fuel_category = safe_property(item, "fuel_category"),
        fuel_value = safe_property(item, "fuel_value"),
        place_result = place_result and place_result.name or nil,
        burnt_result = burnt_result and burnt_result.name or nil
    }
end

local function describe_entity(entity)
    local burner_prototype = safe_property(entity, "burner_prototype")
    local burner = nil
    if burner_prototype then
        burner = {
            fuel_categories = sorted_true_keys(burner_prototype.fuel_categories),
            effectivity = burner_prototype.effectivity
        }
    end
    return {
        name = entity.name,
        type = entity.type,
        hidden = entity.hidden,
        tile_width = safe_property(entity, "tile_width"),
        tile_height = safe_property(entity, "tile_height"),
        collision_box = copy_bounding_box(safe_property(entity, "collision_box")),
        crafting_categories = sorted_true_keys(
            safe_property(entity, "crafting_categories")
        ),
        resource_categories = sorted_true_keys(
            safe_property(entity, "resource_categories")
        ),
        burner = burner
    }
end

local function recipe_produces(recipe, prototype_name)
    for _, product in pairs(recipe.products or {}) do
        if product.name == prototype_name then return true end
    end
    return false
end

function Wiki.lookup(raw_query, force)
    if type(raw_query) ~= "string" then
        return nil, "wiki requires an item, entity, or recipe prototype name"
    end
    local query = string.match(raw_query, "^%s*(.-)%s*$")
    if query == "" then
        return nil, "wiki query must not be blank"
    end
    if string.len(query) > 200 then
        return nil, "wiki query is too long"
    end

    local item = prototypes.item[query]
    local entity = prototypes.entity[query]
    local recipe = prototypes.recipe[query]
    if item and item.place_result then entity = item.place_result end

    local producing_names = {}
    for name, candidate in pairs(prototypes.recipe) do
        if recipe_produces(candidate, query) then
            table.insert(producing_names, name)
        end
    end
    table.sort(producing_names)

    if not item and not entity and not recipe and #producing_names == 0 then
        return nil, "No live item, entity, or recipe prototype matched: " .. query
    end

    local producing = {}
    local included = math.min(#producing_names, PRODUCING_RECIPE_LIMIT)
    for index = 1, included do
        table.insert(
            producing,
            describe_recipe(prototypes.recipe[producing_names[index]], force)
        )
    end

    return {
        source = "current-game-prototypes",
        query = query,
        force = force and force.name or nil,
        item = item and describe_item(item) or nil,
        entity = entity and describe_entity(entity) or nil,
        recipe = recipe and describe_recipe(recipe, force) or nil,
        recipes_that_produce = producing,
        producing_recipe_count = #producing_names,
        producing_recipes_truncated = #producing_names > PRODUCING_RECIPE_LIMIT
    }
end

return Wiki
