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
automud sites                        # list verified public targets
automud connect achaea               # by name, or: automud connect <host> <port>
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
| `sites` | directory of ~45 verified public targets (MUDs, MOOs, BBSes, services) |

Every entry in `sites` was verified reachable by a live probe. Connect to any of them by
name: `automud connect aardwolf`, `automud connect telehack`, `automud connect fics`.

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

## Driving it with an agent

There is no agent mode and nothing to configure: an agent drives the same verbs a person
does, and everything past that is behavior. One pattern is worth writing down because it
falls out of the design. Since the agent holding the session sits between the user and
the wire, it can be the user's entire interface to the game and re-voice everything that
crosses it: a total conversion of the setting, applied live. The user names the world
they want in conversation ("Achaea, but it is Paris on the 14th of July 1789 and I am a
hated noble"), and from then on the agent translates both directions, the user's stated
intent into real commands and the raw replies into the agreed fiction, mechanics
included if the user wants them (vitals re-skinned as a HUD). What keeps it honest:

- The theme is the user's to pick. If they have not said what they want, ask; do not
  invent on their behalf.
- Translate, do not decide. The user's intent picks the command, and the server's actual
  reply decides what happened. Failures and deaths render in-fiction, but they render.
- Keep the mapping stable: the same room, denizen, or stat appears under the same
  converted name every time.
- `out.log` keeps the untranslated transcript, so the fiction is always auditable
  against what the server really said.

## Local single-player (Cataclysm)

`automud connect cdda` launches a local game of [Cataclysm: Dark Days
Ahead](https://cataclysmdda.org) and drives it through the same verbs, so there is a
world to play even with no MUD to connect to, an obligate single-player mode for when
every public server is finally gone. It is a second *backend*: a persistent tmux session
holds the game open the way the telnet daemon holds a socket, and `send` / `recv` /
`wait` / `state` / `status` / `close` all work against it.

Requirements: `tmux` and a terminal `cataclysm` binary. On Windows the game runs in WSL
and automud bridges to it automatically (`AUTOMUD_CDDA_DISTRO` selects the distro;
`AUTOMUD_CDDA_DIR` / `AUTOMUD_CDDA_BIN` / `AUTOMUD_CDDA_LAUNCH` point at the binary).

```
automud connect cdda
automud send north              # move; the reply is the message log + a status line
automud send examine east       # a named action, not the seven keystrokes e-x-a-m-i-n-e
automud send nearby             # list items/creatures around you (no map needed)
automud state                   # the character as JSON: stats, hp, needs, place, threats
automud send help               # the action vocabulary for the current screen
```

**The map is obscured on purpose.** A blurry ASCII minimap is neither reliable for a
language model to parse nor necessary to one that reads a room description, so game-mode
output is the message log plus a structured status line (vitals, place, time, nearby
threats), never the tilemap. Ask for spatial detail when you want it: `send look`,
`send examine <dir>`, `send nearby`.

**Actions, not raw keys.** `send` understands word directions (`north`, `se`), named
keys (`enter`, `escape`), and named game actions (`examine`, `pickup`, `eat`, `wait`,
`wield`, ...) that map to the right key. An unrecognized word is reported, not typed out
letter by letter. Single characters are still sent literally, so raw CDDA keys work too.

**Character creation is one line per choice.** `send newgame` drives the whole
pre-chargen main menu (a maze with no readable cursor where New Game and World look
identical) to the character-creation screen, and each choice is its own verb:

```
automud send newgame                             # main menu -> character creation
automud send options profession                  # list every choice on a tab (they scroll)
automud send scenario Sheltered
automud send profession "Sheltered Survivor"    # or: send class ...
automud send stats 8 10 10 10                    # STR DEX INT PER; reports the real values
automud send trait Fleet-Footed
automud send name Dougal
automud send finalize
```

Each reports what it actually committed (and refuses a locked or mismatched entry rather
than silently selecting the wrong one), and in chargen `state` returns the build so far
(scenario, profession, stats, name). Since the option lists are long and scroll off
screen (roughly 60 scenarios, 175 professions, 120 backgrounds, 200 traits), `send
options [TAB]` enumerates every choice on a tab so nothing is hidden below the fold. The missing-mod prompts a fresh world throws are
cleared automatically.

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
