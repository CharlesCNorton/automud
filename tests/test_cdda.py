"""Unit tests for the local Cataclysm backend (automud_cdda): mode detection, the
map-obscuring parsers, character-state extraction, and the send-token vocabulary. All of
these are pure functions over captured screen text, so the suite needs no tmux, no game
binary, and no WSL - it runs against saved fixtures.

Run from the repo root:  python -m unittest discover -s tests -t .
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import automud_cdda as cd

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    # utf-8-sig drops the BOM PowerShell writes into the fixture files.
    with open(os.path.join(FIX, name), "r", encoding="utf-8-sig") as fh:
        return fh.read()


_MAP_GLYPHS = set("#{}~*")            # glyphs that only appear in the map, never in prose


class TestModeDetection(unittest.TestCase):
    def test_game(self):
        self.assertEqual(cd.detect_mode(load("game120.txt")), "game")

    def test_look(self):
        self.assertEqual(cd.detect_mode(load("look120.txt")), "look")

    def test_surroundings(self):
        self.assertEqual(cd.detect_mode(load("surroundings120.txt")), "surroundings")

    def test_death_not_misread_as_game(self):
        # The tombstone renders inside the map region; detection must key off the
        # memorial text, not a clean "The End" banner.
        self.assertEqual(cd.detect_mode(load("death.txt")), "death")

    def test_mod_prompt_first(self):
        raw = "Mod no_npc_food not found in mods folder, remove it? (Case Sensitive)"
        self.assertEqual(cd.detect_mode(raw), "mod_prompt")

    def test_debug_popup(self):
        raw = "An error has occurred!\nREPORTING FUNCTION : foo\nPress space bar"
        self.assertEqual(cd.detect_mode(raw), "debug_popup")

    def test_confirm(self):
        self.assertEqual(cd.detect_mode("Are you sure? [Y]es  [N]o"), "confirm")

    def test_world_select(self):
        self.assertEqual(cd.detect_mode("World selection\nPick a world to enter game"),
                         "world_select")

    def test_world_manage(self):
        raw = "Manage world 'Duarte'\n m Show World Mods\n d Delete World"
        self.assertEqual(cd.detect_mode(raw), "world_manage")

    def test_dropdown_option_is_not_the_create_dialog(self):
        # The main-menu dropdown lists "Create World" as an option; that must read as the
        # menu, not as the actual Create-World dialog (which has a World name field). This
        # distinction is what lets newgame navigate the menu instead of stalling.
        dropdown = "[MOTD]  [New Game]  [Load]\n Create World\n Duarte (0)"
        self.assertEqual(cd.detect_mode(dropdown), "main_menu")
        dialog = "Create World\nWorld name: Duarte\n[f][ Finish ]"
        self.assertEqual(cd.detect_mode(dialog), "world_create")


class TestGameObscuring(unittest.TestCase):
    def setUp(self):
        self.lines = cd.parse(load("game120.txt"), "game")
        self.text = "\n".join(self.lines)

    def test_message_log_surfaced(self):
        # The whole point of launching 120x40: the message log renders and we read it.
        self.assertIn("You open the window with curtains.", self.text)
        self.assertIn("You hear whump!", self.text)

    def test_status_line_present(self):
        self.assertTrue(any(ln.startswith("[") and "evac shelter A-23" in ln
                            for ln in self.lines))

    def test_no_map_rows_leak(self):
        # No emitted line may be a run of map glyphs (the left-hand map / minimap).
        for ln in self.lines:
            body = ln.lstrip("> ").strip()
            if body and not body.startswith("["):
                self.assertFalse(all(c in cd.MAP_CHARS or c == " " for c in body),
                                 "map row leaked: %r" % ln)

    def test_no_bare_map_glyph_lines(self):
        for ln in self.lines:
            stripped = ln.replace(" ", "")
            leak = sum(1 for c in stripped if c in _MAP_GLYPHS)
            self.assertLessEqual(leak, 2, "too many map glyphs in %r" % ln)


class TestCharacterState(unittest.TestCase):
    def setUp(self):
        self.state = cd.character_state(load("game120.txt"))

    def test_stats(self):
        self.assertEqual(self.state["stats"],
                         {"str": 8, "dex": 12, "int": 10, "per": 8})

    def test_needs_always_present(self):
        needs = self.state["needs"]
        for key in ("hunger", "thirst", "pain", "rest"):
            self.assertIn(key, needs)
        # Blank sidebar fields report "ok", not a bled-in neighbouring label or map dots.
        self.assertEqual(needs["hunger"], "ok")
        self.assertNotIn(":", needs["pain"])

    def test_time_keeps_colons(self):
        # The Time value contains its own colons; the colon-tolerant pattern must keep it.
        self.assertEqual(self.state.get("time"), "8:00:47 AM")

    def test_place(self):
        self.assertEqual(self.state.get("place"), "evac shelter A-23")

    def test_threat_detected(self):
        self.assertIn("deer", " ".join(self.state.get("threats", [])))

    def test_no_arrow_glyphs(self):
        for value in self.state.values():
            if isinstance(value, str):
                self.assertTrue(all(ord(c) < 128 for c in value),
                                "non-ascii leaked: %r" % value)


class TestSurroundings(unittest.TestCase):
    def test_lists_items_without_map(self):
        lines = cd.parse(load("surroundings120.txt"), "surroundings")
        text = "\n".join(lines)
        self.assertTrue(any("Items" in ln for ln in lines))
        for ln in lines:                          # no left-hand map preview leaks through
            self.assertFalse(all(c in cd.MAP_CHARS or c == " " for c in ln) and ln.strip())


class TestLookPanel(unittest.TestCase):
    def test_terrain_description(self):
        text = "\n".join(cd.parse(load("look120.txt"), "look"))
        self.assertIn("Evac shelter A-23", text)
        self.assertIn("Floor", text)


class TestDialogue(unittest.TestCase):
    """Dialogue renders in a box over the live game; the parser must slice to the box
    interior so the sidebar beside it and the message log below it do not leak in."""

    def setUp(self):
        self.lines = cd.parse(load("dialogue120.txt"), "dialogue")
        self.text = "\n".join(self.lines)

    def test_npc_speech_and_quest(self):
        self.assertIn("Dialogue: Dino Hutchison", self.text)
        self.assertIn("put her out of her misery", self.text)

    def test_response_options_present(self):
        self.assertIn("I'll do it!", self.text)
        self.assertIn("Not interested.", self.text)

    def test_no_sidebar_leak(self):
        # The HP bar row and stat lines sit level with the box but outside it.
        self.assertNotIn("L ARM", self.text)
        self.assertNotIn("Sound:", self.text)

    def test_no_message_log_leak(self):
        # The message log below the box must not bleed up into the dialogue.
        self.assertNotIn("Faction succession", self.text)


class TestTokenVocabulary(unittest.TestCase):
    def test_directions(self):
        self.assertEqual(cd._token_key("north"), "k")
        self.assertEqual(cd._token_key("se"), "n")

    def test_named_actions_not_spelled_out(self):
        # The footgun fix: "examine" is one key (e), never e-x-a-m-i-n-e (whose 'i'
        # used to open the inventory).
        self.assertEqual(cd._token_key("examine"), "e")
        self.assertEqual(cd._token_key("inventory"), "i")
        self.assertEqual(cd._token_key("pickup"), "g")

    def test_named_keys(self):
        self.assertEqual(cd._token_key("enter"), "Enter")
        self.assertEqual(cd._token_key("escape"), "Escape")

    def test_single_char_literal(self):
        self.assertEqual(cd._token_key("x"), "x")
        self.assertEqual(cd._token_key("Y"), "Y")

    def test_unknown_word_rejected(self):
        # Unknown multi-char words return None so the caller can report them, rather than
        # being typed one keystroke at a time.
        self.assertIsNone(cd._token_key("frobnicate"))
        self.assertIsNone(cd._token_key("examinate"))

    def test_special_actions_registered(self):
        for action in ("scenario", "profession", "class", "stats", "trait", "name",
                       "finalize", "nearby", "help", "type"):
            self.assertIn(action, cd._SPECIAL_ACTIONS)


class TestOptionEnumeration(unittest.TestCase):
    def test_selected_option_name(self):
        raw = "Identity: Survivor (male) (press @ to switch)"
        self.assertEqual(cd._selected_option_name(raw), "Survivor")
        raw2 = "Identity: Sheltered Survivor (female)"
        self.assertEqual(cd._selected_option_name(raw2), "Sheltered Survivor")

    def test_strip_list_markers(self):
        self.assertEqual(cd._strip_list_markers("^ Evacuee"), "Evacuee")
        self.assertEqual(cd._strip_list_markers("» Sheltered"), "Sheltered")
        self.assertEqual(cd._strip_list_markers("Wilderness v"), "Wilderness")

    def test_visible_list_entries(self):
        # The list/detail divider sits around column 57 in the real chargen layout.
        div = 57
        raw = (
            "Summary |  Lifestyle: average\n"
            + "^ Evacuee".ljust(div) + "│ Origin: Dark Days Ahead\n"
            + "  Sheltered".ljust(div) + "│ Identity: Evacuee (male)\n"
            + "  Wilderness".ljust(div) + "│ Press # to change date\n")
        entries = cd._visible_list_entries(raw)
        self.assertEqual(entries, ["Evacuee", "Sheltered", "Wilderness"])

    def test_visible_trait_columns(self):
        raw = (
            "Summary |  Lifestyle: average\n"
            "│ Fleet-Footed │ Asthmatic │ Eye color │\n"
            "│ Fast Reader  │ Near-Sighted │ Hair │\n")
        cols = cd._visible_trait_columns(raw)
        self.assertIn("Fleet-Footed", cols[0])
        self.assertIn("Asthmatic", cols[1])
        self.assertIn("Eye color", cols[2])

    def test_options_action_registered(self):
        self.assertIn("options", cd._SPECIAL_ACTIONS)
        self.assertIn("list", cd._SPECIAL_ACTIONS)


class TestPureHelpers(unittest.TestCase):
    def test_ascii_only_strips_arrows(self):
        self.assertEqual(cd._ascii_only("Calm ⇗"), "Calm")

    def test_is_map_segment(self):
        self.assertTrue(cd.is_map_segment("#.#.#.#"))
        self.assertFalse(cd.is_map_segment("You move north"))
        self.assertFalse(cd.is_map_segment("@"))       # lone player marker kept

    def test_sidebar_col_is_modal(self):
        # One anchor shifted by a sub-mode must not drag the boundary; the common column
        # wins. Build rows where most labels share column 40 but one sits at 20.
        rows = ["%sStr: 8" % (" " * 40), "%sPlace: x" % (" " * 40),
                "%sTime: 1" % (" " * 40), "%sDate: y" % (" " * 20)]
        self.assertEqual(cd._sidebar_col("\n".join(rows)), 40)

    def test_selected_trait_panes(self):
        colored = "\x1b[1m\x1b[32mFleet-Footed\x1b[0m and \x1b[31mAsthmatic\x1b[0m"
        panes = cd.selected_trait_panes(colored)
        self.assertIn("Fleet-Footed", panes["positive"])
        self.assertIn("Asthmatic", panes["negative"])


if __name__ == "__main__":
    unittest.main()
