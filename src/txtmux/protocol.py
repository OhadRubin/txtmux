"""Message protocol for client-server IPC communication."""

from dataclasses import dataclass
from enum import IntEnum
import struct


class MessageType(IntEnum):
    """Protocol message types."""

    IDENTIFY = 0
    NEW_SESSION = 1
    ATTACH = 2
    DETACH = 3
    LIST_SESSIONS = 4
    RESIZE = 5
    INPUT = 6
    OUTPUT = 7
    ERROR = 8
    SESSION_INFO = 9
    SHELL_EXITED = 10
    KILL_SESSION = 11


HEADER_FORMAT = "!II"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


@dataclass
class Message:
    """Protocol message with type and payload."""

    msg_type: MessageType
    payload: bytes

    def encode(self) -> bytes:
        """Encode message to bytes: header (type, length) + payload."""
        header = struct.pack(HEADER_FORMAT, self.msg_type, len(self.payload))
        return header + self.payload


def decode(data: bytes) -> tuple[Message | None, bytes]:
    """
    Decode a message from bytes.

    Returns (message, remaining_bytes) if complete message found.
    Returns (None, data) if incomplete.
    """
    if len(data) < HEADER_SIZE:
        return (None, data)

    msg_type_int, payload_len = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    total_len = HEADER_SIZE + payload_len

    if len(data) < total_len:
        return (None, data)

    payload = data[HEADER_SIZE:total_len]
    remaining = data[total_len:]

    return (Message(MessageType(msg_type_int), payload), remaining)


def encode_identify(width: int, height: int) -> Message:
    """Encode IDENTIFY message with terminal dimensions."""
    payload = struct.pack("!HH", width, height)
    return Message(MessageType.IDENTIFY, payload)


def decode_identify(payload: bytes) -> tuple[int, int]:
    """Decode IDENTIFY payload to (width, height)."""
    width, height = struct.unpack("!HH", payload)
    return (width, height)


def encode_new_session(name: str) -> Message:
    """Encode NEW_SESSION message with session name."""
    name_bytes = name.encode("utf-8")
    payload = struct.pack("!I", len(name_bytes)) + name_bytes
    return Message(MessageType.NEW_SESSION, payload)


def decode_new_session(payload: bytes) -> str:
    """Decode NEW_SESSION payload to session name."""
    name_len = struct.unpack("!I", payload[:4])[0]
    name = payload[4 : 4 + name_len].decode("utf-8")
    return name


def encode_attach(session_id: int) -> Message:
    """Encode ATTACH message with session ID."""
    payload = struct.pack("!I", session_id)
    return Message(MessageType.ATTACH, payload)


def decode_attach(payload: bytes) -> int:
    """Decode ATTACH payload to session ID."""
    session_id: int = struct.unpack("!I", payload)[0]
    return session_id


def encode_detach() -> Message:
    """Encode DETACH message (empty payload)."""
    return Message(MessageType.DETACH, b"")


def encode_list_sessions() -> Message:
    """Encode LIST_SESSIONS message (empty payload)."""
    return Message(MessageType.LIST_SESSIONS, b"")


def encode_resize(width: int, height: int) -> Message:
    """Encode RESIZE message with dimensions."""
    payload = struct.pack("!HH", width, height)
    return Message(MessageType.RESIZE, payload)


def decode_resize(payload: bytes) -> tuple[int, int]:
    """Decode RESIZE payload to (width, height)."""
    width, height = struct.unpack("!HH", payload)
    return (width, height)


def encode_input(data: bytes) -> Message:
    """Encode INPUT message with raw bytes."""
    return Message(MessageType.INPUT, data)


def decode_input(payload: bytes) -> bytes:
    """Decode INPUT payload to raw bytes."""
    return payload


def encode_output(data: bytes) -> Message:
    """Encode OUTPUT message with raw bytes."""
    return Message(MessageType.OUTPUT, data)


def decode_output(payload: bytes) -> bytes:
    """Decode OUTPUT payload to raw bytes."""
    return payload


def encode_error(message: str) -> Message:
    """Encode ERROR message with error string."""
    msg_bytes = message.encode("utf-8")
    payload = struct.pack("!I", len(msg_bytes)) + msg_bytes
    return Message(MessageType.ERROR, payload)


def decode_error(payload: bytes) -> str:
    """Decode ERROR payload to error message string."""
    msg_len = struct.unpack("!I", payload[:4])[0]
    message = payload[4 : 4 + msg_len].decode("utf-8")
    return message


def encode_session_info(
    session_id: int,
    name: str,
    pane_id: int,
    pid: int,
    width: int,
    height: int,
    created_at: float,
    attached_count: int,
) -> Message:
    """Encode SESSION_INFO message with session details."""
    name_bytes = name.encode("utf-8")
    payload = (
        struct.pack("!I", session_id)
        + struct.pack("!I", len(name_bytes))
        + name_bytes
        + struct.pack("!I", pane_id)
        + struct.pack("!I", pid)
        + struct.pack("!HH", width, height)
        + struct.pack("!d", created_at)
        + struct.pack("!I", attached_count)
    )
    return Message(MessageType.SESSION_INFO, payload)


def decode_session_info(payload: bytes) -> tuple[int, str, int, int, int, int, float, int]:
    """Decode SESSION_INFO payload to (session_id, name, pane_id, pid, width, height, created_at, attached_count)."""
    offset = 0

    session_id = struct.unpack("!I", payload[offset : offset + 4])[0]
    offset += 4

    name_len = struct.unpack("!I", payload[offset : offset + 4])[0]
    offset += 4
    name = payload[offset : offset + name_len].decode("utf-8")
    offset += name_len

    pane_id = struct.unpack("!I", payload[offset : offset + 4])[0]
    offset += 4

    pid = struct.unpack("!I", payload[offset : offset + 4])[0]
    offset += 4

    width, height = struct.unpack("!HH", payload[offset : offset + 4])
    offset += 4

    created_at = struct.unpack("!d", payload[offset : offset + 8])[0]
    offset += 8

    attached_count = struct.unpack("!I", payload[offset : offset + 4])[0]

    return (session_id, name, pane_id, pid, width, height, created_at, attached_count)


def encode_shell_exited(session_id: int, pane_id: int) -> Message:
    """Encode SHELL_EXITED message with session and pane IDs."""
    payload = struct.pack("!II", session_id, pane_id)
    return Message(MessageType.SHELL_EXITED, payload)


def decode_shell_exited(payload: bytes) -> tuple[int, int]:
    """Decode SHELL_EXITED payload to (session_id, pane_id)."""
    session_id, pane_id = struct.unpack("!II", payload)
    return (session_id, pane_id)


def encode_kill_session(session_id: int) -> Message:
    """Encode KILL_SESSION message with session ID."""
    payload = struct.pack("!I", session_id)
    return Message(MessageType.KILL_SESSION, payload)


def decode_kill_session(payload: bytes) -> int:
    """Decode KILL_SESSION payload to session ID."""
    session_id: int = struct.unpack("!I", payload)[0]
    return session_id
