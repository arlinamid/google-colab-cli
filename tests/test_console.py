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

import json
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

try:
    import termios
except ImportError:
    # Windows has no termios; tests requiring raw-terminal mode are skipped.
    termios = None  # type: ignore

from colab_cli.console import connect_console, on_message, on_open
from colab_cli.state import SessionState
import pytest

needs_termios = pytest.mark.skipif(
    termios is None, reason="requires termios (POSIX only)"
)


@pytest.fixture
def mock_session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev",
        endpoint="some-endpoint",
    )


@needs_termios
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.tty.setraw")
@patch("colab_cli.console.termios.tcgetattr")
@patch("colab_cli.console.termios.tcsetattr")
@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.fileno")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_initialization(
    mock_isatty,
    mock_fileno,
    mock_get_term_size,
    mock_tcsetattr,
    mock_tcgetattr,
    mock_setraw,
    mock_ws_app,
    mock_session,
):
    # Setup mocks
    mock_isatty.return_value = True
    mock_fileno.return_value = 0
    mock_get_term_size.return_value = os.terminal_size((80, 24))
    mock_tcgetattr.return_value = ["fake_attrs"]
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance

    # We don't want run_forever to actually block or start threads in the test
    mock_ws_instance.run_forever.return_value = None

    with patch("colab_cli.console.threading.Thread"):
        connect_console(mock_session)

    # 1. Verify URL transformation
    expected_url = "wss://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev/colab/tty?colab-runtime-proxy-token=test-token"
    mock_ws_app.assert_called_once()
    assert mock_ws_app.call_args[1]["url"] == expected_url

    # 2. Verify raw mode setup and teardown
    mock_tcgetattr.assert_called_once_with(sys.stdin.fileno())
    mock_setraw.assert_called_once_with(sys.stdin.fileno(), termios.TCSANOW)

    # Teardown should happen in a finally block
    mock_tcsetattr.assert_called_once_with(
        sys.stdin.fileno(), termios.TCSANOW, ["fake_attrs"]
    )


@needs_termios
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.tty.setraw")
@patch("colab_cli.console.termios.tcgetattr")
@patch("colab_cli.console.termios.tcsetattr")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_piped_input(
    mock_isatty,
    mock_tcsetattr,
    mock_tcgetattr,
    mock_setraw,
    mock_ws_app,
    mock_session,
):
    mock_isatty.return_value = False
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    with patch("colab_cli.console.threading.Thread"):
        connect_console(mock_session)

    # In a piped environment, we should not attempt to use termios or tty
    mock_tcgetattr.assert_not_called()
    mock_setraw.assert_not_called()
    mock_tcsetattr.assert_not_called()


@patch("colab_cli.console.os.get_terminal_size")
def test_on_open_sends_terminal_size(mock_get_term_size):
    mock_ws = MagicMock()
    mock_get_term_size.return_value = os.terminal_size((100, 40))

    on_open(mock_ws)

    # Verify that the initial terminal size is sent
    mock_ws.send.assert_called_once()
    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload == {"cols": 100, "rows": 40}


@patch("colab_cli.console.sys.stdout.buffer.write")
@patch("colab_cli.console.sys.stdout.buffer.flush")
def test_on_message_writes_to_stdout(mock_flush, mock_write):
    mock_ws = MagicMock()
    test_data = "Hello \x1b[34mWorld\x1b[0m"
    message_json = json.dumps({"data": test_data})

    on_message(mock_ws, message_json)

    # Verify that the data is written exactly as received
    mock_write.assert_called_once_with(test_data.encode("utf-8"))
    mock_flush.assert_called_once()


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.sys.stdin")
def test_read_stdin_eof_piped_sends_exit_and_closes_ws(
    mock_stdin, mock_isatty, mock_get_term_size
):
    """When stdin is piped and reaches EOF, the read thread should send 'exit\\n'
    to the remote shell and, if the backend never closes the websocket, close
    it from the client side after the timeout.

    The remote shell at /colab/tty is wrapped in tmux which swallows the bare
    \\x04 (Ctrl-D) we used to send, so EOF used to leave the websocket open
    indefinitely. Sending 'exit\\n' + the fallback ws.close() guarantees clean
    termination.
    """
    import colab_cli.console as console_mod

    console_mod._closed.clear()
    mock_isatty.return_value = False
    # Simulate piped stdin: returns one line then EOF
    mock_stdin.read.side_effect = ["e", "c", "h", "o", " ", "h", "i", "\n", ""]
    mock_get_term_size.return_value = os.terminal_size((80, 24))

    mock_ws = MagicMock()

    # on_open spawns the read thread; we want it to run synchronously here
    # so we patch threading.Thread to call target immediately and join().
    real_thread = []

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target
            real_thread.append(self)

        def start(self):
            self.target()

    console_mod._is_running = True
    with patch("colab_cli.console.threading.Thread", SyncThread):
        # The mock backend never closes, so use a tiny fallback timeout.
        with patch("colab_cli.console.PIPED_EOF_CLOSE_TIMEOUT_SECONDS", 0.01):
            on_open(mock_ws)

    # Collect what was sent to the websocket
    sent_payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]

    # Initial send is the terminal size; everything after is stdin chars or our exit string.
    # Verify "exit\n" was sent on EOF (one send per character)
    assert {"data": "exit\n"} in sent_payloads, (
        f"Expected 'exit\\n' to be sent on piped EOF, got: {sent_payloads}"
    )

    # Verify we closed the websocket from the client side
    mock_ws.close.assert_called_once()


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.sys.stdin")
def test_read_stdin_eof_piped_waits_for_server_close(
    mock_stdin, mock_isatty, mock_get_term_size
):
    """After 'exit\\n' the client waits for the backend to close the socket
    (which it does once tmux exits, after the last output) instead of closing
    it after a fixed delay. On a freshly started runtime the shell can take
    more than the old 0.5s to answer, and the early close dropped its output.
    """
    import colab_cli.console as console_mod

    console_mod._closed.clear()
    mock_isatty.return_value = False
    mock_stdin.read.side_effect = ["l", "s", "\n", ""]
    mock_get_term_size.return_value = os.terminal_size((80, 24))
    mock_ws = MagicMock()
    # Created before threading.Thread is patched below: Timer.__init__ looks
    # Thread up by name and would get the synchronous stand-in.
    server_close = threading.Timer(
        0.05, console_mod.on_close, args=(mock_ws, 1000, "")
    )

    def backend(payload):
        # The backend answers 'exit' by closing the socket a moment later.
        if json.loads(payload).get("data") == "exit\n":
            server_close.start()

    mock_ws.send.side_effect = backend

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    console_mod._is_running = True
    # A fixed 0.01s grace would have closed before the backend did; the wait
    # must outlast that and end on the server-side close, well before 5s.
    with patch("colab_cli.console.threading.Thread", SyncThread), patch(
        "colab_cli.console.PIPED_EOF_CLOSE_TIMEOUT_SECONDS", 5.0
    ):
        started = time.monotonic()
        on_open(mock_ws)
        elapsed = time.monotonic() - started

    assert console_mod._closed.is_set()
    mock_ws.close.assert_not_called()
    assert elapsed < 4.0


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.sys.stdin")
def test_read_stdin_eof_tty_does_not_close_ws(
    mock_stdin, mock_isatty, mock_get_term_size
):
    """When stdin is a real TTY and read() returns empty (which happens on
    Ctrl-D in raw mode), we should NOT inject 'exit\\n' or close the websocket
    \u2014 the user is in interactive mode and may have intended Ctrl-D as a literal
    char. The websocket lifecycle is owned by the remote shell in this case.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    # TTY EOF is rare but possible; should be passed through transparently
    mock_stdin.read.side_effect = [""]
    mock_get_term_size.return_value = os.terminal_size((80, 24))

    mock_ws = MagicMock()

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    console_mod._is_running = True
    # This is the POSIX raw-tty path. On Windows a TTY is read through
    # msvcrt.getwch(), which would block this test waiting for a keypress.
    with patch("colab_cli.console.threading.Thread", SyncThread), patch(
        "colab_cli.console.is_windows", return_value=False
    ):
        on_open(mock_ws)

    sent_payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
    assert {"data": "exit\n"} not in sent_payloads
    mock_ws.close.assert_not_called()


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
def test_read_stdin_windows_tty_sends_translated_keys(
    mock_isatty, mock_get_term_size
):
    """On a Windows console, keys come from read_windows_key() (already
    translated to ANSI) and are forwarded one character per message."""
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    mock_get_term_size.return_value = os.terminal_size((80, 24))
    mock_ws = MagicMock()
    keys = iter(["a", "\x1b[A"])

    def fake_read_key():
        try:
            return next(keys)
        except StopIteration:
            console_mod._is_running = False
            return ""

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    console_mod._is_running = True
    with patch("colab_cli.console.threading.Thread", SyncThread), patch(
        "colab_cli.console.is_windows", return_value=True
    ), patch("colab_cli.console.read_windows_key", side_effect=fake_read_key):
        on_open(mock_ws)

    sent = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
    data = [p["data"] for p in sent if "data" in p]
    assert data == ["a", "\x1b", "[", "A"]
    mock_ws.close.assert_not_called()


@pytest.mark.parametrize(
    "stdin_is_tty, expected",
    [
        # Piped: the terminal's answers can never reach the remote side, so
        # the queries are dropped (and a cut-off tail is flushed at the end).
        (False, ["x", "y", "\x1b["]),
        # Interactive: answers flow back through stdin, so pass queries on.
        (True, ["x\x1b[>c", "y\x1b["]),
    ],
    ids=["piped", "tty"],
)
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_drops_terminal_queries_only_when_piped(
    mock_isatty, mock_ws_app, mock_session, stdin_is_tty, expected
):
    import colab_cli.console as console_mod

    mock_isatty.return_value = stdin_is_tty
    ws = MagicMock()
    mock_ws_app.return_value = ws

    def remote_output():
        console_mod.on_message(ws, json.dumps({"data": "x\x1b[>c"}))
        console_mod.on_message(ws, json.dumps({"data": "y\x1b["}))

    ws.run_forever.side_effect = remote_output
    written = []

    with (
        patch.object(console_mod, "termios", None),
        patch.object(console_mod, "tty", None),
        patch.object(console_mod, "is_windows", return_value=False),
        patch("colab_cli.console.threading.Thread"),
        patch(
            "colab_cli.console.sys.stdout.buffer.write",
            side_effect=lambda b: written.append(b.decode("utf-8")),
        ),
        patch("colab_cli.console.sys.stdout.buffer.flush"),
    ):
        connect_console(mock_session)

    # connect_console prints its own "Connection closed." afterwards.
    remote = [w for w in written if w][: len(expected)]
    assert remote == expected


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_no_termios_degrades_gracefully(mock_isatty, mock_ws_app, mock_session):
    """Windows has no termios/tty/SIGWINCH. connect_console must not crash.

    Regression test for `ModuleNotFoundError: No module named 'termios'`
    which broke every `colab` command on Windows via
    cli -> execution -> console imports.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    with (
        patch.object(console_mod, "termios", None),
        patch.object(console_mod, "tty", None),
        patch("colab_cli.console.threading.Thread"),
    ):
        connect_console(mock_session)

    mock_ws_instance.run_forever.assert_called_once()


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_windows_uses_raw_console_not_termios(
    mock_isatty, mock_ws_app, mock_session
):
    """cmd.exe and PowerShell have no termios. Interactive console must switch
    the Windows console into raw mode instead of staying line-buffered.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None
    raw_cm = MagicMock()

    with (
        patch.object(console_mod, "termios", None),
        patch.object(console_mod, "tty", None),
        patch.object(console_mod, "is_windows", return_value=True),
        patch.object(
            console_mod, "windows_raw_console", return_value=raw_cm
        ) as mock_raw,
        patch("colab_cli.console.threading.Thread"),
    ):
        connect_console(mock_session)

    mock_raw.assert_called_once()
    raw_cm.__enter__.assert_called_once()
    raw_cm.__exit__.assert_called_once()
    mock_ws_instance.run_forever.assert_called_once()


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
def test_read_stdin_windows_tty_forwards_translated_keys(
    mock_isatty, mock_get_term_size
):
    """Arrow keys arrive as Windows scan codes. The remote shell needs the
    ANSI sequence, one character per websocket frame, matching POSIX raw mode.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    mock_get_term_size.return_value = os.terminal_size((80, 24))
    keys = ["\x1b[A", "\r"]

    def fake_key():
        if not keys:
            console_mod._is_running = False
            return ""
        return keys.pop(0)

    mock_ws = MagicMock()

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    console_mod._is_running = True
    with (
        patch.object(console_mod, "is_windows", return_value=True),
        patch.object(console_mod, "read_windows_key", side_effect=fake_key),
        patch("colab_cli.console.threading.Thread", SyncThread),
    ):
        on_open(mock_ws)

    sent = [json.loads(call.args[0]) for call in mock_ws.send.call_args_list]
    assert sent[0] == {"cols": 80, "rows": 24}
    assert [item["data"] for item in sent[1:]] == ["\x1b", "[", "A", "\r"]
