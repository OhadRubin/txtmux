"""Integration tests for server.py."""

import asyncio
import os
import signal
import tempfile
import uuid
import pytest

from txtmux.server import SessionServer, daemonize, get_socket_path
from txtmux.protocol import (
    MessageType,
    decode,
    decode_session_info,
    encode_attach,
    encode_detach,
    encode_identify,
    encode_input,
    encode_list_sessions,
    encode_new_session,
    encode_resize,
)


class TestSessionServer:
    """Tests for SessionServer class."""

    @pytest.fixture
    def temp_socket_path(self):
        """Create a temporary socket path for testing (short path for Unix sockets)."""
        short_id = uuid.uuid4().hex[:8]
        path = f"/tmp/tt-{short_id}.sock"
        yield path
        if os.path.exists(path):
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_server_creates_socket_at_expected_path(
        self,
        temp_socket_path: str,
    ) -> None:
        """Server creates socket at the specified path."""
        server = SessionServer(socket_path=temp_socket_path)

        async def start_and_stop():
            task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)
            assert os.path.exists(temp_socket_path)
            await server.stop()
            await task

        await start_and_stop()

    @pytest.mark.asyncio
    async def test_server_accepts_connection_and_receives_identify(
        self,
        temp_socket_path: str,
    ) -> None:
        """Server accepts connections and receives IDENTIFY message."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            identify_msg = encode_identify(80, 24)
            writer.write(identify_msg.encode())
            await writer.drain()

            await asyncio.sleep(0.05)

            assert 0 in server._client_dimensions
            assert server._client_dimensions[0] == (80, 24)

            writer.close()
            await writer.wait_closed()

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_server_responds_to_list_sessions_empty(
        self,
        temp_socket_path: str,
    ) -> None:
        """Server responds to LIST_SESSIONS with empty list initially."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            list_msg = encode_list_sessions()
            writer.write(list_msg.encode())
            await writer.drain()

            await asyncio.sleep(0.1)

            try:
                data = await asyncio.wait_for(reader.read(1024), timeout=0.2)
            except asyncio.TimeoutError:
                data = b""

            assert data == b""

            writer.close()
            await writer.wait_closed()

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_sigterm_causes_graceful_shutdown(
        self,
        temp_socket_path: str,
    ) -> None:
        """SIGTERM causes graceful shutdown."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            assert os.path.exists(temp_socket_path)

            os.kill(os.getpid(), signal.SIGTERM)

            await asyncio.wait_for(server_task, timeout=2.0)

            assert not os.path.exists(temp_socket_path)

        await run_test()

    @pytest.mark.asyncio
    async def test_sigchld_reaps_zombie_children(
        self,
        temp_socket_path: str,
    ) -> None:
        """SIGCHLD signal reaps zombie child processes."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            child_pid = os.fork()
            if child_pid == 0:
                os._exit(0)

            await asyncio.sleep(0.1)

            os.kill(os.getpid(), signal.SIGCHLD)

            await asyncio.sleep(0.1)

            try:
                result_pid, _ = os.waitpid(child_pid, os.WNOHANG)
                already_reaped = result_pid == 0
            except ChildProcessError:
                already_reaped = True

            assert already_reaped, "Child process should have been reaped by SIGCHLD handler"

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_socket_dir_created_with_proper_permissions(self) -> None:
        """Socket directory is created with 0700 permissions."""
        short_id = uuid.uuid4().hex[:8]
        socket_dir = f"/tmp/tt-{short_id}"
        socket_path = f"{socket_dir}/test.sock"

        server = SessionServer(socket_path=socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            assert os.path.exists(socket_dir)
            assert (os.stat(socket_dir).st_mode & 0o777) == 0o700

            await server.stop()
            await server_task

            if os.path.exists(socket_dir):
                os.rmdir(socket_dir)

        await run_test()

    @pytest.mark.asyncio
    async def test_new_session_creates_session_and_returns_info(
        self,
        temp_socket_path: str,
    ) -> None:
        """NEW_SESSION creates session and returns SESSION_INFO with valid id."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            identify_msg = encode_identify(80, 24)
            writer.write(identify_msg.encode())
            await writer.drain()

            new_session_msg = encode_new_session("test-session")
            writer.write(new_session_msg.encode())
            await writer.drain()

            data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
            message, _ = decode(data)

            if message is None:
                raise RuntimeError("No message received")
            assert message.msg_type == MessageType.SESSION_INFO
            session_id, name, pane_id, pid, width, height, created_at, attached_count = decode_session_info(
                message.payload
            )
            assert session_id == 0
            assert name == "test-session"
            assert pane_id == 0
            assert pid > 0
            assert width == 80
            assert height == 24
            assert created_at > 0
            assert attached_count == 1

            writer.close()
            await writer.wait_closed()

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_attach_to_valid_session_succeeds(
        self,
        temp_socket_path: str,
    ) -> None:
        """ATTACH to valid session succeeds and returns SESSION_INFO."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader1, writer1 = await asyncio.open_unix_connection(temp_socket_path)
            identify_msg = encode_identify(80, 24)
            writer1.write(identify_msg.encode())
            await writer1.drain()
            new_session_msg = encode_new_session("test-session")
            writer1.write(new_session_msg.encode())
            await writer1.drain()
            await asyncio.wait_for(reader1.read(1024), timeout=1.0)

            reader2, writer2 = await asyncio.open_unix_connection(temp_socket_path)
            identify_msg2 = encode_identify(100, 40)
            writer2.write(identify_msg2.encode())
            await writer2.drain()
            attach_msg = encode_attach(0)
            writer2.write(attach_msg.encode())
            await writer2.drain()

            # Read messages - OUTPUT (screen replay) comes before SESSION_INFO
            data = await asyncio.wait_for(reader2.read(4096), timeout=1.0)
            buffer = data
            session_info_found = False
            while buffer:
                message, buffer = decode(buffer)
                if message is None:
                    break
                if message.msg_type == MessageType.SESSION_INFO:
                    session_id, name, _, _, _, _, _, _ = decode_session_info(message.payload)
                    assert session_id == 0
                    assert name == "test-session"
                    session_info_found = True
                    break
            assert session_info_found, "SESSION_INFO message not received"

            writer1.close()
            writer2.close()
            await writer1.wait_closed()
            await writer2.wait_closed()

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_input_message_causes_output(
        self,
        temp_socket_path: str,
    ) -> None:
        """INPUT message causes data to appear in OUTPUT (echo test)."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            identify_msg = encode_identify(80, 24)
            writer.write(identify_msg.encode())
            await writer.drain()

            new_session_msg = encode_new_session("test-session")
            writer.write(new_session_msg.encode())
            await writer.drain()

            await asyncio.wait_for(reader.read(1024), timeout=1.0)

            await asyncio.sleep(0.2)

            input_msg = encode_input(b"echo hello\n")
            writer.write(input_msg.encode())
            await writer.drain()

            received_data = b""
            for _ in range(5):
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=0.3)
                    if chunk:
                        received_data += chunk
                        if b"hello" in received_data:
                            break
                except asyncio.TimeoutError:
                    break

            assert b"hello" in received_data

            writer.close()
            await writer.wait_closed()

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_resize_changes_pty_dimensions(
        self,
        temp_socket_path: str,
    ) -> None:
        """RESIZE message changes PTY dimensions."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            identify_msg = encode_identify(80, 24)
            writer.write(identify_msg.encode())
            await writer.drain()

            new_session_msg = encode_new_session("test-session")
            writer.write(new_session_msg.encode())
            await writer.drain()

            await asyncio.wait_for(reader.read(1024), timeout=1.0)

            resize_msg = encode_resize(120, 40)
            writer.write(resize_msg.encode())
            await writer.drain()

            await asyncio.sleep(0.1)

            session = server._session_manager.find_session(session_id=0, name=None)
            if session is None:
                raise RuntimeError("Session not found")
            pane = session.panes[session.active_pane_id]
            assert pane.width == 120
            assert pane.height == 40

            writer.close()
            await writer.wait_closed()

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_client_disconnect_keeps_session_alive(
        self,
        temp_socket_path: str,
    ) -> None:
        """Client disconnect does not kill the session."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            identify_msg = encode_identify(80, 24)
            writer.write(identify_msg.encode())
            await writer.drain()

            new_session_msg = encode_new_session("test-session")
            writer.write(new_session_msg.encode())
            await writer.drain()

            await asyncio.wait_for(reader.read(1024), timeout=1.0)

            writer.close()
            await writer.wait_closed()

            await asyncio.sleep(0.2)

            sessions = server._session_manager.list_sessions()
            assert len(sessions) == 1
            assert sessions[0][1] == "test-session"

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_second_client_can_attach_to_existing_session(
        self,
        temp_socket_path: str,
    ) -> None:
        """Second client can attach to existing session."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader1, writer1 = await asyncio.open_unix_connection(temp_socket_path)
            identify_msg = encode_identify(80, 24)
            writer1.write(identify_msg.encode())
            await writer1.drain()
            new_session_msg = encode_new_session("test-session")
            writer1.write(new_session_msg.encode())
            await writer1.drain()
            await asyncio.wait_for(reader1.read(1024), timeout=1.0)

            reader2, writer2 = await asyncio.open_unix_connection(temp_socket_path)
            identify_msg2 = encode_identify(100, 40)
            writer2.write(identify_msg2.encode())
            await writer2.drain()
            attach_msg = encode_attach(0)
            writer2.write(attach_msg.encode())
            await writer2.drain()
            await asyncio.wait_for(reader2.read(1024), timeout=1.0)

            attached = server._session_manager.get_attached_clients(0)
            assert len(attached) == 2
            assert 0 in attached
            assert 1 in attached

            writer1.close()
            writer2.close()
            await writer1.wait_closed()
            await writer2.wait_closed()

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_attached_client_receives_output_from_other_client_input(
        self,
        temp_socket_path: str,
    ) -> None:
        """Attached client receives OUTPUT when another client sends INPUT."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader1, writer1 = await asyncio.open_unix_connection(temp_socket_path)
            writer1.write(encode_identify(80, 24).encode())
            await writer1.drain()
            writer1.write(encode_new_session("test-session").encode())
            await writer1.drain()
            await asyncio.wait_for(reader1.read(1024), timeout=1.0)

            reader2, writer2 = await asyncio.open_unix_connection(temp_socket_path)
            writer2.write(encode_identify(80, 24).encode())
            await writer2.drain()
            writer2.write(encode_attach(0).encode())
            await writer2.drain()
            await asyncio.wait_for(reader2.read(1024), timeout=1.0)

            await asyncio.sleep(0.2)

            writer1.write(encode_input(b"echo shared_output\n").encode())
            await writer1.drain()

            client2_data = b""
            for _ in range(5):
                try:
                    chunk = await asyncio.wait_for(reader2.read(4096), timeout=0.3)
                    if chunk:
                        client2_data += chunk
                        if b"shared_output" in client2_data:
                            break
                except asyncio.TimeoutError:
                    break

            assert b"shared_output" in client2_data

            writer1.close()
            writer2.close()
            await writer1.wait_closed()
            await writer2.wait_closed()

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_detach_removes_client_from_session(
        self,
        temp_socket_path: str,
    ) -> None:
        """DETACH message removes client from session's attached clients."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)
            writer.write(encode_identify(80, 24).encode())
            await writer.drain()
            writer.write(encode_new_session("test-session").encode())
            await writer.drain()
            await asyncio.wait_for(reader.read(1024), timeout=1.0)

            attached_before = server._session_manager.get_attached_clients(0)
            assert 0 in attached_before

            writer.write(encode_detach().encode())
            await writer.drain()
            await asyncio.sleep(0.1)

            attached_after = server._session_manager.get_attached_clients(0)
            assert 0 not in attached_after

            sessions = server._session_manager.list_sessions()
            assert len(sessions) == 1

            writer.close()
            await writer.wait_closed()

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_server_pane_screen_tracks_pty_output(
        self,
        temp_socket_path: str,
    ) -> None:
        """Server's pane.screen.display is updated when PTY output flows through."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            writer.write(encode_identify(80, 24).encode())
            await writer.drain()

            writer.write(encode_new_session("test-session").encode())
            await writer.drain()

            await asyncio.wait_for(reader.read(1024), timeout=1.0)
            await asyncio.sleep(0.2)

            writer.write(encode_input(b"echo screen_test_marker\n").encode())
            await writer.drain()

            for _ in range(10):
                try:
                    await asyncio.wait_for(reader.read(4096), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
                session = server._session_manager._sessions.get(0)
                if session:
                    pane = session.panes.get(session.active_pane_id)
                    if pane:
                        screen_content = "\n".join(pane.screen.display)
                        if "screen_test_marker" in screen_content:
                            break
                await asyncio.sleep(0.1)

            session = server._session_manager._sessions.get(0)
            pane = session.panes[session.active_pane_id]
            screen_content = "\n".join(pane.screen.display)
            assert "screen_test_marker" in screen_content, (
                f"Expected 'screen_test_marker' in pane.screen.display, got: {screen_content[:200]}"
            )

            writer.close()
            await writer.wait_closed()

            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_attach_replays_screen_state(
        self,
        temp_socket_path: str,
    ) -> None:
        """ATTACH sends current screen state before live forwarding."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            # Client 1: create session and send command
            reader1, writer1 = await asyncio.open_unix_connection(temp_socket_path)
            writer1.write(encode_identify(80, 24).encode())
            await writer1.drain()
            writer1.write(encode_new_session("replay-test").encode())
            await writer1.drain()
            await asyncio.wait_for(reader1.read(1024), timeout=1.0)
            await asyncio.sleep(0.2)

            # Send command that produces output
            writer1.write(encode_input(b"echo replay_marker_12345\n").encode())
            await writer1.drain()

            # Wait for output to be processed by server's screen
            for _ in range(10):
                try:
                    await asyncio.wait_for(reader1.read(4096), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
                session = server._session_manager._sessions.get(0)
                if session:
                    pane = session.panes.get(session.active_pane_id)
                    if pane:
                        screen_content = "\n".join(pane.screen.display)
                        if "replay_marker_12345" in screen_content:
                            break
                await asyncio.sleep(0.1)

            # Client 1 disconnects
            writer1.close()
            await writer1.wait_closed()
            await asyncio.sleep(0.1)

            # Client 2: attach to existing session
            reader2, writer2 = await asyncio.open_unix_connection(temp_socket_path)
            writer2.write(encode_identify(80, 24).encode())
            await writer2.drain()
            writer2.write(encode_attach(0).encode())
            await writer2.drain()

            # Read response - should contain screen state with marker
            received_data = b""
            for _ in range(10):
                try:
                    chunk = await asyncio.wait_for(reader2.read(4096), timeout=0.3)
                    received_data += chunk
                except asyncio.TimeoutError:
                    break

            # Parse messages and check for OUTPUT containing marker
            found_marker = False
            buffer = received_data
            while buffer:
                message, buffer = decode(buffer)
                if message is None:
                    break
                if message.msg_type == MessageType.OUTPUT:
                    output_text = message.payload.decode("utf-8", errors="replace")
                    if "replay_marker_12345" in output_text:
                        found_marker = True
                        break

            assert found_marker, (
                f"Expected 'replay_marker_12345' in OUTPUT on attach, "
                f"got: {received_data[:500]}"
            )

            writer2.close()
            await writer2.wait_closed()
            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_attach_to_fresh_session_works(
        self,
        temp_socket_path: str,
    ) -> None:
        """ATTACH to fresh session (no output yet) succeeds without error."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            # Client 1: create session but don't send any input
            reader1, writer1 = await asyncio.open_unix_connection(temp_socket_path)
            writer1.write(encode_identify(80, 24).encode())
            await writer1.drain()
            writer1.write(encode_new_session("fresh-session").encode())
            await writer1.drain()
            await asyncio.wait_for(reader1.read(1024), timeout=1.0)

            # Client 1 disconnects immediately (no input sent)
            writer1.close()
            await writer1.wait_closed()
            await asyncio.sleep(0.1)

            # Client 2: attach to the fresh session
            reader2, writer2 = await asyncio.open_unix_connection(temp_socket_path)
            writer2.write(encode_identify(80, 24).encode())
            await writer2.drain()
            writer2.write(encode_attach(0).encode())
            await writer2.drain()

            # Should receive OUTPUT (screen replay) and SESSION_INFO without error
            received_data = b""
            for _ in range(5):
                try:
                    chunk = await asyncio.wait_for(reader2.read(4096), timeout=0.3)
                    received_data += chunk
                except asyncio.TimeoutError:
                    break

            # Parse messages - should find SESSION_INFO
            session_info_found = False
            buffer = received_data
            while buffer:
                message, buffer = decode(buffer)
                if message is None:
                    break
                if message.msg_type == MessageType.SESSION_INFO:
                    session_info_found = True
                    break

            assert session_info_found, (
                f"Expected SESSION_INFO on attach to fresh session, got: {received_data[:200]}"
            )

            writer2.close()
            await writer2.wait_closed()
            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_attach_replays_multiple_lines(
        self,
        temp_socket_path: str,
    ) -> None:
        """ATTACH replays multiple lines of output."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            # Client 1: create session and send multiple commands
            reader1, writer1 = await asyncio.open_unix_connection(temp_socket_path)
            writer1.write(encode_identify(80, 24).encode())
            await writer1.drain()
            writer1.write(encode_new_session("multiline-test").encode())
            await writer1.drain()
            await asyncio.wait_for(reader1.read(1024), timeout=1.0)
            await asyncio.sleep(0.2)

            # Send multiple echo commands
            writer1.write(encode_input(b"echo LINE_ONE_MARKER\n").encode())
            await writer1.drain()
            await asyncio.sleep(0.2)
            writer1.write(encode_input(b"echo LINE_TWO_MARKER\n").encode())
            await writer1.drain()

            # Wait for both markers in server's screen
            for _ in range(15):
                try:
                    await asyncio.wait_for(reader1.read(4096), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
                session = server._session_manager._sessions.get(0)
                if session:
                    pane = session.panes.get(session.active_pane_id)
                    if pane:
                        screen_content = "\n".join(pane.screen.display)
                        if "LINE_ONE_MARKER" in screen_content and "LINE_TWO_MARKER" in screen_content:
                            break
                await asyncio.sleep(0.1)

            # Client 1 disconnects
            writer1.close()
            await writer1.wait_closed()
            await asyncio.sleep(0.1)

            # Client 2: attach and verify both lines are replayed
            reader2, writer2 = await asyncio.open_unix_connection(temp_socket_path)
            writer2.write(encode_identify(80, 24).encode())
            await writer2.drain()
            writer2.write(encode_attach(0).encode())
            await writer2.drain()

            received_data = b""
            for _ in range(10):
                try:
                    chunk = await asyncio.wait_for(reader2.read(4096), timeout=0.3)
                    received_data += chunk
                except asyncio.TimeoutError:
                    break

            # Check for both markers in OUTPUT
            found_line_one = False
            found_line_two = False
            buffer = received_data
            while buffer:
                message, buffer = decode(buffer)
                if message is None:
                    break
                if message.msg_type == MessageType.OUTPUT:
                    output_text = message.payload.decode("utf-8", errors="replace")
                    if "LINE_ONE_MARKER" in output_text:
                        found_line_one = True
                    if "LINE_TWO_MARKER" in output_text:
                        found_line_two = True

            assert found_line_one and found_line_two, (
                f"Expected both LINE_ONE_MARKER and LINE_TWO_MARKER in OUTPUT, "
                f"got line_one={found_line_one}, line_two={found_line_two}"
            )

            writer2.close()
            await writer2.wait_closed()
            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_attach_replays_scrollback_history(
        self,
        temp_socket_path: str,
    ) -> None:
        """Lines that scroll off visible screen are preserved in history."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            # Client 1: create session with 80x24 PTY
            reader1, writer1 = await asyncio.open_unix_connection(temp_socket_path)
            writer1.write(encode_identify(80, 24).encode())
            await writer1.drain()
            writer1.write(encode_new_session("scrollback-test").encode())
            await writer1.drain()
            await asyncio.wait_for(reader1.read(1024), timeout=1.0)
            await asyncio.sleep(0.2)

            # Generate 30 lines (more than 24 visible) with unique markers
            for i in range(30):
                writer1.write(encode_input(f"echo SCROLLBACK_LINE_{i:02d}\n".encode()).encode())
                await writer1.drain()
                await asyncio.sleep(0.05)

            # Wait for last marker to appear (indicates all output processed)
            for _ in range(30):
                try:
                    await asyncio.wait_for(reader1.read(4096), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
                session = server._session_manager._sessions.get(0)
                if session:
                    pane = session.panes.get(session.active_pane_id)
                    if pane:
                        screen_content = "\n".join(pane.screen.display)
                        if "SCROLLBACK_LINE_29" in screen_content:
                            break
                await asyncio.sleep(0.1)

            # Client 1 disconnects
            writer1.close()
            await writer1.wait_closed()
            await asyncio.sleep(0.1)

            # Client 2: attach and verify scrollback history is replayed
            reader2, writer2 = await asyncio.open_unix_connection(temp_socket_path)
            writer2.write(encode_identify(80, 24).encode())
            await writer2.drain()
            writer2.write(encode_attach(0).encode())
            await writer2.drain()

            received_data = b""
            for _ in range(20):
                try:
                    chunk = await asyncio.wait_for(reader2.read(8192), timeout=0.3)
                    received_data += chunk
                except asyncio.TimeoutError:
                    break

            # Collect all OUTPUT payloads
            all_output = ""
            buffer = received_data
            while buffer:
                message, buffer = decode(buffer)
                if message is None:
                    break
                if message.msg_type == MessageType.OUTPUT:
                    all_output += message.payload.decode("utf-8", errors="replace")

            # Verify early lines (scrolled off) are present
            assert "SCROLLBACK_LINE_00" in all_output, (
                "First line (scrolled off) should be in history replay"
            )
            assert "SCROLLBACK_LINE_05" in all_output, (
                "Early line (scrolled off) should be in history replay"
            )
            # Verify late lines (visible) are present
            assert "SCROLLBACK_LINE_29" in all_output, (
                "Last line should be in visible screen replay"
            )

            # Verify history comes BEFORE visible screen (order matters)
            pos_line_00 = all_output.find("SCROLLBACK_LINE_00")
            pos_line_29 = all_output.find("SCROLLBACK_LINE_29")
            assert pos_line_00 < pos_line_29, (
                f"History (line 00 at pos {pos_line_00}) should come before "
                f"visible screen (line 29 at pos {pos_line_29})"
            )

            writer2.close()
            await writer2.wait_closed()
            await server.stop()
            await server_task

        await run_test()


class TestDaemonize:
    """Tests for daemonize function."""

    def test_daemonize_forks_and_writes_pid_file(self) -> None:
        """Daemonize forks to background and writes PID file."""
        short_id = uuid.uuid4().hex[:8]
        pid_file = f"/tmp/tt-{short_id}.pid"
        marker_file = f"/tmp/tt-{short_id}.marker"

        pid = os.fork()
        if pid == 0:
            daemonize(pid_file)
            with open(marker_file, "w") as f:
                f.write("started")
            os._exit(0)

        os.waitpid(pid, 0)

        import time
        for _ in range(20):
            if os.path.exists(pid_file):
                break
            time.sleep(0.1)

        assert os.path.exists(pid_file)

        with open(pid_file) as f:
            daemon_pid = int(f.read().strip())
        assert daemon_pid > 0

        try:
            os.kill(daemon_pid, 0)
            os.kill(daemon_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        finally:
            if os.path.exists(pid_file):
                os.unlink(pid_file)
            if os.path.exists(marker_file):
                os.unlink(marker_file)


class TestGetSocketPath:
    """Tests for get_socket_path function."""

    def test_uses_tmux_tmpdir_if_set(self, monkeypatch, tmp_path) -> None:
        """Uses TMUX_TMPDIR environment variable if set."""
        monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
        path = get_socket_path()
        assert path == str(tmp_path / "default")

    def test_uses_tmp_with_uid_if_no_env(self, monkeypatch) -> None:
        """Uses /tmp/textual-tmux-{uid} if TMUX_TMPDIR not set."""
        monkeypatch.delenv("TMUX_TMPDIR", raising=False)
        path = get_socket_path()
        uid = os.getuid()
        assert path == f"/tmp/textual-tmux-{uid}/default"
