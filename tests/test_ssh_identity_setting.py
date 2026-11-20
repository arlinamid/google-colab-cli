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

"""A default SSH key from settings.json or COLAB_CLI_SSH_IDENTITY.

The tray's SSH and forward buttons run `colab ssh` / `colab forward` without
-i, so a key with another name than id_ed25519 / id_ecdsa could not be used.
"""

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import ssh as ssh_module
from colab_cli.state import Settings

runner = CliRunner()


@pytest.fixture
def key(tmp_path):
    priv = tmp_path / "colab_ed25519"
    priv.write_text("private")
    (tmp_path / "colab_ed25519.pub").write_text("ssh-ed25519 AAAA test")
    return str(priv)


@pytest.fixture
def forward(mock_common_state, mocker, monkeypatch):
    monkeypatch.delenv("COLAB_CLI_SSH_IDENTITY", raising=False)
    mock_common_state.settings_store.load.return_value = Settings()
    mocker.patch.object(ssh_module, "_resolve_session", return_value=MagicMock())
    return mocker.patch.object(ssh_module, "_run_forward_only", return_value=0)


def _identity(run):
    return run.call_args.args[1]


def test_settings_ssh_identity_is_the_default_key(mock_common_state, forward, key):
    mock_common_state.settings_store.load.return_value = Settings(ssh_identity=key)
    result = runner.invoke(app, ["forward", "-s", "s1", "8080"])
    assert result.exit_code == 0, result.output
    assert _identity(forward) == key


def test_env_var_wins_over_settings(mock_common_state, forward, key, monkeypatch):
    mock_common_state.settings_store.load.return_value = Settings(ssh_identity="~/.ssh/other")
    monkeypatch.setenv("COLAB_CLI_SSH_IDENTITY", key)
    result = runner.invoke(app, ["forward", "-s", "s1", "8080"])
    assert result.exit_code == 0, result.output
    assert _identity(forward) == key


def test_dash_i_wins(mock_common_state, forward, key, monkeypatch):
    monkeypatch.setenv("COLAB_CLI_SSH_IDENTITY", "~/.ssh/other")
    result = runner.invoke(app, ["forward", "-s", "s1", "-i", key, "8080"])
    assert result.exit_code == 0, result.output
    assert _identity(forward) == key


def test_no_key_message_names_the_setting(tmp_path, monkeypatch):
    message = ssh_module._no_key_message(tmp_path)
    assert "ssh_identity" in message and "COLAB_CLI_SSH_IDENTITY" in message
