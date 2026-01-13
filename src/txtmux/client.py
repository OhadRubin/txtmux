"""Textual client application for terminal multiplexer."""

import asyncio
import signal

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Header, Static, Tabs, Tab
from textual.message import Message as TextualMessage
from textual import events
from textual.command import Provider, Hit
from textual.screen import ModalScreen
from textual.containers import Container, ScrollableContainer
from textual.strip import Strip
from rich.text import Text

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


class VirtualScrollback(Widget):
    """Virtualized scrollback widget - only renders visible lines."""

    can_focus = True

    def __init__(self, terminal_pane: "TerminalPane") -> None:
        super().__init__()
        self.terminal_pane = terminal_pane
        self._lines: list[Text] = []
        self._build_lines()

    def _build_lines(self) -> None:
        """Build the list of lines (raw data, not rendered yet)."""
        if self.terminal_pane.terminal_screen is None:
            self._lines = [Text("No terminal screen available")]
            return

        # Get history lines (already rendered as Text)
        history = self.terminal_pane.terminal_screen.get_history()

        # Get current screen lines
        current = self.terminal_pane.terminal_screen.render(show_cursor=False)
        current_lines = list(current.split("\n"))

        self._lines = history + current_lines

    def get_content_height(self, container: Widget, viewport: Widget, width: int) -> int:
        """Return virtual height (total lines)."""
        return len(self._lines)

    def render_line(self, y: int) -> Strip:
        """Render a single line - called only for visible lines."""
        if y < 0 or y >= len(self._lines):
            return Strip.blank(self.size.width)

        line = self._lines[y]
        segments = list(line.render(self.app.console))
        return Strip(segments)

    def get_all_text(self) -> str:
        """Get all text for clipboard."""
        return "\n".join(str(line) for line in self._lines)


class CopyModeScreen(ModalScreen[None]):
    """Modal screen for viewing terminal scrollback buffer."""

    BINDINGS = [
        Binding("escape", "dismiss", "Exit Copy Mode"),
        Binding("q", "dismiss", "Quit"),
        Binding("g", "scroll_top", "Go to Top"),
        Binding("G", "scroll_bottom", "Go to Bottom"),
        Binding("k", "scroll_up", "Up"),
        Binding("j", "scroll_down", "Down"),
        Binding("ctrl+u", "page_up", "Page Up"),
        Binding("ctrl+d", "page_down", "Page Down"),
        Binding("y", "copy_all", "Copy All"),
    ]

    CSS = """
    CopyModeScreen {
        align: center middle;
    }

    #copy-mode-container {
        width: 100%;
        height: 100%;
        background: $surface;
        border: thick $primary;
    }

    #scrollback-content {
        width: 100%;
        height: 1fr;
        scrollbar-gutter: stable;
    }

    #status-line {
        dock: bottom;
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    """

    def __init__(self, terminal_pane: "TerminalPane") -> None:
        super().__init__()
        self.terminal_pane = terminal_pane

    def compose(self) -> ComposeResult:
        """Compose the copy mode interface."""
        with Container(id="copy-mode-container"):
            with ScrollableContainer(id="scrollback-content"):
                yield VirtualScrollback(self.terminal_pane)

            yield Static(self._status_text(), id="status-line")

    def _status_text(self) -> str:
        """Generate status line text."""
        return " COPY MODE | q: quit | y: copy all | g/G: top/bottom | j/k: scroll "

    def on_mount(self) -> None:
        """Scroll to bottom when copy mode opens."""
        container = self.query_one("#scrollback-content", ScrollableContainer)
        container.scroll_end(animate=False)

    def action_dismiss(self) -> None:
        """Exit copy mode."""
        self.dismiss()

    def action_scroll_top(self) -> None:
        """Scroll to top of history."""
        container = self.query_one("#scrollback-content", ScrollableContainer)
        container.scroll_home()

    def action_scroll_bottom(self) -> None:
        """Scroll to bottom of history."""
        container = self.query_one("#scrollback-content", ScrollableContainer)
        container.scroll_end()

    def action_scroll_up(self) -> None:
        """Scroll up one line."""
        container = self.query_one("#scrollback-content", ScrollableContainer)
        container.scroll_up()

    def action_scroll_down(self) -> None:
        """Scroll down one line."""
        container = self.query_one("#scrollback-content", ScrollableContainer)
        container.scroll_down()

    def action_page_up(self) -> None:
        """Scroll up one page."""
        container = self.query_one("#scrollback-content", ScrollableContainer)
        container.scroll_page_up()

    def action_page_down(self) -> None:
        """Scroll down one page."""
        container = self.query_one("#scrollback-content", ScrollableContainer)
        container.scroll_page_down()

    def action_copy_all(self) -> None:
        """Copy all visible text to clipboard."""
        scrollback = self.query_one(VirtualScrollback)
        try:
            self.app.copy_to_clipboard(scrollback.get_all_text())
            self.notify("Copied to clipboard")
        except Exception:
            self.notify("Clipboard not supported", severity="warning")


class SessionCommandProvider(Provider):
    """Custom command provider for session-specific commands."""

    async def startup(self) -> None:
        """Called when command palette opens."""
        # Access the app to get session list
        pass

    async def search(self, query: str) -> Hit:
        """Yield commands matching the search query."""
        matcher = self.matcher(query)
        app = self.app

        if not isinstance(app, TerminalApp):
            return

        # Get current sessions from the app
        for session_id, session_name in app._sessions:
            if session_id == app._active_session_id:
                continue  # Skip current session

            score = matcher.match(f"Switch to {session_name}")
            if score > 0:
                yield Hit(
                    score=score,
                    match_display=f"Switch to {session_name}",
                    command_name=f"switch_to_{session_id}",
                    help=f"Switch to session {session_id}",
                    callback=lambda sid=session_id: self._switch_session(sid),
                )

    async def discover(self) -> Hit:
        """Show sessions when palette opens with empty input."""
        app = self.app

        if not isinstance(app, TerminalApp):
            return

        for session_id, session_name in app._sessions:
            if session_id == app._active_session_id:
                continue  # Skip current session

            yield Hit(
                score=1.0,
                match_display=f"Switch to {session_name}",
                command_name=f"switch_to_{session_id}",
                help=f"Session ID: {session_id}",
                callback=lambda sid=session_id: self._switch_session(sid),
            )

    def _switch_session(self, session_id: int) -> None:
        """Switch to specified session."""
        app = self.app
        if not isinstance(app, TerminalApp):
            return

        app._active_session_id = session_id
        terminal = app.query_one(TerminalPane)
        terminal.reconnect(session_id)
        terminal.focus()

        tabs = app.query_one(SessionTabs)
        session_name = tabs.get_session_name(session_id)
        app.title = session_name
        app.query_one(StatusBar).update_session_name(session_name)
        tabs._active_session_id = session_id
        tabs.active = f"session-{session_id}"


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

    # Enable command palette (Ctrl+P by default)
    ENABLE_COMMAND_PALETTE = True

    # Register custom command provider
    COMMANDS = App.COMMANDS | {SessionCommandProvider}

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
        self._active_session_id = event.session_id
        terminal = self.query_one(TerminalPane)
        terminal.reconnect(event.session_id)
        terminal.focus()

        session_name = self.query_one(SessionTabs).get_session_name(event.session_id)
        self.title = session_name
        self.query_one(StatusBar).update_session_name(session_name)

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
            self.query_one(StatusBar).update_session_name(name)

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
            elif event.key == "pageup" or event.key == "left_square_bracket":
                self._enter_copy_mode()
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

    def _enter_copy_mode(self) -> None:
        """Launch copy mode screen for scrollback viewing."""
        terminal = self.query_one(TerminalPane)
        self.push_screen(CopyModeScreen(terminal))

    def _signal_handler(self) -> None:
        """Handle SIGTERM/SIGINT with clean exit."""
        self.exit(message="[interrupted]")

    def on_terminal_pane_detached(self, event: TerminalPane.Detached) -> None:
        self.exit(message="[detached]")

    def on_terminal_pane_shell_exited(self, event: TerminalPane.ShellExited) -> None:
        self.exit(message="[shell exited]")

    def on_terminal_pane_connection_failed(self, event: TerminalPane.ConnectionFailed) -> None:
        self.exit(message=f"[connection failed: {event.error}]")
