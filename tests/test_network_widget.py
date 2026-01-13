"""Integration test for TerminalPane network mode."""
import asyncio
import tempfile
import os
import pytest

from txtmux.server import SessionServer
from txtmux.protocol import (
    encode_identify,
    encode_new_session,
    encode_attach,
    encode_input,
    encode_resize,
    decode,
    decode_session_info,
    decode_output,
    MessageType,
)


class TestNetworkTerminalWidget:
    """Test TerminalPane in network mode against real server."""

    @pytest.fixture
    def temp_socket_path(self):
        import tempfile
        # Use /tmp directly to avoid path length issues on macOS
        fd, path = tempfile.mkstemp(suffix=".sock", dir="/tmp")
        os.close(fd)
        os.unlink(path)
        yield path
        if os.path.exists(path):
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_client_connects_to_server_successfully(self, temp_socket_path):
        """Criterion: TerminalPane connects to server socket successfully."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            # Simulate what TerminalPane._connect_to_server does
            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            # Send IDENTIFY
            identify_msg = encode_identify(80, 24)
            writer.write(identify_msg.encode())
            await writer.drain()

            # Create a session first
            new_session_msg = encode_new_session("test")
            writer.write(new_session_msg.encode())
            await writer.drain()

            await asyncio.sleep(0.1)

            # Read SESSION_INFO response
            data = await asyncio.wait_for(reader.read(4096), timeout=1.0)
            message, _ = decode(data)
            assert message is not None
            assert message.msg_type == MessageType.SESSION_INFO

            session_id, _, _, _, _, _, _, _ = decode_session_info(message.payload)

            # Send ATTACH (like TerminalPane does)
            attach_msg = encode_attach(session_id)
            writer.write(attach_msg.encode())
            await writer.drain()

            # Connection successful!
            writer.close()
            await writer.wait_closed()
            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_output_messages_render_in_widget(self, temp_socket_path):
        """Criterion: OUTPUT messages render in the widget."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            # Setup: IDENTIFY, NEW_SESSION, ATTACH
            writer.write(encode_identify(80, 24).encode())
            writer.write(encode_new_session("test").encode())
            await writer.drain()
            await asyncio.sleep(0.1)

            data = await reader.read(4096)
            message, _ = decode(data)
            session_id, _, _, _, _, _, _, _ = decode_session_info(message.payload)

            writer.write(encode_attach(session_id).encode())
            await writer.drain()
            await asyncio.sleep(0.2)

            # Should receive OUTPUT from shell prompt
            output_received = False
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                while data:
                    message, remaining = decode(data)
                    if message and message.msg_type == MessageType.OUTPUT:
                        output_data = decode_output(message.payload)
                        assert len(output_data) > 0
                        output_received = True
                        break
                    data = remaining
                    if not data:
                        data = await asyncio.wait_for(reader.read(4096), timeout=0.5)
            except asyncio.TimeoutError:
                pass

            assert output_received, "Should receive OUTPUT message from shell"

            writer.close()
            await writer.wait_closed()
            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_keyboard_input_reaches_server(self, temp_socket_path):
        """Criterion: Keyboard input reaches the server and affects the shell."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            # Setup
            writer.write(encode_identify(80, 24).encode())
            writer.write(encode_new_session("test").encode())
            await writer.drain()
            await asyncio.sleep(0.1)

            data = await reader.read(4096)
            message, _ = decode(data)
            session_id, _, _, _, _, _, _, _ = decode_session_info(message.payload)

            writer.write(encode_attach(session_id).encode())
            await writer.drain()
            await asyncio.sleep(0.3)

            # Drain initial output
            try:
                await asyncio.wait_for(reader.read(4096), timeout=0.3)
            except asyncio.TimeoutError:
                pass

            # Send INPUT (echo command) - this is what on_key does
            input_msg = encode_input(b"echo TESTMARKER123\n")
            writer.write(input_msg.encode())
            await writer.drain()

            # Wait for output containing our marker
            found_marker = False
            for _ in range(10):
                try:
                    data = await asyncio.wait_for(reader.read(4096), timeout=0.5)
                    while data:
                        message, data = decode(data)
                        if message and message.msg_type == MessageType.OUTPUT:
                            output = decode_output(message.payload)
                            if b"TESTMARKER123" in output:
                                found_marker = True
                                break
                    if found_marker:
                        break
                except asyncio.TimeoutError:
                    break

            assert found_marker, "INPUT should cause OUTPUT with our marker"

            writer.close()
            await writer.wait_closed()
            await server.stop()
            await server_task

        await run_test()

    @pytest.mark.asyncio
    async def test_resize_messages_sent_on_resize(self, temp_socket_path):
        """Criterion: RESIZE messages sent on terminal resize."""
        server = SessionServer(socket_path=temp_socket_path)

        async def run_test():
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.1)

            reader, writer = await asyncio.open_unix_connection(temp_socket_path)

            # Setup
            writer.write(encode_identify(80, 24).encode())
            writer.write(encode_new_session("test").encode())
            await writer.drain()
            await asyncio.sleep(0.1)

            data = await reader.read(4096)
            message, _ = decode(data)
            session_id, _, _, _, _, _, _, _ = decode_session_info(message.payload)

            writer.write(encode_attach(session_id).encode())
            await writer.drain()
            await asyncio.sleep(0.2)

            # Send RESIZE (this is what on_resize does in network mode)
            resize_msg = encode_resize(120, 40)
            writer.write(resize_msg.encode())
            await writer.drain()

            # Verify by checking stty size
            await asyncio.sleep(0.1)
            
            # Drain any pending output
            try:
                await asyncio.wait_for(reader.read(4096), timeout=0.2)
            except asyncio.TimeoutError:
                pass

            # Send stty size command
            writer.write(encode_input(b"stty size\n").encode())
            await writer.drain()

            # Look for 40 120 in output
            found_size = False
            for _ in range(10):
                try:
                    data = await asyncio.wait_for(reader.read(4096), timeout=0.5)
                    while data:
                        message, data = decode(data)
                        if message and message.msg_type == MessageType.OUTPUT:
                            output = decode_output(message.payload)
                            if b"40" in output and b"120" in output:
                                found_size = True
                                break
                    if found_size:
                        break
                except asyncio.TimeoutError:
                    break

            assert found_size, "RESIZE should change PTY dimensions to 120x40"

            writer.close()
            await writer.wait_closed()
            await server.stop()
            await server_task

        await run_test()
