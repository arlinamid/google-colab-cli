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

import base64
from unittest.mock import MagicMock, patch

import pytest
from rich.markdown import Markdown
from rich.text import Text

from colab_cli.utils import (
    handle_image,
    open_url_in_browser,
    print_kitty,
    render_display_data,
)


@patch("colab_cli.utils.sys.stdout.isatty", return_value=True)
def test_print_kitty_emits_escape_sequence_on_tty(_mock_isatty, capsys):
    print_kitty(b"fake-png-bytes")
    captured = capsys.readouterr()
    assert "_Ga=T,f=100;" in captured.out
    b64 = base64.b64encode(b"fake-png-bytes").decode("ascii")
    assert b64 in captured.out


@patch("colab_cli.utils.sys.stdout.isatty", return_value=False)
def test_print_kitty_silent_when_stdout_not_tty(_mock_isatty, capsys):
    """Kitty graphics escape sequences are useless and visually corrupt the
    output when stdout is redirected (e.g. `colab exec ... > log.txt`,
    `colab exec ... | grep ...`, or any non-Kitty terminal). When stdout is
    not a TTY, print_kitty must not emit anything.
    """
    print_kitty(b"fake-png-bytes")
    captured = capsys.readouterr()
    assert captured.out == "", (
        f"Expected no output when stdout is not a TTY, got: {captured.out!r}"
    )


@patch("colab_cli.utils.tempfile.NamedTemporaryFile")
@patch("colab_cli.utils.print_kitty")
def test_handle_image(mock_print_kitty, mock_tempfile, capsys):
    mock_tmp = MagicMock()
    mock_tmp.name = "/tmp/fake.png"
    mock_tempfile.return_value = mock_tmp

    handle_image(base64.b64encode(b"test").decode("ascii"), "image/png")

    mock_print_kitty.assert_called_once_with(b"test")
    mock_tmp.write.assert_called_once_with(b"test")
    mock_tmp.close.assert_called_once()

    captured = capsys.readouterr()
    assert "/tmp/fake.png" in captured.out


@pytest.mark.parametrize(
    "data, expected_markup",
    [
        ({"text/markdown": "**md**"}, "**md**"),
        ({"text/html": "<b>hi</b>"}, "**hi**\n\n"),
        ({"text/markdown": "**md**", "text/html": "<b>hi</b>"}, "**md**"),
        ({"text/html": "<b>hi</b>", "text/plain": "plain"}, "**hi**\n\n"),
    ],
)
def test_render_display_data_markdown(data, expected_markup):
    result = render_display_data(data)
    assert isinstance(result, Markdown)
    assert result.markup == expected_markup


def test_render_display_data_plain():
    result = render_display_data({"text/plain": "plain"})
    assert isinstance(result, Text)
    assert result.plain == "plain"


def test_render_display_data_none():
    assert render_display_data({"image/png": "..."}) is None


@patch("colab_cli.utils.webbrowser.open", return_value=True)
def test_open_url_in_browser_reports_success(mock_open):
    assert open_url_in_browser("https://example.com/auth") is True
    mock_open.assert_called_once_with("https://example.com/auth")


@patch("colab_cli.utils.webbrowser.open", return_value=False)
def test_open_url_in_browser_headless_returns_false(mock_open):
    """No browser opener (SSH, containers, CI) must not raise."""
    assert open_url_in_browser("https://example.com/auth") is False


@patch("colab_cli.utils.webbrowser.open", side_effect=OSError("no opener"))
def test_open_url_in_browser_swallows_opener_errors(mock_open):
    assert open_url_in_browser("https://example.com/auth") is False


# Git Bash (MSYS) rewrites arguments that look like POSIX paths before a
# native program sees them: `colab ls /content` arrives as
# `C:/Program Files/Git/content`. Remote paths are never Windows paths, so
# the CLI turns them back.
GIT_ROOT = "C:/Program Files/Git"


@pytest.fixture
def git_bash(mocker, monkeypatch):
    from colab_cli import utils

    mocker.patch.object(utils.sys, "platform", "win32")
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    mocker.patch.object(utils, "_msys_root", return_value=GIT_ROOT)


@pytest.mark.parametrize(
    "given, expected",
    [
        ("C:/Program Files/Git/content/x.txt", "/content/x.txt"),
        ("c:/program files/git/content", "/content"),
        (r"C:\Program Files\Git\content\x", "/content/x"),
        ("C:/Program Files/Git/", "/"),
        ("C:/Program Files/Git", "/"),
        ("/content/x.txt", "/content/x.txt"),
        ("content/x.txt", "content/x.txt"),
        ("D:/data/x.txt", "D:/data/x.txt"),
        ("C:/Program Files/Gitlab/x", "C:/Program Files/Gitlab/x"),
    ],
)
def test_undo_msys_path_under_git_bash(git_bash, given, expected):
    from colab_cli.utils import undo_msys_path

    assert undo_msys_path(given) == expected


def test_undo_msys_path_outside_git_bash(mocker, monkeypatch):
    from colab_cli import utils

    mocker.patch.object(utils.sys, "platform", "win32")
    monkeypatch.delenv("MSYSTEM", raising=False)
    root = mocker.patch.object(utils, "_msys_root", return_value=GIT_ROOT)

    path = "C:/Program Files/Git/content"
    assert utils.undo_msys_path(path) == path
    root.assert_not_called()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_undo_msys_path_is_windows_only(mocker, monkeypatch, platform):
    """On Linux and macOS a remote path named like a Windows path is kept."""
    from colab_cli import utils

    mocker.patch.object(utils.sys, "platform", platform)
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    mocker.patch.object(utils, "_msys_root", return_value=GIT_ROOT)

    path = "C:/Program Files/Git/content"
    assert utils.undo_msys_path(path) == path


def test_msys_root_asks_cygpath(mocker):
    from colab_cli import utils

    utils._msys_root.cache_clear()
    run = mocker.patch.object(
        utils.subprocess, "run", return_value=MagicMock(stdout="C:/Program Files/Git/\n")
    )
    assert utils._msys_root() == GIT_ROOT
    assert run.call_args.args[0] == ["cygpath", "-m", "/"]
    utils._msys_root.cache_clear()


@pytest.mark.parametrize(
    "exepath", [r"C:\Program Files\Git\bin", r"C:\Program Files\Git"]
)
def test_msys_root_falls_back_to_exepath(mocker, monkeypatch, exepath):
    from colab_cli import utils

    utils._msys_root.cache_clear()
    mocker.patch.object(utils.subprocess, "run", side_effect=FileNotFoundError)
    monkeypatch.setenv("EXEPATH", exepath)
    assert utils._msys_root() == GIT_ROOT
    utils._msys_root.cache_clear()


@pytest.mark.parametrize(
    "given, expected",
    [
        ("C:/Program Files/Git/content/x.txt", "/content/x.txt"),
        ("--out=C:/Program Files/Git/content/o", "--out=/content/o"),
        ("ls", "ls"),
        ("D:/data", "D:/data"),
        ("--flag", "--flag"),
    ],
)
def test_undo_msys_arg_under_git_bash(git_bash, given, expected):
    from colab_cli.utils import undo_msys_arg

    assert undo_msys_arg(given) == expected


def test_undo_msys_args_outside_git_bash_keeps_everything(mocker, monkeypatch):
    from colab_cli import utils

    mocker.patch.object(utils.sys, "platform", "linux")
    args = ["C:/Program Files/Git/content", "--out=C:/Program Files/Git/x"]
    assert utils.undo_msys_args(args) == args
