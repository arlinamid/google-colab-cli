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

"""Non-ASCII scripts and output must work under any Windows code page.

Windows defaults text I/O to the ANSI code page (cp1252, cp1250, cp932,
cp936, ...), so a UTF-8 script or Japanese kernel output failed with
Unicode errors for most non-English users.
"""

import io
import subprocess
import sys
from unittest.mock import ANY, MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli import utils
from colab_cli.cli import app

runner = CliRunner()
JA = "こんにちは árvíztűrő"


@pytest.fixture
def session(mock_common_state):
    s = MagicMock(url="http://url", token="t", kernel_id=None, session_id=None)
    s.name = "s1"
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    return s


@pytest.fixture
def runtime(mocker):
    rt = mocker.patch("colab_cli.commands.execution.ColabRuntime").return_value
    rt.execute_code.return_value = []
    return rt


def test_read_python_source_utf8(tmp_path):
    p = tmp_path / "a.py"
    p.write_bytes(f'print("{JA}")\n'.encode("utf-8"))
    assert utils.read_python_source(str(p)) == f'print("{JA}")\n'


def test_read_python_source_honors_coding_cookie(tmp_path):
    """A Japanese user's Shift-JIS file with a PEP 263 cookie, as Python reads it."""
    p = tmp_path / "sjis.py"
    p.write_bytes('# -*- coding: cp932 -*-\nprint("こんにちは")\n'.encode("cp932"))
    assert 'print("こんにちは")' in utils.read_python_source(str(p))


def test_read_python_source_utf8_bom(tmp_path):
    p = tmp_path / "bom.py"
    p.write_bytes(b"\xef\xbb\xbf" + 'x = "ő"\n'.encode("utf-8"))
    assert utils.read_python_source(str(p)) == 'x = "ő"\n'


def test_exec_file_sends_exact_unicode(session, runtime, tmp_path):
    p = tmp_path / "a.py"
    p.write_bytes(f'print("{JA}")\n'.encode("utf-8"))
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", str(p)])
    assert result.exit_code == 0, result.output
    runtime.execute_code.assert_any_call(
        f'print("{JA}")\n', output_hook=ANY, timeout=None
    )


def test_exec_file_that_does_not_decode_exits_2(session, runtime, tmp_path):
    p = tmp_path / "bad.py"
    p.write_bytes('print("é")\n'.encode("latin-1"))  # not UTF-8, no cookie
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", str(p)])
    assert result.exit_code == 2
    assert "coding" in result.output
    runtime.execute_code.assert_not_called()


def test_run_script_with_coding_cookie(mocker, mock_common_state, tmp_path):
    from colab_cli.commands import run as run_cmd

    p = tmp_path / "s.py"
    p.write_bytes('# -*- coding: cp932 -*-\nprint("こんにちは")\n'.encode("cp932"))
    payload = run_cmd._build_script_payload(str(p), [], {})
    assert 'print("こんにちは")' in payload


@pytest.mark.parametrize("encoding", ["cp1250", "cp932", "ascii"])
def test_redirected_stdio_never_fails_on_unicode(encoding):
    """Piped stdout in a legacy code page: printing Japanese must not raise."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline="\n")
    fixed = utils.utf8_text_stream(stream)
    fixed.write(JA + "\n")
    fixed.flush()
    assert raw.getvalue() == (JA + "\n").encode("utf-8")


def test_console_streams_are_left_alone():
    console = MagicMock()
    console.isatty.return_value = True
    assert utils.utf8_text_stream(console) is console


@pytest.mark.skipif(sys.platform != "win32", reason="Windows code pages")
def test_cli_prints_japanese_through_a_pipe():
    """End to end: `colab ...` output to a pipe in the system code page."""
    code = (
        "import sys; from colab_cli import cli; cli.configure_stdio(); "
        f"print({JA!r})"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=60
    )
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    assert out.stdout.decode("utf-8").strip() == JA


def test_run_undecodable_script_exits_2_before_assign(mock_common_state, tmp_path):
    p = tmp_path / "bad.py"
    p.write_bytes('print("é")\n'.encode("latin-1"))
    result = runner.invoke(app, ["run", str(p)])
    assert result.exit_code == 2
    mock_common_state.client.assign.assert_not_called()


def test_log_file_is_utf8(tmp_path, monkeypatch):
    """colab.log gets every websocket message at DEBUG, Japanese code included."""
    import logging

    from colab_cli import common

    monkeypatch.setattr(common.os.path, "expanduser", lambda p: str(tmp_path / "cfg"))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        common.setup_logging(log_to_stderr=False)
        new = [h for h in root.handlers if h not in before]
        assert [h.encoding for h in new] == ["utf-8"]
        logging.getLogger("t").debug(JA)
        for h in new:
            h.flush()
        log = (tmp_path / "cfg" / "colab.log").read_text(encoding="utf-8")
        assert JA in log
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
            h.close()
