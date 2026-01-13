import asyncio
from asyncio import StreamReader, StreamWriter

import pyte  # type: ignore[import]
from rich.text import Text
from rich.style import Style
from textual.widget import Widget
from textual.message import Message as TextualMessage
from textual import events, work

from txtmux.pty_handler import spawn_shell, read_pty, write_pty, set_pty_size
from txtmux.protocol import (
    encode_identify,
    encode_attach,
    encode_detach,
    encode_input,
    encode_resize,
    decode,
    decode_output,
    decode_shell_exited,
    MessageType,
)
from txtmux.constants import DEFAULT_TERMINAL_WIDTH, DEFAULT_TERMINAL_HEIGHT


class PyteScreenCompat(pyte.Screen):  # type: ignore[misc]
    """pyte Screen subclass that handles private SGR sequences."""

    def select_graphic_rendition(self, *attrs: int, private: bool = False) -> None:
        """Handle SGR with optional private flag (ignored for compatibility)."""
        super().select_graphic_rendition(*attrs)


class TerminalScreen:
    """Wrapper around pyte for terminal emulation."""

    def __init__(self, width: int, height: int):
        self.screen = PyteScreenCompat(width, height)
        self.stream = pyte.Stream(self.screen)

    def feed(self, data: bytes) -> None:
        """Feed raw bytes from PTY into the terminal emulator."""
        self.stream.feed(data.decode("utf-8", errors="replace"))

    def resize(self, width: int, height: int) -> None:
        """Resize the virtual terminal. Note: pyte uses (lines, columns) order."""
        self.screen.resize(height, width)

    @property
    def cursor(self) -> tuple[int, int]:
        """Return current cursor position as (x, y)."""
        return (self.screen.cursor.x, self.screen.cursor.y)

    def render(self, show_cursor: bool) -> Text:
        """Convert pyte screen buffer to Rich Text with styling."""
        cursor_x, cursor_y = self.cursor
        lines = []
        for row in range(self.screen.lines):
            line = Text()
            for col in range(self.screen.columns):
                char = self.screen.buffer[row][col]
                is_cursor = show_cursor and row == cursor_y and col == cursor_x
                style = self._char_to_style(char, is_cursor)
                line.append(char.data or " ", style=style)
            lines.append(line)
        return Text("\n").join(lines)

    def _char_to_style(self, char: pyte.screens.Char, is_cursor: bool) -> Style:
        """Convert pyte Char attributes to Rich Style."""
        fg = self._pyte_color_to_rich(char.fg)
        bg = self._pyte_color_to_rich(char.bg)
        reverse = char.reverse or is_cursor

        return Style(
            color=fg,
            bgcolor=bg,
            bold=char.bold,
            italic=char.italics,
            underline=char.underscore,
            reverse=reverse,
            strike=char.strikethrough,
        )

    def _pyte_color_to_rich(self, color: str) -> str | None:
        """Convert pyte color string to Rich color."""
        if color == "default":
            return None
        if len(color) == 6 and all(c in "0123456789abcdefABCDEF" for c in color):
            return f"#{color}"
        if color.startswith("bright"):
            return f"bright_{color[6:]}"
        return color


ESCAPE_MAP = {
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "delete": b"\x1b[3~",
    "tab": b"\x09",
    "enter": b"\x0d",
    "escape": b"\x1b",
    "backspace": b"\x7f",
}


class TerminalPane(Widget):
    """Textual widget combining TerminalScreen and PTY into an interactive terminal."""

    can_focus = True

    class Detached(TextualMessage):
        """Posted when client detaches from session."""

        pass

    class ShellExited(TextualMessage):
        """Posted when the shell process exits."""

        pass

    class ConnectionFailed(TextualMessage):
        """Posted when connection to server fails."""

        def __init__(self, error: str) -> None:
            super().__init__()
            self.error = error

    def __init__(
        self,
        shell: str | None,
        socket_path: str | None,
        session_id: int | None,
    ):
        super().__init__()
        self.shell = shell
        self.socket_path = socket_path
        self.prefix_active = False
        self.session_id = session_id
        self.master_fd: int | None = None
        self.child_pid: int | None = None
        self.terminal_screen: TerminalScreen | None = None
        self._reader: StreamReader | None = None
        self._writer: StreamWriter | None = None

    def on_mount(self) -> None:
        width, height = self.size.width, self.size.height
        if width < 1:
            width = DEFAULT_TERMINAL_WIDTH
        if height < 1:
            height = DEFAULT_TERMINAL_HEIGHT
        self.terminal_screen = TerminalScreen(width, height)

        if self.socket_path is not None:
            self._connect_to_server(width, height)
        else:
            if self.shell is None:
                raise RuntimeError("shell is None in direct mode")
            self.master_fd, self.child_pid = spawn_shell(self.shell)
            set_pty_size(self.master_fd, width, height)
            self._start_pty_reader()

    @work(exclusive=True)
    async def _connect_to_server(self, width: int, height: int) -> None:
        """Connect to server socket and send IDENTIFY + ATTACH."""
        if self.socket_path is None:
            raise RuntimeError("socket_path is None in network mode")
        if self.session_id is None:
            raise RuntimeError("session_id is None in network mode")

        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            self.post_message(self.ConnectionFailed(str(e)))
            return

        self._reader = reader
        self._writer = writer

        try:
            identify_msg = encode_identify(width, height)
            writer.write(identify_msg.encode())
            await writer.drain()

            attach_msg = encode_attach(self.session_id)
            writer.write(attach_msg.encode())
            await writer.drain()
        except (ConnectionError, OSError) as e:
            self.post_message(self.ConnectionFailed(str(e)))
            return

        await self._socket_read_loop()

    @work(exclusive=True)
    async def _start_pty_reader(self) -> None:
        while self.master_fd is not None:
            try:
                data = await read_pty(self.master_fd, 4096)
            except OSError:
                break
            if not data:
                break
            if self.terminal_screen is None:
                raise RuntimeError("terminal_screen is None during PTY read loop")
            self.terminal_screen.feed(data)
            self.refresh()

    async def _socket_read_loop(self) -> None:
        """Read messages from server socket and handle OUTPUT/DETACH."""
        buffer = b""
        while self._reader is not None:
            try:
                data = await self._reader.read(4096)
            except Exception:
                break
            if not data:
                break

            buffer += data
            had_output = False

            while True:
                message, buffer = decode(buffer)
                if message is None:
                    break

                if message.msg_type == MessageType.OUTPUT:
                    if self.terminal_screen is None:
                        raise RuntimeError("terminal_screen is None during socket read")
                    self.terminal_screen.feed(decode_output(message.payload))
                    had_output = True
                elif message.msg_type == MessageType.DETACH:
                    self.post_message(self.Detached())
                    return
                elif message.msg_type == MessageType.ERROR:
                    self.post_message(self.Detached())
                    return
                elif message.msg_type == MessageType.SHELL_EXITED:
                    self.post_message(self.ShellExited())
                    return

            if had_output:
                self.refresh()

        self.post_message(self.Detached())

    def on_key(self, event: events.Key) -> None:
        if self.prefix_active:
            return

        if event.key in ("ctrl+b", "escape") and self._writer is not None:
            return

        data = self._key_to_bytes(event)
        if not data:
            return

        if self._writer is not None:
            try:
                input_msg = encode_input(data)
                self._writer.write(input_msg.encode())
            except (ConnectionError, OSError):
                pass  # Read loop will detect disconnection
            event.stop()
        elif self.master_fd is not None:
            write_pty(self.master_fd, data)
            event.stop()

    def _key_to_bytes(self, event: events.Key) -> bytes | None:
        if event.is_printable and event.character:
            return event.character.encode("utf-8")

        if event.key in ESCAPE_MAP:
            return ESCAPE_MAP[event.key]

        if event.key.startswith("ctrl+"):
            char = event.key.split("+", 1)[1]
            if len(char) == 1:
                return bytes([ord(char.upper()) - 64])

        return None

    def send_key(self, event: events.Key) -> None:
        """Send a key event to PTY, bypassing the ctrl+b filter."""
        data = self._key_to_bytes(event)
        if not data:
            return

        if self._writer is not None:
            try:
                input_msg = encode_input(data)
                self._writer.write(input_msg.encode())
            except (ConnectionError, OSError):
                pass  # Read loop will detect disconnection
        elif self.master_fd is not None:
            write_pty(self.master_fd, data)

    def on_resize(self, event: events.Resize) -> None:
        width, height = event.size.width, event.size.height
        if self.terminal_screen:
            self.terminal_screen.resize(width, height)

        if self._writer is not None:
            try:
                resize_msg = encode_resize(width, height)
                self._writer.write(resize_msg.encode())
            except (ConnectionError, OSError):
                pass  # Read loop will detect disconnection
        elif self.master_fd is not None:
            set_pty_size(self.master_fd, width, height)

    def render(self) -> Text:
        if self.terminal_screen is None:
            raise RuntimeError("terminal_screen is None during render")
        return self.terminal_screen.render(show_cursor=self.has_focus)

    def detach(self) -> None:
        """Send DETACH message to server."""
        if self._writer is None:
            raise RuntimeError("cannot detach: not connected")
        try:
            detach_msg = encode_detach()
            self._writer.write(detach_msg.encode())
        except (ConnectionError, OSError):
            pass  # Read loop will detect disconnection

    def reconnect(self, session_id: int) -> None:
        """Disconnect from current session and connect to a new one."""
        if self.socket_path is None:
            raise RuntimeError("cannot reconnect: not in network mode")

        self._close_connection()
        self._reset_screen()
        self.session_id = session_id

        width, height = self.size.width, self.size.height
        if width < 1:
            width = DEFAULT_TERMINAL_WIDTH
        if height < 1:
            height = DEFAULT_TERMINAL_HEIGHT
        self._connect_to_server(width, height)

    def _close_connection(self) -> None:
        """Close the current socket connection."""
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = None
        self._reader = None

    def _reset_screen(self) -> None:
        """Reset the terminal screen to blank state."""
        width, height = self.size.width, self.size.height
        if width < 1:
            width = DEFAULT_TERMINAL_WIDTH
        if height < 1:
            height = DEFAULT_TERMINAL_HEIGHT
        self.terminal_screen = TerminalScreen(width, height)
        self.refresh()
