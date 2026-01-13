# txtmux

A Python terminal multiplexer built with [Textual](https://textual.textualize.io/) and [pyte](https://github.com/selectel/pyte). Create terminal sessions, detach from them (leaving the shell running), and reattach later.

## Installation

```bash
# Install from source after cloning
git clone https://github.com/yourusername/txtmux.git
uv tool install ./txtmux

# Or add to a project
uv add txtmux
```

## Quick Start

```bash
# Start a new session (starts server automatically if needed)
txtmux

# Create a named session
txtmux new -s work

# List all sessions
txtmux ls

# Attach to a session by name or id
txtmux a -t work
txtmux attach -t 0

# Detach from inside a session
# Press: Ctrl+B then D

# Kill the server daemon
txtmux kill-server
```

## Commands

| Command | Aliases | Arguments | Description |
|---------|---------|-----------|-------------|
| `new-session` | `new` | `-s, --name` | Create a new session |
| `attach-session` | `attach`, `a` | `-t, --target` | Attach to existing session |
| `list-sessions` | `ls` | | List all sessions |
| `kill-session` | | `-t, --target` | Kill a session |
| `kill-server` | | | Kill the server daemon |

Running `txtmux` without arguments creates a new session.

## Key Bindings

| Key | Action |
|-----|--------|
| `Ctrl+B D` | Detach from session |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ cli.py (CLI)                                                    │
│  - argparse subcommands: new-session, attach, list-sessions     │
│  - ensure_server_running() spawns daemon if needed              │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│ client.py (Textual App)                                         │
│  - TerminalApp: Header + TerminalPane + StatusBar               │
│  - Ctrl+B D prefix handling for detach                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │ Unix socket
┌──────────────────────────▼──────────────────────────────────────┐
│ server.py (Daemon)                                              │
│  - SessionServer: accepts connections, dispatches messages      │
│  - Manages PTY forwarding loops per session                     │
│  - Uses SessionManager from session.py                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│ session.py                                                      │
│  - Session: id, name, panes dict, active_pane_id                │
│  - Pane: id, pty_fd, pid, pyte.HistoryScreen                    │
│  - SessionManager: create/destroy/attach/detach                 │
└─────────────────────────────────────────────────────────────────┘
```

**Protocol** (`protocol.py`): Binary messages with 8-byte header (type: u32, length: u32) + payload.

**Socket location**: `/tmp/textual-tmux-$USER/default`

## Comparison to tmux

txtmux implements a minimal subset of tmux functionality:

| Feature | txtmux | tmux |
|---------|--------|------|
| Sessions | Yes | Yes |
| Detach/Attach | Yes | Yes |
| Named sessions | Yes | Yes |
| Multiple windows | No | Yes |
| Pane splits | No | Yes |
| Copy mode | No | Yes |
| Configuration file | No | Yes |
| Mouse support | No | Yes |

## Development

```bash
# Run tests
uv run pytest

# Run with mypy
uv run mypy --strict src/txtmux/

# Run server in foreground (for debugging)
uv run python -m txtmux.server

# Clean up socket files
rm -rf /tmp/textual-tmux-*/
```

## License

MIT
