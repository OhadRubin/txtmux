"""Textual client application for terminal multiplexer."""

import asyncio
import signal

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Static, Tabs, Tab
from textual.message import Message as TextualMessage
from textual import events, work

from txtmux.terminal_widget import TerminalPane
from txtmux.protocol import (
    encode_identify,
    encode_new_session,
    encode_list_sessions,
    decode,
    decode_session_info,
    MessageType,
)


class StatusBar(Static):
    """Minimal status bar: session name (left) + detach hint (right)."""

    def __init__(self, session_name: str) -> None:
        super().__init__()
        self.session_name = session_name

    def compose(self) -> ComposeResult:
        yield Static(f" {self.session_name}", id="status-left")
        yield Static("Ctrl+B D: detach ", id="status-right")

    def update_session_name(self, name: str) -> None:
        self.session_name = name
        self.query_one("#status-left", Static).update(f" {name}")


NEW_SESSION_TAB_ID = "__new_session__"


class SessionTabs(Tabs):
    """Tabs widget for switching between sessions."""

    class NewSessionRequested(TextualMessage):
        """Posted when user clicks the + tab."""

        pass

    class SessionSwitchRequested(TextualMessage):
        """Posted when user clicks a session tab."""

        def __init__(self, session_id: int) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self, sessions: list[tuple[int, str]], active_session_id: int) -> None:
        tabs = [Tab(name, id=f"session-{sid}") for sid, name in sessions]
        tabs.append(Tab("+", id=NEW_SESSION_TAB_ID))
        super().__init__(*tabs)
        self._sessions = {sid: name for sid, name in sessions}
        self._active_session_id = active_session_id

    def on_mount(self) -> None:
        self.active = f"session-{self._active_session_id}"

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab_id = event.tab.id
        if tab_id is None:
            raise RuntimeError("tab.id is None")

        if tab_id == NEW_SESSION_TAB_ID:
            self.active = f"session-{self._active_session_id}"
            self.post_message(self.NewSessionRequested())
        else:
            session_id = int(tab_id.replace("session-", ""))
            if session_id != self._active_session_id:
                self._active_session_id = session_id
                self.post_message(self.SessionSwitchRequested(session_id))

    def add_session_tab(self, session_id: int, name: str) -> None:
        """Add a new session tab before the + tab."""
        self._sessions[session_id] = name
        new_tab = Tab(name, id=f"session-{session_id}")
        self.add_tab(new_tab, before=NEW_SESSION_TAB_ID)
        self._active_session_id = session_id
        self.active = f"session-{session_id}"

    def get_session_name(self, session_id: int) -> str:
        return self._sessions[session_id]


class TerminalApp(App[None]):
    """Textual application hosting TerminalPane in network mode."""

    BINDINGS = [
        Binding("ctrl+b", "prefix_key", "Prefix", priority=True),
        Binding("escape", "prefix_key", "Prefix (esc)", priority=True),
    ]

    CSS = """
    SessionTabs {
        dock: top;
        height: 3;
    }
    TerminalPane {
        width: 100%;
        height: 1fr;
    }
    StatusBar {
        dock: bottom;
        height: 1;
        layout: horizontal;
        background: $surface;
        color: $text-muted;
    }
    StatusBar > #status-left {
        width: 1fr;
    }
    StatusBar > #status-right {
        width: auto;
    }
    """

    def __init__(
        self,
        socket_path: str,
        sessions: list[tuple[int, str]],
        active_session_id: int,
    ) -> None:
        super().__init__()
        self.socket_path = socket_path
        self._sessions = sessions
        self._active_session_id = active_session_id
        self._prefix_active = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield SessionTabs(self._sessions, self._active_session_id)
        yield TerminalPane(
            shell=None,
            socket_path=self.socket_path,
            session_id=self._active_session_id,
        )
        active_name = self._get_session_name(self._active_session_id)
        yield StatusBar(active_name)

    def _get_session_name(self, session_id: int) -> str:
        for sid, name in self._sessions:
            if sid == session_id:
                return name
        raise RuntimeError(f"session_id {session_id} not found")

    def on_mount(self) -> None:
        self.title = self._get_session_name(self._active_session_id)
        self.query_one(TerminalPane).focus()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self._signal_handler)
        loop.add_signal_handler(signal.SIGINT, self._signal_handler)

    def on_session_tabs_session_switch_requested(
        self, event: SessionTabs.SessionSwitchRequested
    ) -> None:
        """Handle tab switch: reconnect to the new session."""
        terminal = self.query_one(TerminalPane)
        terminal.loading = True

        try:
            self._active_session_id = event.session_id
            terminal.reconnect(event.session_id)
            terminal.focus()

            session_name = self.query_one(SessionTabs).get_session_name(event.session_id)
            self.title = session_name
            self.query_one(StatusBar).update_session_name(session_name)

            self.notify(
                f"Switched to session '{session_name}'",
                severity="information",
                timeout=2,
            )
        finally:
            terminal.loading = False

    def on_session_tabs_new_session_requested(
        self, event: SessionTabs.NewSessionRequested
    ) -> None:
        """Handle + tab click: create new session and switch to it."""
        self._create_new_session()

    async def _create_new_session_rpc(self) -> tuple[int, str]:
        """Create a new session via RPC and return (session_id, name)."""
        reader, writer = await asyncio.open_unix_connection(self.socket_path)

        identify_msg = encode_identify(80, 24)
        writer.write(identify_msg.encode())
        await writer.drain()

        new_session_msg = encode_new_session("")
        writer.write(new_session_msg.encode())
        await writer.drain()

        buffer = b""
        while True:
            data = await reader.read(4096)
            if not data:
                raise RuntimeError("server closed connection")
            buffer += data
            message, buffer = decode(buffer)
            if message is None:
                continue
            if message.msg_type == MessageType.SESSION_INFO:
                session_id, name, _pane_id, _pid, _width, _height, _created_at, _attached = decode_session_info(message.payload)
                writer.close()
                await writer.wait_closed()
                return (session_id, name)
            if message.msg_type == MessageType.ERROR:
                raise RuntimeError(f"server error: {message.payload.decode()}")

    @work(exclusive=True, exit_on_error=False)
    async def _create_new_session_worker(self) -> None:
        """Create new session with proper error handling."""
        terminal = self.query_one(TerminalPane)
        terminal.loading = True

        try:
            self.notify("Creating new session...", severity="information", timeout=2)
            session_id, name = await self._create_new_session_rpc()
            self._sessions.append((session_id, name))
            self._active_session_id = session_id

            tabs = self.query_one(SessionTabs)
            tabs.add_session_tab(session_id, name)

            terminal.reconnect(session_id)
            terminal.focus()

            self.title = name
            self.query_one(StatusBar).update_session_name(name)

            self.notify(
                f"Session '{name}' created successfully",
                severity="information",
                timeout=2,
            )
        except ConnectionRefusedError:
            self.notify(
                "Cannot create session: server not responding",
                title="Connection Error",
                severity="error",
                timeout=10,
            )
        except RuntimeError as e:
            self.notify(
                f"Server error: {e}",
                title="Session Creation Failed",
                severity="error",
                timeout=10,
            )
        except Exception as e:
            self.notify(
                f"Unexpected error: {type(e).__name__}: {e}",
                title="Error",
                severity="error",
                timeout=10,
            )
            self.log.error(f"Unexpected error in session creation: {e}", exc_info=True)
        finally:
            terminal.loading = False

    def _create_new_session(self) -> None:
        """Entry point for creating new session."""
        self._create_new_session_worker()

    def action_prefix_key(self) -> None:
        """Handle Ctrl+B/Escape prefix key (via BINDINGS)."""
        self._prefix_active = True
        self.query_one(TerminalPane).prefix_active = True

    def on_key(self, event: events.Key) -> None:
        if self._prefix_active:
            self._prefix_active = False
            terminal = self.query_one(TerminalPane)
            terminal.prefix_active = False
            if event.key == "d":
                self._do_detach()
                event.stop()
                return
            self._forward_prefix_and_key(event)
            event.stop()
            return

    def _forward_prefix_and_key(self, event: events.Key) -> None:
        """Forward the Ctrl+B that was consumed plus the current key."""
        terminal = self.query_one(TerminalPane)
        terminal.send_key(events.Key(key="ctrl+b", character=None))
        terminal.send_key(event)
        event.stop()

    def _do_detach(self) -> None:
        terminal = self.query_one(TerminalPane)
        terminal.detach()
        self.exit(message="[detached]")

    def _signal_handler(self) -> None:
        """Handle SIGTERM/SIGINT with clean exit."""
        self.exit(message="[interrupted]")

    async def _delayed_exit(self, message: str, delay: float) -> None:
        """Exit after a delay to allow user to read notification."""
        await asyncio.sleep(delay)
        self.exit(message=message)

    def on_terminal_pane_detached(self, event: TerminalPane.Detached) -> None:
        self.notify("Detached from session", severity="information", timeout=2)
        asyncio.create_task(self._delayed_exit("[detached]", delay=0.5))

    def on_terminal_pane_shell_exited(self, event: TerminalPane.ShellExited) -> None:
        self.notify("Shell process has exited", title="Session Ended", severity="warning", timeout=3)
        asyncio.create_task(self._delayed_exit("[shell exited]", delay=1.0))

    def on_terminal_pane_connection_failed(self, event: TerminalPane.ConnectionFailed) -> None:
        self.notify(
            f"Connection failed: {event.error}",
            title="Connection Error",
            severity="error",
            timeout=5,
        )
        asyncio.create_task(self._delayed_exit(f"[connection failed: {event.error}]", delay=2.0))
