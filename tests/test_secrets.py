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

import subprocess
from unittest.mock import MagicMock

import pytest
import typer

from colab_cli import secrets


# --- Spec parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("HF_TOKEN", ("HF_TOKEN", "env:HF_TOKEN")),
        ("HF_TOKEN=env", ("HF_TOKEN", "env:HF_TOKEN")),
        ("HF_TOKEN=env:MY_HF", ("HF_TOKEN", "env:MY_HF")),
        ("HF_TOKEN=prompt", ("HF_TOKEN", "prompt")),
        ("HF_TOKEN=hf-cli", ("HF_TOKEN", "hf-cli")),
        ("GH_TOKEN=gh-cli", ("GH_TOKEN", "gh-cli")),
    ],
)
def test_parse_spec(spec, expected):
    assert secrets.parse_spec(spec) == expected


@pytest.mark.parametrize(
    "spec",
    ["HF_TOKEN=hf_abc123", "1BAD", "BAD-NAME=env", "X=env:", "X=env:1BAD", ""],
)
def test_parse_spec_rejects_literals_and_bad_names(spec):
    with pytest.raises(typer.Exit) as exc_info:
        secrets.parse_spec(spec)
    assert exc_info.value.exit_code == 2


def test_literal_value_error_points_to_env_or_prompt(capsys):
    with pytest.raises(typer.Exit):
        secrets.parse_spec("HF_TOKEN=hf_abc123")
    err = capsys.readouterr().err
    assert "hf_abc123" not in err
    assert "env:" in err and "prompt" in err


# --- Sources -----------------------------------------------------------------


def test_env_source(monkeypatch):
    monkeypatch.setenv("MY_HF", "hf_from_env")
    assert secrets.read_source("HF_TOKEN", "env:MY_HF") == "hf_from_env"


def test_env_source_missing_returns_none(monkeypatch):
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    assert secrets.read_source("X", "env:NOPE_NOT_SET") is None


def test_prompt_source(mocker):
    gp = mocker.patch("getpass.getpass", return_value="typed")
    assert secrets.read_source("HF_TOKEN", "prompt") == "typed"
    assert "HF_TOKEN" in gp.call_args.args[0]


def test_prompt_source_without_terminal_exits_2(mocker):
    mocker.patch("getpass.getpass", side_effect=EOFError)
    with pytest.raises(typer.Exit) as exc_info:
        secrets.read_source("HF_TOKEN", "prompt")
    assert exc_info.value.exit_code == 2


@pytest.mark.parametrize(
    ("source", "argv"),
    [("hf-cli", ["hf", "auth", "token"]), ("gh-cli", ["gh", "auth", "token"])],
)
def test_cli_sources_never_read_stdin(mocker, source, argv):
    mocker.patch("shutil.which", return_value=None)
    run = mocker.patch(
        "subprocess.run", return_value=MagicMock(stdout="tok\n", returncode=0)
    )
    assert secrets.read_source("X", source) == "tok"
    args, kwargs = run.call_args
    assert args[0] == argv
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] > 0


def test_cli_source_uses_resolved_launcher_path(mocker):
    """On Windows gh may be gh.cmd/gh.exe; run the path shutil.which found."""
    mocker.patch("shutil.which", return_value=r"C:\Tools\gh.cmd")
    run = mocker.patch(
        "subprocess.run", return_value=MagicMock(stdout="tok\n", returncode=0)
    )
    secrets.read_source("X", "gh-cli")
    assert run.call_args.args[0] == [r"C:\Tools\gh.cmd", "auth", "token"]


def test_cli_source_failure_returns_none(mocker):
    mocker.patch(
        "subprocess.run", side_effect=subprocess.CalledProcessError(1, ["gh"])
    )
    assert secrets.read_source("X", "gh-cli") is None


def test_hf_cli_falls_back_to_token_file(mocker, tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("hf_from_file\n")
    monkeypatch.setenv("HF_TOKEN_PATH", str(token_file))
    mocker.patch("subprocess.run", side_effect=FileNotFoundError("hf"))
    assert secrets.read_source("HF_TOKEN", "hf-cli") == "hf_from_file"


# --- Resolution --------------------------------------------------------------


def test_resolve_secrets_registers_redactions(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    history = MagicMock()
    out = secrets.resolve_secrets(["HF_TOKEN"], history)
    assert out == {"HF_TOKEN": "hf_secret"}
    history.add_redaction.assert_called_once_with("hf_secret")


def test_resolve_secrets_missing_value_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(typer.Exit) as exc_info:
        secrets.resolve_secrets(["HF_TOKEN"], MagicMock())
    assert exc_info.value.exit_code == 2
    assert "HF_TOKEN" in capsys.readouterr().err


def test_resolve_first_tries_sources_in_order(monkeypatch, mocker):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    mocker.patch(
        "subprocess.run", return_value=MagicMock(stdout="gh_tok\n", returncode=0)
    )
    history = MagicMock()
    value = secrets.resolve_first("GH_TOKEN", ["env:GH_TOKEN", "gh-cli"], history)
    assert value == "gh_tok"
    history.add_redaction.assert_called_once_with("gh_tok")


def test_resolve_first_returns_none_when_nothing_found(monkeypatch, mocker):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    mocker.patch("subprocess.run", side_effect=FileNotFoundError("gh"))
    assert secrets.resolve_first("GH_TOKEN", ["env:GH_TOKEN", "gh-cli"], MagicMock()) is None


def test_parse_source_option():
    assert secrets.parse_source("env") == "env"
    assert secrets.parse_source("env:X") == "env:X"
    with pytest.raises(typer.Exit):
        secrets.parse_source("hf_literal_token")
