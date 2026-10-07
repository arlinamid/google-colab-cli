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

"""Cross-platform terminal helpers used by console, ssh, and drivemount.

These tests mock the platform so they run on Linux CI and still pin the
Windows PowerShell / cmd.exe behavior.
"""

import subprocess
import sys

import pytest

from colab_cli.terminal import (
    TerminalQueryFilter,
    enable_windows_virtual_terminal,
    join_argv,
    open_url_in_browser,
    read_controlling_line,
    split_windows_command_line,
    translate_windows_key,
    windows_raw_console,
)


# --- terminal query filter (piped `colab console`) ---------------------------

# The queries tmux on the runtime sent when a piped console attached.
TMUX_QUERIES = ["\x1b[c", "\x1b[>c", "\x1b[>q", "\x1b]10;?\x1b\\", "\x1b]11;?\x1b\\"]


@pytest.mark.parametrize(
    "query", TMUX_QUERIES + ["\x1b[0c", "\x1b[=c", "\x1b[6n", "\x1b]4;1;?\x07"]
)
def test_query_filter_drops_terminal_queries(query):
    f = TerminalQueryFilter()
    assert f.feed(f"before{query}after") == "beforeafter"
    assert f.flush() == ""


@pytest.mark.parametrize(
    "keep",
    [
        "\x1b[0m",
        "\x1b[1;31m",
        "\x1b[?1049h",
        "\x1b[2J\x1b[H",
        "\x1b]0;window title\x07",
        "\x1b[?61;6;7c",  # a DA1 *reply* is not a query
    ],
)
def test_query_filter_keeps_other_sequences(keep):
    f = TerminalQueryFilter()
    assert f.feed(f"a{keep}b") + f.flush() == f"a{keep}b"


def test_query_filter_handles_queries_split_across_chunks():
    f = TerminalQueryFilter()
    stream = "hello" + "".join(TMUX_QUERIES) + "\x1b[0mworld"
    out = "".join(f.feed(stream[i : i + 3]) for i in range(0, len(stream), 3))
    out += f.flush()
    assert out == "hello\x1b[0mworld"


def test_query_filter_flushes_incomplete_tail():
    f = TerminalQueryFilter()
    assert f.feed("done\x1b[") == "done"
    assert f.flush() == "\x1b["


# --- Windows command-line splitting (EDITOR) ---------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="uses CommandLineToArgvW")
@pytest.mark.parametrize(
    "cmd, argv",
    [
        ("python D:\\x\\editor.py", ["python", "D:\\x\\editor.py"]),
        (
            '"C:\\Program Files\\My Editor\\edit.exe" --wait',
            ["C:\\Program Files\\My Editor\\edit.exe", "--wait"],
        ),
        ("notepad", ["notepad"]),
    ],
)
def test_split_windows_command_line_keeps_backslashes(cmd, argv):
    assert split_windows_command_line(cmd) == argv


@pytest.mark.skipif(sys.platform != "win32", reason="uses CommandLineToArgvW")
def test_split_windows_command_line_inverts_list2cmdline():
    argv = ["C:\\a b\\x.exe", "say \"hi\"", "a&b", "trailing\\"]
    assert split_windows_command_line(subprocess.list2cmdline(argv)) == argv


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("a", None, "a"),
        ("\r", None, "\r"),  # Enter, same byte a POSIX raw tty produces
        ("\x03", None, "\x03"),  # Ctrl-C must reach the remote shell
        ("\x00", "H", "\x1b[A"),  # up
        ("\xe0", "P", "\x1b[B"),  # down
        ("\x00", "M", "\x1b[C"),  # right
        ("\xe0", "K", "\x1b[D"),  # left
        ("\x00", "G", "\x1b[H"),  # home
        ("\xe0", "O", "\x1b[F"),  # end
        ("\x00", "S", "\x1b[3~"),  # delete
        ("\xe0", "Z", ""),  # unknown scan code is dropped, not forwarded raw
    ],
)
def test_translate_windows_key(first, second, expected):
    assert translate_windows_key(first, second) == expected


def test_join_argv_posix_round_trips_through_shlex():
    import shlex

    argv = ["/usr/bin/python3", "-m", "colab_cli.cli", "ssh", "-s", "a b"]
    cmd = join_argv(argv, windows=False)
    assert shlex.split(cmd) == argv


def test_join_argv_windows_uses_cmd_quoting():
    """Windows OpenSSH runs ProxyCommand via cmd.exe, which does not treat
    single quotes as quoting. Spaces must be double-quoted."""
    argv = [
        r"C:\Program Files\Python\python.exe",
        "-m",
        "colab_cli.cli",
        "ssh",
        "--proxy-mode",
        "-s",
        "my session",
    ]
    cmd = join_argv(argv, windows=True)
    assert cmd == subprocess.list2cmdline(argv)
    assert '"C:\\Program Files\\Python\\python.exe"' in cmd
    assert '"my session"' in cmd
    assert "'" not in cmd


def test_read_controlling_line_windows_uses_conin(mocker):
    mocker.patch("colab_cli.terminal.is_windows", return_value=True)
    fake = mocker.mock_open(read_data="\n")
    mocker.patch("builtins.open", fake)
    assert read_controlling_line("Press Enter... ") == "\n"
    fake.assert_called_once_with("CONIN$", "r", encoding="utf-8", errors="replace")


def test_read_controlling_line_posix_uses_dev_tty(mocker):
    mocker.patch("colab_cli.terminal.is_windows", return_value=False)
    fake = mocker.mock_open(read_data="yes\n")
    mocker.patch("builtins.open", fake)
    assert read_controlling_line() == "yes\n"
    fake.assert_called_once_with("/dev/tty", encoding="utf-8", errors="replace")


def test_read_controlling_line_falls_back_to_stdin(mocker):
    mocker.patch("colab_cli.terminal.is_windows", return_value=False)
    mocker.patch("builtins.open", side_effect=OSError("no tty"))
    mocker.patch("sys.stdin.readline", return_value="ok\n")
    assert read_controlling_line() == "ok\n"


def test_open_url_windows_uses_startfile(mocker):
    mocker.patch("colab_cli.terminal.is_windows", return_value=True)
    startfile = mocker.Mock()
    mocker.patch("colab_cli.terminal.os.startfile", startfile, create=True)
    assert open_url_in_browser("https://accounts.google.com/o/oauth2/v2/auth") is True
    startfile.assert_called_once_with("https://accounts.google.com/o/oauth2/v2/auth")


def test_open_url_linux_skips_when_xdg_open_missing(mocker):
    mocker.patch("colab_cli.terminal.is_windows", return_value=False)
    mocker.patch("colab_cli.terminal.sys.platform", "linux")
    mocker.patch("colab_cli.terminal.shutil.which", return_value=None)
    assert open_url_in_browser("https://example.com") is False


def test_open_url_linux_uses_xdg_open(mocker):
    mocker.patch("colab_cli.terminal.is_windows", return_value=False)
    mocker.patch("colab_cli.terminal.sys.platform", "linux")
    mocker.patch("colab_cli.terminal.shutil.which", return_value="/usr/bin/xdg-open")
    popen = mocker.patch("colab_cli.terminal.subprocess.Popen")
    assert open_url_in_browser("https://example.com") is True
    assert popen.call_args.args[0] == ["/usr/bin/xdg-open", "https://example.com"]


def test_open_url_macos_uses_open(mocker):
    mocker.patch("colab_cli.terminal.is_windows", return_value=False)
    mocker.patch("colab_cli.terminal.sys.platform", "darwin")
    mocker.patch("colab_cli.terminal.shutil.which", return_value="/usr/bin/open")
    popen = mocker.patch("colab_cli.terminal.subprocess.Popen")
    assert open_url_in_browser("https://example.com") is True
    assert popen.call_args.args[0][0] == "/usr/bin/open"


def test_enable_virtual_terminal_noop_off_windows(mocker):
    mocker.patch("colab_cli.terminal.is_windows", return_value=False)
    assert enable_windows_virtual_terminal() is False


def test_windows_raw_console_noop_off_windows(mocker):
    mocker.patch("colab_cli.terminal.is_windows", return_value=False)
    with windows_raw_console():
        pass
