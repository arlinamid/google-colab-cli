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
from colab_cli.commands import git as git_cmd
from colab_cli.state import SessionState

runner = CliRunner()


@pytest.fixture
def runtime(mock_common_state, mocker, monkeypatch):
    s = SessionState(name="s1", token="t", url="https://u", endpoint="e1")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    # No local gh login unless a test says so.
    mocker.patch("colab_cli.secrets._token_from_cli", return_value=None)
    rt = mocker.patch("colab_cli.commands.automation.ColabRuntime").return_value
    rt.execute_code.return_value = [{"text": "Cloned\n"}]
    return rt


def _code(runtime):
    return runtime.execute_code.call_args.args[0]


@pytest.mark.parametrize(
    ("repo", "url", "name"),
    [
        ("octocat/Hello-World", "https://github.com/octocat/Hello-World.git", "Hello-World"),
        ("https://github.com/a/b", "https://github.com/a/b", "b"),
        ("https://github.com/a/b.git", "https://github.com/a/b.git", "b"),
        ("https://gitlab.com/g/sub/proj.git", "https://gitlab.com/g/sub/proj.git", "proj"),
    ],
)
def test_repo_url_and_default_dest(repo, url, name):
    assert git_cmd._repo_url(repo) == url
    assert git_cmd._default_dest(url) == f"/content/{name}"


@pytest.mark.parametrize(
    "repo",
    ["", "noslash", "git@github.com:a/b.git", "http://github.com/a/b", "https://x:tok@github.com/a/b", "a/b/c"],
)
def test_repo_url_rejects_unsupported(repo):
    import typer

    with pytest.raises(typer.Exit) as exc_info:
        git_cmd._repo_url(repo)
    assert exc_info.value.exit_code == 2


def test_public_clone_without_token(runtime, mock_common_state):
    result = runner.invoke(app, ["clone", "-s", "s1", "octocat/Hello-World"])
    assert result.exit_code == 0, result.output
    code = _code(runtime)
    assert "'https://github.com/octocat/Hello-World.git'" in code
    assert "'/content/Hello-World'" in code
    assert "extraHeader" not in code
    mock_common_state.history.add_redaction.assert_not_called()


def test_clone_options(runtime):
    result = runner.invoke(
        app,
        ["clone", "-s", "s1", "a/b", "/content/x", "--branch", "dev", "--depth", "1"],
    )
    assert result.exit_code == 0, result.output
    code = _code(runtime)
    assert "'--branch', 'dev'" in code
    assert "'--depth', '1'" in code
    assert "'/content/x'" in code


def test_token_from_env_goes_in_header_not_url(runtime, mock_common_state, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_secretvalue")
    result = runner.invoke(app, ["clone", "-s", "s1", "me/private"])
    assert result.exit_code == 0, result.output
    code = _code(runtime)
    # Only the base64 credential is in the code. It reaches git through
    # GIT_CONFIG_* environment variables for this one command: not in argv,
    # not in the URL, never written to .git/config.
    assert "ghp_secretvalue" not in code
    assert "'GIT_CONFIG_KEY_0': 'http.extraHeader'" in code
    assert "Authorization: Basic " in code
    assert "'https://github.com/me/private.git'" in code
    redacted = [c.args[0] for c in mock_common_state.history.add_redaction.call_args_list]
    assert "ghp_secretvalue" in redacted
    assert git_cmd._basic_auth("ghp_secretvalue") in redacted
    assert "ghp_secretvalue" not in result.output


def test_token_from_gh_cli(runtime, mocker):
    mocker.patch("colab_cli.secrets._token_from_cli", return_value="gho_fromcli")
    result = runner.invoke(app, ["clone", "-s", "s1", "me/private"])
    assert result.exit_code == 0, result.output
    assert git_cmd._basic_auth("gho_fromcli") in _code(runtime)


def test_token_none_disables_auto_token(runtime, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    result = runner.invoke(app, ["clone", "-s", "s1", "a/b", "--token", "none"])
    assert result.exit_code == 0, result.output
    assert "extraHeader" not in _code(runtime)


def test_token_prompt(runtime, mocker):
    mocker.patch("getpass.getpass", return_value="typed_tok")
    result = runner.invoke(app, ["clone", "-s", "s1", "a/b", "--token", "prompt"])
    assert result.exit_code == 0, result.output
    assert git_cmd._basic_auth("typed_tok") in _code(runtime)


def test_explicit_token_source_missing_exits_2(runtime):
    result = runner.invoke(app, ["clone", "-s", "s1", "a/b", "--token", "env:NOPE_X"])
    assert result.exit_code == 2
    runtime.execute_code.assert_not_called()


def test_token_never_sent_to_non_https_or_other_hosts_by_default(runtime, monkeypatch):
    """The auto GitHub token only goes to github.com."""
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    result = runner.invoke(app, ["clone", "-s", "s1", "https://gitlab.com/g/p.git"])
    assert result.exit_code == 0, result.output
    assert "extraHeader" not in _code(runtime)


def test_clone_failure_exits_1(runtime):
    runtime.execute_code.return_value = [
        {"output_type": "error", "ename": "E", "evalue": "x", "traceback": []}
    ]
    result = runner.invoke(app, ["clone", "-s", "s1", "a/b"])
    assert result.exit_code == 1


def test_bad_depth_exits_2(runtime):
    result = runner.invoke(app, ["clone", "-s", "s1", "a/b", "--depth", "0"])
    assert result.exit_code == 2


def test_clone_uses_a_long_timeout(runtime):
    runner.invoke(app, ["clone", "-s", "s1", "a/b"])
    assert runtime.execute_code.call_args.kwargs["timeout"] >= 600
