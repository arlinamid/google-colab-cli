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

"""`colab ssh` checks for a key before it creates a runtime or starts ssh.

The key is read by the ProxyCommand child, so a missing key used to end as
ssh's own exit 255, and only after an interactive `colab ssh` had already
assigned a (billable) runtime.
"""

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import ssh as ssh_module

runner = CliRunner()


@pytest.fixture
def ssh_dir(tmp_path, monkeypatch):
    d = tmp_path / ".ssh"
    d.mkdir()
    monkeypatch.setattr(ssh_module, "_ssh_dir", lambda: d)
    return d


@pytest.fixture
def no_ssh_or_runtime(mocker, mock_common_state):
    session = MagicMock(url="https://abc.colab.dev", token="TOK")
    session.name = "s1"
    mock_common_state.store.get.return_value = session
    mock_common_state.resolve_session.return_value = "s1"
    return {
        "call": mocker.patch("subprocess.call", return_value=0),
        "interactive": mocker.patch.object(
            ssh_module, "_select_interactive_session"
        ),
        "resolve": mocker.patch.object(ssh_module, "_resolve_session"),
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["ssh"],
        ["ssh", "-s", "s1", "--", "whoami"],
        ["ssh", "-s", "s1", "-N", "-L", "8888"],
        ["forward", "-s", "s1", "8888"],
    ],
    ids=["interactive", "cmd", "forward-only", "forward"],
)
def test_no_key_exits_2_before_any_runtime_or_ssh(ssh_dir, no_ssh_or_runtime, argv):
    result = runner.invoke(app, argv)

    assert result.exit_code == 2, result.output
    assert "no SSH public key" in result.output
    no_ssh_or_runtime["call"].assert_not_called()
    no_ssh_or_runtime["interactive"].assert_not_called()
    no_ssh_or_runtime["resolve"].assert_not_called()


def test_no_default_key_suggests_other_keys(ssh_dir, no_ssh_or_runtime):
    (ssh_dir / "colab_ed25519.pub").write_text("ssh-ed25519 AAAA me@host\n")
    (ssh_dir / "old_rsa.pub").write_text("ssh-rsa AAAA me@host\n")

    result = runner.invoke(app, ["ssh", "-s", "s1", "--", "whoami"])

    assert result.exit_code == 2
    assert "colab_ed25519" in result.output
    assert "-i" in result.output
    # The server rejects RSA keys, so they are not suggested.
    assert "old_rsa" not in result.output


def test_missing_identity_file_exits_2(ssh_dir, no_ssh_or_runtime, tmp_path):
    result = runner.invoke(
        app, ["ssh", "-s", "s1", "-i", str(tmp_path / "nope"), "--", "whoami"]
    )

    assert result.exit_code == 2
    assert "nope" in result.output
    no_ssh_or_runtime["call"].assert_not_called()


@pytest.mark.parametrize("given", ["key", "key.pub"])
def test_identity_with_either_half_of_the_pair_passes(
    ssh_dir, no_ssh_or_runtime, tmp_path, given
):
    (tmp_path / "key").write_text("PRIVATE")
    no_ssh_or_runtime["resolve"].return_value = MagicMock(name="s1")

    ssh_module._require_ssh_key(str(tmp_path / given))  # does not exit


def test_default_key_passes(ssh_dir):
    (ssh_dir / "id_ecdsa.pub").write_text("ecdsa-sha2-nistp256 AAAA\n")

    ssh_module._require_ssh_key(None)  # does not exit
