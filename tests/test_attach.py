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


"""`colab attach`: a runtime opened elsewhere (Colab web UI) as a local session."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.state import SessionState

runner = CliRunner()
EXPIRES = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _assignment(endpoint, accelerator="NONE", variant="DEFAULT"):
    a = MagicMock(endpoint=endpoint)
    a.accelerator.value = accelerator
    a.variant.name = variant
    a.machine_shape.name = "STANDARD"
    a.runtime_proxy_info.token = f"tok-{endpoint}"
    a.runtime_proxy_info.url = f"https://8080-{endpoint}.prod.colab.dev"
    a.runtime_proxy_info.expires_at.return_value = EXPIRES
    return a


@pytest.fixture
def server(mock_common_state):
    """One local session (`mine`) and one runtime opened in the web UI."""
    mine = SessionState(name="mine", token="t", url="u", endpoint="ep-mine")
    mock_common_state.sync_sessions.return_value = (
        {"mine": mine},
        [_assignment("ep-mine"), _assignment("m-s-use1c1-239y7roywmzti", "T4", "GPU")],
    )
    mock_common_state.store.get.side_effect = lambda n: mine if n == "mine" else None
    return mock_common_state


def _saved(server):
    server.store.add.assert_called_once()
    return server.store.add.call_args.args[0]


def test_attach_saves_the_runtime_as_a_session(server):
    result = runner.invoke(app, ["attach", "--endpoint", "m-s-use1c1-239y7roywmzti", "-s", "web"])

    assert result.exit_code == 0, result.output
    s = _saved(server)
    assert (s.name, s.endpoint) == ("web", "m-s-use1c1-239y7roywmzti")
    assert s.token == "tok-m-s-use1c1-239y7roywmzti"
    assert s.url == "https://8080-m-s-use1c1-239y7roywmzti.prod.colab.dev"
    assert (s.accelerator, s.variant, s.machine_shape) == ("T4", "GPU", "STANDARD")
    assert s.token_expires_at == EXPIRES and s.kernel_id is None
    server.history.log_event.assert_called_once()
    assert server.history.log_event.call_args.args[:2] == ("web", "session_attached")
    assert "colab exec -s web" in result.output
    assert "own kernel" in result.output and "stops it there too" in result.output


def test_attach_names_the_session_after_the_endpoint(server):
    result = runner.invoke(app, ["attach", "--endpoint", "m-s-use1c1-239y7roywmzti"])
    assert result.exit_code == 0, result.output
    assert _saved(server).name == "web-239y7r"


def test_attach_unknown_endpoint(server):
    result = runner.invoke(app, ["attach", "--endpoint", "ep-gone"])
    assert result.exit_code == 1
    assert "ep-gone" in result.output
    server.store.add.assert_not_called()


def test_attach_a_runtime_that_already_has_a_session(server):
    result = runner.invoke(app, ["attach", "--endpoint", "ep-mine"])
    assert result.exit_code == 0
    assert "already the session 'mine'" in result.output
    server.store.add.assert_not_called()


def test_attach_does_not_overwrite_another_session(server):
    result = runner.invoke(app, ["attach", "--endpoint", "m-s-use1c1-239y7roywmzti", "-s", "mine"])
    assert result.exit_code == 2
    server.store.add.assert_not_called()
