"""Session server daemon for terminal multiplexer."""

import asyncio
import atexit
import os
import signal
import sys
from asyncio import StreamReader, StreamWriter

from txtmux.protocol import (
    Message,
    MessageType,
    decode,
    decode_attach,
    decode_identify,
    decode_input,
    decode_new_session,
    decode_resize,
    decode_kill_session,
    encode_error,
    encode_output,
    encode_session_info,
    encode_shell_exited,
)
from txtmux.pty_handler import read_pty, set_pty_size, write_pty
from txtmux.session import SessionManager


def get_socket_path() -> str:
    """Get Unix socket path for server."""
    tmpdir = os.environ.get("TMUX_TMPDIR")
    if tmpdir:
        return os.path.join(tmpdir, "default")
    uid = os.getuid()
    return f"/tmp/textual-tmux-{uid}/default"


def get_pid_file_path() -> str:
    """Get PID file path for server."""
    socket_path = get_socket_path()
    return socket_path + ".pid"


class SessionServer:
    """Unix domain socket server managing terminal sessions."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._session_manager: SessionManager = SessionManager()
        self._server: asyncio.Server | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._next_client_id: int = 0
        self._clients: dict[int, StreamWriter] = {}
        self._client_dimensions: dict[int, tuple[int, int]] = {}
        self._client_sessions: dict[int, int] = {}
        self._pty_tasks: dict[int, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Start the server and listen for connections."""
        socket_dir = os.path.dirname(self._socket_path)
        if not os.path.exists(socket_dir):
            os.makedirs(socket_dir, mode=0o700)

        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, self._signal_stop)
        loop.add_signal_handler(signal.SIGCHLD, self._reap_children)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        atexit.register(self._atexit_cleanup)

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._socket_path,
        )

        os.chmod(self._socket_path, 0o600)

        async with self._server:
            await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Stop the server gracefully."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

        for session_id, _ in list(self._session_manager.list_sessions()):
            session = self._session_manager.find_session(session_id=session_id, name=None)
            if session:
                for pane in session.panes.values():
                    try:
                        os.kill(pane.pid, signal.SIGKILL)
                    except OSError:
                        pass

        await asyncio.sleep(0.1)

        for task in self._pty_tasks.values():
            task.cancel()
        self._pty_tasks.clear()

        for session_id, _ in list(self._session_manager.list_sessions()):
            self._session_manager.destroy_session(session_id)

        for writer in self._clients.values():
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        self._clients.clear()

        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        pid_file = get_pid_file_path()
        if os.path.exists(pid_file):
            os.unlink(pid_file)

        self._shutdown_event.set()

    def _signal_stop(self) -> None:
        """Handle SIGTERM signal."""
        asyncio.create_task(self.stop())

    def _reap_children(self) -> None:
        """Reap zombie child processes on SIGCHLD."""
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break

    def _atexit_cleanup(self) -> None:
        """Backup cleanup if stop() wasn't called."""
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        pid_file = get_pid_file_path()
        if os.path.exists(pid_file):
            os.unlink(pid_file)

    async def _pty_forward_loop(self, session_id: int, pane_id: int) -> None:
        """Forward PTY output to all attached clients, feeding through pyte.Screen."""
        session = self._session_manager._sessions.get(session_id)
        if session is None:
            raise RuntimeError(f"Session {session_id} not found")
        pane = session.panes.get(pane_id)
        if pane is None:
            raise RuntimeError(f"Pane {pane_id} not found in session {session_id}")

        while True:
            try:
                data = await read_pty(pane.pty_fd, 4096)
            except OSError:
                break
            if not data:
                break

            pane.feed(data)

            output_msg = encode_output(data)
            client_ids = self._session_manager.get_attached_clients(session_id)
            for client_id in client_ids:
                writer = self._clients.get(client_id)
                if writer:
                    try:
                        writer.write(output_msg.encode())
                        await writer.drain()
                    except Exception:
                        self._remove_dead_client(client_id)

        # Shell exited - mark pane as dead and notify clients
        pane.is_dead = True
        await self._broadcast_shell_exited(session_id, pane_id)

    async def _broadcast_shell_exited(self, session_id: int, pane_id: int) -> None:
        """Broadcast SHELL_EXITED message to all clients attached to session."""
        client_ids = self._session_manager.get_attached_clients(session_id)
        exit_msg = encode_shell_exited(session_id, pane_id)
        for client_id in client_ids:
            writer = self._clients.get(client_id)
            if writer:
                try:
                    writer.write(exit_msg.encode())
                    await writer.drain()
                except Exception:
                    self._remove_dead_client(client_id)

    def _remove_dead_client(self, client_id: int) -> None:
        """Remove a dead client from tracking."""
        session_id = self._client_sessions.pop(client_id, None)
        if session_id is not None:
            try:
                self._session_manager.detach_client(session_id, client_id)
            except KeyError:
                pass
        self._clients.pop(client_id, None)
        self._client_dimensions.pop(client_id, None)

    def _start_pty_forwarding(self, session_id: int, pane_id: int) -> None:
        """Start PTY forwarding task for a session if not already running."""
        if session_id in self._pty_tasks:
            return
        task = asyncio.create_task(self._pty_forward_loop(session_id, pane_id))
        self._pty_tasks[session_id] = task

    async def _handle_client(
        self,
        reader: StreamReader,
        writer: StreamWriter,
    ) -> None:
        """Handle a connected client."""
        client_id = self._next_client_id
        self._next_client_id += 1
        self._clients[client_id] = writer

        buffer = b""

        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break

                buffer += data

                while True:
                    message, buffer = decode(buffer)
                    if message is None:
                        break

                    await self._dispatch_message(client_id, message, writer)

        except Exception as e:
            error_msg = encode_error(str(e))
            try:
                writer.write(error_msg.encode())
                await writer.drain()
            except Exception:
                pass
            raise
        finally:
            session_id = self._client_sessions.pop(client_id, None)
            if session_id is not None:
                self._session_manager.detach_client(session_id, client_id)
            del self._clients[client_id]
            self._client_dimensions.pop(client_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch_message(
        self,
        client_id: int,
        message: Message,
        writer: StreamWriter,
    ) -> None:
        """Dispatch a message to the appropriate handler."""
        if message.msg_type == MessageType.IDENTIFY:
            width, height = decode_identify(message.payload)
            self._client_dimensions[client_id] = (width, height)

        elif message.msg_type == MessageType.LIST_SESSIONS:
            sessions = self._session_manager.list_sessions()
            for session_id, name in sessions:
                session = self._session_manager.find_session(
                    session_id=session_id,
                    name=None,
                )
                if session is None:
                    raise RuntimeError(f"Session {session_id} not found")
                pane = session.panes[session.active_pane_id]
                attached_count = len(self._session_manager.get_attached_clients(session_id))
                info_msg = encode_session_info(
                    session_id=session.id,
                    name=session.name,
                    pane_id=pane.id,
                    pid=pane.pid,
                    width=pane.width,
                    height=pane.height,
                    created_at=session.created_at,
                    attached_count=attached_count,
                )
                writer.write(info_msg.encode())
            await writer.drain()

        elif message.msg_type == MessageType.NEW_SESSION:
            name = decode_new_session(message.payload)
            # Generate default name if not provided
            if not name:
                existing = self._session_manager.list_sessions()
                if not existing:
                    name = "main"
                else:
                    existing_names = {n for _, n in existing}
                    i = 1
                    while f"session-{i}" in existing_names:
                        i += 1
                    name = f"session-{i}"
            dimensions = self._client_dimensions.get(client_id)
            if dimensions is None:
                raise RuntimeError("Client must send IDENTIFY before NEW_SESSION")
            width, height = dimensions
            shell = os.environ.get("SHELL", "/bin/sh")
            session = self._session_manager.create_session(
                name=name,
                shell=shell,
                width=width,
                height=height,
            )
            self._session_manager.attach_client(session.id, client_id)
            self._client_sessions[client_id] = session.id
            pane = session.panes[session.active_pane_id]
            self._start_pty_forwarding(session.id, pane.id)
            attached_count = len(self._session_manager.get_attached_clients(session.id))
            info_msg = encode_session_info(
                session_id=session.id,
                name=session.name,
                pane_id=pane.id,
                pid=pane.pid,
                width=pane.width,
                height=pane.height,
                created_at=session.created_at,
                attached_count=attached_count,
            )
            writer.write(info_msg.encode())
            await writer.drain()

        elif message.msg_type == MessageType.ATTACH:
            session_id = decode_attach(message.payload)
            session = self._session_manager.find_session(
                session_id=session_id,
                name=None,
            )
            if session is None:
                raise RuntimeError(f"Session {session_id} not found")
            pane = session.panes[session.active_pane_id]
            # If pane is dead, notify client immediately
            if pane.is_dead:
                exit_msg = encode_shell_exited(session.id, pane.id)
                writer.write(exit_msg.encode())
                await writer.drain()
                return
            self._session_manager.attach_client(session.id, client_id)
            self._client_sessions[client_id] = session.id
            # Send current screen state before starting live forwarding
            screen_data = pane.render_to_ansi()
            output_msg = encode_output(screen_data)
            writer.write(output_msg.encode())
            await writer.drain()
            self._start_pty_forwarding(session.id, pane.id)
            attached_count = len(self._session_manager.get_attached_clients(session.id))
            info_msg = encode_session_info(
                session_id=session.id,
                name=session.name,
                pane_id=pane.id,
                pid=pane.pid,
                width=pane.width,
                height=pane.height,
                created_at=session.created_at,
                attached_count=attached_count,
            )
            writer.write(info_msg.encode())
            await writer.drain()

        elif message.msg_type == MessageType.INPUT:
            session_id_opt: int | None = self._client_sessions.get(client_id)
            if session_id_opt is None:
                raise RuntimeError("Client not attached to any session")
            session_id = session_id_opt
            session = self._session_manager.find_session(
                session_id=session_id,
                name=None,
            )
            if session is None:
                raise RuntimeError(f"Session {session_id} not found")
            pane = session.panes[session.active_pane_id]
            data = decode_input(message.payload)
            write_pty(pane.pty_fd, data)

        elif message.msg_type == MessageType.RESIZE:
            session_id_opt = self._client_sessions.get(client_id)
            if session_id_opt is None:
                raise RuntimeError("Client not attached to any session")
            session_id = session_id_opt
            session = self._session_manager.find_session(
                session_id=session_id,
                name=None,
            )
            if session is None:
                raise RuntimeError(f"Session {session_id} not found")
            pane = session.panes[session.active_pane_id]
            width, height = decode_resize(message.payload)
            pane.width = width
            pane.height = height
            set_pty_size(pane.pty_fd, width, height)
            pane.resize_screen(width, height)

        elif message.msg_type == MessageType.DETACH:
            session_id_opt = self._client_sessions.get(client_id)
            if session_id_opt is not None:
                session_id = session_id_opt
                self._session_manager.detach_client(session_id, client_id)
                del self._client_sessions[client_id]

        elif message.msg_type == MessageType.KILL_SESSION:
            session_id = decode_kill_session(message.payload)
            session = self._session_manager.find_session(
                session_id=session_id,
                name=None,
            )
            if session is None:
                error_msg = encode_error(f"Session {session_id} not found")
                writer.write(error_msg.encode())
                await writer.drain()
                return

            # Detach all clients from this session
            client_ids = self._session_manager.get_attached_clients(session_id).copy()
            for cid in client_ids:
                self._session_manager.detach_client(session_id, cid)
                self._client_sessions.pop(cid, None)

            # Cancel PTY forwarding task if running
            task = self._pty_tasks.pop(session_id, None)
            if task:
                task.cancel()

            # Destroy the session
            self._session_manager.destroy_session(session_id)

        else:
            error_msg = encode_error(
                f"Unhandled message type: {message.msg_type.name}"
            )
            writer.write(error_msg.encode())
            await writer.drain()


def daemonize(pid_file_path: str) -> None:
    """Fork into background daemon and write PID file."""
    pid_dir = os.path.dirname(pid_file_path)
    if not os.path.exists(pid_dir):
        os.makedirs(pid_dir, mode=0o700)

    pid = os.fork()
    if pid > 0:
        os._exit(0)

    os.setsid()

    pid = os.fork()
    if pid > 0:
        os._exit(0)

    sys.stdin.close()
    sys.stdout.close()
    sys.stderr.close()

    null_fd = os.open("/dev/null", os.O_RDWR)
    os.dup2(null_fd, 0)
    os.dup2(null_fd, 1)
    os.dup2(null_fd, 2)
    if null_fd > 2:
        os.close(null_fd)

    with open(pid_file_path, "w") as f:
        f.write(str(os.getpid()))


async def run_server(socket_path: str) -> None:
    """Run the session server."""
    server = SessionServer(socket_path=socket_path)
    await server.start()


if __name__ == "__main__":
    socket_path = get_socket_path()
    pid_file = get_pid_file_path()

    if "--daemon" in sys.argv:
        daemonize(pid_file)

    asyncio.run(run_server(socket_path))
