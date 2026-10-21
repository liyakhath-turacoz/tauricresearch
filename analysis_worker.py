"""Run the existing CLI, exiting if its owning dashboard process disappears."""

from __future__ import annotations

import ctypes
import os
import runpy
import sys
import threading
import time


def guard_parent(parent_pid: int):
    if parent_pid <= 0 or (os.name != "nt" and os.getppid() != parent_pid):
        raise RuntimeError("The owning dashboard process is no longer available")
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForMultipleObjects.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel.WaitForMultipleObjects.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # A held process handle follows the original process, never a reused PID.
        # Windows virtual environments use a redirector process. Watch both the
        # dashboard and our immediate parent: stopping that redirector must also
        # stop the actual interpreter performing the analysis.
        handles = []
        for pid in dict.fromkeys([parent_pid, os.getppid()]):
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                for opened in handles:
                    kernel.CloseHandle(opened)
                raise RuntimeError("Cannot monitor the owning dashboard; no analysis was started")
            handles.append(handle)
        array = (wintypes.HANDLE * len(handles))(*handles)

        def watch():
            kernel.WaitForMultipleObjects(len(handles), array, False, 0xFFFFFFFF)
            for handle in handles:
                kernel.CloseHandle(handle)
            os._exit(143)
    else:

        def watch():
            while os.getppid() == parent_pid:
                time.sleep(0.5)
            os._exit(143)

    threading.Thread(target=watch, name="dashboard-parent-guard", daemon=True).start()


def main():
    if len(sys.argv) < 4 or sys.argv[2:4] != ["-m", "cli.main"]:
        raise SystemExit("Usage: analysis_worker.py PARENT_PID -m cli.main [CLI options]")
    try:
        parent_pid = int(sys.argv[1])
        guard_parent(parent_pid)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    sys.argv = ["cli.main", *sys.argv[4:]]
    runpy.run_module("cli.main", run_name="__main__")


if __name__ == "__main__":
    main()
