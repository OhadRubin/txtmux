import asyncio
import os
import signal
import time

import pytest

from txtmux.pty_handler import spawn_shell, set_pty_size, read_pty, write_pty, close_pty


class TestSpawnShell:
    """Tests for spawn_shell function."""

    def test_spawn_shell_returns_valid_fd_and_pid(self):
        """spawn_shell() returns (master_fd, child_pid) with valid fd and pid."""
        master_fd, pid = spawn_shell("/bin/sh")
        try:
            assert master_fd > 0, "master_fd should be positive"
            assert pid > 0, "pid should be positive"
        finally:
            close_pty(master_fd)
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)

    def test_child_output_readable_from_master(self):
        """Child process output is readable from master_fd."""
        master_fd, pid = spawn_shell("/bin/sh")
        try:
            write_pty(master_fd, b"echo hello\n")
            time.sleep(0.1)
            output = os.read(master_fd, 1024)
            assert b"hello" in output, f"Expected 'hello' in output, got: {output}"
        finally:
            close_pty(master_fd)
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)

    def test_write_to_master_readable_by_child(self):
        """Writing to master_fd is readable by child process (via cat)."""
        master_fd, pid = spawn_shell("/bin/sh")
        try:
            write_pty(master_fd, b"cat\n")
            time.sleep(0.05)
            write_pty(master_fd, b"test input\n")
            time.sleep(0.1)
            output = os.read(master_fd, 1024)
            assert b"test input" in output, f"Expected 'test input' in output, got: {output}"
        finally:
            write_pty(master_fd, b"\x04")
            close_pty(master_fd)
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)


class TestSetPtySize:
    """Tests for set_pty_size function."""

    def test_set_pty_size_succeeds(self):
        """set_pty_size(fd, 80, 24) succeeds without error."""
        master_fd, pid = spawn_shell("/bin/sh")
        try:
            set_pty_size(master_fd, 80, 24)
        finally:
            close_pty(master_fd)
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)

    def test_set_pty_size_various_dimensions(self):
        """set_pty_size works with various dimensions."""
        master_fd, pid = spawn_shell("/bin/sh")
        try:
            set_pty_size(master_fd, 120, 40)
            set_pty_size(master_fd, 40, 10)
        finally:
            close_pty(master_fd)
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)


class TestReadPty:
    """Tests for async read_pty function."""

    @pytest.mark.asyncio
    async def test_read_pty_returns_child_output(self):
        """read_pty returns output from child process."""
        master_fd, pid = spawn_shell("/bin/sh")
        try:
            write_pty(master_fd, b"echo async_test\n")
            await asyncio.sleep(0.1)
            output = await read_pty(master_fd, 1024)
            assert b"async_test" in output, f"Expected 'async_test' in output, got: {output}"
        finally:
            close_pty(master_fd)
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)


class TestChildExit:
    """Tests for child process exit detection."""

    def test_child_exit_detectable(self):
        """Child shell exit is detectable via os.waitpid."""
        master_fd, pid = spawn_shell("/bin/sh")
        write_pty(master_fd, b"exit\n")
        time.sleep(0.1)
        close_pty(master_fd)
        waited_pid, status = os.waitpid(pid, 0)
        assert waited_pid == pid, f"Expected pid {pid}, got {waited_pid}"
        assert os.WIFEXITED(status), "Expected child to exit normally"
