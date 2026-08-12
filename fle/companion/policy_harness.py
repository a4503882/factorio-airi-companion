"""Upstream FLE policy-harness adapter for the AIRI companion.

The companion deliberately reuses :class:`FactorioNamespace` and
``parse_response``.  Only the game transport is adapted: namespace functions
make synchronous calls through the localhost UDP bridge instead of RCON.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import enum
import math
import re
from types import SimpleNamespace
import sys
import time
from typing import Any, Callable, Iterable

from fle.agents.llm.parsing import Policy, PythonParser, parse_response
from fle.env.entities import (
    BoundingBox,
    BuildingBox,
    Direction,
    Ingredient,
    Inventory,
    Position,
    Product,
    Recipe,
    ResourcePatch,
)
from fle.env.game_types import Prototype, prototype_by_name
from fle.env.namespace import FactorioNamespace


CommandRunner = Callable[[str, dict[str, Any], float], Any]
CancellableCommandRunner = Callable[
    [str, dict[str, Any], float, Callable[[], bool] | None],
    Any,
]


# ``LuaScriptManager.setup_tools`` discovers these names from
# ``fle/env/tools/agent`` in upstream FLE.  Keeping the contract explicit here
# makes transport parity testable without starting Factorio or silently
# substituting a Companion-only verb for an upstream tool.
UPSTREAM_AGENT_API = frozenset(
    {
        "can_place_entity",
        "connect_entities",
        "craft_item",
        "extract_item",
        "get_connection_amount",
        "get_entities",
        "get_entity",
        "get_prototype_recipe",
        "get_research_progress",
        "get_resource_patch",
        "harvest_resource",
        "insert_item",
        "inspect_inventory",
        "launch_rocket",
        "move_to",
        "nearest",
        "nearest_buildable",
        "pickup_entity",
        "place_entity",
        "place_entity_next_to",
        "print",
        "rotate_entity",
        "score",
        "send_message",
        "set_entity_recipe",
        "set_research",
        "sleep",
    }
)

COMPANION_EXTENSION_API = frozenset(
    {
        "factorio_wiki",
        "find_resource",
        "follow",
        "harness_help",
        "inspect_entity",
        "mine_resource",
        "observe",
        "skill_help",
        "spawn",
        "status",
        "stop",
        "wait",
        "wiki",
    }
)


POLICY_HARNESS_INSTRUCTIONS = r"""
## FLE Python policy harness (authoritative game-action contract)

This section replaces any earlier instruction that asks you to call one JSON
Factorio function at a time. Game actions are performed by writing a Python
policy for the existing Factorio Learning Environment harness. Never emit Lua,
console commands, JSON tool calls, imports, filesystem access, network access,
or operating-system code.

For a turn that needs game action:

1. Inspect the supplied current context and briefly state the immediate plan.
2. Emit exactly one fenced `python` block containing one coherent policy.
   Keep it focused on the current objective, but use as many lines as needed
   to express the plan clearly and safely.
3. The harness executes the whole policy and returns its trace plus a fresh
   local observation. Use that real feedback to repair or continue in another
   policy when necessary. A multi-stage build should normally use at most one
   discovery/precheck policy followed by a policy that batches every currently
   feasible acquisition, crafting wait, placement, fueling, and verification
   action. Never spend one model turn per prerequisite or per game action.
4. When the requested outcome is verified, answer in normal prose with no code
   block. For conversation that needs no game action, answer in prose directly.
5. A prose-only response ends the turn immediately. Never say that you will
   check documentation, search, inspect, retry, or continue later. Do that work
   now: use native web search when exposed in the current Responses session for external Factorio
   facts, call `harness_help(...)` for this local adapter, or emit the next
   Python policy. Terminal prose is only for a verified result, an explicit
   capability/input blocker, or a question whose answer is genuinely required.
6. Every name listed below, including `wiki`, `harness_help`, and `skill_help`,
   is a Python namespace function, not a provider function tool. Call it only
   inside the fenced Python policy. Never emit a Responses `function_call` for
   a game or local-documentation function. Native `web_search` is separate: the
   provider executes it as a built-in search Item. Never web-search for local
   harness/skill documentation or current game state, and normally use no more
   than two searches before returning to a Python policy.
   Obvious game-action turns start with web search deferred. If live
   `wiki(...)` and local documentation genuinely cannot answer a required
   external fact, respond with exactly `WEB_SEARCH_NEEDED: <specific query>`;
   the bridge will expose native search for the next model turn only. Do not use
   this marker when the missing information is current game state.

Available values and functions:

- `Prototype`, `Resource`, `Position`, and `Direction` are the upstream FLE
  objects. Use `Direction.NORTH`, `Direction.EAST`, `Direction.SOUTH`, or
  `Direction.WEST`, never raw direction integers. East increases x; south
  increases y. A belt travels toward its direction. A mining drill outputs on
  the side selected by its direction, but the exact output tile depends on its
  footprint. For inserters, Factorio's direction points toward the pickup side;
  the drop side is opposite. Always use the returned entity's `drop_position`
  and `pickup_position` instead of guessing offsets.
- The complete upstream agent namespace is available under its original names:
  `nearest`, `get_resource_patch`, `harvest_resource`, `move_to`,
  `nearest_buildable`, `can_place_entity`, `place_entity`,
  `place_entity_next_to`, `connect_entities`, `get_connection_amount`,
  `get_entity`, `get_entities`, `inspect_inventory`, `insert_item`,
  `extract_item`, `rotate_entity`, `pickup_entity`, `craft_item`,
  `get_prototype_recipe`, `set_entity_recipe`, `get_research_progress`,
  `set_research`, `launch_rocket`, `send_message`, `score`, and `sleep`.
  Preserve their upstream argument order; use `harness_help("upstream-api")`
  when the exact signature is unfamiliar.
- Companion-only extensions remain available in addition to that contract:
  `spawn()`, `follow()`, `stop()`, `status()`, `observe(radius=32)`,
  `find_resource(resource, radius=32)`, `mine_resource(resource, count)`,
  `inspect_entity(entity_or_position)`, `wait(seconds)`, and the documentation
  functions below. `harvest_resource(position, quantity, radius)` is the
  upstream position-based harvesting tool; `mine_resource(resource, count)` is
  the Companion convenience task that can walk to a named ore patch.
- `harness_help(topic=None)` reads the local adapter documentation without a
  game action. Useful topics include `smelting`, `placement`, `directions`,
  `inventory`, `crafting`, and `verification`.
- `skill_help(topic=None)` reads a goal-oriented local playbook without a game
  action. Initial skills are `smelting`, `burner-mining-line`, and
  `basic-logistics`; each gives preconditions, actions, verification, and
  recovery guidance.
- `wiki(subject)` reads the current save's live item, entity, and recipe
  prototypes. Use it before guessing an unfamiliar recipe or after a craft
  failure; for example `wiki(Prototype.IronPlate)`. This local result reflects
  active mods and is stronger evidence than a generic webpage. Use native web
  search only when `wiki(...)`, `harness_help(...)`, and `skill_help(...)` do
  not cover a broader external game mechanic or strategy question.

There is no separate smelt action. To smelt naturally, place a furnace, insert
fuel such as coal, insert the smeltable ingredient such as iron ore into the
same furnace, wait for game ticks, then inspect its inventory. `insert_item`
routes fuel to a burner's fuel inventory and other accepted items to the
entity's normal input. Verify that the output item count grows before relying
on it for crafting.

Build incrementally but use one policy for a coherent multi-action step. Call
`can_place_entity` before destructive placement, retain returned entity objects,
and print useful checks. If placement fails, inspect the reported blockers;
AIRI's own character may occupy the requested tile, so move away before retrying.
Do not claim an automated line works merely because placement calls succeeded.
Wait and verify that the intended output inventory or item count actually grows.
If the current user message contains a preloaded local task skill, reuse it;
do not spend another policy calling `skill_help` for the same playbook. Reading
documentation or repeating observations is not progress once the needed facts
are already present. If work remains, the next policy must perform the largest
safe coherent block of real work that the known state permits.
""".strip()


class CompanionCommandError(RuntimeError):
    """A Factorio command reached the mod but did not complete successfully."""


class PolicyValidationError(ValueError):
    """A generated policy is unsafe or outside the companion execution budget."""


class PolicyCancelledError(RuntimeError):
    """The player cancelled the policy while it was running."""


_HARNESS_HELP_TOPICS = {
    "upstream-api": (
        "Upstream FLE signatures: nearest(type); get_resource_patch(resource, "
        "position, radius=30); harvest_resource(position, quantity=1, radius=10); "
        "move_to(position, laying=None, leading=None); nearest_buildable(entity, "
        "BuildingBox(height, width), center_position); can_place_entity(entity, "
        "direction=Direction.NORTH, position=Position(0,0)); place_entity(entity, "
        "direction=Direction.NORTH, position=Position(0,0), exact=True); "
        "place_entity_next_to(entity, reference_position=Position(0,0), "
        "direction=Direction.RIGHT, spacing=0); connect_entities(*waypoints, "
        "connection_type=Prototype.Pipe, dry_run=False); "
        "get_connection_amount(source, target, connection_type=Prototype.Pipe); "
        "get_entity(entity, position); get_entities(entities=set(), position=None, "
        "radius=1000); inspect_inventory(entity=None, all_players=False); "
        "insert_item(item, target, quantity=5); extract_item(item, source, "
        "quantity=5); rotate_entity(entity, direction); pickup_entity(entity); "
        "craft_item(item, quantity=1); get_prototype_recipe(prototype); "
        "set_entity_recipe(entity, prototype); get_research_progress(technology=None); "
        "set_research(technology); launch_rocket(silo); send_message(message, "
        "recipient=None, metadata=None); score(); sleep(seconds)."
        " Compatibility limits: get_entities uses the upstream 1000-tile "
        "square search but raises instead of silently truncating when more than "
        "96 entities match, so narrow the prototype, position, or radius. "
        "connect_entities supports surface belts, pipes, electric poles, and "
        "walls; include a surface variant when offering underground prototypes. "
        "The Companion connector does not reproduce upstream underground-route "
        "optimisation or every multi-fluid endpoint heuristic."
    ),
    "smelting": (
        "Smelting is natural Factorio simulation, not craft_item(). Place a "
        "furnace, insert fuel (for example Prototype.Coal), insert ore into the "
        "same furnace, wait for game ticks, then inspect_inventory(furnace). "
        "Only use the resulting plates after their count actually increases."
    ),
    "placement": (
        "Use can_place_entity before place_entity. Keep the returned entity and "
        "its exact position/bounding_box. If placement fails, read blockers or "
        "observe nearby entities, move AIRI away when she is the blocker, and "
        "retry at a verified free position."
    ),
    "directions": (
        "Use Direction.NORTH/EAST/SOUTH/WEST, never raw integers. Belts travel "
        "toward their direction. Use returned pickup_position/drop_position for "
        "inserters and mining drills instead of guessing offsets."
    ),
    "inventory": (
        "inspect_inventory() reads AIRI's backpack; inspect_inventory(entity) "
        "reads an entity. insert_item(item, entity, quantity) transfers only the "
        "accepted amount and returns the refreshed target entity. A successful "
        "pickup_entity(entity) returns the entity item and its recoverable "
        "contents to AIRI's backpack, so a temporary furnace can be reclaimed "
        "without repeatedly guessing how to extract its result inventory."
    ),
    "crafting": (
        "craft_item(item, quantity) follows the upstream blocking contract: it "
        "queues the required number of recipe crafts, waits for normal game ticks, "
        "verifies that at least quantity output items reached AIRI's backpack, and "
        "returns that completed item count. A failure means the recipe is locked, "
        "cannot be hand-crafted, lacks ingredients, or did not complete inside the "
        "bounded wait; inspect inventory or the live recipe before retrying."
    ),
    "verification": (
        "Placement is not production. Record the intended output inventory or "
        "belt contents, wait for real game ticks, inspect again, and require the "
        "target count to increase before reporting completion."
    ),
}


_SKILL_HELP_TOPICS = {
    "smelting": (
        "Goal: turn ore into plates through real furnace simulation. "
        "PRECHECK: call wiki(output_item) to confirm a recipe whose category is "
        "smelting; inspect AIRI's inventory for a placeable furnace, the recipe "
        "ingredient, and fuel accepted by the furnace burner. ACTION: move within "
        "reach of a verified free tile; can_place_entity then place_entity the "
        "furnace; record inspect_inventory(furnace); insert fuel, then insert ore; "
        "wait for game ticks; inspect again. SUCCESS: the recipe product count in "
        "the furnace inventory is greater than before. RECOVERY: if the furnace is "
        "missing, wiki(furnace) and its recipe before crafting; if fuel or ore is "
        "missing, acquire it; if placement is blocked, read blockers and move or "
        "choose another verified tile; if output does not grow, inspect the entity "
        "status, inputs, burner, and recipe category before changing anything. "
        "Never use craft_item for a smelting-category recipe and never accept "
        "placement or insertion alone as completion."
    ),
    "burner-mining-line": (
        "Goal: make a burner drill deliver mined items through logistics into a "
        "container. TURN BUDGET: normally use one precheck/material policy and one "
        "placement/verification policy, not one model turn per component. PRECHECK: "
        "locate the requested resource; inspect inventory once for a "
        "burner-mining-drill, fuel, two belts, a burner-inserter, and a chest; query "
        "wiki only for an actually unknown or failed recipe. Prefer a wooden chest "
        "for a minimal early-game line unless a suitable chest is already present. "
        "MATERIALS: acquire and smelt missing inputs in one coherent policy; a "
        "successful pickup_entity on a temporary furnace recovers its products. "
        "Use dependent craft_item calls in the same policy and retain their completed "
        "counts; inspect_inventory() before placement when exact remaining stock "
        "matters. "
        "ACTION: choose a drill position on the resource, preflight every footprint, "
        "place the drill, and use its returned drop_position instead of guessing; "
        "build belts toward the chest; orient the inserter using its returned "
        "pickup_position/drop_position; fuel every burner that needs fuel. SUCCESS: "
        "record chest inventory, wait long enough for mining and transport, then "
        "require the requested item count in the chest to increase. RECOVERY: "
        "inspect the first stage where items stop moving (drill output, belt, "
        "inserter, chest), then correct only that stage."
    ),
    "basic-logistics": (
        "Goal: transfer items deterministically between entities. Belts travel "
        "toward their Direction. Factorio inserter Direction points toward its "
        "pickup side and its drop side is opposite, so retain the placed entity and "
        "read pickup_position/drop_position. Build and verify one boundary at a "
        "time: source-to-belt, belt-to-inserter pickup, inserter drop-to-target. "
        "Record the destination inventory or belt contents before waiting, then "
        "require the intended item count to grow. If it does not, inspect both sides "
        "of the failed boundary before rotating, moving, or rebuilding anything."
    ),
}


def _harness_help(topic: str | None = None) -> str:
    if topic is None or not str(topic).strip():
        topics = ", ".join(sorted(_HARNESS_HELP_TOPICS))
        return f"Available harness documentation topics: {topics}"
    normalized = str(topic).strip().lower().replace("_", "-")
    aliases = {
        "api": "upstream-api",
        "tools": "upstream-api",
        "upstream": "upstream-api",
        "furnace": "smelting",
        "smelt": "smelting",
        "fuel": "smelting",
        "place": "placement",
        "direction": "directions",
        "inserter": "directions",
        "items": "inventory",
        "verify": "verification",
    }
    normalized = aliases.get(normalized, normalized)
    documentation = _HARNESS_HELP_TOPICS.get(normalized)
    if documentation is None:
        topics = ", ".join(sorted(_HARNESS_HELP_TOPICS))
        return f"Unknown harness topic {topic!r}. Available topics: {topics}"
    return documentation


def _skill_help(topic: str | None = None) -> str:
    if topic is None or not str(topic).strip():
        topics = ", ".join(sorted(_SKILL_HELP_TOPICS))
        return f"Available task skills: {topics}"
    normalized = str(topic).strip().lower().replace("_", "-")
    aliases = {
        "furnace": "smelting",
        "smelt": "smelting",
        "mining-line": "burner-mining-line",
        "coal-line": "burner-mining-line",
        "belts": "basic-logistics",
        "inserter": "basic-logistics",
        "logistics": "basic-logistics",
    }
    normalized = aliases.get(normalized, normalized)
    documentation = _SKILL_HELP_TOPICS.get(normalized)
    if documentation is None:
        topics = ", ".join(sorted(_SKILL_HELP_TOPICS))
        return f"Unknown task skill {topic!r}. Available skills: {topics}"
    return documentation


_TASK_SKILL_ROUTES = (
    (
        "smelting",
        re.compile(
            r"(?:熔炼|烧板|炼(?:铁|铜)?板|铁板|铜板|"
            r"\b(?:smelt|smelting|iron plate|copper plate)\b)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "burner-mining-line",
        re.compile(
            r"(?:流水线|生产线|产线|挖煤|挖矿线|采矿线|"
            r"\b(?:mining|production)\s+line\b|burner\s+mining\s+drill)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "basic-logistics",
        re.compile(
            r"(?:传送带|机械臂|物流|运输|"
            r"\b(?:belt|inserter|logistics)\b)",
            flags=re.IGNORECASE,
        ),
    ),
)


def task_skill_for_message(text: str) -> tuple[str, str] | None:
    """Select one existing local playbook to preload for an obvious task."""

    for name, pattern in _TASK_SKILL_ROUTES:
        if pattern.search(str(text or "")) is not None:
            return name, _SKILL_HELP_TOPICS[name]
    return None


@dataclass
class CompanionEntity:
    """Small entity value used by policy code without recreating FLE entities."""

    name: str
    position: Position
    type: str | None = None
    direction: Direction | None = None
    direction_name: str | None = None
    status: str | int | None = None
    inventory: Inventory | None = None
    pickup_position: Position | None = None
    drop_position: Position | None = None
    bounding_box: BoundingBox | None = None
    burner: dict[str, Any] | None = None
    transport_lines: dict[str, Any] | None = None
    connections: list[Position] = field(default_factory=list)
    recipe: str | None = None
    filter: str | None = None
    prototype: Any = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def __getattr__(self, key: str) -> Any:
        raw = object.__getattribute__(self, "raw")
        if key in raw:
            return raw[key]
        raise AttributeError(key)

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except (KeyError, AttributeError):
            return default

    def __repr__(self) -> str:
        parts = [f"name={self.name!r}", f"position={self.position!r}"]
        if self.direction is not None:
            parts.append(f"direction={self.direction!r}")
        if self.status is not None:
            parts.append(f"status={self.status!r}")
        if self.inventory is not None and len(self.inventory):
            parts.append(f"inventory={dict(self.inventory.items())!r}")
        if self.pickup_position is not None:
            parts.append(f"pickup_position={self.pickup_position!r}")
        if self.drop_position is not None:
            parts.append(f"drop_position={self.drop_position!r}")
        if self.recipe is not None:
            parts.append(f"recipe={self.recipe!r}")
        if self.filter is not None:
            parts.append(f"filter={self.filter!r}")
        if self.connections:
            parts.append(f"connections={self.connections!r}")
        return f"CompanionEntity({', '.join(parts)})"


@dataclass
class CompanionEntityGroup:
    """Composable result for Companion connection operations.

    Upstream exposes specialised BeltGroup/PipeGroup/ElectricityGroup models.
    Those models require the full benchmark entity schema, while the Companion
    deliberately returns the smaller live entity representation above.  This
    wrapper preserves the fields policies actually compose: name, position,
    member entities, connection type, and the raw transport result.
    """

    name: str
    position: Position
    entities: list[CompanionEntity]
    connection_type: str
    id: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def belts(self) -> list[CompanionEntity]:
        return self.entities if "belt" in self.connection_type else []

    @property
    def pipes(self) -> list[CompanionEntity]:
        return self.entities if "pipe" in self.connection_type else []

    @property
    def poles(self) -> list[CompanionEntity]:
        return self.entities if "pole" in self.connection_type else []

    def __repr__(self) -> str:
        return (
            f"CompanionEntityGroup(name={self.name!r}, "
            f"connection_type={self.connection_type!r}, "
            f"position={self.position!r}, id={self.id}, "
            f"entities=[{len(self.entities)} entities])"
        )


def compose_policy_system_prompt(character_prompt: str) -> str:
    prompt = character_prompt.strip()
    if not prompt:
        return POLICY_HARNESS_INSTRUCTIONS
    return f"{prompt}\n\n{POLICY_HARNESS_INSTRUCTIONS}"


def _usage_count(usage: dict[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int):
            return value
    return 0


def _direct_python_has_effect(text: str) -> bool:
    if not PythonParser.is_valid_python(text):
        return False
    tree = ast.parse(text)
    effect_nodes = (
        ast.Assign,
        ast.AnnAssign,
        ast.AugAssign,
        ast.Call,
        ast.For,
        ast.While,
        ast.If,
        ast.Try,
        ast.FunctionDef,
    )
    return any(isinstance(node, effect_nodes) for node in ast.walk(tree))


def response_contains_policy(text: str) -> bool:
    """Distinguish an action policy from a normal final chat response."""

    if re.search(r"```(?:python|py)?\s*\n", text, flags=re.IGNORECASE):
        return True
    return _direct_python_has_effect(text.strip())


def parse_policy_text(text: str, usage: dict[str, Any] | None = None) -> Policy | None:
    """Feed provider text through upstream ``parse_response`` unchanged."""

    if not response_contains_policy(text):
        return None
    usage = usage or {}
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(
            prompt_tokens=_usage_count(usage, "prompt_tokens", "input_tokens"),
            completion_tokens=_usage_count(
                usage, "completion_tokens", "output_tokens"
            ),
        ),
    )
    return parse_response(response)


def _prototype_name(value: Any) -> str:
    if isinstance(value, str):
        if value:
            return value
        raise ValueError("prototype name cannot be blank")
    if isinstance(value, enum.Enum):
        value = value.value
    elif hasattr(value, "value"):
        value = value.value
    if isinstance(value, tuple):
        value = value[0]
    if isinstance(value, str) and value:
        return value
    raise ValueError(
        f"expected a Prototype, Resource, or prototype name; got {value!r}"
    )


_DIRECTION_NAMES = {
    "north": Direction.NORTH,
    "up": Direction.NORTH,
    "east": Direction.EAST,
    "right": Direction.EAST,
    "south": Direction.SOUTH,
    "down": Direction.SOUTH,
    "west": Direction.WEST,
    "left": Direction.WEST,
}
_CANONICAL_DIRECTION_NAMES = {0: "north", 4: "east", 8: "south", 12: "west"}

_GROUP_COMPONENT_NAMES = {
    "belt-group": {
        "transport-belt",
        "fast-transport-belt",
        "express-transport-belt",
        "underground-belt",
        "fast-underground-belt",
        "express-underground-belt",
    },
    "pipe-group": {"pipe", "pipe-to-ground"},
    "electricity-group": {
        "small-electric-pole",
        "medium-electric-pole",
        "big-electric-pole",
    },
    "wall-group": {"stone-wall"},
}


def _direction(value: Any) -> tuple[Direction, str]:
    if isinstance(value, str):
        parsed = _DIRECTION_NAMES.get(value.strip().lower())
        if parsed is None:
            raise ValueError("direction must be north, east, south, or west")
        value = parsed
    elif not isinstance(value, enum.Enum):
        raise ValueError(
            "direction must use Direction.NORTH/EAST/SOUTH/WEST, not a raw integer"
        )
    if isinstance(value, enum.Enum):
        value = value.value
    try:
        parsed = Direction(int(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "direction must be Direction.NORTH/EAST/SOUTH/WEST"
        ) from exc
    if parsed.value not in {0, 4, 8, 12}:
        raise ValueError("only cardinal Factorio directions are supported")
    return parsed, _CANONICAL_DIRECTION_NAMES[parsed.value]


def _position(value: Any) -> Position:
    if isinstance(value, Position):
        return value
    if isinstance(value, CompanionEntity):
        return value.position
    if isinstance(value, dict) and "x" in value and "y" in value:
        return Position(x=float(value["x"]), y=float(value["y"]))
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return Position(x=float(value[0]), y=float(value[1]))
    if hasattr(value, "position"):
        return _position(value.position)
    raise ValueError(f"expected Position or entity, got {type(value).__name__}")


def _inventory(value: Any) -> Inventory:
    if isinstance(value, Inventory):
        return value
    if not isinstance(value, dict):
        return Inventory()
    return Inventory(**{str(name): int(count) for name, count in value.items()})


def _bounding_box(value: Any) -> BoundingBox:
    if isinstance(value, BoundingBox):
        return value
    if not isinstance(value, dict):
        raise CompanionCommandError(f"invalid bounding box result: {value!r}")
    left_top = _position(value.get("left_top"))
    right_bottom = _position(value.get("right_bottom"))
    return BoundingBox(
        left_top=left_top,
        right_bottom=right_bottom,
        left_bottom=Position(x=left_top.x, y=right_bottom.y),
        right_top=Position(x=right_bottom.x, y=left_top.y),
    )


def _ingredients(value: Any) -> list[Ingredient]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CompanionCommandError(f"invalid ingredient result: {value!r}")
    ingredients: list[Ingredient] = []
    for item in value:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"]).strip('"')
        count = item.get("count", item.get("amount", 1))
        ingredients.append(
            Ingredient(
                name=name,
                count=int(count if count is not None else 1),
                type=item.get("type"),
            )
        )
    return ingredients


def _products(value: Any) -> list[Product]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CompanionCommandError(f"invalid product result: {value!r}")
    products: list[Product] = []
    for item in value:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        count = item.get("count", item.get("amount"))
        if count is None:
            count = item.get("amount_min", 1)
        products.append(
            Product(
                name=str(item["name"]).strip('"'),
                count=int(count if count is not None else 1),
                probability=float(item.get("probability", 1) or 1),
                type=item.get("type"),
            )
        )
    return products


def _entity(value: dict[str, Any]) -> CompanionEntity:
    position = _position(value.get("position") or value)
    direction_value = value.get("direction")
    parsed_direction: Direction | None = None
    if direction_value is not None:
        try:
            parsed_direction = Direction(int(direction_value))
        except (TypeError, ValueError):
            parsed_direction = None
    inventories = value.get("inventories")
    inventory_value = value.get("inventory")
    if inventory_value is None and isinstance(inventories, dict):
        merged: dict[str, int] = {}
        for contents in inventories.values():
            if isinstance(contents, dict):
                for name, count in contents.items():
                    merged[str(name)] = merged.get(str(name), 0) + int(count)
        inventory_value = merged
    return CompanionEntity(
        name=str(value.get("name") or value.get("entity") or "unknown"),
        position=position,
        type=value.get("type"),
        direction=parsed_direction,
        direction_name=value.get("direction_name"),
        status=value.get("status_name", value.get("status")),
        inventory=_inventory(inventory_value) if inventory_value is not None else None,
        pickup_position=(
            _position(value["pickup_position"])
            if value.get("pickup_position") is not None
            else None
        ),
        drop_position=(
            _position(value["drop_position"])
            if value.get("drop_position") is not None
            else None
        ),
        bounding_box=(
            _bounding_box(value["bounding_box"])
            if value.get("bounding_box") is not None
            else None
        ),
        burner=value.get("burner"),
        transport_lines=value.get("transport_lines"),
        connections=[
            _position(position)
            for position in value.get("connections", [])
            if isinstance(position, dict)
        ],
        recipe=value.get("recipe"),
        filter=value.get("filter"),
        prototype=prototype_by_name.get(
            str(value.get("name") or value.get("entity") or "").replace("_", "-")
        ),
        raw=dict(value),
    )


_BLOCKED_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.Global,
    ast.Nonlocal,
    ast.ClassDef,
    ast.AsyncFunctionDef,
    ast.Await,
    ast.Raise,
    ast.Yield,
    ast.YieldFrom,
)
_BLOCKED_NAMES = {
    "agent_id",
    "agent_index",
    "bytearray",
    "breakpoint",
    "bytes",
    "capture_whole_output",
    "compile",
    "delattr",
    "essential_builtins",
    "eval",
    "eval_with_timeout",
    "exec",
    "execute_body",
    "execute_node",
    "execution_trace",
    "getattr",
    "get_functions",
    "get_messages",
    "globals",
    "help",
    "input",
    "instance",
    "load",
    "load_messages",
    "locals",
    "log",
    "logging_results",
    "loop_context",
    "max_sequential_exception_count",
    "memoryview",
    "open",
    "persistent_vars",
    "pow",
    "prototype_by_name",
    "reset",
    "setattr",
    "tcp_port",
    "vars",
    "__import__",
}


def validate_policy(
    code: str,
    *,
    max_chars: int = 20_000,
    max_nodes: int = 1200,
    max_numeric_literal: float = 100_000,
) -> None:
    if len(code) > max_chars:
        raise PolicyValidationError(
            f"policy has {len(code)} characters; the limit is {max_chars}"
        )
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise PolicyValidationError(f"invalid Python policy: {exc}") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > max_nodes:
        raise PolicyValidationError("policy AST exceeds the execution budget")
    for node in nodes:
        if isinstance(node, _BLOCKED_NODES):
            raise PolicyValidationError(
                f"{type(node).__name__} is not allowed in a companion policy"
            )
        if isinstance(node, ast.Name) and (
            node.id in _BLOCKED_NAMES or node.id.startswith("__")
        ):
            raise PolicyValidationError(f"name {node.id!r} is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise PolicyValidationError("private attributes are not allowed")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            raise PolicyValidationError("exponentiation is not allowed in a policy")
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, (int, float, complex)) and not isinstance(
                value, bool
            ):
                if not math.isfinite(abs(value)) or abs(value) > max_numeric_literal:
                    raise PolicyValidationError(
                        "numeric literal exceeds the policy execution budget"
                    )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _BLOCKED_NAMES:
                raise PolicyValidationError(f"call to {node.func.id!r} is not allowed")


class CompanionFactorioNamespace(FactorioNamespace):
    """FLE's persistent Python namespace backed by AIRI UDP commands."""

    def __init__(
        self,
        command_runner: CommandRunner,
        *,
        tcp_port: int = 0,
        max_actions_per_policy: int = 64,
        cancellable_command_runner: CancellableCommandRunner | None = None,
    ) -> None:
        instance = SimpleNamespace(tcp_port=tcp_port)
        super().__init__(instance, agent_index=0)
        self._command_runner = command_runner
        self._cancellable_command_runner = cancellable_command_runner
        self._max_actions_per_policy = max_actions_per_policy
        self._actions_this_policy = 0
        self._action_names_this_policy: list[str] = []
        self._cancel_requested: Callable[[], bool] | None = None
        self._cancel_wait: Callable[[float], bool] | None = None
        self.capture_whole_output = True

        # These names intentionally match the upstream policy API.
        self.spawn = self._spawn
        self.follow = self._follow
        self.stop = self._stop
        self.status = self._status
        self.observe = self._observe
        self.find_resource = self._find_resource
        self.nearest = self._nearest
        self.get_resource_patch = self._get_resource_patch
        self.harvest_resource = self._harvest_resource
        self.move_to = self._move_to
        self.mine_resource = self._mine_resource
        self.craft_item = self._craft_item
        self.get_prototype_recipe = self._get_prototype_recipe
        self.can_place_entity = self._can_place_entity
        self.place_entity = self._place_entity
        self.place_entity_next_to = self._place_entity_next_to
        self.nearest_buildable = self._nearest_buildable
        self.connect_entities = self._connect_entities
        self.get_connection_amount = self._get_connection_amount
        self.get_entity = self._get_entity
        self.get_entities = self._get_entities
        self.inspect_entity = self._inspect_entity
        self.inspect_inventory = self._inspect_inventory
        self.insert_item = self._insert_item
        self.extract_item = self._extract_item
        self.rotate_entity = self._rotate_entity
        self.pickup_entity = self._pickup_entity
        self.set_entity_recipe = self._set_entity_recipe
        self.get_research_progress = self._get_research_progress
        self.set_research = self._set_research
        self.launch_rocket = self._launch_rocket
        self.send_message = self._send_message
        self.print = self._print
        self.persistent_vars["print"] = self._print
        self.wait = self._wait
        self.sleep = self._sleep
        self.harness_help = _harness_help
        self.skill_help = _skill_help
        self.wiki = self._wiki
        self.factorio_wiki = self._wiki
        self.CompanionEntity = CompanionEntity
        self.CompanionEntityGroup = CompanionEntityGroup

        self._static_members = [
            attr
            for attr in dir(self)
            if not callable(getattr(self, attr)) and not attr.startswith("__")
        ]
        self._freeze_protected_names()

    def score(self, *args: Any, **kwargs: Any) -> tuple[int, int]:
        """Companion chat has no benchmark reward, but the namespace expects it."""

        del args, kwargs
        return 0, 0

    def reset_policy_budget(self) -> None:
        self._actions_this_policy = 0
        self._action_names_this_policy = []

    def _policy_action_names(self) -> tuple[str, ...]:
        return tuple(self._action_names_this_policy)

    def _check_cancelled(self) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            raise PolicyCancelledError("policy cancelled by the player")

    def _run(self, action: str, arguments: dict[str, Any], timeout: float = 120) -> Any:
        self._check_cancelled()
        self._actions_this_policy += 1
        if self._actions_this_policy > self._max_actions_per_policy:
            raise PolicyValidationError(
                f"policy exceeded {self._max_actions_per_policy} Factorio actions"
            )
        self._action_names_this_policy.append(action)
        if (
            self._cancellable_command_runner is not None
            and self._cancel_requested is not None
        ):
            result = self._cancellable_command_runner(
                action,
                arguments,
                timeout,
                self._cancel_requested,
            )
        else:
            result = self._command_runner(action, arguments, timeout)
        self._check_cancelled()
        return result

    def evaluate(
        self,
        code: str,
        *,
        timeout: float = 120,
        max_executed_lines: int = 20_000,
        cancel_requested: Callable[[], bool] | None = None,
        cancel_wait: Callable[[float], bool] | None = None,
    ) -> str:
        """Validate and execute a policy using the upstream AST namespace."""

        self.reset_policy_budget()
        validate_policy(code)
        self._cancel_requested = cancel_requested
        self._cancel_wait = cancel_wait
        self._check_cancelled()
        deadline = time.monotonic() + timeout
        executed_lines = 0
        previous_trace = sys.gettrace()

        def trace(frame, event, arg):
            nonlocal executed_lines
            if event == "line" and frame.f_code.co_filename == "file":
                executed_lines += 1
                self._check_cancelled()
                if executed_lines > max_executed_lines:
                    raise TimeoutError("policy exceeded its Python line budget")
                if time.monotonic() > deadline:
                    raise TimeoutError("policy exceeded its execution-time budget")
            return trace

        try:
            sys.settrace(trace)
            _, _, output = super().eval_with_timeout(code)
            self._check_cancelled()
            return output
        finally:
            sys.settrace(previous_trace)
            self._cancel_requested = None
            self._cancel_wait = None

    def _spawn(self) -> dict[str, Any]:
        return self._run("spawn", {})

    def _follow(self) -> dict[str, Any]:
        return self._run("follow", {})

    def _stop(self) -> dict[str, Any]:
        return self._run("stop", {})

    def _status(self) -> dict[str, Any]:
        result = self._run("status", {})
        self._update_player_location(result)
        return result

    def _observe(self, radius: float = 32) -> dict[str, Any]:
        result = self._run("observe", {"radius": float(radius)})
        self._update_player_location(result)
        return result

    def _find_resource(self, resource: Any, radius: float = 32) -> dict[str, Any]:
        return self._run(
            "find_resource",
            {"resource": _prototype_name(resource), "radius": float(radius)},
        )

    def _nearest(self, type: Any) -> Position:
        result = self._run(
            "nearest",
            {"name": _prototype_name(type), "radius": 500.0},
        )
        if isinstance(result, dict):
            nearest = result.get("position", result)
            if isinstance(nearest, dict) and "x" in nearest and "y" in nearest:
                return _position(nearest)
        raise CompanionCommandError(f"invalid nearest result: {result!r}")

    def _get_resource_patch(
        self,
        resource: Any,
        position: Any,
        radius: int = 30,
    ) -> ResourcePatch:
        target = _position(position)
        name = _prototype_name(resource)
        result = self._run(
            "get_resource_patch",
            {
                "resource": name,
                "x": target.x,
                "y": target.y,
                "radius": int(radius),
            },
        )
        if not isinstance(result, dict):
            raise CompanionCommandError(f"invalid resource patch result: {result!r}")
        return ResourcePatch(
            name=str(result.get("name") or name),
            size=int(result.get("size", 0)),
            bounding_box=_bounding_box(result.get("bounding_box")),
        )

    def _harvest_resource(
        self,
        position: Any,
        quantity: int = 1,
        radius: float = 10,
    ) -> int:
        target = _position(position)
        result = self._run(
            "harvest_resource",
            {
                "x": target.x,
                "y": target.y,
                "count": int(quantity),
                "radius": float(radius),
            },
            timeout=600,
        )
        if isinstance(result, dict):
            return int(result.get("harvested", result.get("count", 0)))
        return int(result)

    def _move_to(
        self,
        position: Any,
        laying: Any = None,
        leading: Any = None,
    ) -> Position:
        if laying is not None and leading is not None:
            raise ValueError("move_to accepts laying or leading, not both")
        target = _position(position)
        if laying is not None or leading is not None:
            status = self._status()
            character = status.get("character") if isinstance(status, dict) else None
            current = (
                _position(character["position"])
                if isinstance(character, dict) and character.get("position")
                else self.player_location
            )
            if laying is not None:
                self._connect_entities(current, target, laying)
            else:
                self._connect_entities(target, current, leading)
        result = self._run("move_to", {"x": target.x, "y": target.y}, timeout=600)
        final = result.get("position") if isinstance(result, dict) else None
        if not final and isinstance(result, dict) and "x" in result:
            final = result
        position_result = _position(final or target)
        self.player_location = position_result
        return position_result

    def _mine_resource(self, resource: Any, count: int = 1) -> int:
        result = self._run(
            "mine_resource",
            {"resource": _prototype_name(resource), "count": int(count)},
            timeout=600,
        )
        if isinstance(result, dict):
            return int(result.get("collected", result.get("count", 0)))
        return int(result)

    def _craft_item(self, entity: Any, quantity: int = 1) -> int:
        requested = int(quantity)
        if requested < 1:
            raise ValueError("craft_item quantity must be at least 1")
        name = _prototype_name(entity)
        before = self._inspect_inventory()
        assert isinstance(before, Inventory)
        before_count = int(before[name])
        result = self._run(
            "craft_item",
            {"recipe": name, "count": requested},
        )
        if not isinstance(result, dict):
            completed = int(result)
            if completed < requested:
                raise CompanionCommandError(
                    f"craft_item completed {completed} of {requested} {name}"
                )
            return requested

        queued = int(result.get("queued", 0))
        if queued < 1:
            raise CompanionCommandError(f"craft_item did not queue {name}")
        energy = float(result.get("energy", 0.5) or 0.5)
        wait_budget = min(90.0, max(8.0, (energy * queued * 2.0) + 15.0))
        deadline = time.monotonic() + wait_budget
        poll_interval = 0.25
        last_count = before_count
        while True:
            inventory = self._inspect_inventory()
            assert isinstance(inventory, Inventory)
            last_count = int(inventory[name])
            if last_count - before_count >= requested:
                return requested
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._wait(min(poll_interval, remaining))
            poll_interval = min(2.0, poll_interval * 1.5)
        raise CompanionCommandError(
            f"craft_item queued {name} but only {last_count - before_count} of "
            f"{requested} requested items completed within {wait_budget:.1f}s"
        )

    def _get_prototype_recipe(self, prototype: Any) -> Recipe:
        name = _prototype_name(prototype)
        wiki = self._wiki(name)
        recipe = wiki.get("recipe")
        if not isinstance(recipe, dict):
            producing = wiki.get("recipes_that_produce")
            if isinstance(producing, list):
                recipe = next(
                    (
                        candidate
                        for candidate in producing
                        if isinstance(candidate, dict)
                        and candidate.get("name") == name
                    ),
                    None,
                )
        if not isinstance(recipe, dict):
            raise CompanionCommandError(
                f"current game has no recipe named {name!r}; a raw resource "
                "or RecipeName may have been supplied"
            )
        return Recipe(
            name=str(recipe.get("name") or name),
            ingredients=_ingredients(recipe.get("ingredients")),
            products=_products(recipe.get("products")),
            energy=float(recipe.get("energy", 0) or 0),
            category=recipe.get("category"),
            enabled=bool(
                recipe.get("force_enabled", recipe.get("prototype_enabled", False))
            ),
        )

    def _wiki(self, subject: Any) -> dict[str, Any]:
        result = self._run("wiki", {"query": _prototype_name(subject)})
        if not isinstance(result, dict):
            raise CompanionCommandError(f"invalid wiki result: {result!r}")
        return result

    def _can_place_entity(
        self,
        entity: Any,
        direction: Any = Direction.NORTH,
        position: Any = Position(x=0, y=0),
    ) -> bool:
        name = _prototype_name(entity)
        parsed_direction, direction_name = _direction(direction)
        target = _position(position)
        result = self._run(
            "can_place_entity",
            {
                "item": name,
                "x": target.x,
                "y": target.y,
                "direction": direction_name,
                "direction_value": parsed_direction.value,
            },
        )
        return bool(result.get("can_place") if isinstance(result, dict) else result)

    def _place_entity(
        self,
        entity: Any,
        direction: Any = Direction.NORTH,
        position: Any = Position(x=0, y=0),
        exact: bool = True,
    ) -> CompanionEntity:
        name = _prototype_name(entity)
        parsed_direction, direction_name = _direction(direction)
        target = _position(position)
        result = self._run(
            "place_entity",
            {
                "item": name,
                "x": target.x,
                "y": target.y,
                "direction": direction_name,
                "direction_value": parsed_direction.value,
                "exact": bool(exact),
            },
        )
        if not isinstance(result, dict):
            raise CompanionCommandError(f"invalid place_entity result: {result!r}")
        return _entity(result)

    def _nearest_buildable(
        self,
        entity: Any,
        building_box: BuildingBox,
        center_position: Any,
        **kwargs: Any,
    ) -> BoundingBox:
        if not isinstance(building_box, BuildingBox):
            if isinstance(building_box, dict):
                building_box = BuildingBox(**building_box)
            else:
                raise ValueError("building_box must be a BuildingBox")
        center = _position(center_position)
        result = self._run(
            "nearest_buildable",
            {
                "item": _prototype_name(entity),
                "building_box": {
                    "height": int(building_box.height),
                    "width": int(building_box.width),
                },
                "x": center.x,
                "y": center.y,
                "max_radius": int(kwargs.get("max_radius", 30)),
            },
        )
        if not isinstance(result, dict):
            raise CompanionCommandError(f"invalid nearest_buildable result: {result!r}")
        return _bounding_box(result)

    @staticmethod
    def _axis_extent(box: BoundingBox | None, axis: str) -> float:
        if box is None:
            return 1.0
        if axis == "x":
            return max(1.0, box.width())
        return max(1.0, box.height())

    def _place_entity_next_to(
        self,
        entity: Any,
        reference_position: Any = Position(x=0, y=0),
        direction: Any = Direction.EAST,
        spacing: int = 0,
    ) -> CompanionEntity:
        parsed_direction, _ = _direction(direction)
        reference = _position(reference_position)
        reference_entity = self._inspect_entity(reference)
        reference_width = self._axis_extent(
            reference_entity.bounding_box if reference_entity else None,
            "x",
        )
        reference_height = self._axis_extent(
            reference_entity.bounding_box if reference_entity else None,
            "y",
        )

        name = _prototype_name(entity)
        prototype = self._wiki(name).get("entity")
        if not isinstance(prototype, dict):
            raise CompanionCommandError(f"{name!r} does not place a live entity")
        entity_width = float(prototype.get("tile_width", 1) or 1)
        entity_height = float(prototype.get("tile_height", 1) or 1)
        if parsed_direction in {Direction.EAST, Direction.WEST}:
            entity_width, entity_height = entity_height, entity_width

        offset_x = (reference_width + entity_width) / 2 + int(spacing)
        offset_y = (reference_height + entity_height) / 2 + int(spacing)
        target_x, target_y = reference.x, reference.y
        if parsed_direction == Direction.NORTH:
            target_y -= offset_y
        elif parsed_direction == Direction.EAST:
            target_x += offset_x
        elif parsed_direction == Direction.SOUTH:
            target_y += offset_y
        else:
            target_x -= offset_x

        target = Position(
            x=math.ceil(target_x * 2) / 2,
            y=math.ceil(target_y * 2) / 2,
        )
        return self._place_entity(entity, parsed_direction, target, exact=True)

    @staticmethod
    def _connection_waypoint(value: Any) -> dict[str, Any]:
        position = _position(value)
        result: dict[str, Any] = {"x": position.x, "y": position.y}
        if isinstance(value, CompanionEntity):
            result.update({"name": value.name, "kind": "entity"})
            if value.pickup_position is not None:
                result["pickup_position"] = {
                    "x": value.pickup_position.x,
                    "y": value.pickup_position.y,
                }
            if value.drop_position is not None:
                result["drop_position"] = {
                    "x": value.drop_position.x,
                    "y": value.drop_position.y,
                }
            if value.connections:
                result["connections"] = [
                    {"x": position.x, "y": position.y}
                    for position in value.connections
                ]
        elif isinstance(value, CompanionEntityGroup):
            result.update({"name": value.name, "kind": "group"})
        else:
            result["kind"] = "position"
        return result

    def _connect_entities(self, *args: Any, **kwargs: Any) -> Any:
        waypoints: list[Any] = []
        connection_types: list[Any] = []
        if "source" in kwargs or "target" in kwargs:
            if "source" not in kwargs or "target" not in kwargs:
                raise ValueError("connect_entities requires both source and target")
            waypoints.extend([kwargs["source"], kwargs["target"]])

        requested_type = kwargs.get("connection_type")
        if requested_type is not None:
            if isinstance(requested_type, (set, frozenset, list, tuple)):
                connection_types.extend(requested_type)
            else:
                connection_types.append(requested_type)

        for value in args:
            if isinstance(value, enum.Enum):
                connection_types.append(value)
            elif isinstance(value, (set, frozenset)) and all(
                isinstance(item, enum.Enum) for item in value
            ):
                connection_types.extend(value)
            else:
                waypoints.append(value)

        if len(waypoints) < 2:
            raise ValueError("connect_entities needs at least two waypoints")
        if not connection_types:
            raise ValueError("connect_entities requires a connection Prototype")
        names = list(dict.fromkeys(_prototype_name(item) for item in connection_types))

        dry_run = bool(kwargs.get("dry_run", False))
        result = self._run(
            "connect_entities",
            {
                "waypoints": [self._connection_waypoint(value) for value in waypoints],
                "connection_types": names,
                "dry_run": dry_run,
            },
            timeout=600,
        )
        if not isinstance(result, dict):
            raise CompanionCommandError(f"invalid connect_entities result: {result!r}")
        if dry_run:
            return result
        members = [
            _entity(item)
            for item in result.get("entities", [])
            if isinstance(item, dict)
        ]
        if not members:
            raise CompanionCommandError("connect_entities returned no connected entities")
        center = result.get("position") or members[len(members) // 2].position
        selected_connection_type = str(result.get("connection_type") or names[0])
        return CompanionEntityGroup(
            name=str(result.get("name") or "entity-group"),
            position=_position(center),
            entities=members,
            connection_type=selected_connection_type,
            id=int(result.get("id", 0) or 0),
            raw=result,
        )

    def _get_connection_amount(
        self,
        source: Any,
        target: Any,
        connection_type: Any = Prototype.Pipe,
    ) -> int:
        result = self._connect_entities(
            source,
            target,
            connection_type,
            dry_run=True,
        )
        return int(result.get("number_of_entities_required", 0))

    def _get_entity(self, entity: Any, position: Any) -> CompanionEntity | None:
        result = self._run(
            "inspect_entity",
            {
                "name": _prototype_name(entity),
                "x": _position(position).x,
                "y": _position(position).y,
            },
        )
        if not result or (isinstance(result, dict) and not result.get("found", True)):
            return None
        return _entity(result)

    def _get_entities(
        self,
        entities: Iterable[Any] | Any = set(),
        position: Any = None,
        radius: float = 1000,
    ) -> list[CompanionEntity | CompanionEntityGroup]:
        if entities is None:
            requested_names: list[str] = []
        elif isinstance(entities, (str, enum.Enum)) or hasattr(entities, "value"):
            requested_names = [_prototype_name(entities)]
        else:
            requested_names = [_prototype_name(item) for item in entities]
        explicit_group_names = [
            name for name in requested_names if name in _GROUP_COMPONENT_NAMES
        ]
        names = [
            name for name in requested_names if name not in _GROUP_COMPONENT_NAMES
        ]
        for group_name in explicit_group_names:
            names.extend(sorted(_GROUP_COMPONENT_NAMES[group_name]))
        names = list(dict.fromkeys(names))
        arguments: dict[str, Any] = {
            "names": names,
            "radius": float(radius),
            "upstream_api": True,
        }
        if position is not None:
            center = _position(position)
            arguments.update({"x": center.x, "y": center.y})
        result = self._run("get_entities", arguments)
        if not isinstance(result, list):
            return []
        found = [_entity(item) for item in result if isinstance(item, dict)]
        group_names = list(explicit_group_names)
        if not requested_names:
            group_names = list(_GROUP_COMPONENT_NAMES)
        elif position is not None:
            for group_name, component_names in _GROUP_COMPONENT_NAMES.items():
                if any(name in component_names for name in requested_names):
                    group_names.append(group_name)
        group_names = list(dict.fromkeys(group_names))
        if not group_names:
            return found

        grouped_names = set().union(
            *(_GROUP_COMPONENT_NAMES[group_name] for group_name in group_names)
        )
        ordinary = [
            entity
            for entity in found
            if entity.name not in grouped_names
        ]
        grouped: list[CompanionEntityGroup] = []
        for group_name in group_names:
            components = [
                entity
                for entity in found
                if entity.name in _GROUP_COMPONENT_NAMES[group_name]
            ]
            grouped.extend(self._group_entities(group_name, components))
        return [*ordinary, *grouped]

    @staticmethod
    def _group_entities(
        group_name: str,
        entities: list[CompanionEntity],
    ) -> list[CompanionEntityGroup]:
        if not entities:
            return []
        id_field = {
            "pipe-group": "fluid_system_id",
            "electricity-group": "electrical_id",
        }.get(group_name)
        components: list[list[CompanionEntity]] = []
        if id_field and all(entity.raw.get(id_field) is not None for entity in entities):
            by_id: dict[Any, list[CompanionEntity]] = {}
            for entity in entities:
                by_id.setdefault(entity.raw[id_field], []).append(entity)
            components = list(by_id.values())
        else:
            remaining = list(entities)
            while remaining:
                component = [remaining.pop(0)]
                cursor = 0
                while cursor < len(component):
                    current = component[cursor]
                    cursor += 1
                    neighbours = [
                        candidate
                        for candidate in remaining
                        if math.hypot(
                            candidate.position.x - current.position.x,
                            candidate.position.y - current.position.y,
                        )
                        <= 1.5
                    ]
                    for neighbour in neighbours:
                        remaining.remove(neighbour)
                        component.append(neighbour)
                components.append(component)

        connection_type = {
            "belt-group": "transport-belt",
            "pipe-group": "pipe",
            "electricity-group": "electric-pole",
            "wall-group": "stone-wall",
        }[group_name]
        groups: list[CompanionEntityGroup] = []
        for index, component in enumerate(components, start=1):
            center = Position(
                x=sum(entity.position.x for entity in component) / len(component),
                y=sum(entity.position.y for entity in component) / len(component),
            )
            groups.append(
                CompanionEntityGroup(
                    name=group_name,
                    position=center,
                    entities=component,
                    connection_type=connection_type,
                    id=index,
                    raw={"id": index},
                )
            )
        return groups

    def _inspect_entity(
        self,
        entity_or_position: Any,
        position: Any = None,
    ) -> CompanionEntity | None:
        name: str | None = None
        target_value = position if position is not None else entity_or_position
        if isinstance(entity_or_position, CompanionEntity):
            name = entity_or_position.name
        elif position is not None:
            name = _prototype_name(entity_or_position)
        target = _position(target_value)
        arguments: dict[str, Any] = {"x": target.x, "y": target.y}
        if name:
            arguments["name"] = name
        result = self._run("inspect_entity", arguments)
        if not result or (isinstance(result, dict) and not result.get("found", True)):
            return None
        return _entity(result)

    def _inspect_inventory(
        self,
        entity: Any = None,
        all_players: bool = False,
    ) -> Inventory | list[Inventory]:
        if all_players:
            # Upstream returns one inventory per controlled agent. Companion
            # mode intentionally has exactly one controlled character.
            return [self._inspect_inventory()]
        if entity is None:
            result = self._run("inspect_inventory", {})
        else:
            target = _position(entity)
            arguments = {"x": target.x, "y": target.y}
            if isinstance(entity, CompanionEntity):
                arguments["name"] = entity.name
            result = self._run("inspect_inventory", arguments)
        if isinstance(result, dict) and "contents" in result:
            result = result["contents"]
        return _inventory(result)

    def _insert_item(
        self,
        entity: Any,
        target: Any,
        quantity: int = 5,
    ) -> CompanionEntity:
        target_position = _position(target)
        arguments = {
            "item": _prototype_name(entity),
            "count": int(quantity),
            "x": target_position.x,
            "y": target_position.y,
        }
        if isinstance(target, CompanionEntity):
            arguments["target_name"] = target.name
        result = self._run("insert_item", arguments)
        if not isinstance(result, dict):
            raise CompanionCommandError(f"invalid insert_item result: {result!r}")
        entity_result = (
            result.get("target")
            if isinstance(result.get("target"), dict)
            else result
        )
        return _entity(entity_result)

    def _extract_item(
        self,
        entity: Any,
        source: Any,
        quantity: int = 5,
    ) -> int:
        source_position = _position(source)
        arguments: dict[str, Any] = {
            "item": _prototype_name(entity),
            "count": int(quantity),
            "x": source_position.x,
            "y": source_position.y,
        }
        if isinstance(source, CompanionEntity):
            arguments["source_name"] = source.name
        result = self._run("extract_item", arguments)
        if isinstance(result, dict):
            return int(result.get("extracted", result.get("count", 0)))
        return int(result)

    def _rotate_entity(
        self,
        entity: Any,
        direction: Any = Direction.NORTH,
    ) -> CompanionEntity:
        target = _position(entity)
        parsed_direction, direction_name = _direction(direction)
        arguments = {
            "x": target.x,
            "y": target.y,
            "direction": direction_name,
            "direction_value": parsed_direction.value,
        }
        if isinstance(entity, CompanionEntity):
            arguments["name"] = entity.name
        result = self._run("rotate_entity", arguments)
        if not isinstance(result, dict):
            raise CompanionCommandError(f"invalid rotate_entity result: {result!r}")
        return _entity(result)

    def _pickup_entity(self, entity: Any, position: Any = None) -> bool:
        if isinstance(entity, CompanionEntityGroup):
            if position is not None:
                raise ValueError(
                    "position must be omitted when pickup_entity receives a group"
                )
            for member in list(entity.entities):
                if not self._pickup_entity(member):
                    return False
            return True
        target = _position(position if position is not None else entity)
        arguments = {"x": target.x, "y": target.y}
        if isinstance(entity, CompanionEntity):
            arguments["name"] = entity.name
        elif position is not None:
            arguments["name"] = _prototype_name(entity)
        result = self._run("pickup_entity", arguments)
        return bool(result.get("picked_up") if isinstance(result, dict) else result)

    def _set_entity_recipe(
        self,
        entity: Any,
        prototype: Any,
    ) -> CompanionEntity:
        target = _position(entity)
        arguments: dict[str, Any] = {
            "recipe": _prototype_name(prototype),
            "x": target.x,
            "y": target.y,
        }
        if isinstance(entity, CompanionEntity):
            arguments["target_name"] = entity.name
        result = self._run("set_entity_recipe", arguments)
        if not isinstance(result, dict):
            raise CompanionCommandError(f"invalid set_entity_recipe result: {result!r}")
        return _entity(result)

    def _get_research_progress(self, technology: Any = None) -> list[Ingredient]:
        arguments: dict[str, Any] = {}
        if technology is not None:
            arguments["technology"] = _prototype_name(technology)
        result = self._run("get_research_progress", arguments)
        return _ingredients(result)

    def _set_research(self, technology: Any) -> list[Ingredient]:
        result = self._run(
            "set_research",
            {"technology": _prototype_name(technology)},
        )
        return _ingredients(result)

    def _launch_rocket(self, silo: Any) -> CompanionEntity:
        target = _position(silo)
        result = self._run(
            "launch_rocket",
            {"x": target.x, "y": target.y},
        )
        if not isinstance(result, dict):
            raise CompanionCommandError(f"invalid launch_rocket result: {result!r}")
        return _entity(result)

    @staticmethod
    def _send_message(
        message: str,
        recipient: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Match upstream's single-agent behaviour: accept and intentionally no-op."""

        del recipient, metadata
        if not isinstance(message, str):
            raise ValueError("message must be a string")
        return True

    @staticmethod
    def _print(*args: Any) -> str:
        """Match the upstream print tool when print is used as an expression."""

        return "\t".join(str(value) for value in args).lstrip("\t")

    def _wait(self, seconds: float = 1) -> float:
        duration = max(0.0, min(float(seconds), 30.0))
        self._check_cancelled()
        if self._cancel_wait is not None:
            if self._cancel_wait(duration):
                raise PolicyCancelledError("policy cancelled by the player")
        elif self._cancel_requested is None:
            time.sleep(duration)
        else:
            deadline = time.monotonic() + duration
            while True:
                self._check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
        self._check_cancelled()
        return duration

    def _sleep(self, seconds: int) -> bool:
        """Expose upstream's sleep(seconds) return contract over live game time."""

        self._wait(float(seconds))
        return True

    def _update_player_location(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        character = result.get("character")
        if isinstance(character, dict) and character.get("position"):
            self.player_location = _position(character["position"])
