# Decktate: how it works

Technical notes for people who want to understand, change or debug Decktate.
For installing and using it, see the [README](../README.md).

## Overview

```
  Claude Code sessions                     Decktate                         Stream Deck
 ┌────────────────────┐   HTTP hooks    ┌───────────────┐   USB HID      ┌─────────────┐
 │ hooks (settings)   │ ──────────────▶ │ server ─▶ hub │ ─────────────▶ │ 15 keys     │
 │ status line        │ ──────────────▶ │        board  │ ◀───────────── │ key presses │
 └────────────────────┘                 └───────┬───────┘                └─────────────┘
          ▲                                     │
          │ typing (AppleScript, CLI)           ├─▶ microphone ─▶ Deepgram (speech to text)
          └───────── terminal ◀─────────────────┤
                     screen text ──────────────▶└─▶ ElevenLabs / `say` ─▶ speaker
```

Claude Code tells Decktate what happens through hooks. Decktate answers two
of them (permission requests and questions) with what you press. Everything
Claude Code has no interface for goes through the terminal: typing prompts
and keys into it, and reading its screen.

## Modules

| Path | What it does |
| --- | --- |
| `core/server.py` | HTTP server on `127.0.0.1`: `POST /hook`, `POST /statusline`, `GET /health`; checks the token |
| `core/hub.py` | The state of all sessions; holds permission requests and questions open until a key decides |
| `core/redact.py` | Removes secrets and code blocks from text before it is spoken |
| `core/audit.py` | Appends every decision made on the deck to `audit.jsonl` |
| `claude/install.py` | Adds and removes the hooks and the status line in `~/.claude/settings.json` |
| `claude/discover.py` | Finds the Claude Code sessions running on this Mac; quits one |
| `inject/` | Types into a session's terminal and reads its screen |
| `speech/stt.py` | Dictation: microphone to Deepgram over a websocket |
| `speech/tts.py` | Voice: ElevenLabs or the Mac's `say`, on a background thread |
| `speech/audio.py` | Microphones, speakers, playback, chimes (PortAudio via `sounddevice`) |
| `speech/sysvol.py` | The Mac's output volume |
| `ui/board.py` | What is on which key, and what a press does |
| `ui/render.py`, `ui/icons.py` | Draws the keys (Pillow, 4x supersampled) |
| `backend/hid.py`, `backend/sim.py` | The real deck, and a simulator served as a web page |
| `macapp.py` | The menu bar app and the `Decktate.app` bundle |
| `cli.py`, `config.py` | Commands and configuration |

## Hooks

While Decktate runs, `~/.claude/settings.json` contains one HTTP hook per
event, posting to `http://127.0.0.1:<port>/hook` with the token in a header:

`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `PermissionRequest`, `PermissionDenied`, `Notification`,
`SubagentStart`, `SubagentStop`, `PreCompact`, `Stop`, `StopFailure`,
`SessionEnd`.

The hook headers also carry the terminal's variables (`TERM_PROGRAM`,
`ITERM_SESSION_ID`, `TMUX_PANE`, ...), which is how Decktate knows where a
session runs.

| Feature | How |
| --- | --- |
| Session state | every event updates the session in the hub |
| Approve / reject / always | the `PermissionRequest` request is held open until a key is pressed; the response carries `allow` or `deny`, and for *always* a rule with `destination: session` |
| Questions | `PreToolUse` for the question tool is held open; the response allows the tool with the chosen answers filled in |
| Spoken summary | `Stop` carries the last assistant message |
| Stats | the status line command posts its input to `/statusline` |

**Held requests.** A held request waits up to `permission_hold_s` (120
seconds) for a key. It is released without a decision, so that Claude Code
shows its own prompt, when the time runs out, the deck is unplugged, the
session moves on, the session is detached, or Decktate quits. The hooks have
a timeout slightly above the hold time and fail open. A decision is only ever
produced by a key press.

**Installing.** Decktate adds its hooks at start and removes them at quit
(`auto_hooks`). It only touches its own entries, writes the file atomically,
and keeps one copy of the original (`settings.json.decktate-backup`). If it
is killed, the hooks stay behind; they fail open and are replaced at the next
start. `decktate install` / `uninstall` do the same by hand.

**Status line.** Claude Code allows one status line command. Decktate puts
its own there, which posts the data to Decktate and then runs the status line
that was configured before, so that one keeps working.

## Sessions

- **Discovery.** Claude Code keeps a file per running process in
  `~/.claude/sessions/<pid>.json` (session id, folder, name, idle or busy).
  Decktate reads these every three seconds, so that sessions are known before
  they send a hook, and drops a session when its process is gone. This is
  not a documented interface.
- **Attached and detached.** Every running session is attached unless you
  detached it. For a detached session the hooks return at once with no
  effect.
- **Keys.** A session keeps its key for as long as it exists. Free keys are
  filled from the queue (requests first, then by latest activity), and gaps
  are closed. `/clear` and `/resume` end one session and start another in the
  same terminal; the new one is matched by its terminal tab and takes over
  key and selection.
- **Selection.** Which session you talk to changes only on a key press.
- **Quit.** Sends `SIGTERM` to the session's process.

## Typing into the terminal

Dictated prompts, slash commands, Esc (interrupt), Shift+Tab (mode) and
Ctrl+Enter (send now) have to be typed, because Claude Code has no other way
to receive them.

| Terminal | How | Notes |
| --- | --- | --- |
| iTerm2 | AppleScript `write text` to the session by id | works in the background |
| Terminal.app | AppleScript `do script` for text; keystrokes for single keys | keys need Accessibility and bring the window forward |
| kitty, WezTerm | `kitty @ send-text`, `wezterm cli send-text` | remote control must be enabled |
| tmux | `tmux send-keys` | used when the session runs in tmux |
| others | keystrokes through System Events | Accessibility; brings the window forward |

Approvals and answers never use this; they go through the hooks.

## Reading the terminal

Claude Code fires no hook when the mode is switched in the terminal, or when
a typed message waits in its queue. In iTerm2 and Terminal.app Decktate reads
the visible text of the session's tab (AppleScript `contents`) and looks at
the lines under the prompt box for Claude Code's own wording ("plan mode on",
"auto mode on", "... to send now", "2 shells, 1 monitor"). The status line is not parsed, so a
custom one does not matter. The selected session is read about once a second
and at once when it is selected; the others every six seconds.

## Speech

- **Dictation.** The microphone opens the moment the key goes down; the
  websocket to Deepgram (`/v1/listen`, Nova-3, 16 kHz linear PCM) is made in
  the background and the audio recorded meanwhile is sent once it is up. On
  release, 0.3 seconds more are recorded, everything is sent, and the stream
  is finalized. The text goes to the session that was selected when the key
  went down.
- **Voice.** ElevenLabs (`/v1/text-to-speech/<voice>/stream`, 24 kHz PCM)
  or the Mac's `say`. Markup is removed, code blocks are skipped, and keys,
  tokens and passwords are redacted first. A finished answer is read out in
  full, split at sentence ends into pieces of about 600
  characters, so the voice starts at once and can be stopped between pieces. With the
  Mac's volume at zero nothing is spoken and no voice is requested.

## Safety

- The server listens on loopback only and requires a token
  (`~/.config/decktate/token`, created on first run, mode 600).
- "Always allow" rules are stored for the session only.
- Every decision is appended to `audit.jsonl`.
- API keys live in `~/.config/decktate/.env` and are never logged.

## Files in `~/.config/decktate`

| File | Content |
| --- | --- |
| `config.yaml` | settings; see [config.example.yaml](../config.example.yaml) |
| `.env` | `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` |
| `token` | the secret the hooks send |
| `audit.jsonl` | decisions made on the deck |
| `layout.json` | which session is on which key, and which is selected |
| `detached.json` | sessions you detached (kept for 30 days) |
| `stats.json` | last reported context, cost and quota |
| `tts-cache/` | audio of short fixed phrases |
| `decktate.log` | output of the menu bar app |

## The app

`decktate install-app` writes `~/Applications/Decktate.app`: a small compiled
launcher that embeds Python and runs this checkout, so macOS knows the
process as "Decktate" and grants microphone and automation permissions to
it. The bundle is ad-hoc signed; rebuilding it can make macOS ask for the
permissions again. It is a menu-bar-only app (`LSUIElement`).

## Commands

    uv run decktate                 # run in the terminal (Ctrl+C quits)
    uv run decktate --sim           # no deck: the board as a web page on http://127.0.0.1:8766/
    uv run decktate app             # run as the menu bar app
    uv run decktate install-app     # create Decktate.app
    uv run decktate doctor          # check deck, Claude Code, hooks, audio, keys
    uv run decktate devices         # list microphones and speakers
    uv run decktate say "hello"     # test the voice
    uv run decktate install         # add the hooks by hand (see auto_hooks)
    uv run decktate uninstall       # remove them

## Development

    uv run pytest                   # simulator only: no deck, network or microphone
    uv run python tests/shots.py    # re-render docs/screens (every page) and docs/decktate.png

The tests drive the board through a simulated deck with a fake clock, fake
speech and fake terminals.

## Status and limits

- Tested against Claude Code 2.1.326 on macOS with a Stream Deck Original
  (15 keys). Approvals and question menus were tested end to end with a real
  session; most other behaviour against the simulator.
- macOS only.
- Two things rely on Claude Code internals that are not documented and may
  change: the session files in `~/.claude/sessions`, and the wording of the
  mode line on its screen.
- Claude Code does not report when it takes a queued message. In iTerm2 and
  Terminal.app the *send now* key follows what the screen says; in other
  terminals it is offered for 45 seconds after the deck sent text to a busy
  session, or until the turn ends.
