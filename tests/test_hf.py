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

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.state import SessionState

runner = CliRunner()


@pytest.fixture
def runtime(mock_common_state, mocker):
    s = SessionState(name="s1", token="t", url="https://u", endpoint="e1")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    rt = mocker.patch("colab_cli.commands.automation.ColabRuntime").return_value
    rt.execute_code.return_value = [{"text": "ok\n"}]
    return rt


def _code(runtime):
    return runtime.execute_code.call_args.args[0]


def test_login_uses_local_hf_token_and_redacts(runtime, mock_common_state, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_login_secret")
    result = runner.invoke(app, ["hf", "login", "-s", "s1"])
    assert result.exit_code == 0, result.output
    code = _code(runtime)
    assert "os.environ['HF_TOKEN'] = 'hf_login_secret'" in code
    assert "login(token=os.environ['HF_TOKEN'], add_to_git_credential=False)" in code
    mock_common_state.history.add_redaction.assert_called_with("hf_login_secret")
    assert "hf_login_secret" not in result.output


def test_login_falls_back_to_hf_cli_then_prompt(runtime, monkeypatch, mocker):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    mocker.patch("colab_cli.secrets._token_from_cli", return_value=None)
    mocker.patch("colab_cli.secrets._hf_token_file", return_value=None)
    gp = mocker.patch("getpass.getpass", return_value="hf_typed")
    result = runner.invoke(app, ["hf", "login", "-s", "s1"])
    assert result.exit_code == 0, result.output
    gp.assert_called_once()
    assert "'hf_typed'" in _code(runtime)


def test_login_explicit_token_source(runtime, monkeypatch):
    monkeypatch.setenv("OTHER", "hf_other")
    result = runner.invoke(app, ["hf", "login", "-s", "s1", "--token", "env:OTHER"])
    assert result.exit_code == 0, result.output
    assert "'hf_other'" in _code(runtime)


def test_login_explicit_source_missing_exits_2(runtime, monkeypatch):
    monkeypatch.delenv("OTHER", raising=False)
    result = runner.invoke(app, ["hf", "login", "-s", "s1", "--token", "env:OTHER"])
    assert result.exit_code == 2
    runtime.execute_code.assert_not_called()


def test_login_literal_token_refused(runtime):
    result = runner.invoke(app, ["hf", "login", "-s", "s1", "--token", "hf_abc"])
    assert result.exit_code == 2
    runtime.execute_code.assert_not_called()


def test_download_builds_snapshot_download_call(runtime):
    result = runner.invoke(
        app,
        [
            "hf", "download", "-s", "s1", "google/gemma-3-1b-it",
            "--repo-type", "model", "--revision", "main",
            "--local-dir", "/content/gemma",
        ],
    )
    assert result.exit_code == 0, result.output
    code = _code(runtime)
    assert "snapshot_download(" in code
    assert "repo_id='google/gemma-3-1b-it'" in code
    assert "repo_type='model'" in code
    assert "revision='main'" in code
    assert "local_dir='/content/gemma'" in code


def test_download_quotes_hostile_repo_id_safely(runtime):
    nasty = "a'); import os; os.system('x'); ('"
    runner.invoke(app, ["hf", "download", "-s", "s1", nasty])
    assert f"repo_id={nasty!r}" in _code(runtime)


def test_download_rejects_unknown_repo_type(runtime):
    result = runner.invoke(
        app, ["hf", "download", "-s", "s1", "x/y", "--repo-type", "bogus"]
    )
    assert result.exit_code == 2
    runtime.execute_code.assert_not_called()


def test_download_kernel_error_exits_1(runtime):
    runtime.execute_code.return_value = [
        {"output_type": "error", "ename": "E", "evalue": "x", "traceback": []}
    ]
    result = runner.invoke(app, ["hf", "download", "-s", "s1", "x/y"])
    assert result.exit_code == 1


def test_missing_huggingface_hub_is_explained(runtime):
    code_holder = {}

    def capture(code, **kwargs):
        code_holder["code"] = code
        return [{"text": "ok"}]

    runtime.execute_code.side_effect = capture
    runner.invoke(app, ["hf", "download", "-s", "s1", "x/y"])
    assert "colab install huggingface_hub" in code_holder["code"]
