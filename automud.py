"""
AutoMUD: a persistent telnet/MUD session you drive by hand (or an agent drives).

There is no LLM in here and no API key. The intelligence is whoever runs it: a person, a
script, or an autonomous agent. It solves the parts of a live MUD connection that a
normal shell can't: a raw telnet session is interactive and blocking, so it can't be held
across separate commands. A small background daemon keeps the connection open and you talk
to it with discrete verbs:

    automud connect --demo achaea     # or: automud connect achaea.com 23
    automud send 2                     # send a line, print the reply
    automud send Maelvorn
    automud recv                        # drain any new output
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
    (Char.Vitals, Room.Info, etc.) into JSON you can read with `state`. It refuses every
    other option it doesn't understand (compression, MSDP, MXP) rather than choking on it.

The control channel is a localhost-only socket with no authentication: anyone able to run
processes as you on this machine can drive the session. That is fine for a personal box;
on a shared host, treat a live session as readable/writable by other local users.

Config (optional):
    AUTOMUD_DIR : session/state directory (default: <tempdir>/automud)
"""

import argparse
import asyncio
import codecs
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from typing import Optional

AUTOMUD_DIR = os.environ.get("AUTOMUD_DIR") or os.path.join(tempfile.gettempdir(), "automud")
SESSION_JSON = os.path.join(AUTOMUD_DIR, "session.json")
OUT_LOG = os.path.join(AUTOMUD_DIR, "out.log")
DAEMON_LOG = os.path.join(AUTOMUD_DIR, "daemon.log")

# Keep at most this many characters of received text in memory (the full stream still goes
# to OUT_LOG). Trimming only ever drops already-read history, never unread output.
BUFFER_CAP = 1_000_000

DEMOS = {
    "zork": ("telehack.com", 23),
    "chess": ("freechess.org", 5000),
    "achaea": ("achaea.com", 23),
}

# Telnet command bytes
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
GA, EOR_CMD = 249, 239
# Telnet options
OPT_GMCP, OPT_EOR = 201, 25
# Options we ask the server to enable. GMCP gives structured state; EOR gives prompt markers.
# We deliberately do NOT request SGA (option 3): suppressing Go-Ahead would kill the other
# prompt marker. Everything not listed here is refused, which also keeps us from accidentally
# enabling compression (MCCP) and turning the stream into zlib garbage.
WANT_DO = {OPT_GMCP, OPT_EOR}

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][AB012]|[\x00-\x08\x0b\x0c\x0e-\x1f]")
# A trailing, still-incomplete escape sequence (no final byte yet). Held back so a sequence
# split across two TCP reads is completed before it is emitted or stripped.
_ANSI_INCOMPLETE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*|[()])?$")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


# ------------------------------ session state files ------------------------------

def _write_session(data: dict) -> None:
    os.makedirs(AUTOMUD_DIR, exist_ok=True)
    tmp = SESSION_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, SESSION_JSON)


def _read_session() -> Optional[dict]:
    try:
        with open(SESSION_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ------------------------------ telnet / GMCP parser ------------------------------

class MudConn:
    """Minimal telnet client: separates plain text from IAC control, answers option
    negotiation (only on state change, so it can't loop), captures GMCP subnegotiation as
    JSON, and flags GA/EOR prompt markers.

    Receipt accounting uses monotonic counters, not buffer indices, so trimming the in-memory
    buffer never disturbs the read cursor or the wait logic:
      total : chars ever received
      read  : chars handed to the client
      base  : chars dropped off the front of `buffer` (buffer == stream[base:total])
    """

    def __init__(self, writer: asyncio.StreamWriter, state: dict, log_fh):
        self.w = writer
        self.s = state
        self.log = log_fh
        self.mode = "text"          # text | iac | neg | sb | sbiac
        self.cmd = None
        self.sb_opt = None
        self.sb = bytearray()
        self.text = bytearray()
        self.him = {}               # server-side option enabled? (None unknown)
        self.us = {}                # our-side option enabled?
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""          # decoded text held back across reads (partial escape)

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
        # emitted or mis-stripped; it completes on the next chunk.
        m = _ANSI_INCOMPLETE.search(emit)
        if m:
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

    def _negotiate(self, cmd: int, opt: int) -> None:
        # Respond only when the option's state actually changes, per the telnet Q-method,
        # so a server that re-announces options can't make us loop.
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
            if self.us.get(opt) is not False:           # we enable nothing on our side
                self.us[opt] = False
                self._raw(IAC, WONT, opt)
        elif cmd == DONT:
            if self.us.get(opt) is not False:
                self.us[opt] = False
                self._raw(IAC, WONT, opt)

    def _send_gmcp(self, package: str, payload: str) -> None:
        msg = (package + " " + payload).encode("utf-8")
        try:
            self.w.write(bytes([IAC, SB, OPT_GMCP]) + msg + bytes([IAC, SE]))
        except Exception:
            pass

    def _gmcp_hello(self) -> None:
        self._send_gmcp("Core.Hello", '{"client":"AutoMUD","version":"1.0"}')
        self._send_gmcp("Core.Supports.Set",
                        '["Char 1","Char.Vitals 1","Char.Status 1","Char.Skills 1",'
                        '"Room 1","Comm.Channel 1"]')

    def _subneg(self, opt: int, payload: bytes) -> None:
        if opt != OPT_GMCP:
            return                                      # MSDP/MXP/etc.: ignore, don't choke
        text = payload.decode("utf-8", "replace")
        sp = text.find(" ")
        if sp == -1:
            package, body = text.strip(), ""
        else:
            package, body = text[:sp].strip(), text[sp + 1:]
        value = None
        if body.strip():
            try:
                value = json.loads(body)
            except Exception:
                value = body
        if package:
            self.s["gmcp"][package] = value


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


def _vitals(state: dict) -> dict:
    v = state["gmcp"].get("Char.Vitals")
    return v if isinstance(v, dict) else {}


async def _do_op(req: dict, state: dict, writer: asyncio.StreamWriter) -> dict:
    op = req.get("op")
    quiet = float(req.get("quiet", 0.3))
    maxw = float(req.get("max", 5.0))
    if op == "send":
        async with state["lock"]:
            since = state["total"]
            state["prompt_seen"] = False
            try:
                writer.write((req.get("data") or "").encode("utf-8").replace(b"\xff", b"\xff\xff") + b"\r\n")
                await writer.drain()
            except Exception as e:
                return {"ok": False, "error": f"send failed: {e}"}
            await _wait_settled(state, since, quiet, maxw)
            return {"ok": True, "data": _drain(state), "connected": state["connected"]}
    if op == "recv":
        async with state["lock"]:
            if req.get("block", True):
                await _wait_settled(state, state["read"], quiet, maxw)
            return {"ok": True, "data": _drain(state), "connected": state["connected"]}
    if op == "state":
        return {"ok": True, "gmcp": state["gmcp"], "connected": state["connected"]}
    if op == "status":
        return {"ok": True, "connected": state["connected"],
                "unread": state["total"] - state["read"], "total_chars": state["total"],
                "gmcp_packages": sorted(state["gmcp"].keys()), "vitals": _vitals(state)}
    if op == "close":
        return {"ok": True}                  # the control handler stops the daemon after this acks
    return {"ok": False, "error": f"unknown op '{op}'"}


async def _daemon_main(host: str, port: int) -> None:
    os.makedirs(AUTOMUD_DIR, exist_ok=True)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=20.0)
    except Exception as e:
        _write_session({"error": str(e), "host": host, "port": port})
        return

    try:
        log_fh = open(OUT_LOG, "a", encoding="utf-8")
    except Exception:
        log_fh = None

    state = {"buffer": "", "total": 0, "read": 0, "base": 0, "connected": True, "gmcp": {},
             "last_rx": time.monotonic(), "prompt_seen": False, "lock": asyncio.Lock()}
    conn = MudConn(writer, state, log_fh)
    stop = asyncio.Event()

    async def control(creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        op = None
        try:
            line = await creader.readline()
            req = json.loads(line.decode("utf-8", "replace") or "{}")
            op = req.get("op")
            resp = await _do_op(req, state, writer)
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
            if op == "close":                # stop only after the ack has been flushed to the client
                stop.set()

    server = await asyncio.start_server(control, "127.0.0.1", 0)
    ctrl_port = server.sockets[0].getsockname()[1]
    _write_session({"host": host, "port": port, "control_port": ctrl_port, "pid": os.getpid()})

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
            state["connected"] = False

    pump_task = asyncio.create_task(pump())
    serve_task = asyncio.create_task(server.serve_forever())
    await stop.wait()
    state["connected"] = False
    for t in (pump_task, serve_task):
        t.cancel()
    for closer in (lambda: writer.close(), lambda: log_fh and log_fh.close(),
                   lambda: os.remove(SESSION_JSON)):
        try:
            closer()
        except Exception:
            pass


# ------------------------------ client (the verbs) ------------------------------

def _control(op: str, _timeout: float = 35.0, **kw) -> dict:
    sess = _read_session()
    if not sess or "control_port" not in sess:
        return {"ok": False, "error": "no active session (run 'connect' first)"}
    try:
        with socket.create_connection(("127.0.0.1", sess["control_port"]), timeout=_timeout) as s:
            s.settimeout(_timeout)
            s.sendall((json.dumps({"op": op, **kw}) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _spawn_daemon(host: str, port: int) -> None:
    os.makedirs(AUTOMUD_DIR, exist_ok=True)
    try:
        os.remove(SESSION_JSON)
    except Exception:
        pass
    open(OUT_LOG, "w", encoding="utf-8").close()
    args = [sys.executable, os.path.abspath(__file__), "--daemon", host, str(port)]
    logf = open(DAEMON_LOG, "w", encoding="utf-8")
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


def cmd_connect(host: str, port: int, quiet: float, maxw: float) -> int:
    old = _read_session()
    if old and old.get("control_port"):                  # a real live daemon: ask it to exit
        _control("close")
        for _ in range(30):                              # and wait for it to clear its session file
            if not os.path.exists(SESSION_JSON):
                break
            time.sleep(0.1)
    _spawn_daemon(host, port)
    deadline = time.time() + 25
    sess = None
    while time.time() < deadline:
        sess = _read_session()
        if sess:
            break
        time.sleep(0.3)
    if not sess:
        print(f"daemon did not start within 25s; see {DAEMON_LOG}")
        return 1
    if sess.get("error"):
        print(f"connect failed: {sess['error']}")
        return 1
    print(f"connected to {host}:{port}")
    _print(_control("recv", _timeout=_wait_timeout(maxw), block=True, quiet=quiet, max=maxw).get("data", ""))
    return 0


def cmd_send(text: str, quiet: float, maxw: float) -> int:
    r = _control("send", _timeout=_wait_timeout(maxw), data=text, quiet=quiet, max=maxw)
    if not r.get("ok"):
        print(f"send failed: {r.get('error')}")
        return 1
    _print(r.get("data", ""))
    return 0


def cmd_recv(quiet: float, maxw: float) -> int:
    r = _control("recv", _timeout=_wait_timeout(maxw), block=True, quiet=quiet, max=maxw)
    if not r.get("ok"):
        print(f"recv failed: {r.get('error')}")
        return 1
    _print(r.get("data", ""))
    return 0


def cmd_state(key: Optional[str]) -> int:
    r = _control("state")
    if not r.get("ok"):
        print(f"no session: {r.get('error')}")
        return 1
    gmcp = r.get("gmcp", {})
    if key:
        if key not in gmcp:
            print(f"no GMCP package '{key}' yet (have: {', '.join(sorted(gmcp)) or 'none'})")
            return 1
        print(json.dumps(gmcp[key], indent=2))
    elif not gmcp:
        print("no GMCP data yet (the server may not push it until you're in the game)")
    else:
        print(json.dumps(gmcp, indent=2))
    return 0


def cmd_status() -> int:
    r = _control("status")
    if not r.get("ok"):
        print(f"no session: {r.get('error')}")
        return 1
    vit = r.get("vitals")
    vit = vit if isinstance(vit, dict) else {}
    vit_str = f" vitals: hp={vit.get('hp')} mp={vit.get('mp')}" if vit else ""
    print(f"connected={r['connected']} unread={r['unread']} chars "
          f"gmcp=[{', '.join(r.get('gmcp_packages', []))}]" + vit_str)
    return 0


def cmd_close() -> int:
    r = _control("close")
    print("closed" if r.get("ok") else f"close failed: {r.get('error')}")
    return 0 if r.get("ok") else 1


def cmd_log(tail: int) -> int:
    try:
        with open(OUT_LOG, "r", encoding="utf-8") as f:
            data = f.read()
    except Exception:
        print("no session log yet")
        return 1
    if tail > 0:
        data = data[-tail:]
    _print(data)
    return 0


# ------------------------------ entrypoint ------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="automud",
        description="Persistent telnet/MUD session driven by discrete verbs, with smart waiting "
                    "and GMCP capture. No LLM, no API key; the operator supplies the intelligence.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Verbs: connect / send / recv / state / status / log / close. Demos: "
               + ", ".join(sorted(DEMOS)) + ".")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_wait(sp):
        sp.add_argument("--quiet", type=float, default=0.3,
                        help="seconds of silence that counts as 'server done' (default 0.3)")
        sp.add_argument("--max", type=float, default=5.0,
                        help="hard cap on how long to wait for the reply (default 5)")

    c = sub.add_parser("connect", help="open a session (starts the background daemon)")
    c.add_argument("host", nargs="?", help="telnet host")
    c.add_argument("port", nargs="?", type=int, help="telnet port")
    c.add_argument("--demo", choices=sorted(DEMOS), help="use a built-in demo target")
    add_wait(c)

    s = sub.add_parser("send", help="send one line, then print the reply")
    s.add_argument("text", nargs="+", help="the line to send (joined with spaces)")
    add_wait(s)

    r = sub.add_parser("recv", help="print any new output (waits for it to settle)")
    add_wait(r)

    st = sub.add_parser("state", help="print captured GMCP game state as JSON")
    st.add_argument("--key", help="print only one package, e.g. Char.Vitals or Room.Info")

    sub.add_parser("status", help="show connection + vitals summary")
    sub.add_parser("close", help="close the session and stop the daemon")

    lg = sub.add_parser("log", help="print the full session output log")
    lg.add_argument("--tail", type=int, default=0, help="only the last N characters (0 = all)")
    return p


def main() -> None:
    # MUD text is UTF-8. Force it on stdout/stderr so non-ASCII output (box drawing, accents,
    # CJK, emoji) never raises UnicodeEncodeError when the console codepage is narrow or the
    # output is captured/redirected, which is exactly how an agent runs this.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(sys.argv) >= 4 and sys.argv[1] == "--daemon":
        asyncio.run(_daemon_main(sys.argv[2], int(sys.argv[3])))
        return

    args = build_parser().parse_args()
    if args.cmd == "connect":
        if args.demo:
            host, port = DEMOS[args.demo]
        elif args.host and args.port:
            host, port = args.host, args.port
        else:
            print("usage: connect HOST PORT   |   connect --demo NAME")
            sys.exit(2)
        sys.exit(cmd_connect(host, port, quiet=args.quiet, maxw=args.max))
    elif args.cmd == "send":
        sys.exit(cmd_send(" ".join(args.text), quiet=args.quiet, maxw=args.max))
    elif args.cmd == "recv":
        sys.exit(cmd_recv(quiet=args.quiet, maxw=args.max))
    elif args.cmd == "state":
        sys.exit(cmd_state(args.key))
    elif args.cmd == "status":
        sys.exit(cmd_status())
    elif args.cmd == "close":
        sys.exit(cmd_close())
    elif args.cmd == "log":
        sys.exit(cmd_log(tail=args.tail))


if __name__ == "__main__":
    main()
