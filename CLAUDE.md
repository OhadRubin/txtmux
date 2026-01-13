# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**txtmux** - A Python terminal multiplexer (tmux clone) using Textual for TUI and pyte for terminal emulation. Implements client-server architecture over Unix domain sockets with detach/reattach functionality.

## Project Structure

```
src/txtmux/          # Main package
  cli.py             # CLI entry point
  client.py          # Textual TUI app
  server.py          # Daemon server
  session.py         # Session/Pane management
  protocol.py        # Binary protocol
  pty_handler.py     # PTY operations
  terminal_widget.py # Terminal widget
tests/               # Test files
docs/                # Plans and docs
```

## Commands

```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_server.py -v

# Run single test
uv run pytest tests/test_server.py::TestSessionServer::test_input_message_causes_output -v

# CLI usage
txtmux                    # new session (starts server if needed)
txtmux new -s work        # named session
txtmux a -t work          # attach by name or id
txtmux ls                 # list all sessions
txtmux kill-server        # kill server daemon

# Or via uv run
uv run txtmux
uv run python -m txtmux

# Run server directly (for debugging)
uv run python -m txtmux.server           # foreground
uv run python -m txtmux.server --daemon  # background

# Clean up
txtmux kill-server
rm -rf /tmp/textual-tmux-*/
```

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
- Message types: IDENTIFY, NEW_SESSION, ATTACH, DETACH, LIST_SESSIONS, RESIZE, INPUT, OUTPUT, ERROR, SESSION_INFO, SHELL_EXITED

**Terminal Widget** (`terminal_widget.py`):
- `TerminalPane` widget with two modes: direct PTY (standalone) or network (connected to server)
- Uses pyte.Screen for terminal emulation, renders to Rich Text
- Network mode: sends INPUT/RESIZE messages, receives OUTPUT

**PTY Handler** (`pty_handler.py`): Low-level PTY operations - spawn_shell(), read_pty(), write_pty(), set_pty_size()

## Socket Location

Default: `/tmp/textual-tmux-$USER/default` (socket) and `default.pid` (PID file)