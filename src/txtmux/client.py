"""Textual client application for terminal multiplexer."""

import asyncio
import signal

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Static, Tabs, Tab
from textual.message import Message as TextualMessage
from textual import events
from textual.reactive import reactive
from textual.containers import Horizontal

from txtmux.terminal_widget import TerminalPane
from txtmux.protocol import (
    encode_identify,
    encode_new_session,
    encode_list_sessions,
    decode,
    decode_session_info,
    MessageType,
)
from txtmux.constants import DEFAULT_TERMINAL_WIDTH, DEFAULT_TERMINAL_HEIGHT


class EnhancedStatusBar(Static):
    """Rich status bar with reactive properties for automatic updates."""

    session_name: reactive[str] = reactive("")
    session_id: reactive[int] = reactive(0)
    connected: reactive[bool] = reactive(True)

    def __init__(self, session_name: str, session_id: int) -> None:
        super().__init__()
        self.session_name = session_name
        self.session_id = session_id

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(id="status-session")
            yield Static(id="status-state")
            yield Static(id="status-hint")

    def on_mount(self) -> None:
        """Initialize display when widget is mounted."""
        self._update_session_display()
        self._update_state_display()
        self._update_hint_display()

    def watch_session_name(self, new_name: str) -> None:
        """Automatically update UI when session name changes."""
        self._update_session_display()

    def watch_session_id(self, new_id: int) -> None:
        """Automatically update UI when session ID changes."""
        self._update_session_display()

    def watch_connected(self, is_connected: bool) -> None:
        """Update connection state indicator."""
        self._update_state_display()

    def _update_session_display(self) -> None:
        """Update the session name and ID display."""
        session_widget = self.query_one("#status-session", Static)
        session_widget.update(f" [{self.session_id}] {self.session_name}")

    def _update_state_display(self) -> None:
        """Update the connection state display."""
        state_widget = self.query_one("#status-state", Static)
        if self.connected:
            state_widget.update("")
            state_widget.remove_class("disconnected")
        else:
            state_widget.update(" [disconnected]")
            state_widget.add_class("disconnected")

    def _update_hint_display(self) -> None:
        """Update the keybinding hint display."""
        hint_widget = self.query_one("#status-hint", Static)
        hint_widget.update("Ctrl+B D: detach ")


NEW_SESSION_TAB_ID = "__new_session__"


class SessionTabs(Tabs):
    """Tabs widget with visual state indicators."""

    # Track session states
    session_activity: reactive[dict[int, bool]] = reactive(dict, init=False)
    session_connected: reactive[dict[int, bool]] = reactive(dict, init=False)

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

        # Initialize state tracking
        self.session_activity = {sid: False for sid, _ in sessions}
        self.session_connected = {sid: True for sid, _ in sessions}

    def on_mount(self) -> None:
        self.active = f"session-{self._active_session_id}"

    def mark_activity(self, session_id: int) -> None:
        """Mark a session as having new activity."""
        if session_id != self._active_session_id:
            self.session_activity[session_id] = True
            try:
                tab = self.query_one(f"#session-{session_id}", Tab)
                tab.add_class("has-activity")
            except Exception:
                pass  # Tab might not exist yet

    def clear_activity(self, session_id: int) -> None:
        """Clear activity indicator when session becomes active."""
        if session_id in self.session_activity:
            self.session_activity[session_id] = False
        try:
            tab = self.query_one(f"#session-{session_id}", Tab)
            tab.remove_class("has-activity")
        except Exception:
            pass  # Tab might not exist yet

    def mark_disconnected(self, session_id: int) -> None:
        """Mark a session as disconnected."""
        self.session_connected[session_id] = False
        try:
            tab = self.query_one(f"#session-{session_id}", Tab)
            tab.add_class("disconnected")
        except Exception:
            pass  # Tab might not exist yet

    def mark_connected(self, session_id: int) -> None:
        """Mark a session as connected."""
        self.session_connected[session_id] = True
        try:
            tab = self.query_one(f"#session-{session_id}", Tab)
            tab.remove_class("disconnected")
        except Exception:
            pass  # Tab might not exist yet

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
                # Clear activity indicator when switching to a tab
                self.clear_activity(session_id)
                self._active_session_id = session_id
                self.post_message(self.SessionSwitchRequested(session_id))

    def add_session_tab(self, session_id: int, name: str) -> None:
        """Add a new session tab before the + tab."""
        self._sessions[session_id] = name
        new_tab = Tab(name, id=f"session-{session_id}")
        self.add_tab(new_tab, before=NEW_SESSION_TAB_ID)
        self._active_session_id = session_id
        self.active = f"session-{session_id}"

        # Initialize state for new session
        self.session_activity[session_id] = False
        self.session_connected[session_id] = True

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
    EnhancedStatusBar {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
    }
    EnhancedStatusBar Horizontal {
        height: 100%;
        width: 100%;
    }
    EnhancedStatusBar > Horizontal > #status-session {
        width: 1fr;
    }
    EnhancedStatusBar > Horizontal > #status-state {
        width: auto;
    }
    EnhancedStatusBar > Horizontal > #status-hint {
        width: auto;
    }
    EnhancedStatusBar .disconnected {
        color: $error;
        text-style: bold;
    }

    /* Tab state indicators */
    Tab.has-activity {
        background: $warning-darken-2;
        text-style: italic;
    }

    Tab.has-activity:hover {
        background: $warning-darken-1;
    }

    Tab.disconnected {
        background: $error-darken-2;
        color: $text-muted;
    }

    Tab.disconnected:hover {
        background: $error-darken-1;
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
        yield EnhancedStatusBar(active_name, self._active_session_id)

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
        self._active_session_id = event.session_id
        terminal = self.query_one(TerminalPane)
        terminal.reconnect(event.session_id)
        terminal.focus()

        session_name = self.query_one(SessionTabs).get_session_name(event.session_id)
        self.title = session_name
        status_bar = self.query_one(EnhancedStatusBar)
        status_bar.session_name = session_name
        status_bar.session_id = event.session_id

    def on_session_tabs_new_session_requested(
        self, event: SessionTabs.NewSessionRequested
    ) -> None:
        """Handle + tab click: create new session and switch to it."""
        self._create_new_session()

    async def _create_new_session_rpc(self) -> tuple[int, str]:
        """Create a new session via RPC and return (session_id, name)."""
        reader, writer = await asyncio.open_unix_connection(self.socket_path)

        identify_msg = encode_identify(DEFAULT_TERMINAL_WIDTH, DEFAULT_TERMINAL_HEIGHT)
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

    def _create_new_session(self) -> None:
        """Create new session and switch to it."""
        async def do_create() -> None:
            session_id, name = await self._create_new_session_rpc()
            self._sessions.append((session_id, name))
            self._active_session_id = session_id

            tabs = self.query_one(SessionTabs)
            tabs.add_session_tab(session_id, name)

            terminal = self.query_one(TerminalPane)
            terminal.reconnect(session_id)
            terminal.focus()

            self.title = name
            status_bar = self.query_one(EnhancedStatusBar)
            status_bar.session_name = name
            status_bar.session_id = session_id

        asyncio.create_task(do_create())

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

    def on_terminal_pane_detached(self, event: TerminalPane.Detached) -> None:
        self.exit(message="[detached]")

    def on_terminal_pane_shell_exited(self, event: TerminalPane.ShellExited) -> None:
        self.exit(message="[shell exited]")

    def on_terminal_pane_connection_failed(self, event: TerminalPane.ConnectionFailed) -> None:
        status_bar = self.query_one(EnhancedStatusBar)
        status_bar.connected = False
        self.exit(message=f"[connection failed: {event.error}]")
