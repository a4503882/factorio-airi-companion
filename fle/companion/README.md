# AIRI Companion mode

This adaptation turns the Factorio Learning Environment action model into a
single-client, in-game companion instead of an evaluation cluster that controls
the human player's character.

## Architecture

- `factorio_mod/` is a normal Factorio 2.0 mod. It owns AIRI's unbound
  `character`, in-game GUI, local perception boundary, movement, mining,
  crafting, placement, and save-persistent state.
- `policy_harness.py` reuses the upstream FLE `parse_response`, `Policy`,
  `PythonParser`, and persistent `FactorioNamespace`. Its
  `CompanionFactorioNamespace` is a transport adapter: familiar FLE Python
  functions execute through the localhost UDP bridge instead of an RCON-backed
  evaluation server.
- `bridge.py` owns API credentials, model calls, conversation state, and the
  policy-feedback loop. The model plans by emitting one Python program, the
  upstream parser extracts it, the persistent namespace executes all of its
  actions, and the real output plus a fresh permitted observation are returned
  to the model before it can continue or report completion.
- `launcher.py` installs/enables the mod, starts the bridge without a visible
  helper window on Windows, and launches one graphical Factorio process with
  `--enable-lua-udp`.

No second Factorio client, second Steam account, Docker cluster, or RCON server
is required.

This mode is designed for single-player and for a multiplayer host while the
owning graphical player is online. On a headless dedicated server with no
owner client, the UDP bridge stays inactive. The companion character and local
buttons remain save-safe, but remote model commands require the owner client.

## Quick start

### Research Control Center

The recommended entry point for local experiments is the Tkinter Control
Center:

```powershell
python -m fle.companion.control_center
```

On this Windows checkout, `Start AIRI Control Center.cmd` provides the same
entry point for double-click startup.

The first version provides four tabs:

- **Provider / API** stores named endpoint, model, API mode, reasoning effort,
  and native-search settings. API keys are stored as generic credentials in
  Windows Credential Manager and are never written to profile JSON, command
  lines, the mod, or Factorio saves.
- **System Prompt** edits and duplicates named prompt presets. Every Bridge
  start freezes the selected prompt into a session snapshot with its SHA-256.
- **Launch and status** starts/stops AgentBridge, launches Factorio with
  `--enable-lua-udp`, updates the mod, and reports Bridge/Factorio/Mod state.
- **Research log** displays Bridge output. Each session also writes a manifest,
  prompt snapshot, JSONL event trajectory, Bridge log, and status snapshot to
  `%USERPROFILE%\.airi-factorio\sessions\<session-id>\`.

Set `AIRI_FACTORIO_DATA_DIR` before startup to use another persistent data
directory. The home-relative default avoids Microsoft Store Python's hidden
`LOCALAPPDATA` package redirection and remains stable if the Python runtime is
changed later.

The local JSONL trajectory intentionally contains player messages, permitted
game observations, provider-visible thinking in separate `model_reasoning`
events when the API returns it, final output in `model_response`, extracted
`model_policy` programs, individual `game_command` / `game_result` events, and
combined `policy_result` feedback for research reproducibility. Reasoning is
never mixed into the final reply or shown in the in-game chat. Treat the
session directory as private gameplay data even though it never contains the
API key.

To migrate the existing three-line local configuration, use **Import ds.txt**
in the Provider tab. The source file is read but not changed or deleted. Its API
key is moved into Windows Credential Manager; the non-secret base URL and model
become a normal Provider profile.

Changing a Prompt or Provider does not mutate a running model session. Stop and
start Bridge to create a clean new research session with the newly selected
configuration.

On Control Center startup, the one known pre-policy-harness Dango preset is
migrated to the current bundled prompt only when both its name and exact legacy
SHA-256 match. Any user-edited prompt is preserved, even when it has the same
display name.

### Command-line mode

Install the mod without starting Factorio:

```powershell
python -m fle.companion.launcher --install-only
```

Launch the bridge and the normal graphical Factorio client:

```powershell
python -m fle.companion.launcher
```

The defaults use Factorio UDP port `31500` and bridge port `31501`. The mod's
`AIRI bridge UDP port` setting must match the bridge port.

Without model configuration the bridge runs a dependency-free Chinese/English
command parser. It understands commands such as `跟着我`, `停下`, `去 20,-10`,
and `挖 32 铁矿`.

For an OpenAI-compatible endpoint, configure credentials in the environment so
they never enter the Factorio save or UDP packets. Both Chat Completions and
Responses wire formats are supported:

```powershell
$env:AIRI_FACTORIO_MODEL='your-model-name'
$env:AIRI_FACTORIO_BASE_URL='https://provider.example/v1'
$env:AIRI_FACTORIO_API_KEY='your-local-secret'
$env:AIRI_FACTORIO_API_MODE='chat-completions' # or 'responses'
python -m fle.companion.launcher
```

The launcher can instead read a local three-line provider file containing the
API key, base URL, and model name. The key is read only by AgentBridge and is
never copied into the repository, mod, command line, save, or UDP packets:

```powershell
python -m fle.companion.launcher `
  --provider-config 'F:\ds.txt' `
  --api-mode responses
```

Factorio actions are not exposed as provider function tools in either API
mode. They are Python functions inside the reused FLE namespace. In Responses
mode, the only optional provider tool is native `web_search`. The model is told
to prefer the current game's live prototype Wiki and local harness/task docs,
then use search only for broader external mechanics, versions, strategies, or
mod documentation that those local sources do not cover. Search is never
evidence of game state. Disable it for a session with `--no-web-search`. For stateless Responses-compatible
providers, AgentBridge retains and replays typed response Items locally,
including assistant messages and `web_search_call` Items; policy results return
as explicit environment/user Items rather than fabricated
`function_call_output` Items. It does not rely on `previous_response_id`.

An optional local session token can be set in both places:

```powershell
$env:AIRI_FACTORIO_SESSION_TOKEN='a-random-local-token'
```

Enter the same value in Factorio's `AIRI bridge session token` runtime setting.

The provider, model name, API mode, and API key remain entirely outside the
Factorio save and mod directory. Chat Completions remains available for
providers that do not implement `/responses`.

## In-game use

Press `G` to open the in-game chat window. The key is a normal Factorio custom
input and can be rebound under **Settings > Controls > Mods**. The window keeps
the most recent 80 player/companion/system messages in the save, supports Enter
to send, and shows an elapsed processing indicator while the bridge/model is
working. It intentionally never displays provider reasoning or chain-of-thought;
only user messages, final companion replies, errors, and a generic busy state
are rendered.

The top-screen chat button and console command remain available:

```text
/airi
/airi spawn
/airi follow
/airi stop
/airi status
/airi observe 32
/airi find iron-ore 64
/airi move 20 -10
/airi mine iron-ore 32
/airi craft transport-belt 10
```

Structured perception is intentionally limited to the configured radius. The
general observation reports a per-resource-type overview so one dense patch
cannot hide other resource types. `/airi find` and the model-facing
`find_resource(...)` Python function queries one named resource on demand. Entity
inspection includes real direction, status, bounding box, valid inventories,
burner state, belt contents, and inserter/mining-drill pickup or drop positions
where the engine exposes them. Large resource patches expose exact entity
counts but mark their amount sample as truncated instead of presenting a capped
sample as the total. The companion does not scan the entire generated surface
or delete, replace, or take control of any real player's character.

For a game-action turn, the authoritative prompt asks for one coherent fenced
Python policy without an arbitrary source-line limit. A policy may use
variables, conditions, bounded loops, and multiple coherent actions such as
`nearest(Resource.Coal)`, `place_entity(...)`, `insert_item(...)`, and
`inspect_inventory(...)`. Character count, AST size, numeric literals, host
capabilities, Factorio action count, executed-line count, and wall time remain
bounded. Host imports, file/network/process access, private attributes,
unbounded execution, and raw direction integers are rejected before execution.
The namespace stays persistent between policy steps, while every step receives
genuine execution output and a new local observation.

The local knowledge layer is deliberately split by responsibility:

- `wiki(Prototype.IronPlate)` asks the running save for live item, entity, and
  recipe prototypes, including recipe category/ingredients/products, current
  force enablement, place results, machine crafting categories, and burner fuel
  categories. It therefore follows active mods instead of assuming vanilla
  recipes.
- `harness_help(...)` documents adapter semantics such as placement,
  directions, inventory transfer, natural furnace simulation, and production
  verification.
- `skill_help(...)` retrieves goal-oriented playbooks. The initial smelting,
  burner-mining-line, and basic-logistics skills specify prechecks, actions,
  observable success conditions, and failure recovery.

### Upstream agent API compatibility

`CompanionFactorioNamespace` exposes every tool currently discovered from
upstream `fle/env/tools/agent` under the original public name:

```text
can_place_entity, connect_entities, craft_item, extract_item,
get_connection_amount, get_entities, get_entity, get_prototype_recipe,
get_research_progress, get_resource_patch, harvest_resource, insert_item,
inspect_inventory, launch_rocket, move_to, nearest, nearest_buildable,
pickup_entity, place_entity, place_entity_next_to, print, rotate_entity,
score, send_message, set_entity_recipe, set_research, sleep
```

This is an API-surface bridge, not a claim that every large upstream planning
algorithm has been copied byte-for-byte. Public keyword names, argument order,
ordinary world actions, and the return fields needed to compose later calls are
preserved. Live query results are converted back to the upstream `Position`,
`BoundingBox`, `ResourcePatch`, `Recipe`, `Ingredient`, `Product`, and
`Inventory` models where those are the public contract. Connection results use
`CompanionEntityGroup`: it preserves the upstream group name, position, member
entities, and the `belts` / `pipes` / `poles` accessors without fabricating the
much larger benchmark-only entity schema.

The Companion Mod keeps the transport implementation in
`scripts/upstream.lua`. It includes tile-aware water queries, connected resource
patches, position-based harvesting, extraction, research, recipe/filter
selection, nearest-buildable spiral search, and obstacle-aware connection
preflight/placement. `connect_entities(..., dry_run=True)` powers
`get_connection_amount` without changing the world. It accepts compatible sets
of transport-belt, pipe, pole, and wall connection prototypes, selects an
available surface variant, and rejects mixed network kinds rather than silently
choosing one.

The remaining compatibility limits are explicit:

- `connect_entities` implements a common obstacle-aware surface route. It does
  not yet reproduce upstream's underground-belt/pipe optimisation, sparse
  electric-pole placement, or all multi-fluid endpoint resolvers. An
  underground-only prototype set therefore raises a clear error.
- `place_entity_next_to` preserves upstream size-aware adjacency and direction
  semantics, but not every upstream smart-alternative placement heuristic.
- `get_entities` uses the upstream square search and supports its default
  1000-tile radius instead of inheriting the passive Companion perception
  radius. A single UDP result is bounded to 96 entities; a larger match raises
  an error asking the policy to narrow its prototype, position, or radius. It
  never silently returns a partial list.
- Entity and group wrappers expose the composable public fields used by the
  harness, not every benchmark-only subclass field from upstream's full entity
  model hierarchy.

Three behaviours are deliberate single-agent translations rather than missing
tools:

- `send_message(...)` returns `True` without a side effect, exactly as upstream
  does when `is_multiagent` is false.
- `score()` returns `(0, 0)` because Companion chat has no benchmark reward.
- `inspect_inventory(all_players=True)` returns a one-element list containing
  AIRI's inventory because the Companion controls exactly one agent character.

`spawn`, `follow`, `stop`, `status`, `observe`, `find_resource`,
`mine_resource`, `inspect_entity`, `wait`, `wiki`, `harness_help`, and
`skill_help` remain Companion extensions. They no longer replace similarly
named upstream tools: for example, `harvest_resource(position, quantity,
radius)` is the upstream position-based action, while
`mine_resource(resource, count)` is the walking Companion convenience task.

For obvious smelting, mining-line, and logistics requests, AgentBridge selects
and embeds the matching existing playbook in the first provider request. This
records `task_skill_preloaded` and avoids spending a model round merely asking
for the same skill. Multi-stage work is expected to use at most one discovery
policy before batching currently feasible material acquisition, asynchronous
crafting waits, placement, fueling, and verification. If an action turn emits a
policy containing only documentation or read-only inspection, the trajectory
records `policy_no_progress` and the next provider request receives a strict
batch-and-act correction.

A prose-only provider response is normally terminal. AgentBridge detects the
narrow failure mode where the model instead promises future work (for example,
"I will check the harness docs") and feeds it back as
`model_nonterminal_response`, requiring the model to query/search/act in the
same turn. Completed reports, explicit blockers, and genuinely required
questions remain valid terminal prose.

`wiki`, `harness_help`, `skill_help`, and Factorio actions are namespace
functions inside a fenced Python policy; they are not provider function tools.
If a Responses-compatible model nevertheless emits a `function_call`, the
Bridge records its name and bounded arguments as
`model_function_call_rejected`, returns a protocol-valid
`function_call_output` explaining the required policy format, and forces the
same task to continue instead of failing or treating the accompanying prose as
final. Native `web_search_call` Items remain supported, but the prompt forbids
using web search as a substitute for local documentation or live game state.

Transient provider transport failures (`URLError` or timeout) are retried twice
with short bounded delays and recorded as `model_request_retry`. Deterministic
HTTP failures such as invalid credentials and malformed JSON responses are not
retried, so configuration errors remain visible immediately.

Responses search is local-first rather than permanently exposed. Obvious
game-action messages such as build, mine, smelt, continue, or retry record
`web_search_deferred` and begin without a web tool, which prevents a search
agent from repeatedly trying to find local harness functions online. If a
local policy proves that a genuinely external fact is required, the model emits
`WEB_SEARCH_NEEDED: <specific query>`; AgentBridge records
`model_web_search_requested` and exposes native search for the next provider
response only. A search response that merely promises to keep reading local
docs is nonterminal, disables further search for that task, and must return to
the Python policy loop.

## Verification and manual acceptance

The automated suite covers protocol validation, command parsing, safe mod
installation, Control Center profile/session handling, credential separation,
provider probes, research event logging, and source-layout invariants:

```powershell
python -m unittest discover -s tests\companion -v
```

`tests/companion/factorio_harness_mod/` is a test-only Factorio mod used to
verify against the real engine that AIRI has an independent character, returns
a JSON-safe local observation, creates a path request, crafts and mines,
preflights placement, inserts inventory, inspects/rotates/picks up entities, and
reads live Wiki facts proving that iron plates use the `smelting` category and
that a stone furnace accepts that category and chemical fuel. It also builds a
working coal line. The line is not accepted merely because its pieces
were placed: the test fuels a burner drill and inserter, waits 900 ticks, and
requires coal in the destination chest to increase. Run it with:

```powershell
python tests\companion\factorio_engine_smoke.py `
  --factorio 'C:\path\to\Factorio\bin\x64\factorio.exe' `
  --save "$env:APPDATA\Factorio\saves\your-save.zip"
```

The supplied save is copied into an isolated temporary directory before the
test mod makes any changes.

The policy smoke test verifies the missing integration layer itself: a
deterministic local provider emits an upstream-style Python policy, the real
parser and persistent namespace execute it through the real UDP bridge, actual
Factorio results are returned as policy feedback, and only then does the stub
produce a final reply. It makes zero paid model requests. Normal single-player
mode is intentional because Factorio benchmark mode does not consume external
UDP commands:

```powershell
python tests\companion\factorio_policy_smoke.py `
  --factorio 'C:\path\to\Factorio\bin\x64\factorio.exe' `
  --save "$env:APPDATA\Factorio\saves\your-save.zip"
```

This test also uses temporary copies of the save, mod directory, and Factorio
configuration. It briefly starts a normal game client, does not automate the
UI, and terminates only the exact process that it created after the protocol
acceptance condition is met.

For manual Control Center acceptance:

1. Run `python -m fle.companion.control_center`.
2. In **Provider / API**, import the existing three-line `ds.txt`; verify that
   the credential status changes to saved and that **Test API** succeeds.
3. Duplicate the default Prompt, edit it, save it, and confirm that its SHA-256
   and selection entry change.
4. Click **Start all**, load a save, and wait for Bridge, Factorio, and Mod to
   report running/connected.
5. Send one request that requires two or more game actions, then open the
   session directory. Verify that the manifest, prompt snapshot, `events.jsonl`,
   Bridge log, and status file exist, and that the trajectory contains
   `model_reasoning` (when returned by the provider), `model_response`,
   `model_policy`, multiple matching
   `game_command` / `game_result` pairs, `policy_result`, and finally
   `assistant_message`.
6. Search those artifacts for the API key; there must be no match.

For in-game acceptance, open the `AIRI` button and check these in order:

1. Press `G` and confirm the chat opens at the newest saved message, focuses its
   input field, sends with Enter, and closes again with `G` or Escape. While
   waiting for the model, the window must show an animated generic processing
   status and elapsed seconds,
   never provider reasoning or chain-of-thought. Player messages and final
   companion replies should remain visible after closing and reopening it.
2. AIRI appears as a second blue character without replacing the player. Her
   configured display name appears above the body, and a same-force marker with
   that name follows her position on the map.
3. `跟着我` makes AIRI walk after the player, and `停下` cancels it.
4. `观察` returns nearby state, while `挖 5 铁矿` makes AIRI use her own body
   and inventory.
5. Give AIRI one burner mining drill, several transport belts, one burner
   inserter, one wooden chest, and enough coal. Stand near a clear coal patch
   and ask in natural language for a small automatic line that mines coal into
   the chest. Do not manually correct the build while it is running.
6. Confirm that AIRI plans with Python policies, uses placement/inspection
   feedback to correct geometry when necessary, fuels the burner entities,
   waits for production, and reports completion only after the chest's coal
   count genuinely increases. The early trajectory for this request should
   include `task_skill_preloaded`. It may use one precheck/material
   policy, but must not spend repeated provider turns rereading documentation or
   queue one component per turn; any read-only detour should record
   `policy_no_progress` and be corrected on the next response. Placement success
   by itself is a failed acceptance result.
7. Give AIRI iron ore, coal, and a stone furnace, then ask for iron plates. She
   should query the live Wiki or smelting skill, use the furnace rather than
   `craft_item(Prototype.IronPlate, ...)`, and require the furnace's iron-plate
   count to increase. A reply that only says she will read documentation is a
   failed acceptance result; if the model attempts it, `events.jsonl` should
   contain `model_nonterminal_response` and the same turn must continue.
8. If the provider emits `function_call` for `wiki`, the chat must not show a
   model-request failure. The trajectory should contain
   `model_function_call_rejected`, followed by a new `model_response` and a
   fenced `model_policy` in the same task.
9. For an obvious game-action request, confirm the first provider call records
   `web_search_deferred` and does not spend search calls on `wiki`,
   `harness_help`, or `skill_help`. A genuinely external lookup may proceed only
   after `model_web_search_requested` with a specific query.
10. Inspect that session's `events.jsonl`: every claimed action must have a real
   `game_result`, and the verifying inventory observation must appear in a
   `policy_result` before the final `assistant_message`.
11. Save, quit, and reload; AIRI's body, chat history, and task state should
   restore without a second Factorio process.

The older optional `tests/companion/factorio_udp_smoke.py` script directly
exercises `status`, `observe`, and `move_to` through the UDP protocol without
the provider/parser/namespace layer. Prefer `factorio_policy_smoke.py` for the
complete harness path. Run either only when no other bridge or Factorio process
is using its selected ports.
