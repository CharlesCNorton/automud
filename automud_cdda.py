"""
AutoMUD local backend: Cataclysm: Dark Days Ahead as an obligate single-player MUD.

automud connects to telnet MUDs. This module makes a local Cataclysm process look
like one more session so the same verbs drive it: `automud connect cdda` launches the
game, `send`/`recv`/`wait`/`state`/`status`/`close` work exactly as they do over telnet.
There is no MUD server to run and nothing to host; when every public MUD is finally
gone, this is a world that still answers.

Transport is tmux, not a socket: a persistent tmux session holds the ncurses game open
across separate automud invocations the way the telnet daemon holds a connection. On
POSIX we drive tmux directly; on Windows we bridge through `wsl.exe` (Cataclysm and tmux
live in WSL). Nothing here touches the telnet core.

The map is deliberately obscured. A blurry ASCII minimap is neither reliably parseable
by a language model nor necessary to one that reads a room description; `recv` in game
mode returns the message log and a structured status line (vitals, place, time, threats),
and the agent uses `look` / `examine` for spatial detail on demand. `automud state`
returns the character as JSON. The raw screen is always available with the `cdda-raw`
escape hatch for debugging.

Configuration (all optional):
    AUTOMUD_CDDA_DISTRO   WSL distro to run the game in on Windows (auto-detected:
                          prefers Ubuntu, then WSLExperiments, then any running distro).
    AUTOMUD_CDDA_DIR      directory containing the cataclysm binary (default /root/cdda).
    AUTOMUD_CDDA_BIN      binary to launch (default ./cataclysm).
    AUTOMUD_CDDA_LAUNCH   full shell command to start the game, overriding DIR/BIN.
    AUTOMUD_CDDA_TMUX     tmux executable name (default tmux).
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# tmux command bytes for named keys; single characters are sent literally.
SESSION = "automud_cdda"
DISTRO = ""            # WSL distro on Windows; "" means direct tmux (POSIX)

BOX_VERT = "│"
BOX_TL, BOX_TR, BOX_BL, BOX_BR = "┌", "┐", "└", "┘"
BOX_L, BOX_R, BOX_H, BOX_T, BOX_B, BOX_X = (
    "├", "┤", "─", "┬", "┴", "┼")
BORDER = (BOX_VERT + BOX_TL + BOX_TR + BOX_BL + BOX_BR + BOX_L + BOX_R
          + BOX_H + BOX_T + BOX_B + BOX_X)

# Map/minimap glyphs. The obscuring strategy does not rely on this being exhaustive
# (game-mode rendering surfaces only labeled fields), but it is used to drop map noise
# from message rows and boxed panels.
MAP_CHARS = frozenset(
    ".#>{}+\"*F~%^v<@"
    + BOX_VERT + BOX_TL + BOX_TR + BOX_BL + BOX_BR + BOX_L + BOX_R
    + BOX_H + BOX_T + BOX_B + BOX_X
    + "═║╔╗╚╝─")

# Sidebar field labels. character_state() reads these; game-mode recv renders a subset.
_STATUS_LABELS = (
    "Sound", "Mood", "Focus", "Stam", "Speed", "Move", "Str", "Dex", "Int", "Per",
    "Power", "Safe", "Weariness", "Activity", "Pain", "Thirst", "Rest", "Hunger",
    "Heat", "Weight", "Owner", "Place", "Lighting", "Weather", "Snow", "Moon",
    "Date", "Time", "Wind", "Temperature",
)

# Chargen stat order and which right-panel phrases identify the selected stat. CDDA shows
# the selected stat's effects in the detail panel; matching a phrase there tells us which
# stat the cursor is on without guessing at cursor position.
CHARGEN_STAT_NAMES = ("Strength", "Dexterity", "Intelligence", "Perception")
_STAT_PANEL_HINTS = {
    "Strength": ("Carry weight", "Bash damage", "Maximum HP", "Melee weapons"),
    "Dexterity": ("Melee to-hit", "Dodge", "Ranged penalty", "Faster movement"),
    "Intelligence": ("Read times", "Crafting bonus", "Bionics", "learn a recipe"),
    "Perception": ("Ranged to-hit", "Aiming", "Trap detection", "sight"),
}

CHARGEN_TABS = ("SCENARIO", "PROFESSION", "BACKGROUND", "STATS", "TRAITS",
                "SKILLS", "DESCRIPTION")

STOP_MODES = {"confirm", "death", "loading", "main_menu", "pause_menu", "popup",
              "debug_popup", "mod_prompt"}

# Confirms with an obviously-safe Y answer when they fire mid-play, and the missing-mod
# recovery prompts the trimmed data set provokes on world load. Answering these is what
# turns "the bundled world is broken on first launch" into "the game just starts".
SAFE_Y_CONFIRMS = (
    "Stop moving items", "Stop hauling", "Really step into", "You are freezing",
    "Stop reading", "Stop crafting", "Stop construction", "Stop disassembling",
)
MOD_REMOVE_PROMPTS = (
    "not found in mods folder",       # "Mod X not found ... remove it from this world?"
    "remove it from this world",
    "Cancel aborts load",
)
OVERLAY_ESCAPE_MARKERS = (
    "< Actions >", "Wield item", "Wear item", "Use item", "Safe mode manager",
    "Auto pickup manager", "Distractions manager",
)

# token -> tmux key name for named keys, and word directions -> vi movement keys.
NAMED_KEYS = {
    "enter": "Enter", "return": "Enter", "space": "Space", "tab": "Tab",
    "btab": "BTab", "back": "BTab", "escape": "Escape", "esc": "Escape",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "backspace": "BSpace", "bspace": "BSpace", "delete": "DC", "home": "Home",
    "end": "End", "pageup": "PageUp", "pagedown": "PageDown",
}
DIRECTIONS = {
    "north": "k", "south": "j", "east": "l", "west": "h",
    "ne": "u", "nw": "y", "se": "n", "sw": "b",
    "northeast": "u", "northwest": "y", "southeast": "n", "southwest": "b",
}
# Named game actions -> the CDDA key that opens them. So `send examine north` sends e
# then the direction, instead of spelling "examine" out as seven keystrokes (the old
# behaviour, where the 'i' in "examine" popped the inventory). Actions that need a
# follow-up (direction, menu choice) are driven by sending the follow-up as the next
# token.
# pickup / eat / wait / sleep are NOT here: they are high-level actions (below) that drive
# their whole multi-prompt flow to completion, rather than just opening the menu.
GAME_ACTIONS = {
    "look": "x", "examine": "e", "inventory": "i", "inv": "i",
    "wield": "w", "wear": "W", "takeoff": "T",
    "read": "R", "craft": "&", "construct": "*",
    "fire": "f", "throw": "t", "reload": "r",
    "drop": "d", "apply": "a", "use": "a", "activate": "a",
    "smash": "s", "bash": "s", "open": "o", "close": "c",
    "unload": "U", "butcher": "B", "chat": "C", "talk": "C", "safemode": "!",
}


class CddaError(RuntimeError):
    pass


# --------------------------------------------------------------------------- transport

def configure(session: str, distro: Optional[str] = None) -> None:
    global SESSION, DISTRO
    SESSION = session or SESSION
    if distro is not None:
        DISTRO = distro
    elif os.name == "nt":
        DISTRO = os.environ.get("AUTOMUD_CDDA_DISTRO", "") or _auto_distro()


def _auto_distro() -> str:
    """Pick a running WSL distro on Windows. wsl.exe emits UTF-16 LE with a BOM."""
    try:
        out = subprocess.run(["wsl.exe", "--list", "--running", "--quiet"],
                             capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return "Ubuntu"
    raw = out.stdout
    if raw.startswith(b"\xff\xfe"):
        raw = raw[2:]
    try:
        text = raw.decode("utf-16-le")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
    running = [x.strip() for x in text.replace("\x00", "").replace("\r", "").split("\n")
               if x.strip()]
    for pref in ("Ubuntu", "WSLExperiments"):
        if pref in running:
            return pref
    return running[0] if running else "Ubuntu"


def _tmux_argv(*args: str) -> List[str]:
    tmux = os.environ.get("AUTOMUD_CDDA_TMUX", "tmux")
    if os.name == "nt":
        # wsl.exe hands the post-`--` command to a shell, so a bare key like `|` (wait),
        # `$` (sleep) or `&` (craft) becomes a shell metacharacter and errors. Build the
        # command as one bash -c string with every argument shell-quoted, so those keys
        # reach tmux literally.
        inner = " ".join(shlex.quote(a) for a in (tmux, *args))
        return ["wsl.exe", "-d", DISTRO or _auto_distro(), "--", "bash", "-c", inner]
    return [tmux, *args]


def run_tmux(*args: str) -> str:
    try:
        result = subprocess.run(_tmux_argv(*args), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise CddaError("tmux transport failed: %s" % exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise CddaError(detail or "tmux %s exit %d" % (" ".join(args), result.returncode))
    return result.stdout


def capture_raw() -> str:
    return run_tmux("capture-pane", "-t", SESSION, "-p")


def capture_colored() -> str:
    return run_tmux("capture-pane", "-t", SESSION, "-e", "-p")


def session_alive() -> bool:
    try:
        run_tmux("has-session", "-t", SESSION)
        return True
    except CddaError:
        return False


def send_keys(*keys: str, delay_ms: int = 25) -> None:
    for key in keys:
        if len(key) == 1:
            run_tmux("send-keys", "-t", SESSION, "-l", key)
        else:
            run_tmux("send-keys", "-t", SESSION, key)
        if delay_ms:
            time.sleep(delay_ms / 1000.0)


def wait_for_change(before: Optional[str] = None, timeout: float = 3.0,
                    interval: float = 0.15, settle: float = 0.2,
                    settle_timeout: float = 1.0) -> str:
    """Poll until the screen changes, then until it holds still for `settle` seconds."""
    if before is None:
        before = capture_raw()
    elapsed = 0.0
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval
        latest = capture_raw()
        if latest != before:
            stable, held, sett = latest, 0.0, 0.0
            while held < settle and sett < settle_timeout:
                time.sleep(0.05)
                sett += 0.05
                cur = capture_raw()
                if cur == stable:
                    held += 0.05
                else:
                    stable, held = cur, 0.0
            return stable
    return before


def launch() -> None:
    """Start (or replace) the tmux session running Cataclysm."""
    if session_alive():
        try:
            run_tmux("kill-session", "-t", SESSION)
        except CddaError:
            pass
    launch_cmd = os.environ.get("AUTOMUD_CDDA_LAUNCH")
    if not launch_cmd:
        directory = os.environ.get("AUTOMUD_CDDA_DIR", "/root/cdda")
        binary = os.environ.get("AUTOMUD_CDDA_BIN", "./cataclysm")
        launch_cmd = "cd %s && %s" % (directory, binary)
    # 120x40, not the 80x24 minimum: at the minimum size CDDA collapses the message log
    # to nothing, so the player would see no "You move north" / "You bump into a wall"
    # feedback at all. The wider terminal makes the log render; we parse it and drop the
    # (now larger) map by geometry regardless.
    run_tmux("new-session", "-d", "-s", SESSION, "-x", "120", "-y", "40", launch_cmd)
    try:                                          # detached sessions need explicit sizing
        run_tmux("set-option", "-t", SESSION, "window-size", "manual")
        run_tmux("resize-window", "-t", SESSION, "-x", "120", "-y", "40")
    except CddaError:
        pass


def kill() -> None:
    if session_alive():
        run_tmux("kill-session", "-t", SESSION)


# --------------------------------------------------------------------------- text utils

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", (text or "").lstrip("﻿"))


def _clean(text: str) -> str:
    return (text or "").strip(BORDER + " ").strip()


def _ascii_only(text: str) -> str:
    """Drop non-ASCII glyphs (map arrows like the compass wind indicator) from a short
    field value, collapsing the whitespace they leave behind."""
    return re.sub(r"\s{2,}", " ", "".join(c for c in text if ord(c) < 128)).strip()


def is_map_segment(seg: str) -> bool:
    """A run that is purely map/minimap glyphs. A lone @ is kept (player marker in
    compass/NPC contexts)."""
    stripped = seg.strip()
    if stripped == "@":
        return False
    return bool(stripped) and all(c in MAP_CHARS or c == " " for c in seg)


def _rejoin_wrapped(lines: List[str]) -> List[str]:
    """Join ncurses continuation lines so wrapped prose stays readable."""
    if not lines:
        return lines
    out = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        prev = out[-1]
        # A line ending in " <single letter>" is usually a word ncurses broke mid-word
        # ("strangely v" + "icious"), which rejoins with no space. But "a"/"A"/"I" are
        # whole words, so "support a" + "roof" must stay "support a roof", not "aroof".
        if (len(prev) >= 2 and prev[-2] == " " and prev[-1].isalpha()
                and prev[-1] not in "aAI" and line[0].islower()):
            out[-1] = prev + line
        elif line[0].islower() or line[0] in ",.;:!?)'\"":
            out[-1] += " " + line
        else:
            out.append(line)
    return out


def _unique(lines: List[str]) -> List[str]:
    out, seen = [], set()
    for line in lines:
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


# --------------------------------------------------------------------------- mode detect

def _looks_like_popup(text: str) -> bool:
    lines = [ln.rstrip() for ln in text.split("\n") if ln.strip()]
    if len(lines) < 3:
        return False
    if not lines[0].lstrip().startswith(BOX_TL):
        return False
    bordered = sum(1 for ln in lines
                   if ln.lstrip().startswith((BOX_TL, BOX_VERT, BOX_BL)))
    return bordered >= max(3, len(lines) // 2)


def detect_mode(text: str) -> str:
    # Missing-mod recovery and engine debug popups sit on top of everything and must be
    # cleared before anything else can proceed; detect them first.
    if any(m in text for m in MOD_REMOVE_PROMPTS):
        return "mod_prompt"
    if "An error has occurred" in text and "REPORTING FUNCTION" in text:
        return "debug_popup"
    if re.search(r"\[Y\]es\s+\[N\]o", text) or "(Y/N/Q)" in text:
        return "confirm"
    # Death: the tombstone renders in the map region, so key off the memorial text, not
    # a clean "The End" banner (which the trimmed build does not always show intact).
    if "In memory of:" in text and ("The End" in text or "Last Words:" in text
                                    or "Survived:" in text):
        return "death"
    if "Your scores" in text and "KILLS" in text:
        return "postmortem"
    if "Loading files" in text or "Verifying" in text or "Finalizing" in text:
        return "loading"
    if "SCENARIO" in text and "PROFESSION" in text and "STATS" in text:
        if "<%sTRAITS%s>" % (BOX_VERT, BOX_VERT) in text:
            return "chargen_traits"
        return "chargen"
    if "ENCUMBRANCE AND WARMT" in text or ("SPEED:" in text and "MOVE COST" in text
                                           and "Strength:" in text):
        return "character_sheet"
    if "Crafting" in text and ("[RETURN] Craft" in text or "FAVORITE" in text
                               or ("WEAPON" in text and "APPLIANCE" in text)):
        return "recipe_menu"
    if (("Required skills" in text or "Time to complete" in text)
            and ("Construction" in text or "Crafting" in text or "Result:" in text)):
        return "craft_menu"
    if "< Look around >" in text:
        return "look"
    if "Inventory" in text and ("Bulk Volume" in text or "Total Weight" in text):
        return "inventory"
    if "Dialogue:" in text or "Your response:" in text:
        return "dialogue"
    if "[c] Creature" in text and "[t] Terrain" in text:
        return "extended"
    if "KEYBINDINGS" in text and "[f] Filter" in text:
        return "keybindings"
    if "MAIN MENU" in text and "Save and quit" in text:
        return "pause_menu"
    if re.search(r"Items \(\d+\)", text) and "Monsters" in text:
        return "surroundings"
    if "What to do with" in text or "What do you want to do" in text:
        return "overlay"
    if "Pick a world to enter game" in text or "World selection" in text:
        return "world_select"
    if "Show World Mods" in text or "Manage world" in text or "Delete World" in text:
        return "world_manage"
    # The real Create-World dialog has the World name field; the main-menu dropdown merely
    # lists "Create World" as an option and must not be mistaken for it.
    if "World name" in text and ("Finish" in text or "Difficulty" in text):
        return "world_create"
    if ("Custom Character" in text or "Play Now" in text
            or ("[New Game]" in text and "[Load]" in text)):
        return "main_menu"
    if _looks_like_popup(text):
        return "popup"
    return "game"


# --------------------------------------------------------------------------- extraction

def extract_messages(raw: str) -> List[str]:
    """Readable game messages from the top-left rows (rendered over the map)."""
    msgs = []
    for line in raw.split("\n")[:6]:
        left = line[:37]
        for seg in re.split(r" {2,}", left):
            seg = seg.strip()
            if not seg or is_map_segment(seg):
                continue
            cleaned = _clean(seg)
            if cleaned and any(c.isalpha() for c in cleaned):
                msgs.append(cleaned)
    return _unique(msgs)


def extract_boxed(raw: str) -> List[str]:
    """Content inside a bordered panel (look, popup, dialogue, menus), map dropped."""
    out = []
    for line in raw.split("\n"):
        r = line.rstrip()
        if not r.strip():
            continue
        stripped = r.strip()
        if all(ch in BORDER + " " for ch in stripped):
            continue
        if BOX_VERT in r:
            cells = [_clean(p) for p in r.split(BOX_VERT)]
            cells = [c for c in cells if c and not is_map_segment(c)]
            out.extend(cells)
            continue
        cleaned = _clean(r)
        if cleaned and not is_map_segment(cleaned):
            out.append(cleaned)
    return _rejoin_wrapped(_unique(out))


def _threats(raw: str) -> List[str]:
    """Creatures listed under the compass in the sidebar (e.g. 'v goose', 'D deer', 'v 11
    turkeys'). Each entry is the creature's map glyph, a space, an optional count, then the
    lowercase creature name. The leading glyph varies per creature (a bird is 'v', a deer
    'D'), so match any single glyph; the lowercase name and a length cap keep uppercase
    sidebar labels ('L ARM') and full message sentences out. This is the only spatial
    awareness game mode gives without the map."""
    out = []
    for line in raw.split("\n"):
        for seg in re.split(r" {2,}", line):
            seg = seg.strip()
            if len(seg) > 28 or is_map_segment(seg):
                continue
            m = re.match(r"^\S\s+(\d+\s+)?[a-z][a-z]+$", seg)
            if m:
                parts = seg.split(None, 1)
                if len(parts) == 2:
                    out.append(parts[1].strip())
    return _unique(out)


# First words of every labelled sidebar field, so the message-log extractor can tell a
# status readout ("Place: ...") from a game message ("You open the window.").
_LABEL_HEADS = frozenset(list(_STATUS_LABELS) + [
    "L", "R", "HEAD", "TORSO", "Str", "Dex", "Int", "Per", "Transition", "Style",
    "Wield", "Wind", "NW", "N", "NE", "W", "E", "SW", "S", "SE", "Weary"])


def _sidebar_col(raw: str) -> int:
    """Left column of the right-hand sidebar/message region. Taken as the MOST COMMON
    start column across many status anchors, not the minimum: one label shifted by a
    sub-mode (examine/look move the map and nudge a row) would otherwise drag the
    boundary left and slice into the labels, leaking 'ate: Thursday' style fragments
    into the message log."""
    counts: Dict[int, int] = {}
    anchors = ("L ARM", "HEAD", "TORSO", "Sound:", "Stam:", "Str:", "Dex:", "Int:",
               "Per:", "Place:", "Time:", "Date:", "Weather:", "Moon:", "Wind:")
    for line in raw.split("\n"):
        for a in anchors:
            idx = line.find(a)
            if idx > 20:
                counts[idx] = counts.get(idx, 0) + 1
    if counts:
        return max(counts, key=lambda c: (counts[c], -c))
    return 78


def extract_message_log(raw: str) -> List[str]:
    """The recent CDDA message log. It renders in the lower right; extract the prose lines
    from the sidebar column that are not themselves labelled status fields, and rejoin the
    ones ncurses wrapped. Requires a terminal tall/wide enough to show the log (we launch
    120x40 for exactly this)."""
    col = _sidebar_col(raw)
    out = []
    for line in raw.split("\n"):
        seg = line[col:].strip() if len(line) > col else ""
        if not seg or is_map_segment(seg):
            continue
        head = seg.split()[0].rstrip(":")
        if head in _LABEL_HEADS:
            continue
        if not any(c.islower() for c in seg):
            continue
        if " " not in seg and not seg.endswith(("!", ".", "?")):
            continue
        out.append(seg)
    return _rejoin_wrapped(_unique(out))


# --------------------------------------------------------------------------- char state

_HP_PARTS = ("L ARM", "HEAD", "R ARM", "L LEG", "TORSO", "R LEG")


def character_state(raw: Optional[str] = None) -> Dict[str, Any]:
    if raw is None:
        raw = capture_raw()
    text = strip_ansi(raw)
    out: Dict[str, Any] = {}

    stats = {}
    for stat in ("Str", "Dex", "Int", "Per"):
        m = re.search(r"\b%s:\s*(\d+)" % stat, text)
        if m:
            stats[stat.lower()] = int(m.group(1))
    if stats:
        out["stats"] = stats

    hp = {}
    for part in _HP_PARTS:
        m = re.search(r"%s\s+([|\\/-]+)" % re.escape(part), text)
        if m:
            hp[part.lower().replace(" ", "_")] = m.group(1).count("|")
    if hp:
        out["hp"] = hp

    # Survival needs are always reported, even when the sidebar leaves them blank (CDDA
    # shows nothing next to "Hunger:" until you are actually hungry). A blank field
    # becomes "ok" so a blind player can see the need is tracked and currently fine,
    # rather than the key silently vanishing.
    needs = {}
    for key, label in (("hunger", "Hunger"), ("thirst", "Thirst"), ("pain", "Pain"),
                       ("rest", "Rest"), ("weariness", "Weariness")):
        # Only ONE space is consumed after the colon: a blank field (many spaces before
        # the next column's label) then captures empty and reads "ok", instead of the
        # lazy match bleeding across the gap into the neighbouring field's label.
        m = re.search(r"%s:[ \t]?([^\n]*?)(?:\s{2,}|$)" % label, text, re.M)
        if not m:
            continue
        val = _ascii_only(m.group(1)).strip(" .")
        needs[key] = val if (val and ":" not in val and any(c.isalnum() for c in val)) \
            else "ok"
    if needs:
        out["needs"] = needs

    for label in ("Heat", "Mood", "Focus", "Weight", "Lighting", "Weather", "Place",
                  "Wield", "Moon", "Safe", "Activity"):
        # Time/Date carry colons; those are handled below with a colon-tolerant pattern.
        m = re.search(r"%s:\s+([A-Za-z0-9][^\n:]*?)(?:\s{2,}|$)" % label, text, re.M)
        if m:
            out[label.lower()] = _ascii_only(m.group(1))

    # Time and Date values contain their own colons ("8:00:03 AM"); capture to the field
    # boundary (2+ spaces or EOL) rather than stopping at the first colon.
    for label in ("Time", "Date"):
        m = re.search(r"%s:\s+(.+?)(?:\s{2,}|$)" % label, text, re.M)
        if m:
            out[label.lower()] = _ascii_only(m.group(1))

    threats = _threats(raw)
    if threats:
        out["threats"] = threats
    out["mode"] = detect_mode(raw)
    return out


def _status_line(state: Dict[str, Any]) -> str:
    bits = []
    hp = state.get("hp") or {}
    if hp:
        bits.append("HP torso %s/5" % hp.get("torso", "?"))
    if "place" in state:
        bits.append(state["place"])
    if "time" in state:
        bits.append(state["time"])
    if "weather" in state:
        bits.append(state["weather"])
    if state.get("safe"):
        bits.append("Safe: %s" % state["safe"])
    if state.get("threats"):
        bits.append("nearby: " + ", ".join(state["threats"]))
    return " | ".join(str(b) for b in bits)


# --------------------------------------------------------------------------- parsers

def _parse_game(raw: str) -> List[str]:
    """Map obscured: the recent message log, then a status line with threats. The map and
    minimap are dropped; the message log is what tells the player what just happened."""
    threats = set(_threats(raw))
    msgs = [m for m in extract_message_log(raw) if m not in threats]
    out = ["> " + m for m in msgs]
    state = character_state(raw)
    status = _status_line(state)
    if status:
        out.append("[" + status + "]")
    return out or ["(nothing new; use 'look', 'nearby', or 'state')"]


def _parse_menu(raw: str) -> List[str]:
    profiles, menu, notices = [], [], []
    for line in raw.split("\n"):
        r = line.rstrip()
        if not r.strip():
            continue
        for cell in re.findall(r"%s([^%s]+)%s" % (BOX_VERT, BOX_VERT, BOX_VERT), r):
            c = cell.strip()
            if c and any(ch.isalnum() for ch in c) and len(c) <= 32:
                sel = c.startswith("»")
                c = c.lstrip("» ").rstrip()
                profiles.append(("> " if sel else "  ") + c)
        if "[" in r and "]" in r and "[Quit]" in r:
            menu = re.findall(r"\[([^\]]+)\]", r)
        cleaned = _clean(r)
        if "Tip of the day:" in cleaned or "Bugs?" in cleaned:
            notices.append(cleaned)
    out = []
    if profiles:
        out.append("Profiles:")
        out.extend(_unique(profiles))
    if menu:
        out.append("Menu: " + " | ".join(menu))
    out.extend(_unique(notices))
    return out or extract_boxed(raw)


def _parse_chargen(raw: str) -> List[str]:
    left, right, tabs = [], [], ""
    seen_summary = False
    for line in raw.split("\n"):
        r = line.rstrip()
        if not r.strip():
            continue
        if "SCENARIO" in r and "PROFESSION" in r:
            active = re.search(r"<%s([A-Z]+)%s>" % (BOX_VERT, BOX_VERT), r)
            active_tab = active.group(1) if active else ""
            names = [t for t in CHARGEN_TABS if t in r]
            tabs = " | ".join("[%s]" % t if t == active_tab else t for t in names)
            continue
        if "Summary" in r and "Lifestyle" in r:
            seen_summary = True
            continue
        if "Press ?" in r or "Press k," in r or "Press TAB" in r or "[s] sort" in r:
            continue
        if not seen_summary:
            continue
        divider = -1
        for i, ch in enumerate(r):
            if ch == BOX_VERT and 30 <= i <= 46:
                divider = i
                break
        if divider > 0:
            lp, rp = _clean(r[:divider], ), _clean(r[divider + 1:])
            lp = re.sub(r"[\^v]$", "", lp).strip()
            if lp:
                left.append(lp)
            if rp:
                right.append(rp)
        else:
            cleaned = re.sub(r"[\^v]$", "", _clean(r)).strip()
            if cleaned and any(c.isalnum() for c in cleaned):
                (right if (len(r) - len(r.lstrip())) > 30 else left).append(cleaned)
    right = _rejoin_wrapped(right)
    out = []
    if tabs:
        out.append(tabs)
        out.append("")
    out.extend(left)
    if right:
        out.append("---")
        out.extend(right)
    return out


def _parse_confirm(raw: str) -> List[str]:
    questions = []
    for line in raw.split("\n"):
        if "?" not in line:
            continue
        for m in re.finditer(r"[A-Z][^?]{1,70}\?", line):
            q = re.sub(r"\s+", " ", m.group(0)).strip()
            if 2 <= len(q.split()) <= 12:
                questions.append(q)
                break
    out = _unique(questions)
    out.append("Choices: [Y]es / [N]o")
    return out


def _parse_death(raw: str) -> List[str]:
    out = []
    if "The End" in raw:
        out.append("The End")
    for label in ("In memory of:", "Survived:", "Kills:", "Last Words:"):
        for line in raw.split("\n"):
            if label in line:
                frag = _clean(line[line.index(label):])
                if frag:
                    out.append(frag)
                break
    return _unique(out) or extract_boxed(raw)


def _parse_surroundings(raw: str) -> List[str]:
    """The V surroundings menu (items/monsters/terrain near the player) is a list panel
    beside a map preview. The map uses digit/letter terrain glyphs the ASCII allowlist
    can't catch, so obscure it by geometry: find the panel's left edge (the 'Items ('
    header, or the '[e] Examine' command row) and keep only what is to the right of it."""
    col = None
    for needle in ("Items (", "[e] Examine", "[I] Compare"):
        for line in raw.split("\n"):
            i = line.find(needle)
            if i >= 0:
                col = i if col is None else min(col, i)
                break
    if col is None:
        col = _sidebar_col(raw)
    out = []
    for line in raw.split("\n"):
        region = line[col:] if len(line) > col else ""
        for seg in re.split(r" {2,}", region):
            seg = seg.strip()
            if seg and any(c.isalpha() for c in seg) and not is_map_segment(seg):
                out.append(seg)
    return _rejoin_wrapped(_unique(out))


def _parse_panel(raw: str) -> List[str]:
    """A dialogue box or an NPC interaction menu, both of which render OVER the game with
    the sidebar (and sometimes map) still visible around them. When a bordered box is
    present, slice to its interior so the sidebar to the right of the box and any map
    bleeding through its edge are dropped. Otherwise keep menu rows and prose while
    discarding map runs."""
    lines = raw.split("\n")
    left = right = top = None
    for idx, line in enumerate(lines):
        i = line.find(BOX_TL)
        if i >= 0:
            j = line.find(BOX_TR, i + 1)
            if j - i > 10:
                left, right, top = i, j, idx
                break
    out = []
    if left is not None:
        # Only the rows BETWEEN this box's top and bottom border: the sidebar above it
        # and the message log below it also sit in this column range and would leak.
        bottom = len(lines)
        for idx in range(top + 1, len(lines)):
            if left < len(lines[idx]) and lines[idx][left] == BOX_BL:
                bottom = idx
                break
        for line in lines[top + 1:bottom]:
            if len(line) <= left:
                continue
            seg = _clean(line[left + 1:right])
            if seg and not is_map_segment(seg):
                out.append(seg)
    else:
        for line in lines:
            for seg in re.split(r" {2,}", line):
                seg = seg.strip()
                if not seg or is_map_segment(seg):
                    continue
                if (re.match(r"^[A-Za-z][):]?\s+[A-Za-z]", seg) or seg.endswith("?")
                        or (" " in seg and any(c.islower() for c in seg))):
                    out.append(seg)
    return _rejoin_wrapped(_unique(out))


def _parse_overlay(raw: str) -> List[str]:
    """The in-grid NPC interaction / 'what to do with' menu. Unlike dialogue it has no
    border: the options sit in a middle column between the left map and the right sidebar.
    Anchor on the question and keep a column window around it, so the map and the sidebar
    (both outside that window) fall away and only the question and its keyed options
    remain."""
    lines = raw.split("\n")
    header_col = None
    for line in lines:
        plain = strip_ansi(line)
        i = plain.find("What do you want to do")
        if i < 0:
            i = plain.find("What to do with")
        if i >= 0:
            header_col = i
            break
    lo = max(0, header_col - 2) if header_col is not None else 0
    hi = header_col + 45 if header_col is not None else 200
    out = []
    for line in lines:
        window = strip_ansi(line)[lo:hi]
        qm = re.search(r"(What (?:do you want to do|to do with[^?]*)\?)", window)
        if qm:
            out.append(qm.group(1))
        for seg in re.split(r" {2,}", window):
            seg = seg.strip()
            if not seg or is_map_segment(seg) or "?" in seg:
                continue
            if seg in _HP_PARTS or (":" in seg and seg.split(":")[0].strip() in _LABEL_HEADS):
                continue                          # a sidebar HP part / labelled field
            if re.match(r"^[A-Za-z0-9]\s+[A-Z]", seg) and len(seg) <= 55:
                out.append(seg)                   # a keyed menu option: "t Talk to ..."
    return _unique(out)


def _parse_recipe_menu(raw: str) -> List[str]:
    """The & crafting recipe browser: a full-screen menu (category tabs across the top,
    the recipe list, and key hints at the bottom) with no game sidebar, so keep every
    readable line. An empty list just means nothing is craftable from what's on hand."""
    out = []
    for line in raw.split("\n"):
        cleaned = _clean(line)
        if cleaned and any(c.isalpha() for c in cleaned) and not is_map_segment(cleaned):
            out.append(cleaned)
    return _rejoin_wrapped(_unique(out))


def _parse_craft_menu(raw: str) -> List[str]:
    """The construction / crafting menu: a two-pane box (a list of buildable/craftable
    things on the left, the selected one's details on the right) with the game sidebar
    above it. Split the box interior at its internal divider so the options and the
    selected recipe read as two clean sections."""
    lines = raw.split("\n")
    left = right = top = None
    for i, line in enumerate(lines):
        j = line.find(BOX_TL)
        if j >= 0 and BOX_H in line:
            k = line.find(BOX_TR, j + 1)
            if k - j > 30:
                left, right, top = j, k, i
                break
    if top is None:
        return _parse_panel(raw)
    div = lines[top].find(BOX_T, left + 1)            # ┬ marks the internal divider column
    bottom = len(lines)
    for i in range(top + 1, len(lines)):
        if left < len(lines[i]) and lines[i][left] == BOX_BL:
            bottom = i
            break
    names, details = [], []
    for line in lines[top + 1:bottom]:
        interior = line[left + 1:right]
        if div > left:
            lcol = _clean(_strip_list_markers(interior[:div - left - 1]))
            rcol = _clean(interior[div - left:])
        else:
            lcol, rcol = _clean(interior), ""
        if lcol and any(c.isalpha() for c in lcol) and not lcol.startswith(("<<", "All ")):
            names.append(lcol)
        if rcol and any(c.isalpha() for c in rcol) and not rcol.startswith("Press "):
            details.append(rcol)
    out = []
    if names:
        out.append("--- Options ---")
        out.extend(_unique(names))
    details = _rejoin_wrapped([d for d in details if d])
    if details:
        out.append("")
        out.append("--- Selected ---")
        out.extend(details)
    return out


def _parse_character_sheet(raw: str) -> List[str]:
    """The @ character screen: a multi-column panel (stats, encumbrance, speed, then a
    description) occupying the left of the screen, with the game sidebar and map still
    visible to its right. Keep the left panel up to the sidebar column, split its internal
    columns, and drop map runs and the sidebar's bar fragments."""
    col = min(_sidebar_col(raw), 78)                  # the panel is ~78 wide; sidebar beyond
    out = []
    for line in raw.split("\n"):
        left = line[:col] if len(line) > col else line
        # Split only on the column borders, so each cell keeps its label WITH its value
        # ("Strength:  8 ( 8)"); collapse the padding for readability.
        for cell in left.split("│"):
            cell = re.sub(r"\s+", " ", _clean(cell)).strip()
            if not cell or is_map_segment(cell):
                continue
            if sum(c.isalpha() for c in cell) < 2:    # drop bar fragments like "M |||||"
                continue
            out.append(cell)
    return _rejoin_wrapped(_unique(out))


def _parse_loading(raw: str) -> List[str]:
    out = []
    for line in raw.split("\n"):
        cleaned = _clean(line)
        if cleaned and not is_map_segment(cleaned):
            out.append(cleaned)
    return _unique(out)


def parse(raw: str, mode: str) -> List[str]:
    if mode == "game":
        return _parse_game(raw)
    if mode in ("chargen", "chargen_traits"):
        return _parse_chargen(raw)
    if mode == "main_menu":
        return _parse_menu(raw)
    if mode == "confirm":
        return _parse_confirm(raw)
    if mode == "death":
        return _parse_death(raw)
    if mode == "loading":
        return _parse_loading(raw)
    if mode == "surroundings":
        return _parse_surroundings(raw)
    if mode == "character_sheet":
        return _parse_character_sheet(raw)
    if mode == "recipe_menu":
        return _parse_recipe_menu(raw)
    if mode == "craft_menu":
        return _parse_craft_menu(raw)
    if mode == "overlay":
        return _parse_overlay(raw)
    # dialogue / inventory / look render in a box laid over the game with the sidebar and
    # message log around it; slice to the box interior to drop them.
    if mode in ("dialogue", "inventory", "look"):
        return _parse_panel(raw)
    # popup, pause_menu, world_create, mod_prompt, debug_popup, extended, keybindings:
    # clean full-screen bordered panels.
    return extract_boxed(raw)


# --------------------------------------------------------------------------- auto-clear

def auto_clear(raw: Optional[str] = None, max_steps: int = 8) -> str:
    """Clear the blockers that otherwise wedge a fresh single-player launch or a long
    walk: the trimmed data set's missing-mod prompts, engine debug popups, and the
    recurring safe-Y confirms. Bounded so a self-refiring popup can't spin forever."""
    if raw is None:
        raw = capture_raw()
    for _ in range(max_steps):
        text = strip_ansi(raw)
        mode = detect_mode(raw)
        if mode == "mod_prompt":
            send_keys("Y")                       # remove the missing mod (case-sensitive)
            raw = wait_for_change(before=raw, timeout=2.0)
            continue
        if mode == "debug_popup":
            send_keys("Space")                   # continue past the error report
            after = wait_for_change(before=raw, timeout=1.5)
            if detect_mode(after) == "debug_popup":
                send_keys("I")                   # ignore this message in future
                after = wait_for_change(before=after, timeout=1.5)
            raw = after
            continue
        if "[Y]es" in text and any(s in text for s in SAFE_Y_CONFIRMS):
            send_keys("Y")
            raw = wait_for_change(before=raw, timeout=1.0)
            continue
        if any(marker in text for marker in OVERLAY_ESCAPE_MARKERS):
            send_keys("Escape")
            raw = wait_for_change(before=raw, timeout=1.2)
            continue
        return raw
    return raw


def _drive_keys(keys: List[str], timeout: float = 3.0) -> str:
    """Send already-mapped keys one at a time, auto-clearing blockers before each and
    stopping on a modal state so later keys do not spill into the wrong screen."""
    raw = auto_clear()
    for key in keys:
        raw = auto_clear(raw)
        before = raw
        send_keys(key)
        raw = wait_for_change(before=before, timeout=timeout)
        if detect_mode(raw) in STOP_MODES:
            break
    return auto_clear(raw)


def _token_key(token: str) -> Optional[str]:
    """Map one send token to a single tmux keystroke, or None if it is an unrecognized
    multi-character word. A bare single character is sent literally; named keys, word
    directions, and named game actions map to their key. Unknown words are NOT spelled
    out letter by letter (that used to make `send examine` open the inventory on its 'i')
    - the caller reports them instead."""
    low = token.lower()
    if low in NAMED_KEYS:
        return NAMED_KEYS[low]
    if low in DIRECTIONS:
        return DIRECTIONS[low]
    if low in GAME_ACTIONS:
        return GAME_ACTIONS[low]
    if len(token) == 1:
        return token
    return None


# --------------------------------------------------------------------------- chargen ops

def detect_chargen_tab(raw: str) -> Optional[str]:
    for tab in CHARGEN_TABS:
        if "<%s%s%s>" % (BOX_VERT, tab, BOX_VERT) in raw:
            return tab
    return None


def _goto_tab(target: str) -> bool:
    idx = CHARGEN_TABS.index(target)
    for _ in range(len(CHARGEN_TABS) * 2):
        now = detect_chargen_tab(capture_raw())
        if now is None:
            return False
        if CHARGEN_TABS.index(now) == idx:
            return True
        send_keys("Tab" if CHARGEN_TABS.index(now) < idx else "BTab")
        wait_for_change(timeout=1.0)
    return False


def _selected_stat(colored: str, plain: str) -> Optional[str]:
    """Which stat the STATS cursor is on. Primary signal: the detail panel shows the
    selected stat's effects, matched by a hint phrase. Fallback: the highlighted row
    carries a blue-background/standout SGR in the colored capture."""
    for stat, hints in _STAT_PANEL_HINTS.items():
        if any(h in plain for h in hints):
            return stat
    for line in colored.split("\n"):
        if ("\x1b[7m" in line or "\x1b[44m" in line):
            for stat in CHARGEN_STAT_NAMES:
                if stat in strip_ansi(line):
                    return stat
    return None


def _read_stat(stat: str, text: Optional[str] = None) -> Optional[int]:
    if text is None:
        text = strip_ansi(capture_raw())
    m = re.search(r"%s:\s+(\d+)" % re.escape(stat), text)
    return int(m.group(1)) if m else None


def read_all_stats() -> Dict[str, int]:
    """Ground-truth stat values, always readable regardless of cursor position."""
    text = strip_ansi(capture_raw())
    out = {}
    for stat in CHARGEN_STAT_NAMES:
        v = _read_stat(stat, text)
        if v is not None:
            out[stat.lower()[:3]] = v
    return out


def chargen_set_stats(values: Dict[str, int]) -> Dict[str, Any]:
    """Set chargen stats to targets. Navigates by detecting which stat is selected
    (never by assuming cursor position, which drifts when the list wraps) and returns
    the values actually on screen afterward, so the report can never diverge from
    reality. `values` is keyed by str/dex/int/per (any subset)."""
    if not _goto_tab("STATS"):
        raise CddaError("could not reach the STATS tab")
    key_to_name = {"str": "Strength", "dex": "Dexterity",
                   "int": "Intelligence", "per": "Perception"}
    for key, target in values.items():
        stat = key_to_name.get(key)
        if not stat:
            continue
        target = int(target)
        if not _goto_stat(stat):
            continue
        stuck = 0
        for _ in range(30):
            cur = _read_stat(stat)
            if cur is None or cur == target:
                break
            send_keys("Right" if cur < target else "Left")
            time.sleep(0.12)
            if _read_stat(stat) == cur:
                stuck += 1
                if stuck >= 2:                   # bouncing off a min/max cap
                    break
            else:
                stuck = 0
    actual = read_all_stats()
    ok = all(actual.get(k) == int(v) for k, v in values.items() if k in actual)
    return {"actual": actual, "requested": {k: int(v) for k, v in values.items()},
            "ok": ok}


def _goto_stat(target: str) -> bool:
    for _ in range(12):
        colored = capture_colored()
        plain = strip_ansi(colored)
        cur = _selected_stat(colored, plain)
        if cur == target:
            return True
        if cur is None:
            send_keys("Down")
            wait_for_change(timeout=0.6)
            continue
        ci, ti = CHARGEN_STAT_NAMES.index(cur), CHARGEN_STAT_NAMES.index(target)
        send_keys("Down" if ti > ci else "Up")
        wait_for_change(timeout=0.6)
    return _selected_stat(capture_colored(), strip_ansi(capture_raw())) == target


def _panel_locked(raw: str) -> bool:
    text = strip_ansi(raw)
    return ("You must complete the achievement" in text or "to unlock this" in text)


def _panel_identity(raw: str) -> str:
    m = re.search(r"Identity:\s*(.+?)(?:\s*\((?:male|female)\)|\s{2,}|$)",
                  strip_ansi(raw))
    return m.group(1).strip() if m else ""


def chargen_filter_commit(text: str) -> Dict[str, Any]:
    """Filter a chargen list to `text` and commit the landed entry, but refuse to commit
    a locked or mismatched row. The trimmed data set surfaces achievement-locked entries
    (e.g. professions) that the old blind double-Enter would silently commit; here the
    landed row is checked first and the confirm is withheld on a bad target."""
    raw = capture_raw()
    if not detect_mode(raw).startswith("chargen"):
        raise CddaError("not in chargen (mode: %s)" % detect_mode(raw))
    on_traits = detect_mode(raw) == "chargen_traits"
    send_keys("f")
    wait_for_change(before=raw, timeout=2.0)
    for ch in text:
        send_keys(ch)
    send_keys("Enter")                            # closes filter, lands cursor on a match
    raw = wait_for_change(timeout=2.0)
    if "Nothing found" in strip_ansi(raw):
        send_keys("Escape")
        wait_for_change(timeout=0.6)
        send_keys("r")
        return {"ok": False, "committed": None, "reason": "no match for %r" % text}
    if _panel_locked(raw):
        send_keys("r")                            # reset filter, do not confirm a locked row
        return {"ok": False, "committed": _panel_identity(raw),
                "reason": "landed row is locked; not committed"}
    identity = _panel_identity(raw)
    if not on_traits:
        send_keys("Enter")                        # CONFIRM (persists spawn-side selection)
        raw = wait_for_change(timeout=2.0)
        identity = _panel_identity(raw) or identity
    return {"ok": True, "committed": identity, "reason": ""}


def chargen_set_name(name: str) -> Dict[str, Any]:
    if not _goto_tab("DESCRIPTION"):
        raise CddaError("could not reach the DESCRIPTION tab")
    raw = capture_raw()
    send_keys("Enter")
    wait_for_change(before=raw, timeout=1.0)
    for ch in name:
        send_keys(ch)
        time.sleep(0.04)
    send_keys("Enter")
    wait_for_change(timeout=1.0)
    m = re.search(r"Name:\s+(.+?)\s{2,}", strip_ansi(capture_raw()))
    return {"name": m.group(1).strip() if m else None}


# CDDA marks a selected positive trait bold green and a selected negative trait red; an
# extra blue background appears when the cursor is also on it. Selection state is only
# expressed through colour, so it must be read from a colored capture.
_BOLD_GREEN_RUN = re.compile(r"\x1b\[1m\x1b\[32m(?:\x1b\[44m)?([^\x1b\n]+)")
_BOLD_RED_RUN = re.compile(r"(?:\x1b\[1m)?\x1b\[31m(?:\x1b\[44m)?([^\x1b\n]+)")
_SUMMARY_RATINGS = frozenset({
    "weak", "underpowered", "average", "strong", "powerful", "overpowered",
    "fragile", "sturdy", "overwhelming"})


def selected_trait_panes(colored: str) -> Dict[str, List[str]]:
    """{'positive': [...], 'negative': [...]} of currently-selected traits, read from a
    colored capture. The Summary rating words (also bold green) are excluded."""
    scoped = "\n".join(ln for ln in colored.split("\n")
                       if not ("Summary" in strip_ansi(ln) and "Lifestyle" in strip_ansi(ln)))

    def collect(rx):
        out, seen = [], set()
        for text in rx.findall(scoped):
            name = text.strip()
            if len(name) <= 1 or name in ("^", "v") or name.lower() in _SUMMARY_RATINGS:
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out
    return {"positive": collect(_BOLD_GREEN_RUN), "negative": collect(_BOLD_RED_RUN)}


def _trait_reset_filter() -> None:
    send_keys("r")
    wait_for_change(timeout=0.8)


def trait_toggle(name: str) -> Dict[str, Any]:
    """Find a trait across the positive and negative panes, toggle it, and verify the
    selection actually flipped. Caller must be on the TRAITS tab. Returns before/after
    selection state and which pane it lives in."""
    raw = capture_raw()
    if detect_mode(raw) != "chargen_traits":
        raise CddaError("not on the TRAITS tab (mode: %s)" % detect_mode(raw))
    before = selected_trait_panes(capture_colored())
    was = name in before["positive"] or name in before["negative"]
    _trait_reset_filter()
    for _ in range(3):                            # positive, negative, cosmetic panes
        send_keys("f")
        wait_for_change(timeout=1.0)
        for ch in name:
            send_keys(ch)
        send_keys("Enter")
        raw = wait_for_change(timeout=1.0)
        if "Nothing found" in strip_ansi(raw):
            send_keys("Escape")
            wait_for_change(timeout=0.6)
            _trait_reset_filter()
            send_keys("Right")
            wait_for_change(timeout=0.5)
            continue
        if name in strip_ansi(raw):
            send_keys("Enter")                    # toggle
            wait_for_change(timeout=1.0)
            after = selected_trait_panes(capture_colored())
            now = name in after["positive"] or name in after["negative"]
            pane = ("positive" if name in after["positive"] or name in before["positive"]
                    else "negative" if name in after["negative"] or name in before["negative"]
                    else "unknown")
            _trait_reset_filter()
            return {"ok": was != now, "name": name, "before": was, "after": now,
                    "pane": pane}
        _trait_reset_filter()
        send_keys("Right")
        wait_for_change(timeout=0.5)
    _trait_reset_filter()
    return {"ok": False, "name": name, "before": was, "after": was, "pane": "unknown",
            "reason": "trait not found in either pane"}


def _strip_list_markers(seg: str) -> str:
    seg = re.sub(r"^[\^v»>]+\s*", "", seg)   # leading scroll / selection markers
    seg = re.sub(r"\s*[\^v]$", "", seg)           # trailing scroll marker
    return seg.strip()


def _list_divider_col(lines: List[str]) -> Optional[int]:
    for line in lines:
        i = line.find(BOX_VERT, 40)
        if 40 <= i <= 74:
            return i
    return None


def _visible_list_entries(raw: str) -> List[str]:
    """Option names in the left list column of a single-column chargen tab (SCENARIO /
    PROFESSION / BACKGROUND / SKILLS), for the currently-scrolled window."""
    lines = raw.split("\n")
    col = _list_divider_col(lines)
    out = []
    seen_summary = False
    for line in lines:
        r = line.rstrip()
        if "Summary" in r and "Lifestyle" in r:
            seen_summary = True
            continue
        if not seen_summary or ("SCENARIO" in r and "PROFESSION" in r):
            continue
        seg = _strip_list_markers(_clean(r[:col] if col else r))
        if seg and any(c.isalpha() for c in seg) and not seg.startswith("Press "):
            out.append(seg)
    return out


def _visible_trait_columns(raw: str) -> List[List[str]]:
    """The three trait panes (positive / negative / cosmetic) visible in the current
    TRAITS window."""
    cols: List[List[str]] = [[], [], []]
    seen_summary = False
    for line in raw.split("\n"):
        r = line.rstrip()
        if "Summary" in r and "Lifestyle" in r:
            seen_summary = True
            continue
        if not seen_summary or r.count(BOX_VERT) < 2:
            continue
        parts = [_clean(p) for p in r.split(BOX_VERT)]
        while parts and not parts[0]:
            parts.pop(0)
        while parts and not parts[-1]:
            parts.pop()
        for i in range(min(3, len(parts))):
            name = _strip_list_markers(parts[i])
            if name and any(c.isalpha() for c in name):
                cols[i].append(name)
    return cols


def _selected_option_name(raw: str) -> Optional[str]:
    m = re.search(r"Identity:\s*(.+?)(?:\s*\((?:male|female)\)|\s{2,}|$)",
                  strip_ansi(raw))
    return m.group(1).strip() if m else None


def _walk_options(read_selected, limit: int = 400) -> List[str]:
    """Enumerate a single-column chargen list exactly and in order. Window scrolling is
    unreliable (a burst of Down keys is coalesced into ~one move, and page boundaries
    behave inconsistently), so instead step ONE item at a time - a single Down always
    moves exactly one - and read the unambiguous selected-item readout each step. Stops
    when the selection loops back to the first item (the list wrapped = full pass) or
    stops advancing (bottom of a non-wrapping list)."""
    seen: List[str] = []
    stuck = 0
    for _ in range(limit):
        raw = capture_raw()
        cur = read_selected(raw)
        if not cur:
            break
        if cur in seen:
            if cur == seen[0] and len(seen) > 1:
                break                             # wrapped to the top: every item seen
            stuck += 1
            if stuck >= 3:
                break                             # not moving: bottom of the list
        else:
            seen.append(cur)
            stuck = 0
        before = raw
        send_keys("Down")
        wait_for_change(before=before, timeout=0.5)
    return seen


def _collect_scrolling(read_window, max_pages: int = 40) -> List[str]:
    """Every entry in the focused chargen list, gathered without needing to find the top
    first: page DOWN collecting until the window stops yielding anything new (the bottom),
    then page UP doing the same (the top). From any starting position the two passes
    together cover the whole list. Paging uses a single PageDown / PageUp per step: a
    burst of individual Down keys sent at once is coalesced by the game into roughly one
    move, so it never actually scrolls. read_window() returns the entries visible now."""
    seen: List[str] = []
    for direction in ("PageDown", "PageUp"):
        stale = 0
        for _ in range(max_pages):
            added = 0
            for entry in read_window():
                if entry not in seen:
                    seen.append(entry)
                    added += 1
            if added == 0:
                stale += 1
                if stale >= 2:
                    break
            else:
                stale = 0
            before = capture_raw()
            send_keys(direction)
            wait_for_change(before=before, timeout=0.7)
    return seen


def chargen_list_options(tab: Optional[str] = None) -> Dict[str, Any]:
    """Every selectable option on a chargen tab, gathered by scrolling the list top to
    bottom (the window only shows ~20-30 at a time). Answers "what can I pick here" for
    the long lists (120 professions, 172 traits) that never fit on one screen."""
    raw = capture_raw()
    if not detect_mode(raw).startswith("chargen"):
        raise CddaError("not in chargen (mode: %s)" % detect_mode(raw))
    cur = detect_chargen_tab(raw)
    if tab:
        tab = tab.upper()
        if tab != cur and _goto_tab(tab):
            cur = tab
    if cur == "STATS":
        return {"tab": "STATS", "count": 4, "options": list(CHARGEN_STAT_NAMES)}
    if cur == "TRAITS":
        panes = {"positive": [], "negative": [], "cosmetic": []}
        keys = ("positive", "negative", "cosmetic")
        for _ in range(3):                        # anchor on the leftmost pane
            send_keys("Left")
            time.sleep(0.05)
        for idx in range(3):
            panes[keys[idx]] = _collect_scrolling(
                lambda i=idx: _visible_trait_columns(capture_raw())[i])
            send_keys("Right")                    # advance to the next pane
            time.sleep(0.1)
        return {"tab": "TRAITS", "count": sum(len(v) for v in panes.values()), **panes}
    # SCENARIO / PROFESSION / BACKGROUND expose the selection on the Identity line, so walk
    # them item by item for an exact, ordered enumeration.
    if cur in ("SCENARIO", "PROFESSION", "BACKGROUND"):
        options = _walk_options(_selected_option_name)
        return {"tab": cur, "count": len(options), "options": options}
    options = _collect_scrolling(lambda: _visible_list_entries(capture_raw()))
    return {"tab": cur, "count": len(options), "options": options}


def chargen_summary() -> Dict[str, Any]:
    """A parseable snapshot of the character build so far: current tab, the selected
    entry on it, the stat line (always readable), and, by a quick hop to DESCRIPTION, the
    scenario/profession the build will actually spawn with. Returns to the origin tab."""
    raw = capture_raw()
    origin = detect_chargen_tab(raw)
    out: Dict[str, Any] = {"mode": detect_mode(raw), "tab": origin}
    ident = _panel_identity(raw)
    if ident:
        out["selected_here"] = ident
    stats = read_all_stats()
    if stats:
        out["stats"] = stats
    if origin and origin != "DESCRIPTION" and _goto_tab("DESCRIPTION"):
        desc = strip_ansi(capture_raw())
        for label in ("Scenario", "Profession", "Name"):
            m = re.search(r"%s:\s*(.+?)(?:\s{2,}|$)" % label, desc, re.M)
            if m and m.group(1).strip():
                out[label.lower()] = m.group(1).strip()
        _goto_tab(origin)
    return out


def chargen_finalize(expect_scenario: Optional[str] = None,
                     expect_profession: Optional[str] = None) -> Dict[str, Any]:
    """Tab past DESCRIPTION to trigger the finalize confirm. Verifies expected names are
    visible before committing so a cascade-reset build isn't finalized blind. CDDA
    cascade-wipes downstream selections when scenario/profession change, so the caller
    must set scenario, then profession, then stats, then traits, then name, in order."""
    if not _goto_tab("DESCRIPTION"):
        raise CddaError("could not reach the DESCRIPTION tab")
    text = strip_ansi(capture_raw())
    issues = []
    if expect_scenario and expect_scenario not in text:
        issues.append("scenario %r not visible" % expect_scenario)
    if expect_profession and expect_profession not in text:
        issues.append("profession %r not visible" % expect_profession)
    if issues:
        return {"ok": False, "reason": "; ".join(issues)}
    send_keys("Tab")
    raw = wait_for_change(timeout=2.0)
    if detect_mode(raw) != "confirm":
        return {"ok": False, "reason": "no finalize confirm appeared"}
    send_keys("Y")
    raw = wait_for_change(timeout=10.0)
    return {"ok": True, "mode": detect_mode(auto_clear(raw))}


# ------------------------------------------------------------------- main-menu -> chargen

def _escape_to_main_menu(max_steps: int = 8) -> str:
    """Back out of any submenu to the main menu. A quit/finish confirm (which Escape at
    the main menu raises) is DECLINED with N, so this never quits the game."""
    for _ in range(max_steps):
        raw = capture_raw()
        mode = detect_mode(raw)
        if mode == "main_menu":
            return raw
        if mode == "confirm":
            send_keys("N")
            wait_for_change(before=raw, timeout=1.0)
            continue
        if mode == "world_manage":                # this menu's back-out is 'q', not Escape
            send_keys("q")
            wait_for_change(before=raw, timeout=1.0)
            continue
        send_keys("Escape")
        wait_for_change(before=raw, timeout=1.0)
    return capture_raw()


def newgame_reach_chargen(max_steps: int = 60) -> str:
    """Drive the main-menu maze to character creation. The menu gives no readable cursor,
    and New Game and World show an IDENTICAL world dropdown, so the only way to tell the
    top items apart is by what Enter opens. This probes and homes:

      - Enter the active top item and classify the result.
      - New Game opens the 'Pick a world' popup (world_select) -> success, drill in.
      - World opens 'Manage world' (world_manage); Load opens a load list. Both sit to the
        RIGHT of New Game, so step LEFT toward it. A MOTD/other popup sits to the LEFT, so
        step RIGHT. New Game lies between them, so this converges without ever Entering
        Tutorial or Quit.
      - world_select : Enter plays the highlighted world.
      - world_create : Finish (f) + confirm (Y), for a first run with no world yet.
      - loading      : wait; missing-mod / debug popups are cleared by auto_clear.

    Returns the chargen capture, or raises if it cannot get there."""
    _escape_to_main_menu()
    for _ in range(max_steps):
        raw = auto_clear(capture_raw())
        mode = detect_mode(raw)
        text = strip_ansi(raw)
        if mode in ("chargen", "chargen_traits"):
            return raw
        if mode == "loading":
            time.sleep(0.6)
            continue
        if mode == "world_select":
            send_keys("Enter")
            wait_for_change(before=raw, timeout=2.5)
            continue
        if mode == "world_create":
            send_keys("f")
            r = wait_for_change(before=raw, timeout=1.5)
            if detect_mode(r) == "confirm":
                send_keys("Y")
                wait_for_change(before=r, timeout=2.5)
            continue
        if mode == "world_manage":                # World is right of New Game: go left
            _escape_to_main_menu()
            send_keys("Left")
            time.sleep(0.1)
            continue
        if "Custom Character" in text and "Play Now" in text and mode != "main_menu":
            for _ in range(6):                    # anchor top of the build list = Custom
                send_keys("Up")
                time.sleep(0.04)
            send_keys("Enter")
            wait_for_change(timeout=2.0)
            continue
        if mode == "main_menu":
            send_keys("Enter")                    # probe the active top item
            wait_for_change(before=raw, timeout=2.0)
            time.sleep(0.3)                        # let the opened screen settle before reading
            after = auto_clear(capture_raw())
            am = detect_mode(after)
            at = strip_ansi(after).lower()
            if (am in ("world_select", "world_create", "loading")
                    or am.startswith("chargen")):
                continue                          # New Game found; outer loop drills in
            if am == "world_manage" or "characters to load" in at or "load character" in at:
                _escape_to_main_menu()            # World / Load: New Game is to the left
                send_keys("Left")
                time.sleep(0.1)
            elif am == "main_menu":               # Enter opened nothing readable: nudge left
                send_keys("Left")
                time.sleep(0.1)
            else:                                 # MOTD / other popup: New Game is to the right
                _escape_to_main_menu()
                send_keys("Right")
                time.sleep(0.1)
            continue
        _escape_to_main_menu()                    # unknown popup: back out and nudge left
        send_keys("Left")
        time.sleep(0.1)
    raise CddaError("could not reach character creation within the step budget")


# --------------------------------------------------------------------------- automud API
#
# These are what automud.py's verbs call once a session's backend is "cdda". They reuse
# automud's own session file and transcript so `log` and staleness detection work
# uniformly across telnet and cdda sessions.

def _automud():
    # Bind to the SAME automud module instance whose globals main() mutated. Run as a
    # script, automud.py is __main__ and `import automud` would load a second copy with
    # default (unset) state paths; the installed console-script wrapper is a thin __main__
    # that lacks these globals, so we fall through to the real module there.
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and hasattr(main_mod, "_write_session") \
            and hasattr(main_mod, "SESSION_JSON"):
        return main_mod
    import automud
    return automud


def _log_text(text: str) -> None:
    am = _automud()
    if not text:
        return
    try:
        with open(am.OUT_LOG, "a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


def _session_meta() -> dict:
    return _automud()._read_session() or {}


def _render(raw: str) -> Tuple[str, str]:
    mode = detect_mode(raw)
    lines = [ln for ln in parse(raw, mode)]
    return mode, "\n".join(lines)


def cmd_connect(json_mode: bool, settle: float = 1.5) -> int:
    am = _automud()
    distro = os.environ.get("AUTOMUD_CDDA_DISTRO", "") or (
        _auto_distro() if os.name == "nt" else "")
    configure(am_session_name(), distro or None)
    # Replace any existing session (telnet or cdda) the way telnet connect does.
    old = am._read_session()
    if old:
        if old.get("backend") == "cdda":
            try:
                kill()
            except CddaError:
                pass
        elif old.get("control_port"):
            am._control("close", _timeout=5.0)
    try:
        am._ensure_dir()
        launch()
    except CddaError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}) if json_mode
              else "connect failed: %s" % exc)
        return 1
    # Roll the transcript like the telnet path does.
    try:
        if os.path.exists(am.OUT_LOG) and os.path.getsize(am.OUT_LOG) > 0:
            os.replace(am.OUT_LOG, am.OUT_PREV_LOG)
    except OSError:
        pass
    open(am.OUT_LOG, "w", encoding="utf-8").close()
    am._write_session({"backend": "cdda", "tmux_session": SESSION, "distro": distro,
                       "started": time.time(), "version": am.__version__,
                       "host": "cdda", "port": 0})
    # Let the game reach its first interactive screen, clearing any launch popups.
    deadline = time.time() + 30
    raw = capture_raw()
    while time.time() < deadline:
        raw = auto_clear(capture_raw())
        if detect_mode(raw) not in ("loading",):
            if raw.strip():
                break
        time.sleep(0.4)
    time.sleep(settle)
    raw = auto_clear(capture_raw())
    mode, data = _render(raw)
    _log_text(data)
    if json_mode:
        print(json.dumps({"ok": True, "backend": "cdda", "mode": mode, "data": data,
                          "connected": True}))
    else:
        print("connected to cdda (local single-player)%s"
              % ((" via WSL:" + distro) if distro else ""))
        am._print(data)
    return 0


def am_session_name() -> str:
    """Derive the tmux session name from automud's per-session state dir so `-s NAME`
    yields independent games."""
    am = _automud()
    base = os.path.basename(am.STATE_DIR.rstrip(os.sep)) or "default"
    return "automud_cdda_%s" % re.sub(r"[^A-Za-z0-9_]", "_", base)


def _require_session(json_mode: bool) -> Optional[dict]:
    meta = _session_meta()
    if not meta or meta.get("backend") != "cdda":
        msg = "no active cdda session (run 'connect cdda')"
        print(json.dumps({"ok": False, "error": msg}) if json_mode else msg)
        return None
    configure(meta.get("tmux_session") or am_session_name(), meta.get("distro") or None)
    if not session_alive():
        try:
            os.remove(_automud().SESSION_JSON)
        except OSError:
            pass
        msg = "cdda session is gone (run 'connect cdda')"
        print(json.dumps({"ok": False, "error": msg}) if json_mode else msg)
        return None
    return meta


def _emit_action(result: dict, json_mode: bool, prose_prefix: str = "") -> int:
    """Emit a structured high-level action result (chargen ops, nearby, help)."""
    if json_mode:
        print(json.dumps(result))
    else:
        if prose_prefix:
            print(prose_prefix)
        for k, v in result.items():
            if k == "ok":
                continue                          # implied by the exit code in prose mode
            if k == "lines" and isinstance(v, list):
                for line in v:
                    print(line)
            else:
                print("%s: %s" % (k, v))
    return 0 if result.get("ok", True) else 1


# Recurring "Stop ...?" interrupts during a wait/sleep that are safe to ignore and keep
# going, rather than aborting the whole activity.
KNOWN_DISTRACTIONS = (
    "dehydrated", "parched", "thirsty", "hungry", "famished", "starving",
    "asthma attack", "mouth feels so dry", "cold and shiver", "getting chilly",
    "hypothermia", "feel cruddy",
)
WAIT_SPEC_TO_KEY = {
    "20s": "1", "1m": "2", "5m": "3", "30m": "4", "1h": "5", "2h": "6", "3h": "7",
    "6h": "8", "daylight": "d", "noon": "n", "night": "k", "midnight": "m",
}


def _is_stop_confirm(raw: str) -> bool:
    text = strip_ansi(raw)
    return "Case Sensitive" in text and ("Stop " in text or "are you sure" in text.lower())


def _drain_distractions(deadline_s: float, ignore: bool = True) -> str:
    """Poll during a wait/sleep: press I to shrug off a known recurring distraction and
    keep going, bail on a novel interrupt, keep trying to sleep through 'trouble
    sleeping'. Returns when the activity ends or the deadline passes."""
    end = time.time() + deadline_s
    last = capture_raw()
    while time.time() < end:
        time.sleep(0.4)
        raw = capture_raw()
        if raw == last:
            continue
        last = raw
        low = strip_ansi(raw).lower()
        if "finish waiting" in low or "you wake up" in low or "you fall asleep" in low:
            time.sleep(0.4)
            continue
        if _is_stop_confirm(raw):
            if ignore and any(d in low for d in KNOWN_DISTRACTIONS):
                send_keys("I")
                time.sleep(0.3)
            else:
                return raw
        elif "trouble sleeping" in low:
            send_keys("c")                        # "c Continue trying to fall asleep"
            time.sleep(0.3)
    return capture_raw()


def pickup_atomic(name: Optional[str]) -> Tuple[str, bool]:
    """g, optionally filter to NAME, mark the first match, confirm - the whole pickup in
    one call instead of a menu the caller has to drive."""
    raw = capture_raw()
    send_keys("g")
    raw = wait_for_change(before=raw, timeout=1.5)
    text = strip_ansi(raw)
    if "There is nothing" in text or "no items" in text.lower():
        return raw, False
    if "Pickup" not in text and "PICK UP" not in text.upper():
        return raw, False
    if name:
        send_keys("/")
        time.sleep(0.2)
        for ch in name:
            send_keys(ch)
        send_keys("Enter")
        time.sleep(0.4)
    send_keys("l")                                # mark item under the cursor
    time.sleep(0.2)
    send_keys("Enter")                            # confirm
    raw = wait_for_change(timeout=2.0)
    return raw, "pick up" in strip_ansi(raw).lower()


def consume_atomic(name: Optional[str]) -> Tuple[str, bool]:
    """E, optionally filter to NAME, consume the first match."""
    raw = capture_raw()
    send_keys("E")
    raw = wait_for_change(before=raw, timeout=1.5)
    text = strip_ansi(raw)
    if "Consume" not in text and "FOOD" not in text and "eat" not in text.lower():
        return raw, False
    if name:
        send_keys("/")
        time.sleep(0.2)
        for ch in name:
            send_keys(ch)
        send_keys("Enter")
        time.sleep(0.3)
    send_keys("Enter")
    raw = wait_for_change(timeout=2.0)
    text = strip_ansi(raw)
    return raw, ("You drink" in text or "You eat" in text or "You consume" in text)


def sleep_atomic(hours: int = 8) -> Tuple[str, bool]:
    """$, accept the sleep prompt, set an alarm, then ignore recurring distractions until
    the character wakes (or ~90s real time)."""
    raw = capture_raw()
    send_keys("$")
    raw = wait_for_change(before=raw, timeout=1.5)
    text = strip_ansi(raw)
    if "want to sleep" not in text.lower():
        if "MAIN MENU" in text or "Save and quit" in text:
            send_keys("Escape")
            wait_for_change(timeout=0.5)
        return raw, False
    send_keys("Y")
    raw = wait_for_change(timeout=1.5)
    if "alarm" in strip_ansi(raw).lower():
        send_keys(str(int(hours)) if 3 <= hours <= 9 else "N")
        wait_for_change(timeout=1.0)
    return _drain_distractions(90, ignore=True), True


def wait_atomic(spec: str) -> str:
    """| , pick a duration, and hold through recurring distractions."""
    key = WAIT_SPEC_TO_KEY.get(spec)
    if key is None:
        raise CddaError("unknown wait spec %r; try one of: %s"
                        % (spec, ", ".join(WAIT_SPEC_TO_KEY)))
    raw = capture_raw()
    send_keys("|")
    raw = wait_for_change(before=raw, timeout=1.0)
    text = strip_ansi(raw)
    # With an alarm clock (a smartphone counts), | first opens "Wait a while / Set an
    # alarm"; pick "Wait a while" to reach the duration menu. This must run BEFORE the
    # duration-menu check, or that check aborts on the alarm prompt.
    if "alarm clock" in text.lower() or "Wait a while" in text:
        send_keys("w")
        raw = wait_for_change(timeout=1.0)
        text = strip_ansi(raw)
    # Verify the duration menu is up before sending its key: otherwise that key ("2", ...)
    # would be a stray movement command in the game.
    if "wait for how long" not in text.lower() and "wait till" not in text.lower():
        raise CddaError("the wait menu did not open (are you in normal game mode?)")
    send_keys(key)
    time.sleep(0.5)
    return _drain_distractions(90, ignore=True)


def _act_pickup(args: List[str], json_mode: bool) -> int:
    raw, ok = pickup_atomic(" ".join(args).strip() or None)
    mode, data = _render(auto_clear(raw))
    _log_text(data)
    if json_mode:
        print(json.dumps({"ok": True, "picked_up": ok, "mode": mode, "data": data}))
        return 0
    print("[pickup] picked_up=%s" % ok)
    _automud()._print(data)
    return 0


def _act_consume(args: List[str], json_mode: bool) -> int:
    raw, ok = consume_atomic(" ".join(args).strip() or None)
    mode, data = _render(auto_clear(raw))
    _log_text(data)
    if json_mode:
        print(json.dumps({"ok": True, "consumed": ok, "mode": mode, "data": data}))
        return 0
    print("[consume] consumed=%s" % ok)
    _automud()._print(data)
    return 0


def _act_sleep(args: List[str], json_mode: bool) -> int:
    hours = int(args[0]) if args and args[0].isdigit() else 8
    raw, ok = sleep_atomic(hours)
    mode, data = _render(auto_clear(raw))
    _log_text(data)
    if json_mode:
        print(json.dumps({"ok": True, "slept": ok, "mode": mode, "data": data}))
        return 0
    print("[sleep] slept=%s" % ok)
    _automud()._print(data)
    return 0


def _act_wait(args: List[str], json_mode: bool) -> int:
    spec = args[0] if args else "1h"
    try:
        raw = wait_atomic(spec)
    except CddaError as exc:
        return _emit_action({"ok": False, "reason": str(exc)}, json_mode, "[wait]")
    mode, data = _render(auto_clear(raw))
    _log_text(data)
    if json_mode:
        print(json.dumps({"ok": True, "mode": mode, "data": data}))
        return 0
    print("[wait %s]" % spec)
    _automud()._print(data)
    return 0


def _act_nearby(args: List[str], json_mode: bool) -> int:
    """Open CDDA's surroundings list (V), read the items/creatures near the player, and
    leave it. Gives the blind player an adjacency scan without walking the look cursor
    tile by tile."""
    raw = capture_raw()
    send_keys("V")
    raw = wait_for_change(before=raw, timeout=1.5)
    lines = _parse_surroundings(raw)
    send_keys("Escape")
    wait_for_change(timeout=1.0)
    return _emit_action({"ok": True, "lines": lines or ["(nothing listed nearby)"]},
                        json_mode, "[nearby]")


def _act_help(args: List[str], json_mode: bool) -> int:
    result = {
        "ok": True,
        "directions": "north south east west ne nw se sw (or CDDA hjkl yubn)",
        "actions": ", ".join(sorted(GAME_ACTIONS)),
        "named_keys": ", ".join(sorted(NAMED_KEYS)),
        "info": "look/nearby for surroundings, state for vitals, examine <dir>. "
                "High-level (drive the whole flow): pickup [NAME], eat [NAME], "
                "wait <20s|1m|5m|30m|1h|2h|3h|6h|daylight|noon|night>, sleep [HOURS]. "
                "Single letters are sent literally.",
        "chargen": "newgame (menu -> character creation) | options [TAB] (list every "
                   "choice) | scenario NAME | profession NAME | background NAME | "
                   "stats STR DEX INT PER | trait NAME | name NAME | finalize",
        "text_entry": "type WORDS  (types literal text into an input prompt)",
    }
    return _emit_action(result, json_mode, "[help]")


def _act_type(args: List[str], json_mode: bool) -> int:
    for ch in " ".join(args):
        send_keys(ch)
        time.sleep(0.03)
    raw = auto_clear(capture_raw())
    mode, data = _render(raw)
    _log_text(data)
    return _emit(data, mode, json_mode)


def _act_chargen_filter(kind: str, args: List[str], json_mode: bool) -> int:
    name = " ".join(args).strip()
    if not name:
        print("%s: give a name, e.g. 'send %s Sheltered'" % (kind, kind))
        return 2
    tab = {"scenario": "SCENARIO", "profession": "PROFESSION",
           "background": "BACKGROUND"}[kind]
    if not _goto_tab(tab):
        return _emit_action({"ok": False, "reason": "could not reach %s tab" % tab},
                            json_mode)
    res = chargen_filter_commit(name)
    res["ok"] = res.get("ok", False)
    return _emit_action(res, json_mode, "[%s]" % kind)


def _act_stats(args: List[str], json_mode: bool) -> int:
    if len(args) < 4 or not all(a.lstrip("-").isdigit() for a in args[:4]):
        print("stats: give four integers, e.g. 'send stats 8 10 10 10' (STR DEX INT PER)")
        return 2
    vals = {"str": int(args[0]), "dex": int(args[1]),
            "int": int(args[2]), "per": int(args[3])}
    res = chargen_set_stats(vals)
    return _emit_action(res, json_mode, "[stats]")


def _act_trait(args: List[str], json_mode: bool) -> int:
    name = " ".join(args).strip()
    if not name:
        print("trait: give a trait name, e.g. 'send trait Fleet-Footed'")
        return 2
    if not _goto_tab("TRAITS"):
        return _emit_action({"ok": False, "reason": "could not reach TRAITS tab"},
                            json_mode)
    res = trait_toggle(name)
    return _emit_action(res, json_mode, "[trait]")


def _act_name(args: List[str], json_mode: bool) -> int:
    name = " ".join(args).strip()
    if not name:
        print("name: give a character name, e.g. 'send name Dougal'")
        return 2
    res = chargen_set_name(name)
    res["ok"] = res.get("name") is not None
    return _emit_action(res, json_mode, "[name]")


def _act_finalize(args: List[str], json_mode: bool) -> int:
    res = chargen_finalize()
    return _emit_action(res, json_mode, "[finalize]")


def _act_options(args: List[str], json_mode: bool) -> int:
    tab = args[0] if args else None
    try:
        res = chargen_list_options(tab)
    except CddaError as exc:
        return _emit_action({"ok": False, "reason": str(exc)}, json_mode, "[options]")
    if json_mode:
        print(json.dumps({"ok": True, **res}))
        return 0
    print("[options] %s (%d)" % (res.get("tab"), res.get("count", 0)))
    if res.get("tab") == "TRAITS":
        for pane in ("positive", "negative", "cosmetic"):
            print("--- %s ---" % pane)
            for name in res.get(pane, []):
                print("  " + name)
    else:
        for name in res.get("options", []):
            print("  " + name)
    return 0


def _act_newgame(args: List[str], json_mode: bool) -> int:
    try:
        newgame_reach_chargen()
    except CddaError as exc:
        return _emit_action({"ok": False, "reason": str(exc)}, json_mode, "[newgame]")
    summary = chargen_summary()
    summary["ok"] = str(summary.get("mode", "")).startswith("chargen")
    return _emit_action(summary, json_mode, "[newgame]")


# High-level send actions that take arguments and return a structured result rather than
# driving raw keys. These are what make class/stat/scenario selection a one-liner.
_SPECIAL_ACTIONS = {
    "newgame": lambda a, j: _act_newgame(a, j),
    "options": lambda a, j: _act_options(a, j),
    "list": lambda a, j: _act_options(a, j),
    "pickup": lambda a, j: _act_pickup(a, j),
    "grab": lambda a, j: _act_pickup(a, j),
    "get": lambda a, j: _act_pickup(a, j),
    "take": lambda a, j: _act_pickup(a, j),
    "eat": lambda a, j: _act_consume(a, j),
    "drink": lambda a, j: _act_consume(a, j),
    "consume": lambda a, j: _act_consume(a, j),
    "sleep": lambda a, j: _act_sleep(a, j),
    "wait": lambda a, j: _act_wait(a, j),
    "nearby": lambda a, j: _act_nearby(a, j),
    "surroundings": lambda a, j: _act_nearby(a, j),
    "help": lambda a, j: _act_help(a, j),
    "type": lambda a, j: _act_type(a, j),
    "scenario": lambda a, j: _act_chargen_filter("scenario", a, j),
    "profession": lambda a, j: _act_chargen_filter("profession", a, j),
    "class": lambda a, j: _act_chargen_filter("profession", a, j),
    "background": lambda a, j: _act_chargen_filter("background", a, j),
    "stats": lambda a, j: _act_stats(a, j),
    "trait": lambda a, j: _act_trait(a, j),
    "name": lambda a, j: _act_name(a, j),
    "finalize": lambda a, j: _act_finalize(a, j),
}


def verb_send(text: str, maxw: float, json_mode: bool) -> int:
    if _require_session(json_mode) is None:
        return 1
    tokens = [t for t in re.split(r"[\s,]+", text.strip()) if t]
    if not tokens:                                # blank send = press Enter (answer prompts)
        tokens = ["enter"]
    head = tokens[0].lower()
    if head in _SPECIAL_ACTIONS:
        return _SPECIAL_ACTIONS[head](tokens[1:], json_mode)
    keys, unknown = [], []
    for token in tokens:
        key = _token_key(token)
        (unknown if key is None else keys).append(token if key is None else key)
    if unknown:
        hint = ("unknown action(s): %s. Valid: directions (north...), actions (%s), "
                "'help' for the full list, or single keys." %
                (", ".join(unknown), ", ".join(sorted(GAME_ACTIONS))))
        print(json.dumps({"ok": False, "error": hint}) if json_mode
              else "send: " + hint)
        return 2
    raw = _drive_keys(keys, timeout=max(1.0, min(maxw, 5.0)))
    mode, data = _render(raw)
    _log_text(data)
    return _emit(data, mode, json_mode)


def verb_recv(maxw: float, block: bool, json_mode: bool) -> int:
    if _require_session(json_mode) is None:
        return 1
    raw = wait_for_change(timeout=maxw) if block else capture_raw()
    raw = auto_clear(raw)
    mode, data = _render(raw)
    _log_text(data)
    return _emit(data, mode, json_mode)


def verb_wait(pattern: Optional[str], maxw: float, json_mode: bool) -> int:
    if _require_session(json_mode) is None:
        return 1
    if not pattern:
        print("wait: cdda supports --for REGEX")
        return 2
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        print(json.dumps({"ok": False, "error": "bad regex: %s" % exc}) if json_mode
              else "wait failed: bad regex: %s" % exc)
        return 1
    start = time.time()
    while time.time() - start < maxw:
        raw = auto_clear(capture_raw())
        mode, data = _render(raw)
        if rx.search(data) or rx.search(strip_ansi(raw)):
            _log_text(data)
            if json_mode:
                print(json.dumps({"ok": True, "matched": True, "mode": mode,
                                  "data": data, "connected": session_alive()}))
            else:
                _automud()._print(data)
            return 0
        time.sleep(0.3)
    if json_mode:
        print(json.dumps({"ok": True, "matched": False, "data": ""}))
    else:
        print("wait: no match within %gs" % maxw, file=sys.stderr)
    return 1


def verb_state(key: Optional[str], json_mode: bool) -> int:
    if _require_session(json_mode) is None:
        return 1
    raw = capture_raw()
    if detect_mode(raw).startswith("chargen"):
        state = chargen_summary()
    else:
        state = character_state(raw)
    if key:
        state = {key: state.get(key)}
    print(json.dumps(state) if json_mode else json.dumps(state, indent=2))
    return 0


def verb_status(json_mode: bool) -> int:
    meta = _require_session(json_mode)
    if meta is None:
        return 1
    state = character_state()
    alive = session_alive()
    if json_mode:
        print(json.dumps({"ok": True, "backend": "cdda", "connected": alive,
                          "tmux_session": SESSION, "mode": state.get("mode"),
                          "state": state}))
    else:
        print("backend=cdda connected=%s session=%s mode=%s %s"
              % (alive, SESSION, state.get("mode"), _status_line(state)))
    return 0


def verb_close(json_mode: bool) -> int:
    meta = _session_meta()
    if not meta or meta.get("backend") != "cdda":
        print(json.dumps({"ok": False, "error": "no cdda session"}) if json_mode
              else "no cdda session")
        return 1
    configure(meta.get("tmux_session") or am_session_name(), meta.get("distro") or None)
    try:
        kill()
    except CddaError:
        pass
    try:
        os.remove(_automud().SESSION_JSON)
    except OSError:
        pass
    print(json.dumps({"ok": True}) if json_mode else "closed")
    return 0


def verb_raw(json_mode: bool) -> int:
    if _require_session(json_mode) is None:
        return 1
    raw = capture_raw()
    if json_mode:
        print(json.dumps({"ok": True, "raw": raw}))
    else:
        sys.stdout.write(raw if raw.endswith("\n") else raw + "\n")
    return 0


def _emit(data: str, mode: str, json_mode: bool) -> int:
    am = _automud()
    connected = session_alive()
    if json_mode:
        print(json.dumps({"ok": True, "mode": mode, "data": data,
                          "connected": connected}))
    else:
        am._print(data)
    return 0 if connected else 3


def dispatch(args: Any) -> int:
    """Route an already-parsed automud args namespace to the cdda backend. Only the verbs
    that make sense locally are handled; the rest fall through in automud.py."""
    cmd = args.cmd
    json_mode = getattr(args, "json", False)
    maxw = getattr(args, "max", 5.0)
    if cmd == "send":
        text = " ".join(getattr(args, "text", []) or [])
        if getattr(args, "stdin", False):
            text = sys.stdin.read().strip()
        return verb_send(text, maxw, json_mode)
    if cmd == "recv":
        return verb_recv(maxw, not getattr(args, "nowait", False), json_mode)
    if cmd == "wait":
        return verb_wait(getattr(args, "pattern", None), maxw, json_mode)
    if cmd == "state":
        return verb_state(getattr(args, "key", None), json_mode)
    if cmd == "status":
        return verb_status(json_mode)
    if cmd == "close":
        return verb_close(json_mode)
    if cmd == "kill":
        return verb_close(json_mode)
    if cmd == "log":
        return _automud().cmd_log(tail=getattr(args, "tail", 0))
    raise CddaError("cdda backend has no verb %r" % cmd)


# --------------------------------------------------------------------------- standalone

def _main() -> None:
    """Thin CLI for driving/testing the backend directly (outside automud)."""
    argv = sys.argv[1:]
    if argv and argv[0] == "parse_fixture":
        with open(argv[1], "r", encoding="utf-8") as fh:
            raw = fh.read()
        mode = detect_mode(raw)
        print("[%s]" % mode)
        for line in parse(raw, mode):
            print(line)
        return
    configure(os.environ.get("AUTOMUD_CDDA_SESSION", SESSION))
    if not argv or argv[0] == "capture":
        raw = capture_raw()
        mode = detect_mode(raw)
        print("[%s]" % mode)
        for line in parse(raw, mode):
            print(line)
    elif argv[0] == "raw":
        sys.stdout.write(capture_raw())
    elif argv[0] == "character":
        print(json.dumps(character_state(), indent=2))
    elif argv[0] == "do":
        keys = [k for k in (_token_key(t) for t in argv[1:]) if k is not None]
        raw = _drive_keys(keys)
        mode = detect_mode(raw)
        print("[%s]" % mode)
        for line in parse(raw, mode):
            print(line)
    elif argv[0] == "send":
        send_keys(*argv[1:])
    else:
        print("usage: automud_cdda [capture|raw|character|do KEYS|send KEYS|"
              "parse_fixture PATH]")


if __name__ == "__main__":
    _main()
