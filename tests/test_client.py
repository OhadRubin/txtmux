"""Tests for client.py TerminalApp."""

import pytest

from txtmux.client import TerminalApp
from txtmux.terminal_widget import TerminalPane


class TestTerminalApp:
    """Tests for TerminalApp structure and behavior."""

    def test_terminal_app_stores_session_info(self):
        """TerminalApp stores socket_path, session_id, session_name."""
        app = TerminalApp("/tmp/test.sock", 42, "my-session")
        assert app.socket_path == "/tmp/test.sock"
        assert app.session_id == 42
        assert app.session_name == "my-session"

    def test_terminal_app_has_prefix_state(self):
        """TerminalApp has _prefix_active for Ctrl+B D handling."""
        app = TerminalApp("/tmp/test.sock", 0, "test")
        assert hasattr(app, "_prefix_active")
        assert app._prefix_active is False

    def test_terminal_app_has_required_methods(self):
        """TerminalApp has on_key and on_terminal_pane_detached handlers."""
        app = TerminalApp("/tmp/test.sock", 0, "test")
        assert hasattr(app, "on_key")
        assert hasattr(app, "on_terminal_pane_detached")
        assert hasattr(app, "_do_detach")
        assert hasattr(app, "_forward_prefix_and_key")


class TestTerminalPaneDetach:
    """Tests for TerminalPane.detach() method."""

    def test_terminal_pane_has_detach_method(self):
        """TerminalPane has detach() method."""
        pane = TerminalPane(shell=None, socket_path="/tmp/test.sock", session_id=0)
        assert hasattr(pane, "detach")
        assert callable(pane.detach)

    def test_detach_raises_when_not_connected(self):
        """detach() raises RuntimeError when not connected."""
        pane = TerminalPane(shell=None, socket_path="/tmp/test.sock", session_id=0)
        with pytest.raises(RuntimeError, match="cannot detach: not connected"):
            pane.detach()


class TestPrefixKeyHandling:
    """Tests for Ctrl+B D prefix key state machine."""

    def test_ctrl_b_sets_prefix_active(self):
        """Pressing ctrl+b (via action_prefix_key) sets _prefix_active to True."""
        from unittest.mock import MagicMock, patch

        app = TerminalApp("/tmp/test.sock", 0, "test")
        assert app._prefix_active is False

        mock_pane = MagicMock()
        with patch.object(app, "query_one", return_value=mock_pane):
            app.action_prefix_key()

        assert app._prefix_active is True
        assert mock_pane.prefix_active is True

    def test_d_after_prefix_resets_flag(self):
        """Pressing 'd' after prefix resets _prefix_active."""
        from textual import events
        from unittest.mock import MagicMock, patch

        app = TerminalApp("/tmp/test.sock", 0, "test")
        app._prefix_active = True

        mock_pane = MagicMock()
        with patch.object(app, "query_one", return_value=mock_pane):
            with patch.object(app, "_do_detach"):
                event = events.Key(key="d", character="d")
                app.on_key(event)

        assert app._prefix_active is False
        assert mock_pane.prefix_active is False

    def test_other_key_after_prefix_resets_flag(self):
        """Pressing non-'d' key after prefix resets _prefix_active."""
        from textual import events
        from unittest.mock import MagicMock, patch

        app = TerminalApp("/tmp/test.sock", 0, "test")
        app._prefix_active = True

        mock_pane = MagicMock()
        with patch.object(app, "query_one", return_value=mock_pane):
            with patch.object(app, "_forward_prefix_and_key"):
                event = events.Key(key="x", character="x")
                app.on_key(event)

        assert app._prefix_active is False
        assert mock_pane.prefix_active is False

    def test_d_after_prefix_calls_do_detach(self):
        """Pressing 'd' after prefix calls _do_detach."""
        from textual import events
        from unittest.mock import MagicMock, patch

        app = TerminalApp("/tmp/test.sock", 0, "test")
        app._prefix_active = True
        app._do_detach = MagicMock()

        mock_pane = MagicMock()
        with patch.object(app, "query_one", return_value=mock_pane):
            event = events.Key(key="d", character="d")
            app.on_key(event)

        app._do_detach.assert_called_once()

    def test_other_key_after_prefix_forwards_keys(self):
        """Pressing non-'d' key after prefix calls _forward_prefix_and_key."""
        from textual import events
        from unittest.mock import MagicMock, patch

        app = TerminalApp("/tmp/test.sock", 0, "test")
        app._prefix_active = True
        app._forward_prefix_and_key = MagicMock()

        mock_pane = MagicMock()
        with patch.object(app, "query_one", return_value=mock_pane):
            event = events.Key(key="x", character="x")
            app.on_key(event)

        app._forward_prefix_and_key.assert_called_once_with(event)


class TestServerDisconnect:
    """Tests for server disconnect handling."""

    @pytest.mark.asyncio
    async def test_socket_read_loop_posts_detached_on_eof(self):
        """_socket_read_loop posts Detached when server closes connection."""
        import asyncio

        pane = TerminalPane(shell=None, socket_path="/tmp/test.sock", session_id=0)

        reader = asyncio.StreamReader()
        reader.feed_eof()
        pane._reader = reader

        messages = []
        original_post = pane.post_message

        def capture_post(msg):
            messages.append(msg)

        pane.post_message = capture_post

        await pane._socket_read_loop()

        assert len(messages) == 1
        assert isinstance(messages[0], TerminalPane.Detached)
