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

POSIX keeps using ``termios`` raw mode in ``console.py``. Windows consoles
(cmd.exe and PowerShell, both conhost and Windows Terminal) have no
``termios``, no ``/dev/tty``, and no ``SIGWINCH``. This module owns the
Windows console-mode and key-translation pieces, plus the controlling-terminal
line read used by interactive prompts.
"""

import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from typing import Iterator, Optional

# Win32 console mode flags. See
# https://learn.microsoft.com/en-us/windows/console/setconsolemode
_ENABLE_PROCESSED_OUTPUT = 0x0001
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
_ENABLE_EXTENDED_FLAGS = 0x0080

_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12

# Second byte of a Windows console special key (after \x00 or \xe0) mapped to
# the ANSI sequence a POSIX raw tty would emit for the same key.
_SCAN_TO_ANSI = {
    "H": "\x1b[A",  # up
    "P": "\x1b[B",  # down
    "M": "\x1b[C",  # right
    "K": "\x1b[D",  # left
    "G": "\x1b[H",  # home
    "O": "\x1b[F",  # end
    "I": "\x1b[5~",  # page up
    "Q": "\x1b[6~",  # page down
    "S": "\x1b[3~",  # delete
    "R": "\x1b[2~",  # insert
}


def is_windows() -> bool:
    """True on Windows, including cmd.exe and PowerShell."""
    return os.name == "nt"


def translate_windows_key(first: str, second: Optional[str] = None) -> str:
    """Map one Windows console key event to bytes a POSIX raw tty would send.

    ``msvcrt.getwch`` returns a Unicode character. Special keys arrive as
    ``\\x00`` or ``\\xe0`` followed by a scan-code character. Arrow keys must
    become ANSI sequences so the remote shell's line editor understands them.
    Enter arrives as ``\\r``, which is also what a POSIX raw terminal sends
    (local ICRNL is off; the remote pty's ICRNL turns ``\\r`` into ``\\n``).
    Unrecognized scan codes return an empty string so they are not forwarded
    as garbage.
    """
    if first in ("\x00", "\xe0"):
        if not second:
            return ""
        return _SCAN_TO_ANSI.get(second, "")
    return first


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


@contextmanager
def windows_raw_console() -> Iterator[None]:
    """Put the Windows console input handle into raw (non-line) mode.

    Clears line-input, echo, and processed-input so keystrokes — including
    Ctrl-C as ``\\x03`` — reach this process instead of being edited by
    conhost or killing the local CLI. Enables virtual-terminal input so newer
    consoles can emit ANSI sequences themselves. Restores the previous mode
    on exit. No-op when this is not a Windows console (pipes, POSIX).
    """
    if not is_windows():
        yield
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
    old = ctypes.c_uint()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(old)):
        yield
        return
    # Leaving LINE/ECHO/PROCESSED unset is the raw mode. EXTENDED_FLAGS has
    # to be set for the mode change to stick on modern conhost.
    raw = _ENABLE_VIRTUAL_TERMINAL_INPUT | _ENABLE_EXTENDED_FLAGS
    kernel32.SetConsoleMode(handle, raw)
    try:
        yield
    finally:
        kernel32.SetConsoleMode(handle, old.value)


def read_windows_key() -> str:
    """Block for one Windows console key and return an ANSI-compatible string.

    Returns ``""`` for an unrecognized scan code. The caller should not
    forward that.
    """
    import msvcrt

    first = msvcrt.getwch()
    if first in ("\x00", "\xe0"):
        second = msvcrt.getwch()
        return translate_windows_key(first, second)
    return translate_windows_key(first)


def read_controlling_line(prompt: str = "") -> str:
    """Read one line from the controlling terminal, not from a redirected pipe.

    POSIX opens ``/dev/tty``. Windows opens ``CONIN$``, which is the console
    even when stdin is redirected (the same reason the POSIX path avoids
    stdin). Falls back to ``sys.stdin`` and then ``input()`` when neither
    device exists, which is the case under CI and some IDE consoles.
    """
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
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
    Windows OpenSSH runs it via ``cmd.exe`` (``subprocess.list2cmdline``).
    cmd.exe does not treat single quotes as quoting, so a POSIX-quoted path
    such as ``'C:\\Program Files\\Python\\python.exe'`` is passed through
    literally and the bridge never starts.
    """
    if windows is None:
        windows = is_windows()
    if windows:
        return subprocess.list2cmdline(argv)
    import shlex

    return shlex.join(argv)
