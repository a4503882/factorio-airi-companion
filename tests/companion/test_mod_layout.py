from __future__ import annotations

import json
from pathlib import Path
import unittest


MOD_ROOT = Path(__file__).resolve().parents[2] / "fle" / "companion" / "factorio_mod"


class FactorioModLayoutTests(unittest.TestCase):
    def test_manifest_targets_factorio_2(self) -> None:
        manifest = json.loads((MOD_ROOT / "info.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "airi-companion")
        self.assertEqual(manifest["factorio_version"], "2.0")
        self.assertIn("base >= 2.0.73", manifest["dependencies"])

    def test_control_requires_all_runtime_modules(self) -> None:
        control = (MOD_ROOT / "control.lua").read_text(encoding="utf-8")
        for module in (
            "state",
            "character",
            "movement",
            "tasks",
            "actions",
            "observation",
            "transport",
            "gui",
        ):
            self.assertIn(f'require("scripts.{module}")', control)

    def test_companion_never_bulk_deletes_characters(self) -> None:
        lua_source = "\n".join(
            path.read_text(encoding="utf-8") for path in MOD_ROOT.rglob("*.lua")
        )
        self.assertNotIn('find_entities_filtered{type = "character"}', lua_source)
        self.assertNotIn('find_entities_filtered({type = "character"})', lua_source)
        self.assertNotIn("game.connected_players[1].character", lua_source)

    def test_companion_has_force_scoped_world_and_map_identity(self) -> None:
        character = (MOD_ROOT / "scripts" / "character.lua").read_text(
            encoding="utf-8"
        )
        settings = (MOD_ROOT / "settings.lua").read_text(encoding="utf-8")

        self.assertIn("force = owner.force", character)
        self.assertIn("character.force = owner.force", character)
        self.assertIn("rendering.draw_text", character)
        self.assertIn("character.force.add_chart_tag", character)
        self.assertIn('name = "airi-companion-display-name"', settings)

    def test_resource_overview_groups_before_sampling(self) -> None:
        observation = (MOD_ROOT / "scripts" / "observation.lua").read_text(
            encoding="utf-8"
        )

        self.assertIn('filter = "type", type = "resource"', observation)
        self.assertIn("count_entities_filtered(filter)", observation)
        self.assertIn("function Observation.find_resource", observation)
        self.assertIn("truncated = truncated", observation)
        self.assertNotIn(
            'type = "resource",\n        limit = 256',
            observation,
        )

    def test_policy_harness_actions_cover_entity_correction_and_inventory(self) -> None:
        actions = (MOD_ROOT / "scripts" / "actions.lua").read_text(encoding="utf-8")
        observation = (MOD_ROOT / "scripts" / "observation.lua").read_text(
            encoding="utf-8"
        )

        for action in (
            "can_place_entity",
            "insert_item",
            "rotate_entity",
            "pickup_entity",
            "inspect_entity",
            "get_entities",
            "wiki",
        ):
            self.assertIn(f'action == "{action}"', actions)
        self.assertIn("direction must be north, east, south, or west", actions)
        self.assertIn("function Observation.placement_blockers", observation)
        self.assertIn("function Observation.entity_inventory", observation)

        wiki = (MOD_ROOT / "scripts" / "wiki.lua").read_text(encoding="utf-8")
        self.assertIn('require("scripts.wiki")', actions)
        self.assertIn("function Wiki.lookup", wiki)
        self.assertIn("prototypes.recipe", wiki)
        self.assertIn("recipe.category", wiki)
        self.assertIn("force_recipe.enabled", wiki)
        self.assertIn("burner_prototype.fuel_categories", wiki)

    def test_upstream_agent_actions_have_a_dedicated_transport_adapter(self) -> None:
        actions = (MOD_ROOT / "scripts" / "actions.lua").read_text(encoding="utf-8")
        upstream = (MOD_ROOT / "scripts" / "upstream.lua").read_text(
            encoding="utf-8"
        )

        self.assertIn('require("scripts.upstream")', actions)
        self.assertIn("Upstream.execute(action, arguments)", actions)
        for action in (
            "nearest",
            "get_resource_patch",
            "harvest_resource",
            "extract_item",
            "get_research_progress",
            "set_research",
            "set_entity_recipe",
            "launch_rocket",
            "nearest_buildable",
            "connect_entities",
        ):
            self.assertIn(f"{action} =", upstream)
        for water_tile in (
            "water",
            "deepwater",
            "water-green",
            "deepwater-green",
            "water-shallow",
            "water-mud",
        ):
            self.assertIn(f'"{water_tile}"', upstream)
        self.assertIn("fluidbox.get_pipe_connections", (
            MOD_ROOT / "scripts" / "observation.lua"
        ).read_text(encoding="utf-8"))
        observation = (MOD_ROOT / "scripts" / "observation.lua").read_text(
            encoding="utf-8"
        )
        self.assertIn("arguments.upstream_api == true", observation)
        self.assertIn("ENTITY_RESULT_LIMIT + 1", observation)
        self.assertIn("narrow the prototype filter", observation)
        self.assertIn("underground-only routing is not implemented", upstream)
        self.assertIn("one network kind", upstream)

    def test_chat_ui_has_rebindable_hotkey_and_hides_plan_text(self) -> None:
        data_stage = (MOD_ROOT / "data.lua").read_text(encoding="utf-8")
        control = (MOD_ROOT / "control.lua").read_text(encoding="utf-8")
        gui = (MOD_ROOT / "scripts" / "gui.lua").read_text(encoding="utf-8")
        state = (MOD_ROOT / "scripts" / "state.lua").read_text(encoding="utf-8")

        self.assertIn('name = "airi-companion-toggle-chat"', data_stage)
        self.assertIn('key_sequence = "G"', data_stage)
        self.assertIn('consuming = "game-only"', data_stage)
        self.assertIn(
            'script.on_event("airi-companion-toggle-chat"',
            control,
        )
        self.assertIn('type = "scroll-pane"', gui)
        self.assertIn('type = "textfield"', gui)
        self.assertIn('name = SEND_NAME', gui)
        self.assertIn('return {"gui.airi-processing"', gui)
        self.assertNotIn("activity.caption = State.ensure().plan", gui)
        self.assertIn("local CHAT_HISTORY_LIMIT = 80", state)
        self.assertIn("State.set_chat_processing(false)", control)

    def test_chat_ui_retries_latest_message_scroll_after_layout(self) -> None:
        control = (MOD_ROOT / "control.lua").read_text(encoding="utf-8")
        gui = (MOD_ROOT / "scripts" / "gui.lua").read_text(encoding="utf-8")

        self.assertIn("history.scroll_to_bottom()", gui)
        self.assertIn("pending_latest_scroll[player.index] = current_tick() + 1", gui)
        self.assertIn("function Gui.tick(tick)", gui)
        self.assertIn("if tick >= due_tick then", gui)
        self.assertIn("Gui.tick(event.tick)", control)

    def test_stop_cancels_the_correlated_agent_turn_and_rejects_late_packets(self) -> None:
        control = (MOD_ROOT / "control.lua").read_text(encoding="utf-8")
        gui = (MOD_ROOT / "scripts" / "gui.lua").read_text(encoding="utf-8")
        state = (MOD_ROOT / "scripts" / "state.lua").read_text(encoding="utf-8")
        zh_locale = (MOD_ROOT / "locale" / "zh-CN" / "locale.cfg").read_text(
            encoding="utf-8"
        )

        self.assertIn('return {kind = "stop"}', gui)
        self.assertIn('Transport.send("cancel_chat"', control)
        self.assertIn("Gui.begin_processing(request_id)", control)
        self.assertIn("packet_matches_active_chat(packet)", control)
        self.assertIn('Transport.ack(packet, "ignored-stale")', control)
        self.assertIn("chat_request_id", state)
        self.assertNotIn('State.set_chat_processing(rendered ~= "")', gui)
        self.assertIn("airi-turn-cancelled=", zh_locale)


if __name__ == "__main__":
    unittest.main()
