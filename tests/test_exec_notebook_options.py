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

"""`colab exec` notebook options: --cells, --stop-on-error, --check, --check-only,
exit codes, and the missing-file error."""

from unittest.mock import MagicMock

import nbformat
import pytest
from typer.testing import CliRunner

from colab_cli.cli import app

runner = CliRunner()


@pytest.fixture
def mock_runtime_class(mocker):
    return mocker.patch("colab_cli.commands.execution.ColabRuntime")


@pytest.fixture
def session(mock_common_state):
    s = MagicMock()
    s.name = "s1"
    s.kernel_id = None
    s.session_id = None
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    return s


def _write_nb(tmp_path, *sources):
    nb = nbformat.v4.new_notebook()
    nb.cells = [nbformat.v4.new_markdown_cell("# intro")] + [
        nbformat.v4.new_code_cell(src) for src in sources
    ]
    path = tmp_path / "nb.ipynb"
    nbformat.write(nb, str(path))
    return str(path)


def _runtime_running(mock_runtime_class, fail_on=()):
    """The mock runtime records the cell code it ran; code containing any of
    ``fail_on`` returns an error output."""
    ran = []

    def execute_code(code, output_hook=None, **kwargs):
        if code.startswith("import os; os.makedirs('/content'"):
            return []
        ran.append(code)
        if any(marker in code for marker in fail_on):
            out = [{"output_type": "error", "ename": "ValueError", "evalue": "boom"}]
        else:
            out = [{"output_type": "stream", "name": "stdout", "text": "ok\n"}]
        if output_hook:
            for o in out:
                output_hook(o)
        return out

    mock_runtime_class.return_value.execute_code.side_effect = execute_code
    return ran


NB = ("#@title One\nx = 1", "#@title Two\nraise ValueError('two')", "#@title Three\nprint(x)")


def test_runs_all_cells_and_exits_1_when_one_fails(tmp_path, mock_runtime_class, session):
    """Old behavior kept: later cells still run. New: the exit code says a
    cell failed, so scripts can notice."""
    ran = _runtime_running(mock_runtime_class, fail_on=("raise",))
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", _write_nb(tmp_path, *NB)])

    assert result.exit_code == 1
    assert len(ran) == 3
    assert "1 cell(s) raised an error: cell 2 (Two)" in result.stderr


def test_stop_on_error_skips_the_rest(tmp_path, mock_runtime_class, session):
    ran = _runtime_running(mock_runtime_class, fail_on=("raise",))
    path = _write_nb(tmp_path, *NB)
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", path, "--stop-on-error"])

    assert result.exit_code == 1
    assert len(ran) == 2
    assert "Stopping at cell 2 (Two) (--stop-on-error); 1 cell(s) not run." in result.stderr
    # The partial outputs are still saved.
    out = nbformat.read(str(tmp_path / "nb_output.ipynb"), as_version=4)
    code = [c for c in out.cells if c.cell_type == "code"]
    assert code[0].outputs and code[1].outputs and not code[2].outputs


def test_cells_runs_only_the_selection_in_order(tmp_path, mock_runtime_class, session):
    ran = _runtime_running(mock_runtime_class)
    path = _write_nb(tmp_path, *NB)
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", path, "--cells", "Three,1"])

    assert result.exit_code == 0, result.output
    assert [c.splitlines()[0] for c in ran] == ["#@title Three", "#@title One"]
    assert "Executing cell 3 (1/2) - Three..." in result.output
    assert "Executing cell 1 (2/2) - One..." in result.output


def test_bad_cells_spec_fails_before_running(tmp_path, mock_runtime_class, session):
    ran = _runtime_running(mock_runtime_class)
    path = _write_nb(tmp_path, *NB)
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", path, "--cells", "7"])

    assert result.exit_code == 2
    assert "there is no code cell 7" in result.stderr
    assert ran == []


def test_check_stops_a_broken_notebook_before_it_runs(
    tmp_path, mock_runtime_class, session
):
    ran = _runtime_running(mock_runtime_class)
    path = _write_nb(tmp_path, "!pip install x\nx = 1", "#@title Bad\ny = (1,\n")
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", path, "--check"])

    assert result.exit_code == 2
    assert "Check failed (1 problem(s)); nothing was run:" in result.stderr
    assert "cell 2 (Bad), line" in result.stderr
    assert ran == []
    mock_runtime_class.assert_not_called()


def test_check_passes_then_runs(tmp_path, mock_runtime_class, session):
    ran = _runtime_running(mock_runtime_class)
    path = _write_nb(tmp_path, "!pip install x\nx = 1", "%cd /content\nprint(x)")
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", path, "--check"])

    assert result.exit_code == 0, result.output
    assert "Check passed (2 cell(s))." in result.output
    assert len(ran) == 2


def test_check_only_needs_no_session(tmp_path, mock_runtime_class, mock_common_state):
    path = _write_nb(tmp_path, *NB)
    result = runner.invoke(app, ["exec", "-f", path, "--check-only", "--cells", "3,1"])

    assert result.exit_code == 0, result.output
    assert "Would run: cell 3 (Three), cell 1 (One)" in result.output
    mock_common_state.resolve_session.assert_not_called()
    mock_runtime_class.assert_not_called()


def test_check_applies_to_plain_python_files(tmp_path, mock_runtime_class, session):
    script = tmp_path / "job.py"
    script.write_text("def broken(:\n    pass\n")
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", str(script), "--check"])

    assert result.exit_code == 2
    assert f"{script}, line 1:" in result.stderr


def test_missing_file_is_one_line(tmp_path, mock_runtime_class, session):
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", str(tmp_path / "nope.ipynb")])

    assert result.exit_code == 1
    assert "[colab] File not found:" in result.stderr
    assert "Traceback" not in result.output


def test_unreadable_notebook_is_one_line(tmp_path, mock_runtime_class, session):
    path = tmp_path / "broken.ipynb"
    path.write_text("{not json")
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", str(path)])

    assert result.exit_code == 2
    assert "[colab] Cannot read notebook" in result.stderr


@pytest.mark.parametrize("flag", [["--cells", "1"], ["--stop-on-error"]])
def test_notebook_flags_need_a_notebook(tmp_path, mock_runtime_class, session, flag):
    script = tmp_path / "job.py"
    script.write_text("x = 1\n")
    result = runner.invoke(app, ["exec", "-s", "s1", "-f", str(script), *flag])

    assert result.exit_code == 2
    assert "only apply to .ipynb notebooks" in result.stderr
