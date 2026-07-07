"""
AutoMUD: a persistent telnet/MUD session you drive by hand (or an agent drives).

There is no LLM in here and no API key. The intelligence is whoever runs it: a person, a
script, or an autonomous agent. It solves the parts of a live MUD connection that a
normal shell can't: a raw telnet session is interactive and blocking, so it can't be held
across separate commands. A small background daemon keeps the connection open and you talk
to it with discrete verbs:

    automud sites                      # directory of verified public targets
    automud connect achaea             # by name, or: automud connect achaea.com 23
    automud send 2                     # send a line, print the reply
    automud send Maelvorn
    automud recv                        # drain any new output
    automud wait --for "You have died"  # block until the output matches a regex
    automud state                       # structured game state (GMCP) as JSON
    automud status
    automud close

What the daemon does for you:
  * Smart waiting: send/recv return as soon as the server stops talking (an IAC GA/EOR
    prompt marker, or output going quiet), so you never guess a sleep duration. --max caps
    the wait; --quiet sets the idle threshold.
  * Prompt aware: it treats the telnet GA/EOR marker most MUDs send after a prompt as the
    "your turn" signal, and refuses Suppress-Go-Ahead so that marker keeps flowing.
  * GMCP capture: it negotiates GMCP and parses the structured state modern MUDs push
    (Char.Vitals, Room.Info, etc.) into JSON you can read with `state`. Standard list
    deltas (Room.AddPlayer, Char.Afflictions.Add, ...) are applied to their lists, and
    Comm.Channel.Text is kept as a bounded history. Options it does not implement
    (compression, MSDP, MXP) are refused rather than mishandled; TTYPE, NAWS and CHARSET
    are answered.
  * Encodings: --encoding sets the wire charset (default utf-8); telnet CHARSET
    negotiation can switch it when the server asks.
  * TLS: --tls wraps the connection (--tls-insecure skips certificate checks for MUDs
    with self-signed certs).

An agent driving these verbs can also be its user's entire interface to the game: the
user names a setting in conversation, and the agent re-voices both directions live,
intent into real commands and raw replies into the agreed fiction (a total conversion,
with no mode or flag in here). Ask for the theme rather than inventing it, let the
server's actual replies decide events, keep converted names stable, and remember that
out.log stays the untranslated record.

The control channel is a localhost-only socket gated by a per-session token kept in a
private (0700) per-user state directory, so only processes running as you can drive the
session. The daemon pid is tracked: stale sessions are detected and cleared, and `kill`
force-stops a wedged daemon.

Exit codes: 0 success; 1 failure; 2 usage error; 3 the operation succeeded but the MUD
connection is closed.

Config (optional):
    AUTOMUD_DIR : state directory base (default: $XDG_RUNTIME_DIR/automud, else
                  <tempdir>/automud-<uid> on POSIX, <tempdir>/automud on Windows).
                  Each --session NAME lives in its own subdirectory.
"""

import argparse
import asyncio
import codecs
import json
import os
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

__version__ = "0.3.5"


# ------------------------------ state directory ------------------------------

def _default_base() -> str:
    if os.name != "nt":
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            return os.path.join(xdg, "automud")
        return os.path.join(tempfile.gettempdir(), "automud-%d" % os.getuid())
    return os.path.join(tempfile.gettempdir(), "automud")


STATE_BASE = os.environ.get("AUTOMUD_DIR") or _default_base()

# Set by _set_state_dir(); one directory per session name.
STATE_DIR = ""
SESSION_JSON = ""
OUT_LOG = ""
OUT_PREV_LOG = ""
DAEMON_LOG = ""
CONNECT_LOCK = ""


def _set_state_dir(d: str) -> None:
    global STATE_DIR, SESSION_JSON, OUT_LOG, OUT_PREV_LOG, DAEMON_LOG, CONNECT_LOCK
    STATE_DIR = d
    SESSION_JSON = os.path.join(d, "session.json")
    OUT_LOG = os.path.join(d, "out.log")
    OUT_PREV_LOG = os.path.join(d, "out.prev.log")
    DAEMON_LOG = os.path.join(d, "daemon.log")
    CONNECT_LOCK = os.path.join(d, "connect.lock")


_set_state_dir(os.path.join(STATE_BASE, "default"))

# Keep at most this many characters of received text in memory (the full stream still goes
# to OUT_LOG, which `log` reads). Trimming drops the oldest buffered text once the buffer
# exceeds the cap: it prefers already-read history, but a single burst larger than the cap
# also drops not-yet-read bytes from the in-memory buffer (they survive in OUT_LOG). The
# read cursor is advanced past anything dropped so nothing is ever re-served.
BUFFER_CAP = 1_000_000

# Bound on the Comm.Channel.Text history kept under the synthetic Comm.Channel.History key.
COMM_HISTORY_CAP = 200

# A trailing incomplete escape sequence is held back until the next chunk completes it,
# but never more than this many characters (so a never-terminated OSC can't pin output).
PENDING_CAP = 4096

# Suggested public targets, every one verified reachable by a live probe (last run
# 2026-07-01). `connect NAME` or `connect --demo NAME` resolves them; `sites` lists them.
SITES = {
    # --- MUDs ---
    "achaea": ("achaea.com", 23, "Iron Realms fantasy MUD; rich GMCP"),
    "aetolia": ("aetolia.com", 23, "IRE gothic/vampire MUD; GMCP"),
    "aardwolf": ("aardmud.org", 4000, "huge, very active hack-and-slash; rich GMCP"),
    "batmud": ("batmud.bat.org", 23, "Finnish giant, running since 1990"),
    "discworld": ("discworld.starturtle.net", 4242, "Pratchett's Discworld LPMud"),
    "3k": ("3k.org", 3000, "3Kingdoms: long-running LPMud, three realms"),
    "3scapes": ("3scapes.org", 3200, "3Kingdoms' sister LPMud"),
    "alteraeon": ("alteraeon.com", 3000, "active Diku descendant, blind-player friendly"),
    "materiamagica": ("materiamagica.com", 4000, "polished quest-heavy fantasy"),
    "legendmud": ("mud.legendmud.org", 9999, "historical-eras theme, est. 1994"),
    "medievia": ("medievia.com", 4000, "big classic Diku-style world"),
    "realmsofdespair": ("realmsofdespair.com", 4000, "home of the SMAUG codebase"),
    "ancientanguish": ("ancient.anguish.org", 2222, "cozy LPMud, running since 1992"),
    "twotowers": ("t2tmud.org", 9999, "Tolkien Third Age MUD"),
    "mume": ("mume.org", 4242, "Multi-Users in Middle-earth, hardcore PvP"),
    "toril": ("torilmud.org", 9999, "Forgotten Realms, EverQuest's ancestor"),
    "genesis": ("mud.genesismud.org", 3011, "original LPMud lineage, est. 1989"),
    "nannymud": ("mud.lysator.liu.se", 2000, "Swedish LPMud, running since 1990"),
    "threshold": ("thresholdrpg.com", 3333, "roleplay-enforced fantasy, est. 1996"),
    "dsl": ("dsl-mud.org", 4000, "Dark & Shattered Lands, ROM PK/RP"),
    "miriani": ("toastsoft.net", 1234, "space sim MOO, starship crews"),
    "fed2": ("play.federation2.com", 30003, "space trading game from 1985"),
    "lotj": ("legendsofthejedi.com", 5656, "Star Wars, era-based storytelling"),
    "morgengrauen": ("mg.mud.de", 4711, "large German-language LPMud"),
    "ateraan": ("www.ateraan.com", 4002, "New Worlds: Ateraan, roleplay fantasy"),
    "luminari": ("luminarimud.com", 4100, "Pathfinder/d20-flavored MUD"),
    "empiremud": ("empiremud.net", 4000, "empire-building MUD"),
    "igor": ("igormud.org", 1701, "quirky LPMud, running since 1992"),
    "elephant": ("elephant.org", 23, "Elephant MUD LPMud"),
    "sloth": ("slothmud.org", 6101, "SlothMUD III, Diku since 1994"),
    # --- MOOs ---
    "lambdamoo": ("lambda.moo.mud.org", 8888, "the legendary social MOO"),
    "sindome": ("moo.sindome.org", 5555, "cyberpunk roleplay MOO"),
    # --- services ---
    "telehack": ("telehack.com", 23, "simulated 1980s ARPANET playground"),
    "zork": ("telehack.com", 23, "Zork on Telehack (type 'zork')"),
    "fics": ("freechess.org", 5000, "Free Internet Chess Server"),
    "chess": ("freechess.org", 5000, "alias of fics"),
    "mapscii": ("mapscii.me", 23, "zoomable ASCII world map"),
    "starwars": ("towel.blinkenlights.nl", 23, "Star Wars ASCIImation stream"),
    "nethack": ("nethack.alt.org", 23, "NetHack public server (NAO)"),
    "horizons": ("ssd.jpl.nasa.gov", 6775, "JPL Horizons ephemeris system"),
    "sdf": ("sdf.org", 23, "SDF public-access Unix"),
    "mtrek": ("mtrek.com", 1701, "multiplayer Star Trek combat"),
    "nist-time": ("time.nist.gov", 13, "NIST daytime; prints the time, disconnects"),
    # --- BBSes ---
    "fozztexx": ("bbs.fozztexx.com", 23, "retro BBS"),
    "particles": ("particlesbbs.dyndns.org", 6400, "Particles! BBS (Commodore)"),
    "20forbeers": ("20forbeers.com", 1337, "20 For Beers BBS"),
    "cavebbs": ("cavebbs.homeip.net", 23, "The Cave BBS, Synchronet"),
}

# Telnet command bytes
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
GA, EOR_CMD = 249, 239
# Telnet options
OPT_TTYPE, OPT_EOR, OPT_NAWS, OPT_CHARSET, OPT_GMCP = 24, 25, 31, 42, 201
# Options we ask the server to enable. GMCP gives structured state; EOR gives prompt
# markers; CHARSET lets the server pick a wire encoding. We deliberately do NOT request
# SGA (option 3): suppressing Go-Ahead would kill the other prompt marker. Everything not
# listed here is refused, which also keeps us from accidentally enabling compression
# (MCCP) and turning the stream into zlib garbage.
WANT_DO = {OPT_GMCP, OPT_EOR, OPT_CHARSET}
# Options we agree to enable on our side when the server asks (some servers gate features
# or hold menus until terminal negotiation answers).
WILL_US = {OPT_TTYPE, OPT_NAWS}

CHARSET_REQUEST, CHARSET_ACCEPTED, CHARSET_REJECTED = 1, 2, 3
TTYPE_IS, TTYPE_SEND = 0, 1
TTYPE_CYCLE = ("AUTOMUD", "ANSI", "MTTS 1")
NAWS_COLS, NAWS_ROWS = 120, 40

_OPT_NAMES = {1: "ECHO", 3: "SGA", 24: "TTYPE", 25: "EOR", 31: "NAWS", 42: "CHARSET",
              69: "MSDP", 70: "MSSP", 85: "MCCP1", 86: "MCCP2", 90: "MSP", 91: "MXP",
              200: "ATCP", 201: "GMCP"}


def _optname(opt: int) -> str:
    return _OPT_NAMES.get(opt, str(opt))


# CSI, OSC (BEL- or ST-terminated), DCS/SOS/PM/APC (ST-terminated), charset shifts,
# other single-char escapes, and stray control characters (not \t \n \r).
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[PX^_][^\x1b]*\x1b\\"
    r"|\x1b[()][AB012]"
    r"|\x1b[=>78@-Z\\-_]"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f]"
)
# A trailing, still-incomplete escape sequence (no final byte / terminator yet). Held back
# so a sequence split across two TCP reads is completed before it is emitted or stripped.
_ANSI_INCOMPLETE = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*|\][^\x07\x1b]*\x1b?|[PX^_][^\x1b]*\x1b?|[()])?$"
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


# ------------------------------ process helpers ------------------------------

def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. Never signals the process."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows TERMINATES the process, so query via the API.
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True     # can't tell; assume alive rather than clearing a live session
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _kill_pid(pid: int) -> None:
    """Terminate a daemon: SIGTERM, then SIGKILL if it lingers (POSIX)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# ------------------------------ session state files ------------------------------

def _ensure_dir() -> None:
    """Create the state directory, private to this user. On POSIX, refuse to use a
    directory owned by someone else (a predictable path in /tmp can be pre-created by
    another local user, who could then swap session.json under us)."""
    for d in (os.path.dirname(STATE_DIR), STATE_DIR):
        os.makedirs(d, exist_ok=True)
        if os.name != "nt":
            st = os.stat(d)
            if st.st_uid != os.geteuid():
                raise RuntimeError(
                    "state dir %s is owned by uid %d, not you (uid %d); "
                    "point AUTOMUD_DIR somewhere private" % (d, st.st_uid, os.geteuid()))
            os.chmod(d, 0o700)


def _harden(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)        # no-op on Windows; %TEMP% is already per-user there
    except OSError:
        pass


def _write_session(data: dict) -> None:
    _ensure_dir()
    tmp = SESSION_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    _harden(tmp, 0o600)
    os.replace(tmp, SESSION_JSON)


def _read_session() -> Optional[dict]:
    try:
        with open(SESSION_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _acquire_connect_lock() -> None:
    """One connect at a time per session dir, so racing connects can't strand a daemon
    whose control port was never recorded. Stale locks (dead pid) are reclaimed."""
    for _ in range(2):
        try:
            fd = os.open(CONNECT_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return
        except FileExistsError:
            try:
                with open(CONNECT_LOCK, "r", encoding="ascii") as f:
                    pid = int(f.read().strip() or "0")
            except Exception:
                pid = 0
            if pid and _pid_alive(pid):
                raise RuntimeError("another connect is in progress (pid %d)" % pid)
            try:
                os.remove(CONNECT_LOCK)
            except OSError:
                pass
    raise RuntimeError("could not acquire connect lock")


def _release_connect_lock() -> None:
    try:
        os.remove(CONNECT_LOCK)
    except OSError:
        pass


# ------------------------------ GMCP state ------------------------------

# Standard list-delta packages: applied to their canonical list so `state` reflects
# current membership instead of the last event.
_GMCP_LIST_DELTAS = {
    "Char.Afflictions.Add": ("Char.Afflictions.List", True),
    "Char.Afflictions.Remove": ("Char.Afflictions.List", False),
    "Char.Defences.Add": ("Char.Defences.List", True),
    "Char.Defences.Remove": ("Char.Defences.List", False),
    "Room.AddPlayer": ("Room.Players", True),
    "Room.RemovePlayer": ("Room.Players", False),
}


def _entry_name(e: Any) -> Any:
    return e.get("name") if isinstance(e, dict) else e


def _apply_gmcp(state: dict, package: str, value: Any) -> None:
    now = time.monotonic()
    gmcp = state["gmcp"]
    gmcp[package] = value
    state["gmcp_n"] += 1
    state["gmcp_seq"][package] = state["gmcp_n"]
    state["gmcp_time"][package] = now
    if package == "Comm.Channel.Text":
        hist = gmcp.setdefault("Comm.Channel.History", [])
        hist.append(value)
        del hist[:-COMM_HISTORY_CAP]
        state["gmcp_seq"]["Comm.Channel.History"] = state["gmcp_n"]
        state["gmcp_time"]["Comm.Channel.History"] = now
        return
    delta = _GMCP_LIST_DELTAS.get(package)
    if delta:
        target_key, is_add = delta
        target = gmcp.get(target_key)
        if not isinstance(target, list):
            target = gmcp[target_key] = []
        if is_add:
            target.append(value)
        else:
            removed = value if isinstance(value, list) else [value]
            names = {_entry_name(n) for n in removed}
            gmcp[target_key] = [e for e in target if _entry_name(e) not in names]
        state["gmcp_seq"][target_key] = state["gmcp_n"]
        state["gmcp_time"][target_key] = now


# ------------------------------ telnet / GMCP parser ------------------------------

class MudConn:
    """Minimal telnet client: separates plain text from IAC control, answers option
    negotiation (only on state change, so it can't loop), captures GMCP subnegotiation as
    JSON, answers TTYPE/NAWS/CHARSET, and flags GA/EOR prompt markers.

    Receipt accounting uses monotonic counters, not buffer indices, so trimming the
    in-memory buffer never disturbs the read cursor or the wait logic:
      total : chars ever received
      read  : chars handed to the client
      base  : chars dropped off the front of `buffer` (buffer == stream[base:total])
    """

    def __init__(self, writer: asyncio.StreamWriter, state: dict, log_fh,
                 encoding: str = "utf-8", debug: bool = False):
        self.w = writer
        self.s = state
        self.log = log_fh
        self.debug = debug
        try:
            self.enc = codecs.lookup(encoding).name
        except LookupError:
            self.enc = "utf-8"
        self.mode = "text"          # text | iac | neg | sb | sbiac
        self.cmd: Optional[int] = None
        self.sb_opt: Optional[int] = None
        self.sb = bytearray()
        self.text = bytearray()
        self.him: Dict[int, Optional[bool]] = {}    # server-side option enabled?
        self.us: Dict[int, Optional[bool]] = {}     # our-side option enabled?
        self._ttype_idx = 0
        self._decoder = codecs.getincrementaldecoder(self.enc)("replace")
        self._pending = ""          # decoded text held back across reads (partial escape)

    def _dbg(self, msg: str) -> None:
        if self.debug:
            print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)

    # ---- byte stream ----
    def feed(self, data: bytes) -> None:
        i, n = 0, len(data)
        while i < n:
            if self.mode == "text":
                k = data.find(IAC, i)
                if k == -1:
                    self.text += data[i:n]
                    break
                if k > i:
                    self.text += data[i:k]
                self.mode = "iac"
                i = k + 1
            else:
                self._byte(data[i])
                i += 1
        self._flush_text()

    def _byte(self, b: int) -> None:
        m = self.mode
        if m == "iac":
            if b == IAC:
                self.text.append(IAC)            # escaped 0xFF
                self.mode = "text"
            elif b in (DO, DONT, WILL, WONT):
                self.cmd = b
                self.mode = "neg"
            elif b == SB:
                self.sb_opt = None
                self.sb = bytearray()
                self.mode = "sb"
            elif b in (GA, EOR_CMD):
                self._prompt()
                self.mode = "text"
            else:
                self.mode = "text"               # other 1-byte commands: ignore
        elif m == "neg":
            self._negotiate(self.cmd, b)
            self.mode = "text"
        elif m == "sb":
            if b == IAC:
                self.mode = "sbiac"
            elif self.sb_opt is None:
                self.sb_opt = b
            else:
                self.sb.append(b)
        elif m == "sbiac":
            if b == IAC:
                self.sb.append(IAC)              # escaped 0xFF inside SB
                self.mode = "sb"
            elif b == SE:
                self._subneg(self.sb_opt, bytes(self.sb))
                self.mode = "text"
            else:
                self.mode = "text"               # malformed; resync

    # ---- handlers ----
    def _append(self, s: str) -> None:
        self.s["buffer"] += s
        self.s["total"] += len(s)
        self.s["last_rx"] = time.monotonic()
        if self.log is not None:
            try:
                self.log.write(s)
                self.log.flush()
            except Exception:
                pass
        buf = self.s["buffer"]
        if len(buf) > BUFFER_CAP:
            drop = len(buf) - BUFFER_CAP
            self.s["buffer"] = buf[drop:]
            self.s["base"] += drop
            if self.s["read"] < self.s["base"]:          # dropped some unread; don't re-serve it
                self.s["read"] = self.s["base"]

    def _flush_text(self) -> None:
        if not self.text:
            return
        # Decode incrementally so a multibyte char split across reads is completed, not
        # turned into a replacement char.
        self._pending += self._decoder.decode(bytes(self.text))
        self.text = bytearray()
        emit = self._pending
        # Hold back a trailing incomplete escape so a sequence split across reads isn't
        # emitted or mis-stripped; it completes on the next chunk. Capped so a
        # never-terminated sequence can't pin the stream.
        m = _ANSI_INCOMPLETE.search(emit)
        if m and len(emit) - m.start() <= PENDING_CAP:
            self._pending = emit[m.start():]
            emit = emit[:m.start()]
        else:
            self._pending = ""
        s = strip_ansi(emit)
        if s:
            self._append(s)

    def _prompt(self) -> None:
        self._flush_text()
        self.s["prompt_seen"] = True
        self.s["last_rx"] = time.monotonic()

    def _raw(self, *seq: int) -> None:
        try:
            self.w.write(bytes(seq))
        except Exception:
            pass

    def _raw_sb(self, opt: int, payload: bytes) -> None:
        try:
            self.w.write(bytes([IAC, SB, opt])
                         + payload.replace(b"\xff", b"\xff\xff") + bytes([IAC, SE]))
        except Exception:
            pass

    def _negotiate(self, cmd: int, opt: int) -> None:
        # Respond only when the option's state actually changes, per the telnet Q-method,
        # so a server that re-announces options can't make us loop.
        self._dbg("neg: %s %s" % ({DO: "DO", DONT: "DONT", WILL: "WILL", WONT: "WONT"}.get(cmd, cmd),
                                  _optname(opt)))
        if cmd == WILL:
            want = opt in WANT_DO
            if want and not self.him.get(opt):
                self.him[opt] = True
                self._raw(IAC, DO, opt)
                if opt == OPT_GMCP:
                    self._gmcp_hello()
            elif not want and self.him.get(opt) is not False:
                self.him[opt] = False
                self._raw(IAC, DONT, opt)
        elif cmd == WONT:
            if self.him.get(opt) is not False:
                self.him[opt] = False
                self._raw(IAC, DONT, opt)
        elif cmd == DO:
            if opt in WILL_US:
                if not self.us.get(opt):
                    self.us[opt] = True
                    self._raw(IAC, WILL, opt)
                    self._us_enabled(opt)
            elif self.us.get(opt) is not False:
                self.us[opt] = False
                self._raw(IAC, WONT, opt)
        elif cmd == DONT:
            if self.us.get(opt) is not False:
                self.us[opt] = False
                self._raw(IAC, WONT, opt)

    def _us_enabled(self, opt: int) -> None:
        if opt == OPT_NAWS:
            self._raw_sb(OPT_NAWS, bytes([NAWS_COLS >> 8, NAWS_COLS & 0xFF,
                                          NAWS_ROWS >> 8, NAWS_ROWS & 0xFF]))
        elif opt == OPT_TTYPE:
            self._ttype_idx = 0

    def _send_gmcp(self, package: str, payload: str) -> None:
        msg = (package + " " + payload).encode("utf-8") if payload else package.encode("utf-8")
        self._raw_sb(OPT_GMCP, msg)

    def _gmcp_hello(self) -> None:
        self._send_gmcp("Core.Hello",
                        json.dumps({"client": "AutoMUD", "version": __version__}))
        self._send_gmcp("Core.Supports.Set",
                        '["Char 1","Char.Vitals 1","Char.Status 1","Char.Skills 1",'
                        '"Char.Afflictions 1","Char.Defences 1","Room 1","Comm.Channel 1"]')

    def _subneg(self, opt: int, payload: bytes) -> None:
        if opt == OPT_GMCP:
            self._gmcp_in(payload)
        elif opt == OPT_TTYPE:
            if payload[:1] == bytes([TTYPE_SEND]):
                name = TTYPE_CYCLE[min(self._ttype_idx, len(TTYPE_CYCLE) - 1)]
                self._ttype_idx += 1
                self._dbg("ttype: sending %r" % name)
                self._raw_sb(OPT_TTYPE, bytes([TTYPE_IS]) + name.encode("ascii"))
        elif opt == OPT_CHARSET:
            self._charset(payload)
        # MSDP/MXP/etc.: ignore, don't choke

    def _gmcp_in(self, payload: bytes) -> None:
        text = payload.decode("utf-8", "replace")
        sp = text.find(" ")
        if sp == -1:
            package, body = text.strip(), ""
        else:
            package, body = text[:sp].strip(), text[sp + 1:]
        value: Any = None
        if body.strip():
            try:
                value = json.loads(body)
            except Exception:
                value = body
        if package:
            self._dbg("gmcp: %s" % package)
            _apply_gmcp(self.s, package, value)

    def _charset(self, payload: bytes) -> None:
        if payload[:1] != bytes([CHARSET_REQUEST]):
            return
        body = payload[1:]
        if body.startswith(b"[TTABLE]"):
            body = body[len(b"[TTABLE]") + 1:]           # skip literal + version byte
        if not body:
            self._raw_sb(OPT_CHARSET, bytes([CHARSET_REJECTED]))
            return
        sep, names = body[0:1], [n for n in body[1:].split(body[0:1]) if n]
        offered: List[Tuple[bytes, str]] = []
        for n in names:
            try:
                offered.append((n, codecs.lookup(n.decode("ascii", "replace").strip()).name))
            except Exception:
                continue
        chosen = None
        for n, cname in offered:                          # prefer what we're already using
            if cname == self.enc:
                chosen = (n, cname)
                break
        if chosen is None:
            for n, cname in offered:                      # then utf-8
                if cname == "utf-8":
                    chosen = (n, cname)
                    break
        if chosen is None and offered:
            chosen = offered[0]
        if chosen is None:
            self._dbg("charset: rejected %r" % names)
            self._raw_sb(OPT_CHARSET, bytes([CHARSET_REJECTED]))
            return
        raw_name, cname = chosen
        self._dbg("charset: accepted %s" % cname)
        self._raw_sb(OPT_CHARSET, bytes([CHARSET_ACCEPTED]) + raw_name)
        if cname != self.enc:
            self._pending += self._decoder.decode(b"", final=True)
            self._decoder = codecs.getincrementaldecoder(cname)("replace")
            self.enc = cname


# ------------------------------ daemon ------------------------------

async def _wait_settled(state: dict, since_total: int, quiet: float, maxw: float) -> None:
    """Block until the server stops talking: a prompt marker arrived, or output produced
    after `since_total` went idle for `quiet` seconds. Capped at `maxw`."""
    start = time.monotonic()
    # phase 1: wait for genuinely new output (or a prompt) after the snapshot
    while time.monotonic() - start < maxw:
        if state["total"] > since_total or state["prompt_seen"]:
            break
        if not state["connected"]:
            return
        await asyncio.sleep(0.05)
    # phase 2: let the burst settle
    while time.monotonic() - start < maxw:
        if state["prompt_seen"]:
            return
        if time.monotonic() - state["last_rx"] >= quiet:
            return
        await asyncio.sleep(0.05)


def _drain(state: dict) -> str:
    """Return all unread text and advance the read cursor."""
    data = state["buffer"][state["read"] - state["base"]:]
    state["read"] = state["total"]
    state["prompt_seen"] = False
    return data


def _unread(state: dict) -> str:
    return state["buffer"][state["read"] - state["base"]:]


def _vitals(state: dict) -> dict:
    v = state["gmcp"].get("Char.Vitals")
    return v if isinstance(v, dict) else {}


def _outbound(text: str, enc: str) -> bytes:
    """Normalize newlines to CRLF, encode for the wire, escape IAC, terminate the line."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    body = "\r\n".join(text.split("\n"))
    return body.encode(enc, "replace").replace(b"\xff", b"\xff\xff") + b"\r\n"


async def _do_op(req: dict, state: dict, conn: MudConn) -> dict:
    op = req.get("op")
    quiet = float(req.get("quiet", 0.3))
    maxw = float(req.get("max", 5.0))
    start = time.monotonic()

    def took() -> float:
        return round(time.monotonic() - start, 3)

    if op == "send":
        async with state["lock"]:
            since = state["total"]
            state["prompt_seen"] = False
            try:
                conn.w.write(_outbound(req.get("data"), conn.enc))
                await asyncio.wait_for(conn.w.drain(), timeout=10.0)
            except asyncio.TimeoutError:
                return {"ok": False, "error": "send stalled (socket buffer full for 10s)"}
            except Exception as e:
                return {"ok": False, "error": "send failed: %s" % e}
            await _wait_settled(state, since, quiet, maxw)
            prompt = state["prompt_seen"]
            return {"ok": True, "data": _drain(state), "prompt": prompt,
                    "connected": state["connected"], "elapsed": took()}

    if op == "recv":
        async with state["lock"]:
            if req.get("block", True):
                await _wait_settled(state, state["read"], quiet, maxw)
            prompt = state["prompt_seen"]
            return {"ok": True, "data": _drain(state), "prompt": prompt,
                    "connected": state["connected"], "elapsed": took()}

    if op == "wait":
        pattern, pkg = req.get("pattern"), req.get("gmcp")
        rx = None
        if pattern:
            try:
                rx = re.compile(pattern)
            except re.error as e:
                return {"ok": False, "error": "bad regex: %s" % e}
        if rx is None and not pkg:
            return {"ok": False, "error": "wait needs a pattern and/or a gmcp package"}
        seq0 = state["gmcp_seq"].get(pkg, 0) if pkg else 0
        while True:
            hit = (rx is not None and rx.search(_unread(state)) is not None) or \
                  (bool(pkg) and state["gmcp_seq"].get(pkg, 0) > seq0)
            if hit:
                async with state["lock"]:
                    prompt = state["prompt_seen"]
                    data = _drain(state)
                resp = {"ok": True, "matched": True, "data": data, "prompt": prompt,
                        "connected": state["connected"], "elapsed": took()}
                if pkg:
                    resp["gmcp"] = state["gmcp"].get(pkg)
                return resp
            if not state["connected"] or time.monotonic() - start >= maxw:
                # No match: leave the unread text for recv.
                return {"ok": True, "matched": False, "data": "",
                        "connected": state["connected"], "elapsed": took()}
            await asyncio.sleep(0.05)

    if op == "gmcp":
        pkg = (req.get("package") or "").strip()
        if not pkg:
            return {"ok": False, "error": "missing GMCP package"}
        try:
            conn._send_gmcp(pkg, req.get("data") or "")
            await asyncio.wait_for(conn.w.drain(), timeout=10.0)
        except Exception as e:
            return {"ok": False, "error": "gmcp send failed: %s" % e}
        return {"ok": True, "elapsed": took()}

    if op == "state":
        now = time.monotonic()
        ages = {k: round(now - t, 1) for k, t in state["gmcp_time"].items()}
        return {"ok": True, "gmcp": state["gmcp"], "ages": ages,
                "connected": state["connected"]}

    if op == "status":
        server_opts = sorted(_optname(o) for o, v in conn.him.items() if v)
        client_opts = sorted(_optname(o) for o, v in conn.us.items() if v)
        return {"ok": True, "connected": state["connected"],
                "unread": state["total"] - state["read"], "total_chars": state["total"],
                "gmcp_packages": sorted(state["gmcp"].keys()), "vitals": _vitals(state),
                "last_rx_age": round(time.monotonic() - state["last_rx"], 1),
                "uptime": round(time.time() - state["started_wall"], 1),
                "encoding": conn.enc, "prompt_seen": state["prompt_seen"],
                "options": {"server": server_opts, "client": client_opts}}

    if op == "close":
        return {"ok": True}                  # the control handler stops the daemon after this acks
    return {"ok": False, "error": "unknown op '%s'" % op}


def _keepalive(writer: asyncio.StreamWriter) -> None:
    """Enable TCP keepalive so a NAT-dropped link is eventually detected instead of
    looking connected forever."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # TransportSocket has no ioctl, so tune via setsockopt where the platform
        # exposes the options (Linux, macOS, Windows 10+); bare SO_KEEPALIVE otherwise.
        for opt, val in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 20), ("TCP_KEEPCNT", 4)):
            if hasattr(socket, opt):
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
                except OSError:
                    pass
    except Exception:
        pass


def _dlog(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


async def _daemon_main(host: str, port: int, tls: bool = False, tls_insecure: bool = False,
                       encoding: str = "utf-8", idle_exit: float = 0.0,
                       debug: bool = False) -> None:
    try:
        _ensure_dir()
    except Exception as e:
        _dlog("state dir unusable: %s" % e)
        return
    ctx = None
    if tls:
        ctx = ssl.create_default_context()
        if tls_insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx), timeout=20.0)
    except Exception as e:
        _write_session({"error": str(e), "host": host, "port": port})
        _dlog("connect failed: %s" % e)
        return
    _keepalive(writer)
    _dlog("connected to %s:%d (tls=%s encoding=%s)" % (host, port, tls, encoding))

    try:
        log_fh = open(OUT_LOG, "a", encoding="utf-8")
        _harden(OUT_LOG, 0o600)
    except Exception:
        log_fh = None

    state = {"buffer": "", "total": 0, "read": 0, "base": 0, "connected": True, "gmcp": {},
             "gmcp_seq": {}, "gmcp_time": {}, "gmcp_n": 0,
             "last_rx": time.monotonic(), "prompt_seen": False, "lock": asyncio.Lock(),
             "started_wall": time.time(), "last_ctl": time.monotonic()}
    conn = MudConn(writer, state, log_fh, encoding=encoding, debug=debug)
    stop = asyncio.Event()
    token = secrets.token_hex(16)            # per-session secret; only our own processes know it

    loop = asyncio.get_running_loop()
    for sig in ("SIGTERM", "SIGINT"):        # graceful teardown on kill (POSIX only)
        try:
            loop.add_signal_handler(getattr(signal, sig), stop.set)
        except (NotImplementedError, RuntimeError, AttributeError, ValueError):
            pass

    async def control(creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        op = None
        authed = False
        state["last_ctl"] = time.monotonic()
        try:
            line = await creader.readline()
            if not line.strip():                        # blank line: ignore, just close
                return
            req = json.loads(line.decode("utf-8", "replace").strip() or "{}")
            if secrets.compare_digest(str(req.get("token")), token):
                authed = True
                op = req.get("op")
                resp = await _do_op(req, state, conn)
            else:
                resp = {"ok": False, "error": "auth failed"}
            cwriter.write((json.dumps(resp) + "\n").encode("utf-8"))
            await cwriter.drain()
        except Exception as e:
            try:
                cwriter.write((json.dumps({"ok": False, "error": str(e)}) + "\n").encode("utf-8"))
                await cwriter.drain()
            except Exception:
                pass
        finally:
            try:
                cwriter.close()
            except Exception:
                pass
            if authed and op == "close":      # stop only after an authenticated close acks
                _dlog("close requested")
                stop.set()

    server = await asyncio.start_server(control, "127.0.0.1", 0, limit=1 << 20)
    ctrl_port = server.sockets[0].getsockname()[1]
    _write_session({"host": host, "port": port, "control_port": ctrl_port,
                    "pid": os.getpid(), "token": token, "tls": tls,
                    "encoding": encoding, "started": state["started_wall"],
                    "version": __version__})

    async def pump() -> None:
        try:
            while not stop.is_set():
                try:
                    data = await reader.read(4096)
                except (ConnectionError, OSError):
                    break                                # reset/abort: treat as disconnect
                if not data:
                    break
                conn.feed(data)
        finally:
            if state["connected"]:
                _dlog("MUD connection closed")
            state["connected"] = False

    async def watchdog() -> None:
        while not stop.is_set():
            await asyncio.sleep(5.0)
            if idle_exit > 0 and time.monotonic() - state["last_ctl"] > idle_exit:
                _dlog("idle-exit: no control ops for %.0fs" % idle_exit)
                stop.set()
                return

    tasks = [asyncio.create_task(pump()), asyncio.create_task(server.serve_forever()),
             asyncio.create_task(watchdog())]
    await stop.wait()
    state["connected"] = False
    _dlog("terminating")
    for t in tasks:
        t.cancel()
    for closer in (server.close, writer.close,
                   lambda: log_fh and log_fh.close(),
                   lambda: os.remove(SESSION_JSON)):
        try:
            closer()
        except Exception:
            pass


# ------------------------------ client (the verbs) ------------------------------

def _control(op: str, _timeout: float = 35.0, **kw: Any) -> dict:
    sess = _read_session()
    if not sess or "control_port" not in sess:
        return {"ok": False, "error": "no active session (run 'connect' first)"}
    pid = sess.get("pid")
    if pid and not _pid_alive(pid):
        try:
            os.remove(SESSION_JSON)                     # stale: daemon died without cleanup
        except OSError:
            pass
        return {"ok": False, "error": "daemon is gone (stale session cleared; run 'connect')"}
    try:
        with socket.create_connection(("127.0.0.1", sess["control_port"]), timeout=_timeout) as s:
            s.settimeout(_timeout)
            req = {"op": op, "token": sess.get("token", ""), **kw}
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _spawn_daemon(host: str, port: int, tls: bool, tls_insecure: bool,
                  encoding: str, idle_exit: float, debug: bool) -> None:
    _ensure_dir()
    try:
        os.remove(SESSION_JSON)
    except OSError:
        pass
    # Rotate the previous transcript instead of destroying it.
    try:
        if os.path.exists(OUT_LOG) and os.path.getsize(OUT_LOG) > 0:
            os.replace(OUT_LOG, OUT_PREV_LOG)
            _harden(OUT_PREV_LOG, 0o600)
    except OSError:
        pass
    open(OUT_LOG, "w", encoding="utf-8").close()
    _harden(OUT_LOG, 0o600)
    args = [sys.executable, os.path.abspath(__file__), "--daemon",
            "--dir", STATE_DIR, "--host", host, "--port", str(port),
            "--encoding", encoding]
    if tls:
        args.append("--tls")
    if tls_insecure:
        args.append("--tls-insecure")
    if idle_exit:
        args += ["--idle-exit", str(idle_exit)]
    if debug:
        args.append("--debug")
    logf = open(DAEMON_LOG, "w", encoding="utf-8")
    _harden(DAEMON_LOG, 0o600)
    kwargs: dict = {"stdout": logf, "stderr": logf, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, **kwargs)
    finally:
        logf.close()                                     # the child has its own inherited handle


def _print(text: str) -> None:
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")


def _wait_timeout(maxw: float) -> float:
    return max(35.0, maxw + 15.0)


def _finish(r: dict, json_mode: bool, fail_prefix: str) -> int:
    """Common tail for send/recv: print the payload, surface disconnection (exit 3)."""
    if not r.get("ok"):
        if json_mode:
            print(json.dumps(r))
        else:
            print("%s: %s" % (fail_prefix, r.get("error")))
        return 1
    if json_mode:
        print(json.dumps(r))
    else:
        _print(r.get("data", ""))
        if r.get("connected") is False:
            print("(connection closed by server)", file=sys.stderr)
    return 0 if r.get("connected", True) else 3


def cmd_connect(host: str, port: int, quiet: float, maxw: float, tls: bool,
                tls_insecure: bool, encoding: str, idle_exit: float, debug: bool,
                json_mode: bool) -> int:
    try:
        codecs.lookup(encoding)
    except LookupError:
        print("unknown encoding '%s'" % encoding)
        return 2
    try:
        _ensure_dir()
        _acquire_connect_lock()
    except (RuntimeError, OSError) as e:
        print("connect failed: %s" % e)
        return 1
    try:
        old = _read_session()
        if old and old.get("control_port"):
            pid = old.get("pid")
            alive = bool(pid) and _pid_alive(pid)
            if alive:                                    # a real live daemon: ask it to exit
                _control("close", _timeout=5.0)
                for _ in range(30):                      # and wait for it to clear its session file
                    if not os.path.exists(SESSION_JSON):
                        break
                    time.sleep(0.1)
                if os.path.exists(SESSION_JSON) and _pid_alive(pid):
                    _kill_pid(pid)                       # wedged daemon: force it
            try:
                os.remove(SESSION_JSON)
            except OSError:
                pass
        _spawn_daemon(host, port, tls, tls_insecure, encoding, idle_exit, debug)
        deadline = time.time() + 25
        sess = None
        while time.time() < deadline:
            sess = _read_session()
            if sess:
                break
            time.sleep(0.3)
    finally:
        _release_connect_lock()
    if not sess:
        print("daemon did not start within 25s; see %s" % DAEMON_LOG)
        return 1
    if sess.get("error"):
        print("connect failed: %s" % sess["error"])
        return 1
    r = _control("recv", _timeout=_wait_timeout(maxw), block=True, quiet=quiet, max=maxw)
    if json_mode:
        print(json.dumps({"ok": True, "host": host, "port": port, "tls": tls,
                          "encoding": encoding, "data": r.get("data", ""),
                          "connected": r.get("connected", True)}))
        return 0
    print("connected to %s:%d%s" % (host, port, " (tls)" if tls else ""))
    _print(r.get("data", ""))
    return 0


def cmd_send(text: str, quiet: float, maxw: float, json_mode: bool) -> int:
    r = _control("send", _timeout=_wait_timeout(maxw), data=text, quiet=quiet, max=maxw)
    return _finish(r, json_mode, "send failed")


def cmd_recv(quiet: float, maxw: float, block: bool, json_mode: bool) -> int:
    r = _control("recv", _timeout=_wait_timeout(maxw), block=block, quiet=quiet, max=maxw)
    return _finish(r, json_mode, "recv failed")


def cmd_wait(pattern: Optional[str], gmcp_key: Optional[str], maxw: float,
             json_mode: bool) -> int:
    if not pattern and not gmcp_key:
        print("wait: give --for REGEX and/or --gmcp PACKAGE")
        return 2
    r = _control("wait", _timeout=_wait_timeout(maxw), pattern=pattern, gmcp=gmcp_key, max=maxw)
    if not r.get("ok"):
        if json_mode:
            print(json.dumps(r))
        else:
            print("wait failed: %s" % r.get("error"))
        return 1
    if json_mode:
        print(json.dumps(r))
        return 0 if r.get("matched") else 1
    if r.get("matched"):
        _print(r.get("data", ""))
        return 0 if r.get("connected", True) else 3
    print("wait: no match within %gs" % maxw, file=sys.stderr)
    return 1


def cmd_gmcp(package: str, payload: str, json_mode: bool) -> int:
    r = _control("gmcp", package=package, data=payload)
    if json_mode:
        print(json.dumps(r))
    else:
        print("sent" if r.get("ok") else "gmcp failed: %s" % r.get("error"))
    return 0 if r.get("ok") else 1


def cmd_state(key: Optional[str], times: bool, json_mode: bool) -> int:
    r = _control("state")
    if not r.get("ok"):
        print(json.dumps(r) if json_mode else "no session: %s" % r.get("error"))
        return 1
    if json_mode:
        print(json.dumps(r))
        return 0
    gmcp = r.get("gmcp", {})
    if times:
        print(json.dumps(r.get("ages", {}), indent=2, sort_keys=True))
        return 0
    if key:
        if key not in gmcp:
            print("no GMCP package '%s' yet (have: %s)" % (key, ", ".join(sorted(gmcp)) or "none"))
            return 1
        print(json.dumps(gmcp[key], indent=2))
    elif not gmcp:
        print("no GMCP data yet (the server may not push it until you're in the game)")
    else:
        print(json.dumps(gmcp, indent=2))
    return 0


def cmd_status(json_mode: bool) -> int:
    r = _control("status")
    if not r.get("ok"):
        print(json.dumps(r) if json_mode else "no session: %s" % r.get("error"))
        return 1
    sess = _read_session() or {}
    if json_mode:
        r["host"], r["port"], r["pid"] = sess.get("host"), sess.get("port"), sess.get("pid")
        print(json.dumps(r))
        return 0
    vit = r.get("vitals")
    vit = vit if isinstance(vit, dict) else {}
    vit_str = " vitals: hp=%s mp=%s" % (vit.get("hp"), vit.get("mp")) if vit else ""
    opts = r.get("options", {})
    where = "%s:%s pid=%s" % (sess.get("host"), sess.get("port"), sess.get("pid"))
    print("connected=%s %s uptime=%ss unread=%s last_rx=%ss encoding=%s "
          "server_opts=[%s] client_opts=[%s] gmcp=[%s]%s"
          % (r["connected"], where, r.get("uptime"), r["unread"], r.get("last_rx_age"),
             r.get("encoding"), ", ".join(opts.get("server", [])),
             ", ".join(opts.get("client", [])), ", ".join(r.get("gmcp_packages", [])),
             vit_str))
    return 0


def cmd_close(json_mode: bool) -> int:
    r = _control("close")
    if json_mode:
        print(json.dumps(r))
    else:
        print("closed" if r.get("ok") else "close failed: %s" % r.get("error"))
    return 0 if r.get("ok") else 1


def cmd_kill() -> int:
    sess = _read_session()
    if not sess:
        print("no session")
        return 0
    pid = sess.get("pid")
    if pid and _pid_alive(pid):
        _kill_pid(pid)
        print("killed daemon pid %s" % pid)
    else:
        print("daemon already gone")
    try:
        os.remove(SESSION_JSON)
    except OSError:
        pass
    return 0


def cmd_sites(json_mode: bool) -> int:
    if json_mode:
        print(json.dumps({n: {"host": h, "port": p, "note": d}
                          for n, (h, p, d) in SITES.items()}, indent=2))
        return 0
    width = max(len(n) for n in SITES)
    for name, (host, port, note) in SITES.items():
        print("%-*s  %-33s %s" % (width, name, "%s:%d" % (host, port), note))
    return 0


def cmd_log(tail: int) -> int:
    if not os.path.exists(OUT_LOG):
        print("no session log yet")
        return 1
    if tail > 0:
        # Scan backwards in blocks; never load more than needed for N lines.
        with open(OUT_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            blocks: List[bytes] = []
            newlines = 0
            while pos > 0 and newlines <= tail:
                step = min(65536, pos)
                pos -= step
                f.seek(pos)
                block = f.read(step)
                blocks.append(block)
                newlines += block.count(b"\n")
        data = b"".join(reversed(blocks)).decode("utf-8", "replace")
        _print("\n".join(data.splitlines()[-tail:]))
        return 0
    # Full dump: stream in text mode (universal newlines) instead of loading the
    # whole transcript into memory.
    with open(OUT_LOG, "r", encoding="utf-8", errors="replace") as f:
        last = ""
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            sys.stdout.write(chunk)
            last = chunk
        if last and not last.endswith("\n"):
            sys.stdout.write("\n")
    return 0


# ------------------------------ entrypoint ------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="automud",
        description="Persistent telnet/MUD session driven by discrete verbs, with smart waiting "
                    "and GMCP capture. No LLM, no API key; the operator supplies the intelligence.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Verbs: connect / send / recv / wait / gmcp / state / status / log / close / "
               "kill / sites.\n"
               "Run 'automud sites' for a directory of verified public targets, or "
               "'automud connect cdda' for the bundled single-player game (Cataclysm, "
               "needs tmux + a cataclysm binary; see the README).\n"
               "Exit codes: 0 ok; 1 failure; 2 usage; 3 ok but the connection is closed.")
    p.add_argument("--version", action="version", version="automud %s" % __version__)
    p.add_argument("-s", "--session", default="default", metavar="NAME",
                   help="named session (each gets its own state dir; default: 'default')")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_wait(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--quiet", type=float, default=0.3,
                        help="seconds of silence that counts as 'server done' (default 0.3)")
        sp.add_argument("--max", type=float, default=5.0,
                        help="hard cap on how long to wait for the reply (default 5)")

    def add_json(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true",
                        help="print the full structured response as one JSON object")

    c = sub.add_parser("connect", help="open a session (starts the background daemon)")
    c.add_argument("host", nargs="?",
                   help="telnet host, a site name from 'sites', or 'cdda' for the local "
                        "single-player game")
    c.add_argument("port", nargs="?", type=int, help="telnet port")
    c.add_argument("--demo", metavar="NAME",
                   help="connect to a named site from the directory (see 'sites')")
    c.add_argument("--tls", action="store_true", help="wrap the connection in TLS")
    c.add_argument("--tls-insecure", action="store_true",
                   help="TLS without certificate verification (self-signed MUDs)")
    c.add_argument("--encoding", default="utf-8",
                   help="wire text encoding (default utf-8; e.g. latin-1, cp437)")
    c.add_argument("--idle-exit", type=float, default=0.0, metavar="SEC",
                   help="daemon exits after SEC seconds with no verbs (default: never)")
    c.add_argument("--debug", action="store_true",
                   help="log telnet negotiation and GMCP traffic to daemon.log")
    add_wait(c)
    add_json(c)

    s = sub.add_parser("send", help="send one line, then print the reply")
    s.add_argument("text", nargs="*",
                   help="the line to send (joined with spaces); omit to send a blank line, "
                        "e.g. to answer a [more] pager. Use '--' before text starting "
                        "with '-'. See also --stdin.")
    s.add_argument("--stdin", action="store_true",
                   help="read the text from stdin instead of argv (keeps secrets out of "
                        "the process list and shell history; multi-line input is sent "
                        "line by line)")
    add_wait(s)
    add_json(s)

    r = sub.add_parser("recv", help="print any new output (waits for it to settle)")
    r.add_argument("--nowait", action="store_true",
                   help="return immediately with whatever is buffered")
    add_wait(r)
    add_json(r)

    w = sub.add_parser("wait", help="block until output matches a regex or GMCP updates")
    w.add_argument("--for", dest="pattern", metavar="REGEX",
                   help="return when unread output matches this regex")
    w.add_argument("--gmcp", metavar="PACKAGE",
                   help="return when this GMCP package next updates (e.g. Char.Vitals)")
    w.add_argument("--max", type=float, default=30.0,
                   help="give up after this many seconds (default 30)")
    add_json(w)

    g = sub.add_parser("gmcp", help="send a GMCP message to the server")
    g.add_argument("package", help="GMCP package, e.g. Char.Skills.Get")
    g.add_argument("payload", nargs="*", help="JSON payload (joined with spaces)")
    add_json(g)

    st = sub.add_parser("state", help="print captured GMCP game state as JSON")
    st.add_argument("--key", help="print only one package, e.g. Char.Vitals or Room.Info")
    st.add_argument("--times", action="store_true",
                    help="print seconds since each package last updated")
    add_json(st)

    stat = sub.add_parser("status", help="show connection + vitals summary")
    add_json(stat)

    cl = sub.add_parser("close", help="close the session and stop the daemon")
    add_json(cl)

    sub.add_parser("kill", help="force-stop a wedged daemon and clear the session")

    si = sub.add_parser("sites", help="list verified public telnet/MUD targets")
    add_json(si)

    lg = sub.add_parser("log", help="print the session output log")
    lg.add_argument("--tail", type=int, default=0, help="only the last N lines (0 = all)")
    return p


def _daemon_argv() -> argparse.Namespace:
    dp = argparse.ArgumentParser(prog="automud --daemon")
    dp.add_argument("--dir", required=True)
    dp.add_argument("--host", required=True)
    dp.add_argument("--port", type=int, required=True)
    dp.add_argument("--tls", action="store_true")
    dp.add_argument("--tls-insecure", action="store_true")
    dp.add_argument("--encoding", default="utf-8")
    dp.add_argument("--idle-exit", type=float, default=0.0)
    dp.add_argument("--debug", action="store_true")
    return dp.parse_args(sys.argv[2:])


def main() -> None:
    # MUD text is UTF-8 by default. Force it on stdout/stderr so non-ASCII output (box
    # drawing, accents, CJK, emoji) never raises UnicodeEncodeError when the console
    # codepage is narrow or the output is captured/redirected, which is exactly how an
    # agent runs this.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        a = _daemon_argv()
        _set_state_dir(a.dir)
        asyncio.run(_daemon_main(a.host, a.port, tls=a.tls, tls_insecure=a.tls_insecure,
                                 encoding=a.encoding, idle_exit=a.idle_exit, debug=a.debug))
        return

    args = build_parser().parse_args()
    _set_state_dir(os.path.join(STATE_BASE, args.session))

    # Local single-player backend: `connect cdda` launches Cataclysm in tmux instead of
    # opening a socket, and the other verbs route to it while its session is active. The
    # telnet path below is never touched.
    if args.cmd == "connect":
        target = (args.demo or "").lower() or (args.host.lower() if args.host else "")
        if target == "cdda":
            import automud_cdda
            sys.exit(automud_cdda.cmd_connect(json_mode=args.json))
    elif args.cmd in ("send", "recv", "wait", "state", "status", "close", "kill", "log"):
        _sess = _read_session()
        if _sess and _sess.get("backend") == "cdda":
            import automud_cdda
            sys.exit(automud_cdda.dispatch(args))

    if args.cmd == "connect":
        name = (args.demo or "").lower() or \
               (args.host.lower() if args.host and not args.port else "")
        if name and name in SITES:
            host, port = SITES[name][0], SITES[name][1]
        elif name:
            print("unknown site '%s'; run 'automud sites' for the directory" % name)
            sys.exit(2)
        elif args.host and args.port:
            host, port = args.host, args.port
        else:
            print("usage: connect HOST PORT   |   connect NAME   "
                  "(run 'automud sites' for names)")
            sys.exit(2)
        sys.exit(cmd_connect(host, port, quiet=args.quiet, maxw=args.max, tls=args.tls,
                             tls_insecure=args.tls_insecure, encoding=args.encoding,
                             idle_exit=args.idle_exit, debug=args.debug,
                             json_mode=args.json))
    elif args.cmd == "send":
        if args.stdin:
            if args.text:
                print("send: give text on argv or --stdin, not both")
                sys.exit(2)
            text = sys.stdin.read().rstrip("\n")
        else:
            text = " ".join(args.text)
        sys.exit(cmd_send(text, quiet=args.quiet, maxw=args.max, json_mode=args.json))
    elif args.cmd == "recv":
        sys.exit(cmd_recv(quiet=args.quiet, maxw=args.max, block=not args.nowait,
                          json_mode=args.json))
    elif args.cmd == "wait":
        sys.exit(cmd_wait(args.pattern, args.gmcp, maxw=args.max, json_mode=args.json))
    elif args.cmd == "gmcp":
        sys.exit(cmd_gmcp(args.package, " ".join(args.payload), json_mode=args.json))
    elif args.cmd == "state":
        sys.exit(cmd_state(args.key, times=args.times, json_mode=args.json))
    elif args.cmd == "status":
        sys.exit(cmd_status(json_mode=args.json))
    elif args.cmd == "close":
        sys.exit(cmd_close(json_mode=args.json))
    elif args.cmd == "kill":
        sys.exit(cmd_kill())
    elif args.cmd == "sites":
        sys.exit(cmd_sites(json_mode=args.json))
    elif args.cmd == "log":
        sys.exit(cmd_log(tail=args.tail))


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # Output was piped into something that closed early (head, a truncating pager).
        # Redirect stdout to devnull so the interpreter's final flush doesn't re-raise,
        # and exit quietly instead of dumping a traceback.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)
