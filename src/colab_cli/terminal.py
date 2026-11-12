# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cross-platform terminal helpers.

Windows consoles (cmd.exe and PowerShell, both conhost and Windows Terminal)
have no ``/dev/tty`` and leave ANSI processing off. This module owns the
Windows console-mode and command-line pieces, plus the controlling-terminal
line read used by interactive prompts.
"""

import os
import shutil
import subprocess
import sys
from typing import Optional

# Win32 console mode flags. See
# https://learn.microsoft.com/en-us/windows/console/setconsolemode
_ENABLE_PROCESSED_OUTPUT = 0x0001
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12


def is_windows() -> bool:
    """True on Windows, including cmd.exe and PowerShell."""
    return os.name == "nt"


def enable_windows_virtual_terminal() -> bool:
    """Enable ANSI processing and UTF-8 on the Windows console.

    cmd.exe and Windows PowerShell 5.1 leave
    ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` off, so cursor movement and colors
    written as raw bytes show up as garbage. Windows Terminal and PowerShell 7
    often already have the bit set; setting it again is harmless. No-op off
    Windows. Returns True when an output mode was updated.
    """
    if not is_windows():
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
        updated = False
        for handle_id in (_STD_OUTPUT_HANDLE, _STD_ERROR_HANDLE):
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            new_mode = (
                mode.value
                | _ENABLE_PROCESSED_OUTPUT
                | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
            if kernel32.SetConsoleMode(handle, new_mode):
                updated = True
        return updated
    except Exception:
        return False


PROMPT_FROM_STDIN_ENV = "COLAB_CLI_PROMPT_FROM_STDIN"


def read_controlling_line(prompt: str = "") -> str:
    """Read one line from the controlling terminal, not from a redirected pipe.

    POSIX opens ``/dev/tty``. Windows opens ``CONIN$``, which is the console
    even when stdin is redirected (the same reason the POSIX path avoids
    stdin). Falls back to ``sys.stdin`` and then ``input()`` when neither
    device exists, which is the case under CI and some IDE consoles.

    A program that drives the CLI as a subprocess (a TUI that owns the
    terminal, for example) can set ``COLAB_CLI_PROMPT_FROM_STDIN=1`` to have
    the answer read from stdin instead of the controlling terminal.
    """
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    if os.environ.get(PROMPT_FROM_STDIN_ENV):
        return sys.stdin.readline()
    if is_windows():
        try:
            with open("CONIN$", "r", encoding="utf-8", errors="replace") as con:
                return con.readline()
        except OSError:
            pass
    else:
        try:
            with open("/dev/tty", encoding="utf-8", errors="replace") as tty:
                return tty.readline()
        except OSError:
            pass
    try:
        return sys.stdin.readline()
    except Exception:
        try:
            return input() + "\n"
        except Exception:
            return ""


def open_url_in_browser(url: str) -> bool:
    """Open ``url`` in the system GUI browser.

    Windows uses ``os.startfile``, the same association cmd and PowerShell use
    for ``start``. macOS uses ``open``. Other platforms use ``xdg-open`` when
    it is on ``PATH``. A missing opener returns False so the caller can keep
    the printed URL. This never launches a TUI browser (lynx, w3m), which
    would steal the terminal the CLI is prompting on.
    """
    if is_windows():
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            return False
        try:
            startfile(url)
            return True
        except OSError:
            return False
    opener_name = "open" if sys.platform == "darwin" else "xdg-open"
    opener = shutil.which(opener_name)
    if not opener:
        return False
    try:
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def join_argv(argv: list[str], *, windows: Optional[bool] = None) -> str:
    """Quote an argv for the shell that will re-parse it.

    POSIX OpenSSH runs ``ProxyCommand`` via ``/bin/sh`` (``shlex.join``).
    OpenSSH for Windows passes it to ``CreateProcess`` and the child splits
    it with the MSVCRT rules (``subprocess.list2cmdline``; checked against
    OpenSSH_for_Windows_9.5p2, where ``a&b`` arrives as one literal argument).
    Those rules do not treat single quotes as quoting, so a POSIX-quoted path
    such as ``'C:\\Program Files\\Python\\python.exe'`` is passed through
    literally and the bridge never starts.
    """
    if windows is None:
        windows = is_windows()
    if windows:
        return subprocess.list2cmdline(argv)
    import shlex

    return shlex.join(argv)


def split_windows_command_line(cmd: str) -> list[str]:
    """Split ``cmd`` the way a Windows program splits its own command line.

    ``shlex.split`` treats backslashes as escapes, so an unquoted
    ``C:\\tools\\edit.exe`` would lose its path separators. This uses
    ``CommandLineToArgvW``, the inverse of ``subprocess.list2cmdline``.
    """
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    ]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    argc = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(cmd, ctypes.byref(argc))
    if not argv:
        raise OSError(ctypes.get_last_error(), f"Cannot parse command line: {cmd!r}")
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)
