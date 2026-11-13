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

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import typer

from colab_cli.client import ListedAssignment
from colab_cli.common import State
from colab_cli.state import SessionState, StateStore


def _assignment(endpoint, token, expires_in=3600):
    return ListedAssignment.model_validate(
        {
            "accelerator": "NONE",
            "endpoint": endpoint,
            "variant": 0,
            "machineShape": 0,
            "runtimeProxyInfo": {
                "token": token,
                "tokenExpiresInSeconds": expires_in,
                "url": "https://new-url",
            },
        }
    )


def _session(expires_at):
    return SessionState(
        name="s1",
        token="old-token",
        url="https://old-url",
        endpoint="ep1",
        token_expires_at=expires_at,
    )


@pytest.fixture
def real_state(tmp_path):
    st = State()
    st._store = StateStore(str(tmp_path / "sessions.json"))
    st._client = MagicMock()
    st._history = MagicMock()
    return st


def test_get_session_fresh_token_skips_refresh(real_state):
    real_state.store.add(_session(datetime.now(timezone.utc) + timedelta(minutes=30)))

    s = real_state.get_session("s1")

    assert s.token == "old-token"
    real_state.client.list_assignments.assert_not_called()


@pytest.mark.parametrize(
    "expires_at",
    [
        None,
        datetime.now(timezone.utc) - timedelta(minutes=1),
        datetime.now(timezone.utc) + timedelta(minutes=2),
    ],
    ids=["missing", "expired", "within_margin"],
)
def test_get_session_refreshes_stale_token(real_state, expires_at):
    real_state.store.add(_session(expires_at))
    real_state.client.list_assignments.return_value = [
        _assignment("other", "wrong-token"),
        _assignment("ep1", "new-token"),
    ]

    s = real_state.get_session("s1")

    assert s.token == "new-token"
    assert s.url == "https://new-url"
    assert s.token_expires_at > datetime.now(timezone.utc) + timedelta(minutes=55)
    persisted = real_state.store.get("s1")
    assert persisted.token == "new-token"
    assert persisted.token_expires_at == s.token_expires_at


def test_get_session_prunes_when_assignment_gone(real_state):
    real_state.store.add(_session(None))
    real_state.client.list_assignments.return_value = []

    assert real_state.get_session("s1", ignore_missing_session=True) is None
    assert real_state.store.get("s1") is None


def test_get_session_unknown_name_exits(real_state, capsys):
    with pytest.raises(typer.Exit) as exc:
        real_state.get_session("nope")

    assert exc.value.exit_code == 1
    assert "Session 'nope' not found." in capsys.readouterr().out
    real_state.client.list_assignments.assert_not_called()


def test_get_session_unknown_name_ignored(real_state):
    assert real_state.get_session("nope", ignore_missing_session=True) is None


def test_sync_sessions_updates_tokens(real_state):
    real_state.store.add(_session(None))
    real_state.client.list_assignments.return_value = [_assignment("ep1", "new-token")]

    sessions, _ = real_state.sync_sessions()

    assert sessions["s1"].token == "new-token"
    assert real_state.store.get("s1").token == "new-token"
    assert real_state.store.get("s1").token_expires_at is not None


def test_redact_secrets_masks_query_tokens_and_bearer():
    from colab_cli.common import redact_secrets

    text = (
        "500 for url: https://x.colab.dev/api?authuser=0"
        "&colab-runtime-proxy-token=eyJabc.def&x=1 Authorization: Bearer abcdefghijkl"
    )
    out = redact_secrets(text)
    assert "eyJabc.def" not in out
    assert "abcdefghijkl" not in out
    assert "authuser=0" in out


def test_colab_log_rotates_and_survives_a_locked_file(tmp_path, mocker):
    import logging

    from colab_cli import common

    path = tmp_path / "colab.log"
    handler = common._RotatingFileHandler(str(path), maxBytes=200, backupCount=2, encoding="utf-8", delay=True)
    logger = logging.getLogger("colab-rotate-test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for i in range(30):
            logger.info("line %d %s", i, "x" * 40)
        assert (tmp_path / "colab.log.1").exists() and not (tmp_path / "colab.log.3").exists()
        assert path.stat().st_size <= 400

        # Another process has the file open (Windows): the rollover is skipped.
        mocker.patch.object(common.RotatingFileHandler, "doRollover", side_effect=PermissionError("in use"))
        logger.info("still logged %s", "y" * 300)
        handler.flush()
        assert "still logged" in path.read_text(encoding="utf-8")
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert common.LOG_MAX_BYTES == 10 * 1024 * 1024


def test_setup_logging_twice_keeps_one_handler_each(tmp_path, monkeypatch):
    """Each call replaced nothing: in one process (the tests) every line was written N times."""
    import logging

    from colab_cli import common

    monkeypatch.setattr(common.os.path, "expanduser", lambda p: str(tmp_path / "cfg"))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        common.setup_logging(log_to_stderr=True)
        common.setup_logging(log_to_stderr=True)
        common.setup_logging(log_to_stderr=False)
        new = [h for h in root.handlers if h not in before]
        assert len(new) == 1 and isinstance(new[0], common._RotatingFileHandler)
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
            h.close()


def test_cli_commands_in_tests_do_not_write_the_real_colab_log():
    from colab_cli import cli, common

    assert cli.setup_logging is not common.setup_logging  # conftest's isolate_colab_log


def test_every_command_sets_up_logging(mocker):
    from typer.testing import CliRunner

    from colab_cli import cli

    setup = mocker.patch.object(cli, "setup_logging")
    CliRunner().invoke(cli.app, ["--logtostderr", "version"])
    setup.assert_called_once_with(True)
