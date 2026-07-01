"""Unit tests for automud: telnet state machine, negotiation, GMCP, encodings,
buffer accounting, wait logic, ops, and the session file/lock/pid helpers.

Run from the repo root:  python -m unittest discover -s tests -t .
"""

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import automud
from automud import (DO, DONT, EOR_CMD, GA, IAC, SB, SE, WILL, WONT,
                     CHARSET_ACCEPTED, CHARSET_REQUEST, OPT_CHARSET, OPT_GMCP,
                     OPT_NAWS, OPT_TTYPE, TTYPE_IS, TTYPE_SEND, MudConn,
                     strip_ansi)


class FakeWriter:
    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass

    def get_extra_info(self, key):
        return None

    def close(self):
        pass


def mkstate():
    return {"buffer": "", "total": 0, "read": 0, "base": 0, "connected": True,
            "gmcp": {}, "gmcp_seq": {}, "gmcp_time": {}, "gmcp_n": 0,
            "last_rx": time.monotonic(), "prompt_seen": False,
            "started_wall": time.time(), "last_ctl": time.monotonic()}


def mkconn(encoding="utf-8"):
    state = mkstate()
    w = FakeWriter()
    return MudConn(w, state, None, encoding=encoding), state, w


class TestTextStream(unittest.TestCase):
    def test_plain_text(self):
        conn, state, _ = mkconn()
        conn.feed(b"hello world")
        self.assertEqual(state["buffer"], "hello world")
        self.assertEqual(state["total"], 11)

    def test_escaped_iac_is_data(self):
        conn, state, _ = mkconn(encoding="latin-1")
        conn.feed(bytes([0x61, IAC, IAC, 0x62]))
        self.assertEqual(state["buffer"], "a\xffb")

    def test_iac_split_across_feeds(self):
        conn, state, w = mkconn()
        conn.feed(b"abc" + bytes([IAC]))
        self.assertEqual(state["buffer"], "abc")
        conn.feed(bytes([WILL, OPT_GMCP]))
        self.assertIn(bytes([IAC, DO, OPT_GMCP]), bytes(w.buf))

    def test_ga_sets_prompt(self):
        conn, state, _ = mkconn()
        conn.feed(b"hp:100> " + bytes([IAC, GA]))
        self.assertTrue(state["prompt_seen"])
        self.assertEqual(state["buffer"], "hp:100> ")

    def test_eor_sets_prompt(self):
        conn, state, _ = mkconn()
        conn.feed(bytes([IAC, EOR_CMD]))
        self.assertTrue(state["prompt_seen"])

    def test_utf8_split_multibyte(self):
        conn, state, _ = mkconn()
        conn.feed(b"caf\xc3")
        self.assertEqual(state["buffer"], "caf")
        conn.feed(b"\xa9!")
        self.assertEqual(state["buffer"], "caf\xe9!")

    def test_latin1_mode(self):
        conn, state, _ = mkconn(encoding="latin-1")
        conn.feed(b"caf\xe9")
        self.assertEqual(state["buffer"], "caf\xe9")

    def test_unknown_encoding_falls_back_to_utf8(self):
        conn, _, _ = mkconn(encoding="no-such-codec")
        self.assertEqual(conn.enc, "utf-8")


class TestAnsi(unittest.TestCase):
    def test_csi_stripped(self):
        self.assertEqual(strip_ansi("a\x1b[31mred\x1b[0mb"), "aredb")

    def test_osc_bel_stripped(self):
        self.assertEqual(strip_ansi("a\x1b]0;window title\x07b"), "ab")

    def test_osc_st_stripped(self):
        self.assertEqual(strip_ansi("a\x1b]2;title\x1b\\b"), "ab")

    def test_dcs_stripped(self):
        self.assertEqual(strip_ansi("a\x1bPsome data\x1b\\b"), "ab")

    def test_control_chars_stripped_but_not_whitespace(self):
        self.assertEqual(strip_ansi("a\x07b\tc\nd\re"), "ab\tc\nd\re")

    def test_csi_split_across_feeds(self):
        conn, state, _ = mkconn()
        conn.feed(b"A\x1b[31")
        self.assertEqual(state["buffer"], "A")
        conn.feed(b"mB")
        self.assertEqual(state["buffer"], "AB")

    def test_osc_split_across_feeds(self):
        conn, state, _ = mkconn()
        conn.feed(b"A\x1b]0;tit")
        self.assertEqual(state["buffer"], "A")
        conn.feed(b"le\x07B")
        self.assertEqual(state["buffer"], "AB")

    def test_pending_cap_flushes_runaway_escape(self):
        old = automud.PENDING_CAP
        automud.PENDING_CAP = 8
        try:
            conn, state, _ = mkconn()
            conn.feed(b"\x1b]" + b"x" * 20)
            # Held-back sequence exceeded the cap: emitted rather than pinned.
            self.assertEqual(state["buffer"], "x" * 20)
        finally:
            automud.PENDING_CAP = old


class TestNegotiation(unittest.TestCase):
    def test_will_gmcp_accepted_once_with_hello(self):
        conn, _, w = mkconn()
        conn.feed(bytes([IAC, WILL, OPT_GMCP]))
        conn.feed(bytes([IAC, WILL, OPT_GMCP]))          # re-announce: no loop
        out = bytes(w.buf)
        self.assertEqual(out.count(bytes([IAC, DO, OPT_GMCP])), 1)
        self.assertIn(b"Core.Hello", out)
        self.assertIn(automud.__version__.encode(), out)
        self.assertIn(b"Core.Supports.Set", out)

    def test_will_unknown_refused(self):
        conn, _, w = mkconn()
        conn.feed(bytes([IAC, WILL, 86]))                # MCCP2
        self.assertIn(bytes([IAC, DONT, 86]), bytes(w.buf))

    def test_do_sga_refused(self):
        conn, _, w = mkconn()
        conn.feed(bytes([IAC, DO, 3]))
        self.assertIn(bytes([IAC, WONT, 3]), bytes(w.buf))

    def test_do_ttype_accepted_and_cycles(self):
        conn, _, w = mkconn()
        conn.feed(bytes([IAC, DO, OPT_TTYPE]))
        self.assertIn(bytes([IAC, WILL, OPT_TTYPE]), bytes(w.buf))
        send = bytes([IAC, SB, OPT_TTYPE, TTYPE_SEND, IAC, SE])
        conn.feed(send)
        self.assertIn(bytes([IAC, SB, OPT_TTYPE, TTYPE_IS]) + b"AUTOMUD", bytes(w.buf))
        conn.feed(send)
        self.assertIn(bytes([IAC, SB, OPT_TTYPE, TTYPE_IS]) + b"ANSI", bytes(w.buf))
        conn.feed(send)
        conn.feed(send)                                   # sticks on the last entry
        self.assertEqual(bytes(w.buf).count(b"MTTS 1"), 2)

    def test_do_naws_accepted_and_size_sent(self):
        conn, _, w = mkconn()
        conn.feed(bytes([IAC, DO, OPT_NAWS]))
        out = bytes(w.buf)
        self.assertIn(bytes([IAC, WILL, OPT_NAWS]), out)
        size = bytes([0, automud.NAWS_COLS, 0, automud.NAWS_ROWS])
        self.assertIn(bytes([IAC, SB, OPT_NAWS]) + size + bytes([IAC, SE]), out)

    def test_dont_after_enable_disables(self):
        conn, _, w = mkconn()
        conn.feed(bytes([IAC, DO, OPT_NAWS]))
        conn.feed(bytes([IAC, DONT, OPT_NAWS]))
        self.assertIn(bytes([IAC, WONT, OPT_NAWS]), bytes(w.buf))
        self.assertIs(conn.us[OPT_NAWS], False)

    def test_wont_acknowledged_once(self):
        conn, _, w = mkconn()
        conn.feed(bytes([IAC, WONT, OPT_GMCP]))
        conn.feed(bytes([IAC, WONT, OPT_GMCP]))
        self.assertEqual(bytes(w.buf).count(bytes([IAC, DONT, OPT_GMCP])), 1)


class TestCharset(unittest.TestCase):
    def _request(self, names):
        return bytes([IAC, SB, OPT_CHARSET, CHARSET_REQUEST]) + b";" + \
            b";".join(names) + bytes([IAC, SE])

    def test_will_charset_accepted(self):
        conn, _, w = mkconn()
        conn.feed(bytes([IAC, WILL, OPT_CHARSET]))
        self.assertIn(bytes([IAC, DO, OPT_CHARSET]), bytes(w.buf))

    def test_prefers_current_encoding(self):
        conn, _, w = mkconn()
        conn.feed(self._request([b"ISO-8859-1", b"UTF-8"]))
        self.assertIn(bytes([SB, OPT_CHARSET, CHARSET_ACCEPTED]) + b"UTF-8", bytes(w.buf))
        self.assertEqual(conn.enc, "utf-8")

    def test_switches_decoder(self):
        conn, state, w = mkconn()
        conn.feed(self._request([b"ISO-8859-1"]))
        self.assertIn(bytes([SB, OPT_CHARSET, CHARSET_ACCEPTED]) + b"ISO-8859-1",
                      bytes(w.buf))
        self.assertEqual(conn.enc, "iso8859-1")
        conn.feed(b"caf\xe9")
        self.assertEqual(state["buffer"], "caf\xe9")

    def test_rejects_garbage(self):
        conn, _, w = mkconn()
        conn.feed(self._request([b"KLINGON-8"]))
        self.assertIn(bytes([SB, OPT_CHARSET, automud.CHARSET_REJECTED]), bytes(w.buf))


class TestGmcp(unittest.TestCase):
    def _gmcp(self, conn, package, body=b""):
        payload = package + (b" " + body if body else b"")
        conn.feed(bytes([IAC, SB, OPT_GMCP]) + payload + bytes([IAC, SE]))

    def test_json_payload_parsed(self):
        conn, state, _ = mkconn()
        self._gmcp(conn, b"Char.Vitals", b'{"hp": 100, "mp": 90}')
        self.assertEqual(state["gmcp"]["Char.Vitals"], {"hp": 100, "mp": 90})
        self.assertIn("Char.Vitals", state["gmcp_time"])
        self.assertEqual(state["gmcp_seq"]["Char.Vitals"], 1)

    def test_escaped_iac_inside_sb(self):
        conn, state, _ = mkconn()
        # 0xFF escaped as IAC IAC inside the subnegotiation must not end it early.
        conn.feed(bytes([IAC, SB, OPT_GMCP]) + b"X.Y " + bytes([IAC, IAC]) +
                  bytes([IAC, SE]))
        self.assertIn("X.Y", state["gmcp"])

    def test_malformed_sb_resyncs(self):
        conn, state, _ = mkconn()
        conn.feed(bytes([IAC, SB, OPT_GMCP]) + b"junk" + bytes([IAC, 99]) + b"after")
        self.assertEqual(state["buffer"], "after")        # parser recovered to text mode

    def test_non_gmcp_subneg_ignored(self):
        conn, state, _ = mkconn()
        conn.feed(bytes([IAC, SB, 69]) + b"MSDP stuff" + bytes([IAC, SE]) + b"ok")
        self.assertEqual(state["buffer"], "ok")
        self.assertEqual(state["gmcp"], {})

    def test_affliction_deltas(self):
        state = mkstate()
        automud._apply_gmcp(state, "Char.Afflictions.List",
                            [{"name": "stun"}, {"name": "blind"}])
        automud._apply_gmcp(state, "Char.Afflictions.Add", {"name": "burn"})
        names = [e["name"] for e in state["gmcp"]["Char.Afflictions.List"]]
        self.assertEqual(names, ["stun", "blind", "burn"])
        automud._apply_gmcp(state, "Char.Afflictions.Remove", ["stun", "burn"])
        names = [e["name"] for e in state["gmcp"]["Char.Afflictions.List"]]
        self.assertEqual(names, ["blind"])

    def test_room_player_deltas(self):
        state = mkstate()
        automud._apply_gmcp(state, "Room.Players", [{"name": "Bob"}])
        automud._apply_gmcp(state, "Room.AddPlayer", {"name": "Eve"})
        automud._apply_gmcp(state, "Room.RemovePlayer", "Bob")
        names = [e["name"] for e in state["gmcp"]["Room.Players"]]
        self.assertEqual(names, ["Eve"])

    def test_comm_history_bounded(self):
        old = automud.COMM_HISTORY_CAP
        automud.COMM_HISTORY_CAP = 5
        try:
            state = mkstate()
            for i in range(8):
                automud._apply_gmcp(state, "Comm.Channel.Text", {"text": str(i)})
            hist = state["gmcp"]["Comm.Channel.History"]
            self.assertEqual(len(hist), 5)
            self.assertEqual(hist[-1]["text"], "7")
            self.assertEqual(hist[0]["text"], "3")
        finally:
            automud.COMM_HISTORY_CAP = old


class TestBufferAccounting(unittest.TestCase):
    def test_trim_preserves_cursor_math(self):
        old = automud.BUFFER_CAP
        automud.BUFFER_CAP = 100
        try:
            conn, state, _ = mkconn()
            conn.feed(b"x" * 150)
            self.assertEqual(state["total"], 150)
            self.assertEqual(state["base"], 50)
            self.assertEqual(state["read"], 50)           # unread bytes dropped: cursor clamped
            self.assertEqual(len(state["buffer"]), 100)
            data = automud._drain(state)
            self.assertEqual(data, "x" * 100)
            self.assertEqual(state["read"], 150)
            conn.feed(b"y" * 30)
            self.assertEqual(automud._drain(state), "y" * 30)
        finally:
            automud.BUFFER_CAP = old

    def test_drain_resets_prompt(self):
        conn, state, _ = mkconn()
        conn.feed(b"hi" + bytes([IAC, GA]))
        self.assertTrue(state["prompt_seen"])
        self.assertEqual(automud._drain(state), "hi")
        self.assertFalse(state["prompt_seen"])


class TestOutbound(unittest.TestCase):
    def test_crlf_and_terminator(self):
        self.assertEqual(automud._outbound("look", "utf-8"), b"look\r\n")

    def test_newline_normalization(self):
        self.assertEqual(automud._outbound("a\nb\r\nc\rd", "utf-8"),
                         b"a\r\nb\r\nc\r\nd\r\n")

    def test_iac_escaped(self):
        self.assertEqual(automud._outbound("\xff", "latin-1"), b"\xff\xff\r\n")

    def test_blank_line(self):
        self.assertEqual(automud._outbound("", "utf-8"), b"\r\n")


class TestWaitSettled(unittest.TestCase):
    def test_prompt_returns_immediately(self):
        async def run():
            state = mkstate()
            state["total"] = 5
            state["prompt_seen"] = True
            t0 = time.monotonic()
            await automud._wait_settled(state, 0, quiet=1.0, maxw=5.0)
            return time.monotonic() - t0
        self.assertLess(asyncio.run(run()), 0.5)

    def test_quiet_returns_after_idle(self):
        async def run():
            state = mkstate()
            state["total"] = 5
            state["last_rx"] = time.monotonic() - 1.0     # already idle
            t0 = time.monotonic()
            await automud._wait_settled(state, 0, quiet=0.2, maxw=5.0)
            return time.monotonic() - t0
        self.assertLess(asyncio.run(run()), 0.5)

    def test_maxw_caps_silent_server(self):
        async def run():
            state = mkstate()
            t0 = time.monotonic()
            await automud._wait_settled(state, state["total"], quiet=0.1, maxw=0.3)
            return time.monotonic() - t0
        took = asyncio.run(run())
        self.assertGreaterEqual(took, 0.25)
        self.assertLess(took, 2.0)

    def test_disconnect_returns(self):
        async def run():
            state = mkstate()
            state["connected"] = False
            t0 = time.monotonic()
            await automud._wait_settled(state, state["total"], quiet=0.1, maxw=5.0)
            return time.monotonic() - t0
        self.assertLess(asyncio.run(run()), 0.5)


class TestOps(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_send_writes_crlf_and_reports_prompt(self):
        async def run():
            conn, state, w = mkconn()
            state["lock"] = asyncio.Lock()
            resp_task = asyncio.create_task(automud._do_op(
                {"op": "send", "data": "look", "quiet": 0.05, "max": 2.0}, state, conn))
            await asyncio.sleep(0.1)
            conn.feed(b"A dark room." + bytes([IAC, GA]))
            resp = await resp_task
            return resp, bytes(w.buf)
        resp, wire = self._run(run())
        self.assertTrue(resp["ok"])
        self.assertEqual(wire, b"look\r\n")
        self.assertEqual(resp["data"], "A dark room.")
        self.assertTrue(resp["prompt"])
        self.assertIn("elapsed", resp)

    def test_recv_nonblocking(self):
        async def run():
            conn, state, _ = mkconn()
            state["lock"] = asyncio.Lock()
            conn.feed(b"buffered")
            return await automud._do_op({"op": "recv", "block": False}, state, conn)
        resp = self._run(run())
        self.assertEqual(resp["data"], "buffered")

    def test_wait_matches_regex(self):
        async def run():
            conn, state, _ = mkconn()
            state["lock"] = asyncio.Lock()
            task = asyncio.create_task(automud._do_op(
                {"op": "wait", "pattern": r"You have (died|won)", "max": 2.0},
                state, conn))
            await asyncio.sleep(0.1)
            conn.feed(b"...You have died. Sorry.")
            return await task
        resp = self._run(run())
        self.assertTrue(resp["matched"])
        self.assertIn("You have died", resp["data"])

    def test_wait_timeout_leaves_data_unread(self):
        async def run():
            conn, state, _ = mkconn()
            state["lock"] = asyncio.Lock()
            conn.feed(b"unrelated text")
            r = await automud._do_op({"op": "wait", "pattern": "NOPE", "max": 0.2},
                                     state, conn)
            return r, automud._unread(state)
        resp, unread = self._run(run())
        self.assertFalse(resp["matched"])
        self.assertEqual(unread, "unrelated text")        # not drained on a miss

    def test_wait_gmcp_update(self):
        async def run():
            conn, state, _ = mkconn()
            state["lock"] = asyncio.Lock()
            task = asyncio.create_task(automud._do_op(
                {"op": "wait", "gmcp": "Char.Vitals", "max": 2.0}, state, conn))
            await asyncio.sleep(0.1)
            automud._apply_gmcp(state, "Char.Vitals", {"hp": 50})
            return await task
        resp = self._run(run())
        self.assertTrue(resp["matched"])
        self.assertEqual(resp["gmcp"], {"hp": 50})

    def test_wait_bad_regex(self):
        async def run():
            conn, state, _ = mkconn()
            state["lock"] = asyncio.Lock()
            return await automud._do_op({"op": "wait", "pattern": "("}, state, conn)
        resp = self._run(run())
        self.assertFalse(resp["ok"])
        self.assertIn("bad regex", resp["error"])

    def test_gmcp_op_sends_subneg(self):
        async def run():
            conn, state, w = mkconn()
            state["lock"] = asyncio.Lock()
            r = await automud._do_op(
                {"op": "gmcp", "package": "Char.Skills.Get", "data": ""}, state, conn)
            return r, bytes(w.buf)
        resp, wire = self._run(run())
        self.assertTrue(resp["ok"])
        self.assertIn(bytes([IAC, SB, OPT_GMCP]) + b"Char.Skills.Get" + bytes([IAC, SE]),
                      wire)

    def test_status_reports_options_and_ages(self):
        async def run():
            conn, state, _ = mkconn()
            state["lock"] = asyncio.Lock()
            conn.feed(bytes([IAC, WILL, OPT_GMCP, IAC, DO, OPT_NAWS]))
            automud._apply_gmcp(state, "Char.Vitals", {"hp": 1})
            st = await automud._do_op({"op": "status"}, state, conn)
            sta = await automud._do_op({"op": "state"}, state, conn)
            return st, sta
        st, sta = self._run(run())
        self.assertIn("GMCP", st["options"]["server"])
        self.assertIn("NAWS", st["options"]["client"])
        self.assertEqual(st["encoding"], "utf-8")
        self.assertIn("last_rx_age", st)
        self.assertIn("uptime", st)
        self.assertIn("Char.Vitals", sta["ages"])

    def test_unknown_op(self):
        async def run():
            conn, state, _ = mkconn()
            state["lock"] = asyncio.Lock()
            return await automud._do_op({"op": "frobnicate"}, state, conn)
        self.assertFalse(self._run(run())["ok"])


class TestFilesAndPids(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="automud-test-")
        automud._set_state_dir(os.path.join(self.tmp, "default"))

    def tearDown(self):
        automud._set_state_dir(os.path.join(automud.STATE_BASE, "default"))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_session_roundtrip(self):
        automud._write_session({"host": "h", "port": 23, "token": "t"})
        self.assertEqual(automud._read_session(),
                         {"host": "h", "port": 23, "token": "t"})

    def test_read_missing_session(self):
        self.assertIsNone(automud._read_session())

    def test_pid_alive_self(self):
        self.assertTrue(automud._pid_alive(os.getpid()))

    def test_pid_alive_dead_child(self):
        self.assertFalse(automud._pid_alive(self._spawn_dead_pid()))

    def _spawn_dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def test_control_clears_stale_session(self):
        dead = self._spawn_dead_pid()
        automud._write_session({"host": "h", "port": 23, "control_port": 1,
                                "token": "t", "pid": dead})
        r = automud._control("status")
        self.assertFalse(r["ok"])
        self.assertIn("stale", r["error"])
        self.assertFalse(os.path.exists(automud.SESSION_JSON))

    def test_control_no_session(self):
        r = automud._control("status")
        self.assertFalse(r["ok"])
        self.assertIn("no active session", r["error"])

    def test_connect_lock_conflict_and_reclaim(self):
        automud._ensure_dir()
        automud._acquire_connect_lock()
        with self.assertRaises(RuntimeError):             # our own live pid holds it
            automud._acquire_connect_lock()
        automud._release_connect_lock()
        with open(automud.CONNECT_LOCK, "w") as f:        # stale lock: dead pid
            f.write(str(self._spawn_dead_pid()))
        automud._acquire_connect_lock()                   # reclaimed without error
        automud._release_connect_lock()

    def test_log_tail_lines(self):
        automud._ensure_dir()
        with open(automud.OUT_LOG, "w", encoding="utf-8") as f:
            f.write("".join("line %d\n" % i for i in range(100)))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = automud.cmd_log(tail=3)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "line 97\nline 98\nline 99\n")

    def test_log_full_stream(self):
        automud._ensure_dir()
        with open(automud.OUT_LOG, "w", encoding="utf-8") as f:
            f.write("alpha\nbeta")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = automud.cmd_log(tail=0)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "alpha\nbeta\n")

    def test_log_missing(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(automud.cmd_log(tail=0), 1)


class TestFinish(unittest.TestCase):
    def test_disconnected_exits_3(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = automud._finish({"ok": True, "data": "bye", "connected": False},
                                 False, "send failed")
        self.assertEqual(rc, 3)
        self.assertEqual(out.getvalue(), "bye\n")
        self.assertIn("closed", err.getvalue())

    def test_ok_exits_0(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = automud._finish({"ok": True, "data": "hi", "connected": True},
                                 False, "send failed")
        self.assertEqual(rc, 0)

    def test_json_mode_emits_envelope(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = automud._finish({"ok": True, "data": "hi", "connected": True,
                                  "prompt": True}, True, "send failed")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()),
                         {"ok": True, "data": "hi", "connected": True, "prompt": True})

    def test_error_exits_1(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = automud._finish({"ok": False, "error": "nope"}, False, "send failed")
        self.assertEqual(rc, 1)
        self.assertIn("nope", out.getvalue())


if __name__ == "__main__":
    unittest.main()
