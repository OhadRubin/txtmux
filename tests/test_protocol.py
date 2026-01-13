"""Unit tests for protocol.py."""

import struct
import pytest
from txtmux.protocol import (
    Message,
    MessageType,
    HEADER_SIZE,
    decode,
    encode_identify,
    decode_identify,
    encode_new_session,
    decode_new_session,
    encode_attach,
    decode_attach,
    encode_detach,
    encode_list_sessions,
    encode_resize,
    decode_resize,
    encode_input,
    decode_input,
    encode_output,
    decode_output,
    encode_error,
    decode_error,
    encode_session_info,
    decode_session_info,
    encode_shell_exited,
    decode_shell_exited,
)


class TestMessageEncodeDecode:
    """Tests for Message encode/decode roundtrip."""

    def test_encode_decode_identify(self):
        msg = encode_identify(80, 24)
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.IDENTIFY
        assert remaining == b""
        assert decode_identify(decoded.payload) == (80, 24)

    def test_encode_decode_new_session(self):
        msg = encode_new_session("my-session")
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.NEW_SESSION
        assert remaining == b""
        assert decode_new_session(decoded.payload) == "my-session"

    def test_encode_decode_attach(self):
        msg = encode_attach(42)
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.ATTACH
        assert remaining == b""
        assert decode_attach(decoded.payload) == 42

    def test_encode_decode_detach(self):
        msg = encode_detach()
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.DETACH
        assert remaining == b""
        assert decoded.payload == b""

    def test_encode_decode_list_sessions(self):
        msg = encode_list_sessions()
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.LIST_SESSIONS
        assert remaining == b""
        assert decoded.payload == b""

    def test_encode_decode_resize(self):
        msg = encode_resize(120, 40)
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.RESIZE
        assert remaining == b""
        assert decode_resize(decoded.payload) == (120, 40)

    def test_encode_decode_input(self):
        data = b"\x1b[A\x1b[B"
        msg = encode_input(data)
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.INPUT
        assert remaining == b""
        assert decode_input(decoded.payload) == data

    def test_encode_decode_output(self):
        data = b"Hello, World!\r\n\x1b[31mRed\x1b[0m"
        msg = encode_output(data)
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.OUTPUT
        assert remaining == b""
        assert decode_output(decoded.payload) == data

    def test_encode_decode_error(self):
        msg = encode_error("Session not found")
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.ERROR
        assert remaining == b""
        assert decode_error(decoded.payload) == "Session not found"

    def test_encode_decode_session_info(self):
        msg = encode_session_info(
            session_id=1,
            name="work",
            pane_id=0,
            pid=12345,
            width=80,
            height=24,
            created_at=1700000000.0,
            attached_count=2,
        )
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.SESSION_INFO
        assert remaining == b""
        assert decode_session_info(decoded.payload) == (1, "work", 0, 12345, 80, 24, 1700000000.0, 2)

    def test_encode_decode_shell_exited(self):
        msg = encode_shell_exited(session_id=5, pane_id=3)
        encoded = msg.encode()
        decoded, remaining = decode(encoded)

        assert decoded is not None
        assert decoded.msg_type == MessageType.SHELL_EXITED
        assert remaining == b""
        assert decode_shell_exited(decoded.payload) == (5, 3)


class TestDecodePartialData:
    """Tests for handling partial/incomplete data."""

    def test_decode_empty_data(self):
        decoded, remaining = decode(b"")
        assert decoded is None
        assert remaining == b""

    def test_decode_partial_header(self):
        msg = encode_identify(80, 24)
        encoded = msg.encode()
        partial = encoded[:4]

        decoded, remaining = decode(partial)
        assert decoded is None
        assert remaining == partial

    def test_decode_partial_payload(self):
        msg = encode_new_session("long-session-name")
        encoded = msg.encode()
        partial = encoded[: HEADER_SIZE + 2]

        decoded, remaining = decode(partial)
        assert decoded is None
        assert remaining == partial

    def test_decode_exact_header_no_payload(self):
        msg = encode_detach()
        encoded = msg.encode()

        decoded, remaining = decode(encoded)
        assert decoded is not None
        assert decoded.msg_type == MessageType.DETACH


class TestDecodeMultipleMessages:
    """Tests for decoding multiple messages from a buffer."""

    def test_decode_two_messages(self):
        msg1 = encode_identify(80, 24)
        msg2 = encode_resize(120, 40)
        buffer = msg1.encode() + msg2.encode()

        decoded1, remaining = decode(buffer)
        assert decoded1 is not None
        assert decoded1.msg_type == MessageType.IDENTIFY
        assert decode_identify(decoded1.payload) == (80, 24)

        decoded2, remaining = decode(remaining)
        assert decoded2 is not None
        assert decoded2.msg_type == MessageType.RESIZE
        assert decode_resize(decoded2.payload) == (120, 40)
        assert remaining == b""

    def test_decode_three_messages_with_trailing(self):
        msg1 = encode_input(b"ls\n")
        msg2 = encode_input(b"pwd\n")
        msg3 = encode_detach()
        trailing = b"\x00\x00"
        buffer = msg1.encode() + msg2.encode() + msg3.encode() + trailing

        decoded1, remaining = decode(buffer)
        assert decoded1 is not None
        assert decode_input(decoded1.payload) == b"ls\n"

        decoded2, remaining = decode(remaining)
        assert decoded2 is not None
        assert decode_input(decoded2.payload) == b"pwd\n"

        decoded3, remaining = decode(remaining)
        assert decoded3 is not None
        assert decoded3.msg_type == MessageType.DETACH

        assert remaining == trailing


class TestPayloadHelpers:
    """Tests for individual payload encode/decode helpers."""

    def test_identify_dimensions_max(self):
        width, height = 65535, 65535
        msg = encode_identify(width, height)
        assert decode_identify(msg.payload) == (width, height)

    def test_new_session_unicode_name(self):
        name = "session-\u4e2d\u6587"
        msg = encode_new_session(name)
        assert decode_new_session(msg.payload) == name

    def test_session_info_large_pid(self):
        msg = encode_session_info(
            session_id=999,
            name="test",
            pane_id=5,
            pid=2**31 - 1,
            width=200,
            height=50,
            created_at=1700000000.5,
            attached_count=5,
        )
        result = decode_session_info(msg.payload)
        assert result == (999, "test", 5, 2**31 - 1, 200, 50, 1700000000.5, 5)

    def test_error_empty_message(self):
        msg = encode_error("")
        assert decode_error(msg.payload) == ""

    def test_input_empty_data(self):
        msg = encode_input(b"")
        assert decode_input(msg.payload) == b""

    def test_output_binary_data(self):
        data = bytes(range(256))
        msg = encode_output(data)
        assert decode_output(msg.payload) == data


class TestMalformedData:
    """Tests for malformed/invalid data handling."""

    def test_decode_invalid_message_type(self):
        invalid_type = 99
        payload = b"test"
        header = struct.pack("!II", invalid_type, len(payload))
        data = header + payload

        with pytest.raises(ValueError):
            decode(data)

    def test_decode_identify_truncated_payload(self):
        with pytest.raises(struct.error):
            decode_identify(b"\x00\x50")

    def test_decode_resize_truncated_payload(self):
        with pytest.raises(struct.error):
            decode_resize(b"\x00")

    def test_decode_attach_truncated_payload(self):
        with pytest.raises(struct.error):
            decode_attach(b"\x00\x00")

    def test_decode_new_session_truncated_length(self):
        with pytest.raises(struct.error):
            decode_new_session(b"\x00\x00")

    def test_decode_session_info_truncated(self):
        with pytest.raises(struct.error):
            decode_session_info(b"\x00\x00\x00\x01")
