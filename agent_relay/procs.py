"""Small cross-platform process helpers (no third-party dependencies)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    if IS_WINDOWS:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def kill_tree(pid: int | None) -> None:
    if not pid or not alive(pid):
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=_NO_WINDOW)
        return
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except OSError:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass


def find_pid(marker: str, wait_s: float = 20.0) -> int | None:
    """PID of a process whose command line contains ``marker`` (for launchers
    like Windows Terminal that do not hand back the child's PID)."""
    deadline = time.time() + wait_s
    while True:
        for pid, cmd in _command_lines():
            if marker in cmd and pid != os.getpid():
                return pid
        if time.time() >= deadline:
            return None
        time.sleep(1.5)


def _command_lines() -> list[tuple[int, str]]:
    if IS_WINDOWS:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
        )
    else:
        proc = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True)
    out = []
    for line in (proc.stdout or "").splitlines():
        if IS_WINDOWS:
            pid, _, cmd = line.partition("\t")
        else:
            pid, _, cmd = line.strip().partition(" ")
        if pid.strip().isdigit() and "powershell" not in cmd.lower()[:60]:
            out.append((int(pid), cmd))
    return out


def user_idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input (Windows); very large elsewhere."""
    if not IS_WINDOWS:
        return 1e9
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0  # type: ignore[attr-defined]
