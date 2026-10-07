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

import contextlib
import json
import logging
import os
import signal
import sys
import threading

try:
    import termios
    import tty
except ImportError:
    # Windows has neither termios nor tty. connect_console() below degrades
    # gracefully to line-buffered input when these are unavailable.
    termios = None  # type: ignore
    tty = None  # type: ignore
from typing import Optional
from urllib.parse import urlparse

import websocket

from colab_cli.state import SessionState
from colab_cli.terminal import (
    TerminalQueryFilter,
    enable_windows_virtual_terminal,
    is_windows,
    read_windows_key,
    windows_raw_console,
)

logger = logging.getLogger(__name__)

# Global flag to stop the read thread when the websocket closes
_is_running = False
_last_error = None
# Set when stdin is piped: drops terminal queries from the remote output,
# because the local terminal's answers could never reach the remote side.
_output_filter: Optional[TerminalQueryFilter] = None

# When stdin is piped and reaches EOF, we send "exit\n" to the remote shell.
# Once bash exits, tmux exits and the backend closes the websocket itself, so
# we wait for that close rather than for a fixed time. A fixed 0.5s grace cut
# off the output on a freshly started runtime: the first attach often needs
# 0.5-1s before the shell answers, and the client closed the socket before
# anything came back (2 of 3 fresh-VM runs). This is only the fallback for a
# backend that never closes; as with `echo cmd | ssh host`, a piped command
# that runs longer than this is cut off.
PIPED_EOF_CLOSE_TIMEOUT_SECONDS = 30.0
# Set by on_close, so the stdin thread can wait for the server-side close.
_closed = threading.Event()


def on_message(ws, message):
    """Callback for when a message is received from the server."""
    try:
        data = json.loads(message)
        if "data" in data:
            # The backend sends raw ANSI escape sequences and string content.
            # We write it directly to stdout buffer to avoid python print() formatting.
            text = data["data"]
            if _output_filter is not None:
                text = _output_filter.feed(text)
            sys.stdout.buffer.write(text.encode("utf-8"))
            sys.stdout.buffer.flush()
    except Exception as e:
        logger.debug(f"Error parsing message: {e}")


def on_error(ws, error):
    """Callback for when a websocket error occurs."""
    global _last_error
    _last_error = error
    logger.error(f"WebSocket Error: {error}")


def on_close(ws, close_status_code, close_msg):
    """Callback for when the websocket is closed."""
    global _is_running
    _is_running = False
    _closed.set()


def send_terminal_size(ws):
    """Sends the current terminal size to the remote backend."""
    try:
        size = os.get_terminal_size()
        payload = json.dumps({"cols": size.columns, "rows": size.lines})
        ws.send(payload)
    except Exception as e:
        logger.debug(f"Failed to send terminal size: {e}")


def on_open(ws):
    """Callback for when the websocket connection is opened."""
    global _is_running
    _is_running = True

    # Send initial terminal size
    send_terminal_size(ws)

    # Setup the background thread to read from stdin
    def read_stdin():
        is_tty = sys.stdin.isatty()
        # Windows consoles are line-buffered unless we switched to raw mode
        # in connect_console(). Read keystrokes through the console API and
        # translate scan codes to ANSI so cmd.exe and PowerShell behave like
        # a POSIX raw tty. Piped stdin stays on sys.stdin.read so EOF still
        # works.
        windows_tty = is_windows() and is_tty
        while _is_running:
            try:
                if windows_tty:
                    text = read_windows_key()
                    if not text:
                        continue
                    chunks = list(text)
                else:
                    # Read a single character (or escape sequence byte)
                    char = sys.stdin.read(1)
                    if not char:
                        if not is_tty:
                            # Piped input has reached EOF. The remote /colab/tty
                            # endpoint wraps bash in tmux which intercepts \x04
                            # (Ctrl-D) as a literal character, so it never exits.
                            # Instead send "exit\n" so bash voluntarily terminates,
                            # wait for the backend to close the websocket once the
                            # shell is gone (all output has arrived by then), and
                            # only close it ourselves if that never happens.
                            try:
                                ws.send(json.dumps({"data": "exit\n"}))
                            except Exception:
                                pass
                            if not _closed.wait(PIPED_EOF_CLOSE_TIMEOUT_SECONDS):
                                try:
                                    ws.close()
                                except Exception:
                                    pass
                        break
                    chunks = [char]
                for chunk in chunks:
                    ws.send(json.dumps({"data": chunk}))
            except Exception:
                break

    thread = threading.Thread(target=read_stdin, daemon=True)
    thread.start()


def connect_console(session: SessionState):
    """
    Connects to the Colab TTY endpoint and sets up a raw terminal session.
    """
    global _is_running, _last_error, _output_filter
    _last_error = None
    _closed.clear()
    # cmd.exe and Windows PowerShell 5.1 do not enable ANSI processing by
    # default. The remote PTY speaks ANSI, and we write those bytes straight
    # to stdout.buffer, so the console mode has to be switched first.
    enable_windows_virtual_terminal()

    # Construct the WebSocket URL from the base URL
    parsed = urlparse(session.url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{parsed.netloc}/colab/tty?colab-runtime-proxy-token={session.token}"

    is_tty = sys.stdin.isatty()
    _output_filter = None if is_tty else TerminalQueryFilter()
    fd = None
    old_settings = None
    can_raw = termios is not None and tty is not None and is_tty
    # Windows has no termios. Raw mode is the console API instead, so
    # interactive `colab console` is not stuck in cooked line editing.
    use_windows_raw = is_windows() and is_tty and not can_raw
    if can_raw:
        try:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
        except Exception:
            fd = None
            old_settings = None
            can_raw = False

    ws = websocket.WebSocketApp(
        url=ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    def handle_sigwinch(signum, frame):
        """Handle window resize events."""
        if _is_running:
            send_terminal_size(ws)

    sigwinch = getattr(signal, "SIGWINCH", None)
    resize_stop = threading.Event()

    def _watch_resize():
        """Poll the console size. Windows has no SIGWINCH."""
        last = None
        while not resize_stop.is_set():
            try:
                size = os.get_terminal_size()
                current = (size.columns, size.lines)
                if current != last:
                    last = current
                    send_terminal_size(ws)
            except Exception:
                pass
            resize_stop.wait(0.4)

    raw_cm = windows_raw_console() if use_windows_raw else contextlib.nullcontext()
    try:
        with raw_cm:
            if use_windows_raw:
                threading.Thread(target=_watch_resize, daemon=True).start()
            if can_raw:
                tty.setraw(fd, termios.TCSANOW)
                if sigwinch is not None:
                    signal.signal(sigwinch, handle_sigwinch)

            # This is a blocking call until the connection is closed
            ws.run_forever()

            if _output_filter is not None:
                tail = _output_filter.flush()
                if tail:
                    sys.stdout.buffer.write(tail.encode("utf-8"))
                    sys.stdout.buffer.flush()

            if _last_error:
                # Re-raise or wrap terminal errors
                err_msg = str(_last_error)
                if "404" in err_msg or "401" in err_msg:
                    # We raise a standard exception that the caller can recognize
                    raise RuntimeError(f"Connection failed: {err_msg}")
    finally:
        resize_stop.set()
        if can_raw:
            # Always ensure the terminal is restored to its original state
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old_settings)
            except Exception:
                pass
            # Restore the default signal handler for resize
            if sigwinch is not None:
                try:
                    signal.signal(sigwinch, signal.SIG_DFL)
                except Exception:
                    pass
        print("\r\nConnection closed.")
