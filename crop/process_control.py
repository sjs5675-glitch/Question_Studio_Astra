"""Keep an inherited Windows job handle for each owned pipeline process tree."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import signal
import subprocess
import time
from pathlib import Path


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong),
        ("period_user", ctypes.c_longlong), ("period_kernel", ctypes.c_longlong),
        ("faults", wintypes.DWORD), ("total", wintypes.DWORD),
        ("active", wintypes.DWORD), ("terminated", wintypes.DWORD),
    ]


class OwnedProcess:
    """Launch suspended, establish kernel ownership, then permit descendants."""

    def __init__(self, command: list[str], cwd: Path) -> None:
        self._job = None
        self.process: subprocess.Popen[str]
        if os.name != "nt":
            self.process = subprocess.Popen(
                command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", start_new_session=True,
            )
            return
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(self._kernel, name)
            function.argtypes = args
            function.restype = result
        self._job = self._kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.process = subprocess.Popen(
                command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=0x00000004 | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
            )
        except OSError:
            self.close()
            raise
        try:
            if not self._kernel.AssignProcessToJobObject(self._job, int(self.process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            native = ctypes.WinDLL("ntdll")
            native.NtResumeProcess.argtypes = [wintypes.HANDLE]
            native.NtResumeProcess.restype = wintypes.LONG
            if native.NtResumeProcess(int(self.process._handle)) != 0:
                raise OSError("owned process could not resume")
        except OSError:
            # Still suspended: no user code or descendants can have escaped.
            self.process.kill()
            self.process.wait(timeout=5)
            self.close()
            raise

    def active(self) -> bool:
        """Include descendants even after the retained parent process exits."""
        if os.name != "nt":
            try:
                os.killpg(self.process.pid, 0)
                return True
            except ProcessLookupError:
                return False
        accounting = _Accounting()
        if not self._kernel.QueryInformationJobObject(
            self._job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.active != 0

    def cancel(self) -> None:
        """Request a graceful stop, then stop only this retained kernel tree."""
        if not self.active():
            return
        try:
            if os.name == "nt":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(self.process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            # Detached Windows pipelines have no console; still observe the grace period.
            pass
        deadline = time.monotonic() + 5
        while self.active() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.active():
            if os.name == "nt":
                if not self._kernel.TerminateJobObject(self._job, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                os.killpg(self.process.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while self.active() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.active():
            raise TimeoutError("owned process tree did not stop")
        self.process.wait(timeout=5)

    def close(self) -> None:
        if self._job:
            self._kernel.CloseHandle(self._job)
            self._job = None
