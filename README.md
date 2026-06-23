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
automud connect --demo achaea      # or: automud connect <host> <port>
automud send 2                      # send a line, print the reply
automud send Maelvorn
automud recv                         # drain any new output
automud state                        # structured game state (GMCP) as JSON
automud status
automud close
```

| verb | what it does |
|------|--------------|
| `connect HOST PORT` / `--demo NAME` | open a session and start the daemon |
| `send TEXT` | send one line, print what comes back |
| `recv` | print any new output |
| `state [--key PKG]` | captured GMCP state as JSON (e.g. `--key Char.Vitals`) |
| `status` | connection and vitals summary |
| `log [--tail N]` | full session transcript |
| `close` | end the session and stop the daemon |

Built-in demo targets: `achaea`, `zork` (telehack.com), `chess` (freechess.org).

## Behaviour

- **Smart waiting.** `send` and `recv` return as soon as the server stops talking, either a
  telnet GA/EOR prompt marker or output going quiet, so you never guess a sleep duration.
  `--max` caps the wait and `--quiet` sets the idle threshold.
- **GMCP.** It negotiates GMCP and parses the structured state modern MUDs push (health,
  room, exits, skills) into JSON for `state`. Options it does not implement (compression,
  MSDP, MXP) are refused rather than mishandled.
- **One session at a time**, held by a background daemon; a new `connect` replaces it.
  Session state and the transcript live under a temp directory (override with `AUTOMUD_DIR`).

## License

MIT. See [LICENSE](LICENSE).
