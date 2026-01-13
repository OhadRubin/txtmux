"""Tests for session data structures."""

import os
import pytest

from txtmux.session import Pane, Session, SessionManager


class TestSessionManager:
    """Tests for SessionManager class."""

    def test_create_session_returns_session_with_one_pane(self) -> None:
        """create_session() returns new Session with one Pane."""
        manager = SessionManager()
        session = manager.create_session(
            name="test",
            shell="/bin/sh",
            width=80,
            height=24,
        )

        assert isinstance(session, Session)
        assert session.id == 0
        assert session.name == "test"
        assert len(session.panes) == 1
        assert session.active_pane_id in session.panes

        pane = session.panes[session.active_pane_id]
        assert isinstance(pane, Pane)
        assert pane.width == 80
        assert pane.height == 24
        assert pane.pty_fd >= 0
        assert pane.pid > 0

        manager.destroy_session(session.id)

    def test_destroy_session_closes_pty_and_terminates_shell(self) -> None:
        """destroy_session() closes PTY fd and terminates shell process."""
        manager = SessionManager()
        session = manager.create_session(
            name="test",
            shell="/bin/sh",
            width=80,
            height=24,
        )

        pane = session.panes[session.active_pane_id]
        pty_fd = pane.pty_fd
        pid = pane.pid

        manager.destroy_session(session.id)

        with pytest.raises(OSError):
            os.read(pty_fd, 1)

        result = os.waitpid(pid, os.WNOHANG)
        assert result[0] == pid or result[0] == 0

    def test_find_session_by_name(self) -> None:
        """find_session(name="foo") returns correct session."""
        manager = SessionManager()
        session = manager.create_session(
            name="foo",
            shell="/bin/sh",
            width=80,
            height=24,
        )

        found = manager.find_session(session_id=None, name="foo")
        assert found is session
        assert found.name == "foo"

        not_found = manager.find_session(session_id=None, name="bar")
        assert not_found is None

        manager.destroy_session(session.id)

    def test_find_session_by_id(self) -> None:
        """find_session(session_id=X) returns correct session."""
        manager = SessionManager()
        session = manager.create_session(
            name="test",
            shell="/bin/sh",
            width=80,
            height=24,
        )

        found = manager.find_session(session_id=session.id, name=None)
        assert found is session

        not_found = manager.find_session(session_id=999, name=None)
        assert not_found is None

        manager.destroy_session(session.id)

    def test_list_sessions_returns_all_sessions(self) -> None:
        """list_sessions() returns all session names and ids."""
        manager = SessionManager()
        s1 = manager.create_session(name="first", shell="/bin/sh", width=80, height=24)
        s2 = manager.create_session(name="second", shell="/bin/sh", width=80, height=24)

        sessions = manager.list_sessions()
        assert len(sessions) == 2
        assert (s1.id, "first") in sessions
        assert (s2.id, "second") in sessions

        manager.destroy_session(s1.id)
        manager.destroy_session(s2.id)

    def test_duplicate_session_name_raises(self) -> None:
        """Creating session with duplicate name raises ValueError."""
        manager = SessionManager()
        manager.create_session(name="dup", shell="/bin/sh", width=80, height=24)

        with pytest.raises(ValueError, match="already exists"):
            manager.create_session(name="dup", shell="/bin/sh", width=80, height=24)

        manager.destroy_session(0)

    def test_destroy_nonexistent_session_raises(self) -> None:
        """Destroying nonexistent session raises KeyError."""
        manager = SessionManager()
        with pytest.raises(KeyError):
            manager.destroy_session(999)

    def test_client_attachment(self) -> None:
        """Client attach/detach/get operations work correctly."""
        manager = SessionManager()
        session = manager.create_session(
            name="test",
            shell="/bin/sh",
            width=80,
            height=24,
        )

        manager.attach_client(session.id, client_id=100)
        manager.attach_client(session.id, client_id=200)

        clients = manager.get_attached_clients(session.id)
        assert clients == {100, 200}

        manager.detach_client(session.id, client_id=100)
        clients = manager.get_attached_clients(session.id)
        assert clients == {200}

        manager.destroy_session(session.id)

    def test_find_session_requires_id_or_name(self) -> None:
        """find_session raises when neither id nor name provided."""
        manager = SessionManager()
        with pytest.raises(ValueError, match="Must provide"):
            manager.find_session(session_id=None, name=None)

    def test_create_pane_adds_pane_to_session(self) -> None:
        """create_pane() adds a new pane to existing session."""
        manager = SessionManager()
        session = manager.create_session(
            name="test",
            shell="/bin/sh",
            width=80,
            height=24,
        )

        assert len(session.panes) == 1
        first_pane_id = session.active_pane_id

        new_pane = manager.create_pane(
            session_id=session.id,
            shell="/bin/sh",
            width=120,
            height=40,
        )

        assert len(session.panes) == 2
        assert new_pane.id in session.panes
        assert new_pane.id != first_pane_id
        assert new_pane.width == 120
        assert new_pane.height == 40
        assert new_pane.pty_fd >= 0
        assert new_pane.pid > 0

        manager.destroy_session(session.id)

    def test_create_pane_nonexistent_session_raises(self) -> None:
        """create_pane() on nonexistent session raises KeyError."""
        manager = SessionManager()
        with pytest.raises(KeyError):
            manager.create_pane(session_id=999, shell="/bin/sh", width=80, height=24)

    def test_destroy_pane_removes_pane(self) -> None:
        """destroy_pane() removes pane and cleans up PTY."""
        manager = SessionManager()
        session = manager.create_session(
            name="test",
            shell="/bin/sh",
            width=80,
            height=24,
        )

        new_pane = manager.create_pane(
            session_id=session.id,
            shell="/bin/sh",
            width=80,
            height=24,
        )

        assert len(session.panes) == 2
        pty_fd = new_pane.pty_fd
        pid = new_pane.pid

        manager.destroy_pane(session.id, new_pane.id)

        assert len(session.panes) == 1
        assert new_pane.id not in session.panes

        with pytest.raises(OSError):
            os.read(pty_fd, 1)

        manager.destroy_session(session.id)

    def test_destroy_last_pane_raises(self) -> None:
        """destroy_pane() raises when trying to destroy last pane."""
        manager = SessionManager()
        session = manager.create_session(
            name="test",
            shell="/bin/sh",
            width=80,
            height=24,
        )

        with pytest.raises(ValueError, match="Cannot destroy last pane"):
            manager.destroy_pane(session.id, session.active_pane_id)

        manager.destroy_session(session.id)

    def test_destroy_pane_updates_active_pane(self) -> None:
        """destroy_pane() updates active_pane_id if active pane destroyed."""
        manager = SessionManager()
        session = manager.create_session(
            name="test",
            shell="/bin/sh",
            width=80,
            height=24,
        )

        first_pane_id = session.active_pane_id
        new_pane = manager.create_pane(
            session_id=session.id,
            shell="/bin/sh",
            width=80,
            height=24,
        )

        session.active_pane_id = first_pane_id
        manager.destroy_pane(session.id, first_pane_id)

        assert session.active_pane_id == new_pane.id

        manager.destroy_session(session.id)
