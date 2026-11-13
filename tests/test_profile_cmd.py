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

"""`colab profile`: list, show, add, rm and run (docs/09)."""

import json
import sys
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.auth import AuthProvider
from colab_cli.cli import app
from colab_cli.commands import profile as profile_cmd
from colab_cli.common import colab_argv
from colab_cli.state import SessionState

runner = CliRunner()


@pytest.fixture
def profiles_file(tmp_path, mock_common_state):
    mock_common_state.config_path = str(tmp_path / "sessions.json")
    path = tmp_path / "profiles.toml"
    (tmp_path / "train.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "server.py").write_text("print(2)\n", encoding="utf-8")
    path.write_text(
        '[train]\nscript = "train.py"\nargs = ["--epochs", "3"]\ngpu = "T4"\nbudget = 2.0\n'
        'secrets = ["HF_TOKEN"]\nenv = { WANDB_MODE = "offline" }\n\n'
        '[ui]\nscript = "server.py"\nmode = "kernel"\nsession = "ui"\n'
        'vars = { CONFIG = { access = "forward" } }\nforward = [8188]\n'
        'open = "http://127.0.0.1:8188"\nready = "http://127.0.0.1:8188/ok"\n'
        'ssh_key = "~/k"\nstop_when_done = false\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def calls(mocker):
    """subprocess.run of the CLI's own subcommands, recorded; exit 0."""
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return MagicMock(returncode=0)

    mocker.patch.object(profile_cmd.subprocess, "run", side_effect=run)
    mocker.patch.object(profile_cmd, "colab_argv", return_value=["colab"])
    return seen


def test_colab_argv_passes_the_global_options(mock_common_state, tmp_path):
    mock_common_state.auth_provider = AuthProvider.ADC
    mock_common_state.config_path = str(tmp_path / "s.json")
    mock_common_state.client_oauth_config = str(tmp_path / "oauth.json")
    mock_common_state.logtostderr = False

    argv = colab_argv(mock_common_state)

    assert argv[:3] == [sys.executable, "-m", "colab_cli"]
    assert argv[3:] == ["--auth=adc", "--config", str(tmp_path / "s.json"),
                        "-c", str(tmp_path / "oauth.json")]


def test_list_and_show(profiles_file):
    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 0, result.output
    assert "train" in result.output and "job" in result.output and "T4" in result.output
    assert "ui" in result.output and "kernel" in result.output

    rec = json.loads(runner.invoke(app, ["profile", "list", "--json"]).stdout)
    assert [p["name"] for p in rec["profiles"]] == ["train", "ui"]

    shown = runner.invoke(app, ["profile", "show", "ui"])
    assert 'mode = "kernel"' in shown.output and "forward = [\"8188\"]" in shown.output


def test_invalid_file_is_reported(profiles_file):
    profiles_file.write_text('[x]\nscript = "a.py"\nbogus = 1\n', encoding="utf-8")
    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 2
    assert "bogus" in result.output


def test_add_and_rm(profiles_file, tmp_path):
    result = runner.invoke(app, [
        "profile", "add", "eval", "--gpu", "L4", "--budget", "1", "--secret", "HF_TOKEN",
        "--env", "A=1", "--keep-runtime", "--", str(tmp_path / "train.py"), "--quick",
    ])
    assert result.exit_code == 0, result.output
    shown = runner.invoke(app, ["profile", "show", "eval", "--json"])
    p = json.loads(shown.stdout)
    assert p["gpu"] == "L4" and p["args"] == ["--quick"] and p["stop_when_done"] is False
    assert p["env"] == {"A": "1"}

    again = runner.invoke(app, ["profile", "add", "eval", "--", "x.py"])
    assert again.exit_code == 2 and "--force" in again.output

    assert runner.invoke(app, ["profile", "rm", "eval"]).exit_code == 0
    assert runner.invoke(app, ["profile", "rm", "eval"]).exit_code == 1


def test_add_rejects_a_bad_profile(profiles_file):
    result = runner.invoke(app, ["profile", "add", "bad", "--gpu", "Z9", "--", "x.py"])
    assert result.exit_code == 2
    assert "gpu" in result.output


def test_run_job_profile_on_a_new_runtime(profiles_file, calls, mock_common_state, tmp_path):
    stored = SessionState(name="x", token="t", url="u", endpoint="e")
    mock_common_state.store.get.return_value = stored

    result = runner.invoke(app, ["profile", "run", "train"])

    assert result.exit_code == 0, result.output
    argv = calls[0]
    assert argv[:3] == ["colab", "run", "--detach"]
    name = argv[argv.index("-s") + 1]
    assert name.startswith("train-")
    assert argv[argv.index("--gpu") + 1] == "T4"
    assert argv[argv.index("--budget") + 1] == "2.0"
    assert argv[argv.index("--secret") + 1] == "HF_TOKEN"
    assert argv[argv.index("--env") + 1] == "WANDB_MODE=offline"
    assert argv[-3:] == [str(tmp_path / "train.py"), "--epochs", "3"]
    # The session remembers its profile, for the tray.
    assert stored.profile == "train" and stored.stop_when_done is True
    mock_common_state.store.add.assert_called_with(stored)
    assert "colab tray" in result.output


def test_run_job_profile_reuses_its_live_session(profiles_file, calls, mock_common_state, tmp_path):
    with open(profiles_file, "a", encoding="utf-8") as f:
        f.write('\n[again]\nscript = "train.py"\nsession = "box"\n')
    box = SessionState(name="box", token="t", url="u", endpoint="e")
    mock_common_state.sync_sessions.return_value = ({"box": box}, [])
    mock_common_state.store.get.return_value = box

    result = runner.invoke(app, ["profile", "run", "again"])

    assert result.exit_code == 0, result.output
    assert calls[0] == ["colab", "job", "start", "-s", "box", "-f", str(tmp_path / "train.py")]


def test_run_kernel_profile_with_forward(profiles_file, calls, mock_common_state, mocker, tmp_path):
    forward = MagicMock()
    forward.poll.return_value = None
    forward.wait.return_value = 0
    popen = mocker.patch.object(profile_cmd.subprocess, "Popen", return_value=forward)
    from colab_cli.commands import ssh as ssh_cmd

    mocker.patch.object(ssh_cmd, "_require_ssh_key")
    answers = iter([OSError("refused"), MagicMock(status=200)])

    def urlopen(url, timeout):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    mocker.patch.object(profile_cmd.urllib.request, "urlopen", side_effect=urlopen)
    mocker.patch.object(profile_cmd.time, "sleep")
    opened = mocker.patch.object(profile_cmd.webbrowser, "open")
    mock_common_state.store.get.return_value = SessionState(name="ui", token="t", url="u", endpoint="e")

    result = runner.invoke(app, ["profile", "run", "ui"])

    assert result.exit_code == 0, result.output
    assert calls[0] == ["colab", "new", "-s", "ui"]
    exec_argv = calls[1]
    assert exec_argv[:5] == ["colab", "exec", "-s", "ui", "-f"]
    assert exec_argv[exec_argv.index("--var") + 1] == 'CONFIG={"access": "forward"}'
    fwd = popen.call_args.args[0]
    assert fwd[:4] == ["colab", "forward", "-s", "ui"] and fwd[-1] == "8188"
    assert fwd[fwd.index("-i") + 1].endswith("k")
    opened.assert_called_once_with("http://127.0.0.1:8188")
    assert len(calls) == 2  # stop_when_done = false: no `colab stop`


def test_run_kernel_profile_stops_when_done_and_on_failure(profiles_file, calls, mock_common_state, mocker):
    with open(profiles_file, "a", encoding="utf-8") as f:
        f.write('\n[once]\nscript = "server.py"\nmode = "kernel"\nsession = "o"\n')
    results = iter([0, 1])  # new ok, exec fails

    def run(argv, **kw):
        calls.append(argv)
        return MagicMock(returncode=next(results, 0))

    mocker.patch.object(profile_cmd.subprocess, "run", side_effect=run)
    mock_common_state.store.get.return_value = SessionState(name="o", token="t", url="u", endpoint="e")

    result = runner.invoke(app, ["profile", "run", "once"])

    assert result.exit_code == 1
    assert calls[-1] == ["colab", "stop", "-s", "o"]


def test_run_unknown_profile(profiles_file):
    result = runner.invoke(app, ["profile", "run", "nope"])
    assert result.exit_code == 2
    assert "train" in result.output  # lists what exists


def test_run_missing_script_fails_before_a_runtime(profiles_file, calls):
    (profiles_file.parent / "train.py").unlink()
    result = runner.invoke(app, ["profile", "run", "train"])
    assert result.exit_code == 2
    assert calls == []


def test_forward_profile_without_a_key_fails_before_a_runtime(profiles_file, calls, mocker):
    import typer

    from colab_cli.commands import ssh as ssh_cmd

    def no_key(identity):
        assert identity == "~/k"
        raise typer.Exit(2)

    mocker.patch.object(ssh_cmd, "_require_ssh_key", side_effect=no_key)

    result = runner.invoke(app, ["profile", "run", "ui"])

    assert result.exit_code == 2
    assert calls == []


def test_edit_creates_a_template_and_checks_the_result(tmp_path, mock_common_state, mocker):
    import tomllib

    mock_common_state.config_path = str(tmp_path / "sessions.json")
    path = tmp_path / "profiles.toml"
    edited = []

    def editor(p):
        edited.append(p)
        text = open(p, encoding="utf-8").read()
        assert "# [train-t4]" in text and 'mode = "kernel"' in text  # commented examples
        assert tomllib.loads(text) == {}  # only comments: valid and empty
        with open(p, "a", encoding="utf-8") as f:
            f.write('\n[mine]\nscript = "x.py"\n')

    mocker.patch.object(profile_cmd, "_launch_editor", side_effect=editor)

    result = runner.invoke(app, ["profile", "edit"])

    assert result.exit_code == 0, result.output
    assert edited == [str(path)]
    assert "1 profile(s): mine" in result.output


def test_edit_reports_a_broken_file(profiles_file, mocker):
    def editor(p):
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n[bad]\nscript = 1\n")

    mocker.patch.object(profile_cmd, "_launch_editor", side_effect=editor)
    result = runner.invoke(app, ["profile", "edit"])
    assert result.exit_code == 2
    assert "'bad'" in result.output and "colab profile edit" in result.output


def test_edit_with_a_failing_editor(profiles_file, mocker):
    import click

    mocker.patch.object(profile_cmd, "_launch_editor", side_effect=click.ClickException("vim: Editing failed"))
    result = runner.invoke(app, ["profile", "edit"])
    assert result.exit_code == 1
    assert "vim: Editing failed" in result.output and "Traceback" not in result.output
