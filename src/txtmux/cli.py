#!/usr/bin/env python3
"""CLI entry point for txtmux."""

import argparse
import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

from rich.console import Console
from rich.table import Table

from txtmux.protocol import (
    MessageType,
    decode,
    decode_error,
    decode_session_info,
    encode_identify,
    encode_list_sessions,
    encode_new_session,
)
from txtmux.server import get_pid_file_path, get_socket_path

SessionInfo = tuple[int, str, int, int, int, int, float, int]


def is_server_running(socket_path: str) -> bool:
    """Check if server is running by attempting to connect."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(socket_path)
        sock.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


def ensure_server_running(socket_path: str) -> None:
    """Start server if not running, wait for it to be ready."""
    if is_server_running(socket_path):
        return

    subprocess.Popen(
        [sys.executable, "-m", "txtmux.server", "--daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    max_attempts = 50
    for _ in range(max_attempts):
        time.sleep(0.1)
        if is_server_running(socket_path):
            return

    raise RuntimeError("Failed to start server")


async def send_and_receive(
    socket_path: str,
    *messages: bytes,
    expect_multiple: bool,
) -> list[SessionInfo]:
    """Connect to server, send messages, receive responses."""
    reader, writer = await asyncio.open_unix_connection(socket_path)

    try:
        size = shutil.get_terminal_size()
        identify_msg = encode_identify(size.columns, size.lines)
        writer.write(identify_msg.encode())

        for msg in messages:
            writer.write(msg)
        await writer.drain()

        results: list[SessionInfo] = []
        buffer = b""
        timeout = 0.5 if expect_multiple else 5.0

        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
                if not chunk:
                    break
                buffer += chunk

                while True:
                    decoded_msg, buffer = decode(buffer)
                    if decoded_msg is None:
                        break

                    if decoded_msg.msg_type == MessageType.SESSION_INFO:
                        results.append(decode_session_info(decoded_msg.payload))
                    elif decoded_msg.msg_type == MessageType.ERROR:
                        raise RuntimeError(decode_error(decoded_msg.payload))

                    if not expect_multiple:
                        return results

            except asyncio.TimeoutError:
                break

        return results

    finally:
        writer.close()
        await writer.wait_closed()


async def create_session(socket_path: str, name: str) -> tuple[int, str]:
    """Create a new session and return (session_id, session_name)."""
    msg = encode_new_session(name)
    results = await send_and_receive(socket_path, msg.encode(), expect_multiple=False)
    if not results:
        raise RuntimeError("No response from server")
    session_id, session_name, _, _, _, _, _, _ = results[0]
    return session_id, session_name


async def list_sessions(socket_path: str) -> list[SessionInfo]:
    """List all sessions."""
    msg = encode_list_sessions()
    return await send_and_receive(socket_path, msg.encode(), expect_multiple=True)


async def find_session(
    socket_path: str,
    target: str,
) -> tuple[int, str]:
    """Find session by name or id, return (session_id, session_name)."""
    sessions = await list_sessions(socket_path)
    if not sessions:
        raise RuntimeError("No sessions found")

    try:
        target_id = int(target)
        for session_id, name, _, _, _, _, _, _ in sessions:
            if session_id == target_id:
                return session_id, name
        raise RuntimeError(f"Session {target_id} not found")
    except ValueError:
        for session_id, name, _, _, _, _, _, _ in sessions:
            if name == target:
                return session_id, name
        raise RuntimeError(f"Session '{target}' not found")


def cmd_new_session(args: argparse.Namespace) -> None:
    """Handle new-session command."""
    from txtmux.client import TerminalApp

    socket_path = get_socket_path()
    ensure_server_running(socket_path)

    name = args.name if args.name else ""
    session_id, session_name = asyncio.run(create_session(socket_path, name))

    app = TerminalApp(
        socket_path=socket_path,
        session_id=session_id,
        session_name=session_name,
    )
    app.run()


def cmd_attach(args: argparse.Namespace) -> None:
    """Handle attach command."""
    from txtmux.client import TerminalApp

    socket_path = get_socket_path()
    if not is_server_running(socket_path):
        print("No server running", file=sys.stderr)
        sys.exit(1)

    target = getattr(args, "target", None)
    if target is None:
        sessions = asyncio.run(list_sessions(socket_path))
        if not sessions:
            print("No sessions", file=sys.stderr)
            sys.exit(1)
        if len(sessions) > 1:
            print("Multiple sessions exist. Use -t to specify target:", file=sys.stderr)
            for sid, name, *_ in sessions:
                print(f"  {sid}: {name}", file=sys.stderr)
            sys.exit(1)
        session_id, session_name, *_ = sessions[0]
    else:
        session_id, session_name = asyncio.run(find_session(socket_path, target))

    app = TerminalApp(
        socket_path=socket_path,
        session_id=session_id,
        session_name=session_name,
    )
    app.run()


def cmd_list_sessions(args: argparse.Namespace) -> None:
    """Handle list-sessions command."""
    _ = args
    socket_path = get_socket_path()
    if not is_server_running(socket_path):
        print("No server running", file=sys.stderr)
        sys.exit(1)

    sessions = asyncio.run(list_sessions(socket_path))
    if not sessions:
        print("No sessions")
        return

    console = Console()
    table = Table(title="Sessions")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Created", style="dim")
    table.add_column("Attached", justify="right")
    table.add_column("Size")
    table.add_column("PID")

    for session_id, name, _, pid, width, height, created_at, attached_count in sessions:
        created_str = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M")
        table.add_row(
            str(session_id),
            name,
            created_str,
            str(attached_count),
            f"{width}x{height}",
            str(pid),
        )

    console.print(table)


def cmd_kill_session(args: argparse.Namespace) -> None:
    """Handle kill-session command."""
    _ = args
    print("kill-session not implemented yet", file=sys.stderr)
    sys.exit(1)


def cmd_kill_server(args: argparse.Namespace) -> None:
    """Handle kill-server command."""
    _ = args
    pid_file = get_pid_file_path()
    if not os.path.exists(pid_file):
        print("No server running (no PID file)", file=sys.stderr)
        sys.exit(1)

    with open(pid_file) as f:
        pid = int(f.read().strip())

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to server (PID {pid})")
    except ProcessLookupError:
        print(f"Server not running (stale PID file for {pid})", file=sys.stderr)
        os.unlink(pid_file)
        sys.exit(1)
    except PermissionError:
        print(f"Permission denied killing server (PID {pid})", file=sys.stderr)
        sys.exit(1)


def cmd_auto(args: argparse.Namespace) -> None:
    """Default behavior: create new session (matches tmux)."""
    args.name = None
    cmd_new_session(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="txtmux",
        description="A minimal terminal multiplexer",
    )
    subparsers = parser.add_subparsers(dest="command")

    new_session_parser = subparsers.add_parser(
        "new-session", aliases=["new"], help="Create a new session"
    )
    new_session_parser.add_argument("-s", "--name", help="Session name")

    attach_parser = subparsers.add_parser(
        "attach-session",
        aliases=["attach", "a"],
        help="Attach to an existing session",
    )
    attach_parser.add_argument(
        "-t", "--target", help="Session name or id (default: first session)"
    )

    subparsers.add_parser(
        "list-sessions", aliases=["ls"], help="List all sessions"
    )

    kill_session_parser = subparsers.add_parser(
        "kill-session", help="Kill a session"
    )
    kill_session_parser.add_argument(
        "-t", "--target", required=True, help="Session name or id"
    )

    subparsers.add_parser("kill-server", help="Kill the server daemon")

    args = parser.parse_args()

    if args.command is None:
        cmd_auto(args)
        return

    command_map = {
        "new-session": cmd_new_session,
        "new": cmd_new_session,
        "attach-session": cmd_attach,
        "attach": cmd_attach,
        "a": cmd_attach,
        "list-sessions": cmd_list_sessions,
        "ls": cmd_list_sessions,
        "kill-session": cmd_kill_session,
        "kill-server": cmd_kill_server,
    }

    handler = command_map.get(args.command)
    if handler is None:
        raise RuntimeError(f"Unknown command: {args.command}")
    handler(args)


if __name__ == "__main__":
    main()
