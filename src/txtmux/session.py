"""Session and Pane data structures for server-side state management."""

import os
import signal
import time
from dataclasses import dataclass, field

import pyte  # type: ignore[import]

from txtmux.pty_handler import close_pty, set_pty_size, spawn_shell


class Pane:
    """Represents a single terminal pane with PTY and screen state."""

    def __init__(self, id: int, pty_fd: int, pid: int, width: int, height: int) -> None:
        self.id = id
        self.pty_fd = pty_fd
        self.pid = pid
        self.width = width
        self.height = height
        self.screen: pyte.HistoryScreen = pyte.HistoryScreen(height, width, history=2000)
        self.stream: pyte.Stream = pyte.Stream(self.screen)
        self.is_dead: bool = False
        self.exit_code: int | None = None

    def feed(self, data: bytes) -> None:
        """Feed raw PTY output through the terminal emulator."""
        self.stream.feed(data.decode("utf-8", errors="replace"))

    def resize_screen(self, width: int, height: int) -> None:
        """Resize the virtual terminal screen."""
        self.screen.resize(height, width)

    def render_to_ansi(self) -> bytes:
        """Render screen state to ANSI escape sequences for replay on attach."""
        parts: list[bytes] = []

        # Render history lines first (scrolled off top)
        for row in self.screen.history.top:
            line = "".join(row[col].data for col in sorted(row.keys()))
            parts.append(line.encode("utf-8", errors="replace"))
            parts.append(b"\r\n")

        parts.append(b"\x1b[H")  # Home cursor
        # Use explicit cursor positioning per row to avoid scroll issues
        for row, line in enumerate(self.screen.display):
            parts.append(f"\x1b[{row + 1};1H".encode("utf-8"))
            parts.append(line.encode("utf-8"))
        # Position cursor at current location
        cx, cy = self.screen.cursor.x, self.screen.cursor.y
        parts.append(f"\x1b[{cy + 1};{cx + 1}H".encode("utf-8"))
        return b"".join(parts)


@dataclass
class Session:
    """Represents a session containing one or more panes."""

    id: int
    name: str
    panes: dict[int, Pane] = field(repr=False)
    active_pane_id: int
    created_at: float


class SessionManager:
    """Manages sessions, panes, and client attachments."""

    def __init__(self) -> None:
        self._sessions: dict[int, Session] = {}
        self._sessions_by_name: dict[str, Session] = {}
        self._attached_clients: dict[int, set[int]] = {}
        self._next_session_id: int = 0
        self._next_pane_id: int = 0

    def create_session(
        self,
        name: str,
        shell: str,
        width: int,
        height: int,
    ) -> Session:
        """Create a new session with one pane running the specified shell."""
        if name in self._sessions_by_name:
            raise ValueError(f"Session with name '{name}' already exists")

        session_id = self._next_session_id
        self._next_session_id += 1

        pane_id = self._next_pane_id
        self._next_pane_id += 1

        pty_fd, pid = spawn_shell(shell)
        set_pty_size(pty_fd, width, height)

        pane = Pane(id=pane_id, pty_fd=pty_fd, pid=pid, width=width, height=height)
        session = Session(
            id=session_id,
            name=name,
            panes={pane_id: pane},
            active_pane_id=pane_id,
            created_at=time.time(),
        )

        self._sessions[session_id] = session
        self._sessions_by_name[name] = session
        self._attached_clients[session_id] = set()

        return session

    def destroy_session(self, session_id: int) -> None:
        """Destroy a session, closing PTY fds and terminating shell processes."""
        if session_id not in self._sessions:
            raise KeyError(f"Session {session_id} not found")

        session = self._sessions[session_id]

        for pane in session.panes.values():
            close_pty(pane.pty_fd)
            try:
                os.kill(pane.pid, signal.SIGTERM)
                os.waitpid(pane.pid, os.WNOHANG)
            except OSError:
                pass

        del self._sessions[session_id]
        del self._sessions_by_name[session.name]
        del self._attached_clients[session_id]

    def create_pane(
        self,
        session_id: int,
        shell: str,
        width: int,
        height: int,
    ) -> Pane:
        """Create a new pane in an existing session."""
        if session_id not in self._sessions:
            raise KeyError(f"Session {session_id} not found")

        session = self._sessions[session_id]

        pane_id = self._next_pane_id
        self._next_pane_id += 1

        pty_fd, pid = spawn_shell(shell)
        set_pty_size(pty_fd, width, height)

        pane = Pane(id=pane_id, pty_fd=pty_fd, pid=pid, width=width, height=height)
        session.panes[pane_id] = pane

        return pane

    def destroy_pane(self, session_id: int, pane_id: int) -> None:
        """Destroy a pane, closing its PTY fd and terminating its shell process."""
        if session_id not in self._sessions:
            raise KeyError(f"Session {session_id} not found")

        session = self._sessions[session_id]

        if pane_id not in session.panes:
            raise KeyError(f"Pane {pane_id} not found in session {session_id}")

        if len(session.panes) == 1:
            raise ValueError("Cannot destroy last pane in session")

        pane = session.panes[pane_id]
        close_pty(pane.pty_fd)
        try:
            os.kill(pane.pid, signal.SIGTERM)
            os.waitpid(pane.pid, os.WNOHANG)
        except OSError:
            pass

        del session.panes[pane_id]

        if session.active_pane_id == pane_id:
            session.active_pane_id = next(iter(session.panes))

    def find_session(
        self,
        session_id: int | None,
        name: str | None,
    ) -> Session | None:
        """Find a session by ID or name."""
        if session_id is not None:
            return self._sessions.get(session_id)
        if name is not None:
            return self._sessions_by_name.get(name)
        raise ValueError("Must provide either session_id or name")

    def list_sessions(self) -> list[tuple[int, str]]:
        """Return list of (session_id, name) for all sessions."""
        return [(s.id, s.name) for s in self._sessions.values()]

    def attach_client(self, session_id: int, client_id: int) -> None:
        """Attach a client to a session."""
        if session_id not in self._attached_clients:
            raise KeyError(f"Session {session_id} not found")
        self._attached_clients[session_id].add(client_id)

    def detach_client(self, session_id: int, client_id: int) -> None:
        """Detach a client from a session."""
        if session_id not in self._attached_clients:
            raise KeyError(f"Session {session_id} not found")
        self._attached_clients[session_id].discard(client_id)

    def get_attached_clients(self, session_id: int) -> set[int]:
        """Get set of client IDs attached to a session."""
        if session_id not in self._attached_clients:
            raise KeyError(f"Session {session_id} not found")
        return self._attached_clients[session_id].copy()
