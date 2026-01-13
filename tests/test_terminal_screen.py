import pytest
from rich.text import Text

from txtmux.terminal_widget import TerminalScreen


class TestTerminalScreenFeed:
    """Tests for TerminalScreen.feed() method."""

    def test_feed_basic_text(self):
        """feed(b"hello\\r\\n") results in "hello" appearing in render output."""
        screen = TerminalScreen(width=80, height=24)
        screen.feed(b"hello\r\n")
        rendered = screen.render(show_cursor=False)
        assert "hello" in rendered.plain

    def test_feed_multiple_lines(self):
        """Multiple lines are rendered correctly."""
        screen = TerminalScreen(width=80, height=24)
        screen.feed(b"line1\r\nline2\r\n")
        rendered = screen.render(show_cursor=False)
        assert "line1" in rendered.plain
        assert "line2" in rendered.plain

    def test_feed_utf8_with_errors(self):
        """Invalid UTF-8 bytes are replaced, not causing exceptions."""
        screen = TerminalScreen(width=80, height=24)
        screen.feed(b"hello\xff\xfeworld")
        rendered = screen.render(show_cursor=False)
        assert "hello" in rendered.plain
        assert "world" in rendered.plain


class TestTerminalScreenResize:
    """Tests for TerminalScreen.resize() method."""

    def test_resize_changes_dimensions(self):
        """resize(40, 20) changes screen dimensions."""
        screen = TerminalScreen(width=80, height=24)
        screen.resize(40, 20)
        assert screen.screen.columns == 40
        assert screen.screen.lines == 20

    def test_resize_preserves_content(self):
        """Content is preserved after resize (within new bounds)."""
        screen = TerminalScreen(width=80, height=24)
        screen.feed(b"test")
        screen.resize(40, 20)
        rendered = screen.render(show_cursor=False)
        assert "test" in rendered.plain


class TestTerminalScreenRender:
    """Tests for TerminalScreen.render() method."""

    def test_render_returns_rich_text(self):
        """render() returns a Rich Text object."""
        screen = TerminalScreen(width=80, height=24)
        rendered = screen.render(show_cursor=False)
        assert isinstance(rendered, Text)

    def test_render_empty_screen(self):
        """Empty screen renders without error."""
        screen = TerminalScreen(width=80, height=24)
        rendered = screen.render(show_cursor=False)
        assert isinstance(rendered, Text)
        assert len(rendered.plain) > 0


class TestTerminalScreenColors:
    """Tests for color escape sequence handling."""

    def test_red_foreground(self):
        """\\033[31m red \\033[0m renders with red style."""
        screen = TerminalScreen(width=80, height=24)
        screen.feed(b"\033[31mred\033[0m")
        rendered = screen.render(show_cursor=False)
        assert "red" in rendered.plain
        spans = list(rendered.spans)
        red_spans = [s for s in spans if s.style and "red" in str(s.style)]
        assert len(red_spans) > 0, "Expected red styled spans"

    def test_bold_text(self):
        """Bold escape sequence renders with bold style."""
        screen = TerminalScreen(width=80, height=24)
        screen.feed(b"\033[1mbold\033[0m")
        rendered = screen.render(show_cursor=False)
        assert "bold" in rendered.plain
        spans = list(rendered.spans)
        bold_spans = [s for s in spans if s.style and s.style.bold]
        assert len(bold_spans) > 0, "Expected bold styled spans"

    def test_background_color(self):
        """Background color escape sequence is handled."""
        screen = TerminalScreen(width=80, height=24)
        screen.feed(b"\033[44mblue bg\033[0m")
        rendered = screen.render(show_cursor=False)
        assert "blue bg" in rendered.plain


class TestTerminalScreenCursor:
    """Tests for cursor position tracking."""

    def test_cursor_initial_position(self):
        """Cursor starts at (0, 0)."""
        screen = TerminalScreen(width=80, height=24)
        assert screen.cursor == (0, 0)

    def test_cursor_moves_with_text(self):
        """Cursor moves after feeding text."""
        screen = TerminalScreen(width=80, height=24)
        screen.feed(b"hello")
        assert screen.cursor[0] == 5

    def test_cursor_renders_with_reverse_style(self):
        """Cursor position renders with reverse style when show_cursor=True."""
        screen = TerminalScreen(width=80, height=24)
        screen.feed(b"hello")
        rendered = screen.render(show_cursor=True)
        spans = list(rendered.spans)
        cursor_x, cursor_y = screen.cursor
        cursor_pos_in_plain = cursor_y * (screen.screen.columns + 1) + cursor_x
        reverse_spans = [s for s in spans if s.style and s.style.reverse and s.start <= cursor_pos_in_plain < s.end]
        assert len(reverse_spans) > 0, "Expected cursor position to have reverse style"

    def test_cursor_not_rendered_when_show_cursor_false(self):
        """Cursor position has no reverse style when show_cursor=False."""
        screen = TerminalScreen(width=80, height=24)
        rendered = screen.render(show_cursor=False)
        spans = list(rendered.spans)
        reverse_spans = [s for s in spans if s.style and s.style.reverse]
        assert len(reverse_spans) == 0, "Expected no reverse style when cursor hidden"
