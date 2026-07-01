# AutoMUD

A persistent telnet/MUD session you drive with small, discrete commands. There is no
language model and no API key inside it: the intelligence is whoever runs it, a person, a
script, or an autonomous agent. It exists because a raw `telnet` session is interactive and
blocking, so it cannot be held open across separate shell commands. AutoMUD keeps the
connection alive in a small background daemon and exposes simple verbs against it.

## Install

```
pipx install git+https://github.com/CharlesCNorton/automud
```

Or from a clone:

```
pip install .
```

Standard library only, Python 3.8+.

## Use

```
automud connect --demo achaea       # or: automud connect <host> <port>
automud send 2                       # send a line, print the reply
automud send Maelvorn
automud recv                          # drain any new output
automud wait --for "You are hungry"   # block until output matches a regex
automud state                         # structured game state (GMCP) as JSON
automud status
automud close
```

| verb | what it does |
|------|--------------|
| `connect HOST PORT` / `--demo NAME` | open a session and start the daemon (`--tls`, `--encoding ENC`, `--idle-exit SEC`, `--debug`) |
| `send [TEXT]` | send one line, print what comes back (omit TEXT for a blank line; `--stdin` reads the text from stdin) |
| `recv` | print any new output (`--nowait` returns whatever is buffered immediately) |
| `wait --for REGEX` / `--gmcp PKG` | block until output matches, or a GMCP package updates |
| `gmcp PACKAGE [JSON]` | send a GMCP message (e.g. `Char.Skills.Get`) |
| `state [--key PKG] [--times]` | captured GMCP state as JSON (`--times`: seconds since each package updated) |
| `status` | connection, options, vitals summary |
| `log [--tail N]` | session transcript (last N lines with `--tail`) |
| `close` | end the session and stop the daemon |
| `kill` | force-stop a wedged daemon and clear the session |

Built-in demo targets: `achaea`, `zork` (telehack.com), `chess` (freechess.org).

Every reading verb takes `--json` and prints one structured object (`data`, `prompt`,
`connected`, `elapsed`, ...) instead of raw text, which is the natural mode for driving it
from a program. A minimal agent loop is just:

```
automud connect example.com 4000 --json
automud send look --json                 # {"ok": true, "data": "...", "prompt": true, ...}
automud wait --for "^You (win|die)" --max 120 --json
```

Exit codes: `0` ok, `1` failure, `2` usage error, `3` the operation succeeded but the
connection is closed.

## Behaviour

- **Smart waiting.** `send` and `recv` return as soon as the server stops talking, either a
  telnet GA/EOR prompt marker or output going quiet, so you never guess a sleep duration.
  `--max` caps the wait and `--quiet` sets the idle threshold. `wait` extends this to
  regex/GMCP conditions.
- **GMCP.** It negotiates GMCP and parses the structured state modern MUDs push (health,
  room, exits, skills) into JSON for `state`. Standard list deltas (`Room.AddPlayer`,
  `Char.Afflictions.Add`, ...) are applied to their lists, and `Comm.Channel.Text` is kept
  as a bounded `Comm.Channel.History`. TTYPE, NAWS and CHARSET negotiation are answered;
  options it does not implement (compression, MSDP, MXP) are refused rather than
  mishandled.
- **Encodings and TLS.** `--encoding` sets the wire charset (default `utf-8`; use
  `latin-1` or `cp437` for older servers), and telnet CHARSET negotiation can switch it
  when the server asks. `--tls` wraps the connection (`--tls-insecure` for self-signed
  certificates).
- **One session per name**, held by a background daemon; a new `connect` replaces it, and
  `-s NAME` gives you independent parallel sessions. Session state and transcripts live
  under a per-user state directory (override with `AUTOMUD_DIR`); the previous session's
  transcript is kept as `out.prev.log`.
- **Robust lifecycle.** TCP keepalive is enabled, a dead daemon is detected by pid and its
  stale session cleared, concurrent connects are serialized by a lock, a wedged daemon is
  force-killed on reconnect (or by `kill`), and `--idle-exit` stops a forgotten daemon.

## Security

The control channel is a localhost-only socket authenticated by a per-session random
token. State lives in a per-user directory (`$XDG_RUNTIME_DIR/automud`, else
`<tempdir>/automud-<uid>` on POSIX, `%TEMP%\automud` on Windows) that is created `0700`
and refused if another user owns it. Prefer `automud send --stdin` for passwords: argv is
visible to other local processes, and shell history persists. Plain telnet is cleartext;
use `--tls` where the server offers it.

## Tests

```
python -m unittest discover -s tests -t .
```

## License

MIT. See [LICENSE](LICENSE).
