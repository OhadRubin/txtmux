"""Tests for CLI entry point (main.py)."""

import asyncio
import os
import tempfile
import uuid

import pytest
import pytest_asyncio

from txtmux.cli import (
    cmd_list_sessions,
    create_session,
    ensure_server_running,
    find_session,
    is_server_running,
    list_sessions,
)
from txtmux.server import SessionServer


@pytest.fixture
def temp_socket_path():
    """Create a temporary socket path for testing."""
    tmpdir = tempfile.gettempdir()
    socket_name = f"test-cli-{uuid.uuid4().hex[:8]}"
    socket_path = os.path.join(tmpdir, socket_name)
    yield socket_path
    if os.path.exists(socket_path):
        os.unlink(socket_path)


@pytest_asyncio.fixture
async def running_server(temp_socket_path):
    """Start a server for testing."""
    server = SessionServer(socket_path=temp_socket_path)
    task = asyncio.create_task(server.start())
    await asyncio.sleep(0.1)
    yield server, temp_socket_path
    await server.stop()
    await task


class TestIsServerRunning:
    def test_returns_false_when_no_server(self, temp_socket_path):
        assert is_server_running(temp_socket_path) is False

    @pytest.mark.asyncio
    async def test_returns_true_when_server_running(self, running_server):
        _, socket_path = running_server
        assert is_server_running(socket_path) is True


class TestEnsureServerRunning:
    @pytest.mark.asyncio
    async def test_noop_when_server_already_running(self, running_server):
        _, socket_path = running_server
        ensure_server_running(socket_path)
        assert is_server_running(socket_path) is True


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_creates_session_with_name(self, running_server):
        _, socket_path = running_server
        session_id, session_name = await create_session(socket_path, "test-session")
        assert session_id == 0
        assert session_name == "test-session"

    @pytest.mark.asyncio
    async def test_creates_session_with_empty_name_uses_default(self, running_server):
        _, socket_path = running_server
        session_id, session_name = await create_session(socket_path, "")
        assert session_id == 0
        assert session_name == "main"


class TestListSessions:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_sessions(self, running_server):
        _, socket_path = running_server
        sessions = await list_sessions(socket_path)
        assert sessions == []

    @pytest.mark.asyncio
    async def test_returns_sessions_after_create(self, running_server):
        _, socket_path = running_server
        await create_session(socket_path, "my-session")
        sessions = await list_sessions(socket_path)
        assert len(sessions) == 1
        session_id, name, pane_id, pid, width, height, created_at, attached_count = sessions[0]
        assert session_id == 0
        assert name == "my-session"
        assert pane_id == 0
        assert pid > 0
        assert created_at > 0
        assert attached_count == 0


class TestFindSession:
    @pytest.mark.asyncio
    async def test_find_by_id(self, running_server):
        _, socket_path = running_server
        await create_session(socket_path, "first")
        session_id, name = await find_session(socket_path, "0")
        assert session_id == 0
        assert name == "first"

    @pytest.mark.asyncio
    async def test_find_by_name(self, running_server):
        _, socket_path = running_server
        await create_session(socket_path, "my-session")
        session_id, name = await find_session(socket_path, "my-session")
        assert session_id == 0
        assert name == "my-session"

    @pytest.mark.asyncio
    async def test_raises_when_not_found_by_id(self, running_server):
        _, socket_path = running_server
        await create_session(socket_path, "test")
        with pytest.raises(RuntimeError, match="Session 999 not found"):
            await find_session(socket_path, "999")

    @pytest.mark.asyncio
    async def test_raises_when_not_found_by_name(self, running_server):
        _, socket_path = running_server
        await create_session(socket_path, "test")
        with pytest.raises(RuntimeError, match="Session 'nonexistent' not found"):
            await find_session(socket_path, "nonexistent")

    @pytest.mark.asyncio
    async def test_raises_when_no_sessions(self, running_server):
        _, socket_path = running_server
        with pytest.raises(RuntimeError, match="No sessions found"):
            await find_session(socket_path, "0")


