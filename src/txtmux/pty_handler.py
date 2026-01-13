import os
import pty
import fcntl
import struct
import termios
import asyncio


def spawn_shell(shell: str) -> tuple[int, int]:
    """
    Fork and exec shell with PTY as controlling terminal.
    Returns (master_fd, child_pid).
    """
    master_fd, slave_fd = pty.openpty()
    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.environ["TERM"] = "xterm-256color"
        os.execvp(shell, [shell])
        raise RuntimeError("execvp failed")
    os.close(slave_fd)
    return (master_fd, pid)


def set_pty_size(fd: int, width: int, height: int) -> None:
    """Set PTY dimensions using TIOCSWINSZ ioctl."""
    winsize = struct.pack("HHHH", height, width, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


async def read_pty(fd: int, size: int) -> bytes:
    """Async wrapper around os.read using run_in_executor."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, os.read, fd, size)
    except OSError as e:
        raise OSError(f"Failed to read from PTY fd {fd}: {e}") from e


def write_pty(fd: int, data: bytes) -> int:
    """Write data to PTY master fd. Returns bytes written."""
    try:
        return os.write(fd, data)
    except OSError as e:
        raise OSError(f"Failed to write to PTY fd {fd}: {e}") from e


def close_pty(fd: int) -> None:
    """Close PTY file descriptor."""
    try:
        os.close(fd)
    except OSError as e:
        raise OSError(f"Failed to close PTY fd {fd}: {e}") from e
