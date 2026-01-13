import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from textual.app import App
from terminal_widget import TerminalPane


class BasicTerminalApp(App):
    """Minimal terminal app using TerminalPane widget."""

    CSS = """
    TerminalPane {
        width: 100%;
        height: 100%;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
    ]

    def compose(self):
        shell = os.environ.get("SHELL")
        if shell is None:
            raise RuntimeError("SHELL environment variable not set")
        yield TerminalPane(shell=shell, socket_path=None, session_id=None)

    def on_mount(self):
        self.query_one(TerminalPane).focus()


if __name__ == "__main__":
    BasicTerminalApp().run()
