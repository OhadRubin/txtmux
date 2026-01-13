"""Textual client application for terminal multiplexer."""

import asyncio
import signal

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Static
from textual import events

from txtmux.terminal_widget import TerminalPane


class StatusBar(Static):
    """Minimal status bar: session name (left) + detach hint (right)."""

    def __init__(self, session_name: str) -> None:
        super().__init__()
        self.session_name = session_name

    def compose(self) -> ComposeResult:
        yield Static(f" {self.session_name}", id="status-left")
        yield Static("Ctrl+B D: detach ", id="status-right")


class TerminalApp(App[None]):
    """Textual application hosting TerminalPane in network mode."""

    BINDINGS = [
        Binding("ctrl+b", "prefix_key", "Prefix", priority=True),
        Binding("escape", "prefix_key", "Prefix (esc)", priority=True),
    ]

    CSS = """
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

    def __init__(self, socket_path: str, session_id: int, session_name: str) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.session_id = session_id
        self.session_name = session_name
        self._prefix_active = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield TerminalPane(
            shell=None,
            socket_path=self.socket_path,
            session_id=self.session_id,
        )
        yield StatusBar(self.session_name)

    def on_mount(self) -> None:
        self.title = self.session_name
        self.query_one(TerminalPane).focus()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self._signal_handler)
        loop.add_signal_handler(signal.SIGINT, self._signal_handler)

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
        self.exit(message=f"[connection failed: {event.error}]")
