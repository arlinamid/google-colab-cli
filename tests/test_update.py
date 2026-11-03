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

import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.state import Settings

runner = CliRunner()


# ---------- Shared fixtures ----------------------------------------------


@pytest.fixture
def fake_settings(mocker):
    """Return a builder that mocks the SettingsStore with caller-provided overrides.

    The mocked ``load()`` returns a fresh copy of the seeded ``Settings`` on
    each call, mirroring real on-disk reads. Mutations made by
    ``check_for_updates`` to the loaded copy are captured via
    ``SettingsStore.save`` (also mocked) and re-applied to the seed so that
    subsequent ``load()`` calls reflect them — matching the persistence
    behavior end-to-end.
    """

    def _build(**overrides):
        kwargs = {
            "update_url": "http://google.test",
            "fork_release_url": "http://fork.test",
            "last_check": None,
            **overrides,
        }
        # Wrap the live object in a list so the inner closures can rebind it
        # while still letting tests read the latest state via ``current[0]``.
        current = [Settings(**kwargs)]

        def _load():
            return current[0].model_copy()

        def _save(updated):
            current[0] = updated.model_copy()

        mocker.patch("colab_cli.state.SettingsStore.load", side_effect=_load)
        mocker.patch("colab_cli.state.SettingsStore.save", side_effect=_save)

        # Expose a `.current` accessor returning the latest persisted state.
        # The returned proxy supports attribute access for convenience.
        class _Proxy:
            def __getattr__(self, name):
                return getattr(current[0], name)

        return _Proxy()

    return _build


WHEEL = "https://github.com/o/r/releases/download/v{v}/google_colab_cli-{v}-py3-none-any.whl"


def fork_payload(v, *, wheel=True):
    """A GitHub "latest release" response for fork version ``v``."""
    assets = []
    if wheel:
        assets = [
            {"name": f"google_colab_cli-{v}.tar.gz", "browser_download_url": "x"},
            {
                "name": f"google_colab_cli-{v}-py3-none-any.whl",
                "browser_download_url": WHEEL.format(v=v),
            },
        ]
    return {"tag_name": f"v{v}", "assets": assets}


@pytest.fixture
def mock_sources(mocker):
    """Stub ``urlopen``: the fork's GitHub release and Google's PyPI JSON.

    ``fork``/``google`` are versions (None: that source fails); ``error``
    makes every fetch fail.
    """

    def _mock(fork=None, google=None, *, error=None, fork_wheel=True):
        def _open(url, timeout=None):
            target = getattr(url, "full_url", url)
            if error is not None:
                raise error
            if "fork.test" in target:
                if fork is None:
                    raise OSError("fork down")
                payload = fork_payload(fork, wheel=fork_wheel)
            elif "google.test" in target:
                if google is None:
                    raise OSError("pypi down")
                payload = {"info": {"version": google}}
            else:
                raise AssertionError(f"unexpected URL {target}")
            response = mocker.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                payload
            ).encode("utf-8")
            return response

        return mocker.patch("urllib.request.urlopen", side_effect=_open)

    return _mock


@pytest.fixture
def app_version(mocker):
    """Pin the locally-installed CLI version to the requested value."""

    def _set(v):
        mocker.patch("colab_cli.auto_update.get_app_version", return_value=v)

    return _set


# ---------- Fork release source (installable updates) --------------------------------------------------


@pytest.mark.parametrize(
    "current, fork_version, expected_message",
    [
        # Same version: up-to-date with latest = current.
        ("1.0.0", "1.0.0", "up to date (version: 1.0.0, latest: 1.0.0)"),
        # The fork release is older than installed: still up to date.
        ("1.1.0", "1.0.0", "up to date (version: 1.1.0, latest: 1.0.0)"),
    ],
)
def test_fork_no_upgrade(
    app_version, fake_settings, mock_sources, current, fork_version, expected_message
):
    app_version(current)
    mock_sources(fork=fork_version)
    fake_settings()

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert expected_message in result.output


def test_fork_upgrade_uses_pip_hint(mocker, app_version, fake_settings, mock_sources):
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch("sys.executable", "/usr/bin/python")
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Linux")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "available: 1.1.0 (current: 1.0.0)" in result.output
    assert "You can run 'colab update --install' to upgrade in place." in result.output
    assert f"Run 'pip install --upgrade {WHEEL.format(v="1.1.0")}' to update." in result.output

    idx_install = result.output.find(
        "You can run 'colab update --install' to upgrade in place."
    )
    idx_pip = result.output.find(
        f"Run 'pip install --upgrade {WHEEL.format(v="1.1.0")}' to update."
    )
    assert idx_install < idx_pip


def test_fork_upgrade_uses_uv_hint(mocker, app_version, fake_settings, mock_sources):
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch(
        "sys.executable", "/home/user/.local/share/uv/tools/google-colab-cli/bin/python"
    )
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Linux")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "available: 1.1.0 (current: 1.0.0)" in result.output
    assert "You can run 'colab update --install' to upgrade in place." in result.output
    assert f"Run 'uv tool install --force {WHEEL.format(v="1.1.0")}' to update." in result.output

    idx_install = result.output.find(
        "You can run 'colab update --install' to upgrade in place."
    )
    idx_uv = result.output.find(f"Run 'uv tool install --force {WHEEL.format(v="1.1.0")}' to update.")
    assert idx_install < idx_uv


def test_fork_upgrade_uses_pip_hint_macos(
    mocker, app_version, fake_settings, mock_sources
):
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch("sys.executable", "/usr/bin/python")
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Darwin")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "available: 1.1.0 (current: 1.0.0)" in result.output
    assert "You can run 'colab update --install' to upgrade in place." in result.output
    assert f"Run 'pip install --upgrade {WHEEL.format(v="1.1.0")}' to update." in result.output

    idx_install = result.output.find(
        "You can run 'colab update --install' to upgrade in place."
    )
    idx_pip = result.output.find(
        f"Run 'pip install --upgrade {WHEEL.format(v="1.1.0")}' to update."
    )
    assert idx_install < idx_pip


def test_fork_upgrade_uses_pip_hint_windows(
    mocker, app_version, fake_settings, mock_sources
):
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch("sys.executable", "/usr/bin/python")
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Windows")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "available: 1.1.0 (current: 1.0.0)" in result.output
    assert (
        "You can run 'colab update --install' to upgrade in place." not in result.output
    )
    assert f"Run 'pip install --upgrade {WHEEL.format(v="1.1.0")}' to update." in result.output


def test_explicit_update_omits_disable_hint(app_version, fake_settings, mock_sources):
    """`colab update` is explicit user opt-in; the 'how to silence' line
    should NOT appear (it would be condescending after the user just asked)."""
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "available: 1.1.0" in result.output
    assert "To silence this check" not in result.output
    assert "enable_update_check" not in result.output


def test_background_check_includes_disable_hint(app_version, fake_settings, mock_sources):
    """The daily background fetch (triggered by any non-quiet command)
    DOES include the 'how to silence' line so users have an obvious opt-out."""
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings(last_check=datetime.now(timezone.utc) - timedelta(days=2))

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert "available: 1.1.0" in result.output
    assert "To silence this check" in result.output
    assert '"enable_update_check": false' in result.output


def test_cached_banner_includes_disable_hint(mocker, app_version, fake_settings):
    """The cached banner shown between fetches is unsolicited; include the hint."""
    app_version("1.0.0")
    fake_settings(
        last_check=datetime.now(timezone.utc) - timedelta(hours=1),
        latest_version="1.2.0",
    )
    mocker.patch("colab_cli.auto_update.check_for_updates")

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert "available: 1.2.0" in result.output
    assert "To silence this check" in result.output


# ---------- Resilience --------------------------------------------------


def test_fork_fetch_failure_omits_latest(app_version, fake_settings, mock_sources):
    app_version("1.0.0")
    mock_sources(error=OSError("network down"))
    fake_settings()

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "Colab CLI is up to date (version: 1.0.0)" in result.output
    assert "latest:" not in result.output


# ---------- Auto-update wiring ------------------------------------------


def test_auto_update_runs_when_stale(app_version, fake_settings, mock_sources):
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings(last_check=datetime.now(timezone.utc) - timedelta(days=2))

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert "available: 1.1.0 (current: 1.0.0)" in result.output


def test_auto_update_skips_when_recent(mocker, fake_settings):
    fake_settings(last_check=datetime.now(timezone.utc) - timedelta(hours=1))
    mock_check = mocker.patch("colab_cli.auto_update.check_for_updates")

    runner.invoke(app, ["sessions"])
    assert mock_check.call_count == 0


def test_auto_update_runs_on_first_invocation(mocker, app_version, fake_settings):
    app_version("1.0.0")
    fake_settings()  # last_check=None
    mock_check = mocker.patch("colab_cli.auto_update.check_for_updates")

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert mock_check.call_count == 1


# ---------- `latest_version` cache --------------------------------------


def test_settings_default_latest_version_is_none():
    assert Settings().latest_version is None


def test_check_persists_latest_version_from_fork(app_version, fake_settings, mock_sources):
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    settings = fake_settings()

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert settings.latest_version == "1.1.0"


def test_check_preserves_latest_version_on_fetch_failure(
    app_version, fake_settings, mock_sources
):
    """If the fetch fails, the cached `latest_version` must NOT be cleared."""
    app_version("1.0.0")
    mock_sources(error=OSError("network down"))
    settings = fake_settings(latest_version="1.7.0")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert settings.latest_version == "1.7.0"


def test_check_does_not_downgrade_latest_version(app_version, fake_settings, mock_sources):
    """A subsequent fetch returning an older version must not overwrite the cache."""
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    settings = fake_settings(latest_version="2.0.0")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert settings.latest_version == "2.0.0"


# ---------- Cached banner on every invocation ---------------------------


def test_cached_banner_shown_when_throttled(mocker, app_version, fake_settings):
    """When the daily fetch is skipped, a cached newer `latest_version` still
    triggers the upgrade banner — without re-fetching."""
    app_version("1.0.0")
    fake_settings(
        last_check=datetime.now(timezone.utc) - timedelta(hours=1),
        latest_version="1.2.0",
    )
    mock_check = mocker.patch("colab_cli.auto_update.check_for_updates")

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert mock_check.call_count == 0  # throttle still active
    assert "available: 1.2.0 (current: 1.0.0)" in result.output


def test_cached_banner_suppressed_when_up_to_date(mocker, app_version, fake_settings):
    """If the cached `latest_version` is not newer than the current install,
    no banner should appear."""
    app_version("1.2.0")
    fake_settings(
        last_check=datetime.now(timezone.utc) - timedelta(hours=1),
        latest_version="1.2.0",
    )
    mock_check = mocker.patch("colab_cli.auto_update.check_for_updates")

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert mock_check.call_count == 0
    assert "A new version" not in result.output


def test_cached_banner_skipped_for_update_subcommand(
    mocker, app_version, fake_settings
):
    """`colab update` does its own fetch + announce; the callback must not
    duplicate the banner from the cache."""
    app_version("1.0.0")
    fake_settings(
        last_check=datetime.now(timezone.utc) - timedelta(hours=1),
        latest_version="1.2.0",
    )
    # Stub check_for_updates so we can assert the callback didn't print twice.
    mock_check = mocker.patch(
        "colab_cli.auto_update.check_for_updates", return_value=None
    )

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    # The check ran (forced by `update`) but the cached banner from the
    # callback must NOT have fired.
    assert mock_check.call_count == 1
    assert result.output.count("A new version") == 0


def test_cached_banner_suppressed_when_update_check_disabled(
    mocker, app_version, fake_settings
):
    """`enable_update_check=False` is a global opt-out: no fetch AND no cached
    banner. The user has explicitly disabled the update-check subsystem."""
    app_version("1.0.0")
    fake_settings(
        enable_update_check=False,
        latest_version="1.2.0",
    )
    mock_check = mocker.patch("colab_cli.auto_update.check_for_updates")

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert mock_check.call_count == 0
    assert "A new version" not in result.output


# ---------- Quiet subcommands skip the auto-update banner ---------------


@pytest.mark.parametrize("subcommand", ["version", "log", "pay", "help"])
def test_background_check_skipped_for_quiet_subcommands(
    mocker, app_version, fake_settings, subcommand
):
    """`version`, `log`, `pay`, and `help` are short-lived informational
    commands. Their output should never be polluted by the upgrade banner —
    no daily fetch and no cached banner should fire from the global
    callback. (`colab update` is exempted separately because it runs its
    own check.)"""
    app_version("1.0.0")
    fake_settings(
        # Force the daily fetch to be DUE: if the callback runs at all, it
        # would call check_for_updates() and we'd see the assertion fail.
        last_check=datetime.now(timezone.utc) - timedelta(days=2),
        # Also seed a cached newer version so we'd see the cached banner if
        # the callback fell through to maybe_show_cached_banner instead.
        latest_version="1.2.0",
    )
    # Patch `webbrowser.open` to keep `colab pay` from launching a browser
    # in the test environment.
    mocker.patch("webbrowser.open")
    mock_check = mocker.patch("colab_cli.auto_update.check_for_updates")

    result = runner.invoke(app, [subcommand])
    assert result.exit_code == 0, result.output
    assert mock_check.call_count == 0, (
        f"`colab {subcommand}` should NOT trigger the daily update fetch."
    )
    assert "A new version" not in result.output, (
        f"`colab {subcommand}` should NOT print the cached upgrade banner."
    )


# ---------- `--install` self-install flag -------------------------------


def test_install_flag_default_does_not_install(
    mocker, app_version, fake_settings, mock_sources
):
    """Without `--install`, no install command is invoked even when a newer
    version is available on PyPI."""
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    run = mocker.patch("colab_cli.auto_update.subprocess.run")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert run.call_count == 0


def test_install_flag_runs_pip_install_upgrade(
    mocker, app_version, fake_settings, mock_sources
):
    """`colab update --install` shells out to `pip install -U google-colab-cli`
    when PyPI reports a newer version."""
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Linux")
    mocker.patch("sys.executable", "/usr/bin/python")
    run = mocker.patch(
        "colab_cli.auto_update.subprocess.run",
        return_value=mocker.Mock(returncode=0),
    )

    result = runner.invoke(app, ["update", "--install"])
    assert result.exit_code == 0
    assert run.call_count == 1
    args, _ = run.call_args
    # Use sys.executable to avoid PATH ambiguity / virtualenv mixups.
    cmd = args[0]
    assert cmd == ["/usr/bin/python", "-m", "pip", "install", "-U", WHEEL.format(v="1.1.0")]


def test_install_flag_runs_uv_tool_install(
    mocker, app_version, fake_settings, mock_sources
):
    """`colab update --install` shells out to `uv tool install -U google-colab-cli`
    when sys.executable contains '/uv/'."""
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Linux")
    mocker.patch(
        "sys.executable", "/home/user/.local/share/uv/tools/google-colab-cli/bin/python"
    )
    run = mocker.patch(
        "colab_cli.auto_update.subprocess.run",
        return_value=mocker.Mock(returncode=0),
    )

    result = runner.invoke(app, ["update", "--install"])
    assert result.exit_code == 0
    assert run.call_count == 1
    args, _ = run.call_args
    cmd = args[0]
    assert cmd == ["uv", "tool", "install", "--force", WHEEL.format(v="1.1.0")]


def test_install_flag_errors_on_unsupported_platform(
    mocker, app_version, fake_settings, mock_sources
):
    """`--install` is gated to Linux and macOS; on other platforms the command must
    exit non-zero with an explanatory message and skip the pip subprocess."""
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Windows")
    run = mocker.patch("colab_cli.auto_update.subprocess.run")

    result = runner.invoke(app, ["update", "--install"])
    assert result.exit_code != 0
    assert run.call_count == 0
    assert "only supported on Linux and macOS" in result.output


def test_install_flag_runs_on_macos(mocker, app_version, fake_settings, mock_sources):
    """`colab update --install` shells out to pip/uv when running on macOS."""
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Darwin")
    mocker.patch("sys.executable", "/usr/bin/python")
    run = mocker.patch(
        "colab_cli.auto_update.subprocess.run",
        return_value=mocker.Mock(returncode=0),
    )

    result = runner.invoke(app, ["update", "--install"])
    assert result.exit_code == 0
    assert run.call_count == 1
    args, _ = run.call_args
    cmd = args[0]
    assert cmd == ["/usr/bin/python", "-m", "pip", "install", "-U", WHEEL.format(v="1.1.0")]


def test_install_flag_no_op_when_already_up_to_date(
    mocker, app_version, fake_settings, mock_sources
):
    """`--install` should not invoke pip when the cached `latest_version`
    is not newer than the current install."""
    app_version("1.1.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Linux")
    run = mocker.patch("colab_cli.auto_update.subprocess.run")

    result = runner.invoke(app, ["update", "--install"])
    assert result.exit_code == 0
    assert run.call_count == 0


def test_install_flag_propagates_pip_failure(
    mocker, app_version, fake_settings, mock_sources
):
    """If `pip install -U` exits non-zero, `colab update --install` must too."""
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Linux")
    mocker.patch(
        "colab_cli.auto_update.subprocess.run",
        return_value=mocker.Mock(returncode=2),
    )

    result = runner.invoke(app, ["update", "--install"])
    assert result.exit_code == 2


# ---------- Google's PyPI release: notice only, never installed ----------


@pytest.mark.parametrize(
    "current, base", [("0.7.5.1", "0.7.5"), ("0.7.5.2.dev3+gabc", "0.7.5"), ("1.0.0", "1.0.0")]
)
def test_base_version(current, base):
    from colab_cli import auto_update

    assert auto_update._base_version(current) == base


def test_newer_google_release_is_announced_not_offered(
    mocker, app_version, fake_settings, mock_sources
):
    """Google's 0.7.6 must not be offered as an upgrade: installing it would
    replace the fork with Google's package and drop the fork's changes."""
    app_version("0.7.5.1")
    mock_sources(fork="0.7.5.1", google="0.7.6")
    settings = fake_settings()
    run = mocker.patch("colab_cli.auto_update.subprocess.run")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "Colab CLI is up to date (version: 0.7.5.1, latest: 0.7.5.1)" in result.output
    assert "Google released Colab CLI 0.7.6" in result.output
    assert "based on 0.7.5" in result.output
    assert "A new version" not in result.output
    assert "google-colab-cli'" not in result.output
    # Only fork versions are cached (and installed), never Google's.
    assert settings.latest_version == "0.7.5.1"
    assert run.call_count == 0


def test_google_release_the_fork_is_based_on_is_silent(
    app_version, fake_settings, mock_sources
):
    app_version("0.7.5.1")
    mock_sources(fork="0.7.5.1", google="0.7.5")
    fake_settings()

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "Google released" not in result.output


def test_fork_upgrade_and_google_notice_together(
    mocker, app_version, fake_settings, mock_sources
):
    app_version("0.7.5.1")
    mock_sources(fork="0.7.6.1", google="0.7.6")
    fake_settings()
    mocker.patch("sys.executable", "/usr/bin/python")

    result = runner.invoke(app, ["update"])
    assert "available: 0.7.6.1 (current: 0.7.5.1)" in result.output
    # The fork release is already based on Google's 0.7.6: no extra notice.
    assert "Google released" not in result.output


def test_google_notice_in_background_check(app_version, fake_settings, mock_sources):
    app_version("0.7.5.1")
    mock_sources(fork="0.7.5.1", google="0.7.6")
    fake_settings(last_check=datetime.now(timezone.utc) - timedelta(days=2))

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert "Google released Colab CLI 0.7.6" in result.output


def test_google_fetch_failure_still_checks_the_fork(
    mocker, app_version, fake_settings, mock_sources
):
    app_version("1.0.0")
    mock_sources(fork="1.1.0", google=None)
    fake_settings()
    mocker.patch("sys.executable", "/usr/bin/python")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "available: 1.1.0 (current: 1.0.0)" in result.output


# ---------- Fork release details -----------------------------------------


def test_fork_release_url_points_at_this_fork():
    assert Settings().fork_release_url == (
        "https://api.github.com/repos/arlinamid/google-colab-cli/releases/latest"
    )
    # Google's PyPI package stays the upstream reference.
    assert Settings().update_url == "https://pypi.org/pypi/google-colab-cli/json"


def test_check_caches_the_wheel_url(app_version, fake_settings, mock_sources):
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    settings = fake_settings()

    runner.invoke(app, ["update"])
    assert settings.latest_wheel_url == WHEEL.format(v="1.1.0")


def test_release_without_wheel_points_to_the_releases_page(
    mocker, app_version, fake_settings, mock_sources
):
    app_version("1.0.0")
    mock_sources(fork="1.1.0", fork_wheel=False)
    fake_settings()

    result = runner.invoke(app, ["update"])
    assert "available: 1.1.0" in result.output
    assert "github.com/arlinamid/google-colab-cli/releases" in result.output


def test_install_without_a_known_wheel_fails_cleanly(
    mocker, app_version, fake_settings, mock_sources
):
    app_version("1.0.0")
    mock_sources(fork="1.1.0", fork_wheel=False)
    fake_settings()
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Linux")
    run = mocker.patch("colab_cli.auto_update.subprocess.run")

    result = runner.invoke(app, ["update", "--install"])
    assert result.exit_code == 1
    assert run.call_count == 0
    assert "releases" in result.output


def test_github_api_request_sends_a_user_agent(app_version, fake_settings, mock_sources):
    app_version("1.0.0")
    opened = mock_sources(fork="1.0.0", google="1.0.0")
    fake_settings()

    runner.invoke(app, ["update"])
    requests_made = [c.args[0] for c in opened.call_args_list]
    fork_req = next(r for r in requests_made if "fork.test" in getattr(r, "full_url", r))
    assert fork_req.get_header("User-agent").startswith("colab-cli")


@pytest.mark.parametrize(
    "executable",
    [
        r"C:\Users\me\AppData\Roaming\uv\tools\google-colab-cli\Scripts\python.exe",
        "/home/user/.local/share/uv/tools/google-colab-cli/bin/python",
    ],
)
def test_uv_tool_install_detected_on_every_os(
    mocker, app_version, fake_settings, mock_sources, executable
):
    """Windows paths use backslashes; the uv tool install must still be seen."""
    app_version("1.0.0")
    mock_sources(fork="1.1.0")
    fake_settings()
    mocker.patch("sys.executable", executable)
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Windows")

    result = runner.invoke(app, ["update"])
    assert f"Run 'uv tool install --force {WHEEL.format(v='1.1.0')}' to update." in result.output
