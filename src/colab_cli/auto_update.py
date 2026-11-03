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

"""Auto-update subsystem.

Owns version detection, the update probes, the on-disk ``latest_version``
cache, and the upgrade-banner UX.

This is a fork of Google's CLI, so it watches two sources: the fork's latest
GitHub release, which is what gets offered and installed, and Google's PyPI
package, which is only announced when Google ships a newer base version.
Installing Google's package would replace the fork and drop its changes. The CLI's global
callback (``cli.py``) calls ``check_for_updates`` once per day and
``maybe_show_cached_banner`` on every other invocation; the
``colab update`` Typer command (``commands/utility.py``) delegates to
``check_for_updates``.
"""

import json
import platform
import subprocess
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as installed_version
from packaging.version import InvalidVersion, Version
from typing import Optional

import typer

from colab_cli.common import state
from colab_cli.state import Settings

# PyPI distribution name (different from the importable package name `colab`).
PYPI_PACKAGE_NAME = "google-colab-cli"
RELEASES_PAGE = "https://github.com/arlinamid/google-colab-cli/releases"
# GitHub's API requires a User-Agent header.
_USER_AGENT = "colab-cli update check"


# ---------- Version detection -------------------------------------------


def is_self_install_supported() -> bool:
    """Return True if self-install (--install) is supported on the current platform."""
    return platform.system() in ("Linux", "Darwin")


def get_app_version() -> str:
    """Return the installed package version, falling back to the git short hash."""
    try:
        return installed_version("google-colab-cli")
    except (PackageNotFoundError, InvalidVersion):
        pass

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
        ).strip()
    except Exception:
        return "unknown"


# ---------- Source fetchers ---------------------------------------------


def _parse_version(payload: Optional[dict]) -> Optional[str]:
    """Returns ``info.version`` from a PyPI-style payload, or None."""
    return (payload or {}).get("info", {}).get("version")


def _parse_fork_release(
    payload: Optional[dict],
) -> tuple[Optional[str], Optional[str]]:
    """Returns ``(version, wheel_url)`` from a GitHub "latest release" payload."""
    tag = (payload or {}).get("tag_name") or ""
    version = (tag[1:] if tag.startswith("v") else tag) or None
    wheel = next(
        (
            a.get("browser_download_url")
            for a in (payload or {}).get("assets", [])
            if str(a.get("name", "")).endswith(".whl")
        ),
        None,
    )
    return version, wheel


def _base_version(version: str) -> str:
    """The Google release a fork version builds on: ``0.7.5.1`` -> ``0.7.5``."""
    try:
        return ".".join(str(p) for p in Version(version).release[:3])
    except InvalidVersion:
        return version


def _fetch_pypi(url: str, quiet: bool) -> Optional[dict]:
    """Fetches and parses the JSON document at ``url`` (PyPI or GitHub API)."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        if not quiet:
            typer.echo(f"[colab] Warning: Failed to fetch update info: {e}")
        return None


# ---------- Version comparison ------------------------------------------


def _is_newer(candidate: Optional[str], current: str) -> bool:
    """True when ``candidate`` strictly exceeds ``current`` (PEP 440)."""
    if not candidate:
        return False
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return candidate != current


# ---------- UX ----------------------------------------------------------


def announce_upgrade(
    latest: str,
    current: str,
    install_cmd: str,
    *,
    show_disable_hint: bool = False,
) -> None:
    """Print the upgrade banner.

    ``show_disable_hint`` controls whether the trailing line that explains
    how to silence the auto-check is included. It is only added when the
    banner is shown unsolicited (the daily background fetch and the cached
    banner on subsequent invocations); explicit ``colab update`` calls
    omit it because the user already opted in to seeing the result.
    """
    typer.echo(
        f"\n[colab] A new version of Colab CLI is available: {latest} (current: {current})"
    )
    if is_self_install_supported() and ("pip" in install_cmd or "uv" in install_cmd):
        typer.echo("[colab] You can run 'colab update --install' to upgrade in place.")
    typer.echo(f"[colab] Run '{install_cmd}' to update.")
    if show_disable_hint:
        typer.echo(
            "[colab] To silence this check, set "
            '"enable_update_check": false in '
            "~/.config/colab-cli/settings.json"
        )
    typer.echo("")


# ---------- Orchestration -----------------------------------------------


def _is_uv_tool_install() -> bool:
    """True if this CLI runs from a `uv tool install` environment.

    The path is normalized first: on Windows it is ...\\uv\\tools\\..., so a
    plain "/uv/tools/" check missed it and suggested pip.
    """
    import sys

    return "/uv/tools/" in sys.executable.replace("\\", "/")


def _get_install_command(wheel_url: Optional[str]) -> str:
    """Return the command that installs the fork's release wheel."""
    if not wheel_url:
        return f"see {RELEASES_PAGE}"
    if _is_uv_tool_install():
        return f"uv tool install --force {wheel_url}"
    return f"pip install --upgrade {wheel_url}"


def announce_upstream(google_version: str, base: str) -> None:
    """Tell the user Google released a newer base; never offer to install it."""
    typer.echo(
        f"[colab] Google released Colab CLI {google_version}; this fork is based "
        f"on {base}. Watch {RELEASES_PAGE} for a fork release based on it."
    )


def check_for_updates(quiet: bool = False) -> None:
    """Check the fork's releases (and Google's PyPI) and report updates.

    An upgrade is offered only from the fork's latest GitHub release. Google's
    PyPI release is reported when it is newer than the Google version this
    install is based on. The disable-hint is appended to the banner only when
    ``quiet`` is True (the daily background fetch); explicit ``colab update``
    invocations (``quiet=False``) omit it because the user asked for the check.
    """
    settings = state.settings_store.load()
    current = get_app_version()

    try:
        fork_v, wheel = _parse_fork_release(
            _fetch_pypi(settings.fork_release_url, quiet)
        )
        google_v = _parse_version(_fetch_pypi(settings.update_url, quiet))

        if _is_newer(fork_v, current):
            announce_upgrade(
                fork_v,
                current,
                _get_install_command(wheel),
                show_disable_hint=quiet,
            )
        elif not quiet:
            suffix = f", latest: {fork_v}" if fork_v else ""
            typer.echo(f"[colab] Colab CLI is up to date (version: {current}{suffix}).")

        # Compare Google's release with the base of the newest fork version we
        # know of, so a pending fork upgrade that already includes it is quiet.
        newest = fork_v if _is_newer(fork_v, current) else current
        if _is_newer(google_v, _base_version(newest)):
            announce_upstream(google_v, _base_version(current))

        # Cache the highest observed fork version; never downgrade.
        cached = settings.latest_version or "0"
        if _is_newer(fork_v, cached):
            settings.latest_version = fork_v
            settings.latest_wheel_url = wheel

        settings.last_check = datetime.now(timezone.utc)
        state.settings_store.save(settings)

    except Exception as e:
        if not quiet:
            typer.echo(f"[colab] Failed to check for updates: {e}")


# ---------- Background hooks (called from cli.py) -----------------------


def _is_throttled(settings: Settings, *, now: Optional[datetime] = None) -> bool:
    """True if the once-per-day fetch should be skipped."""
    if settings.last_check is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - settings.last_check).days < 1


def maybe_show_cached_banner(settings: Settings) -> None:
    """Print the cached upgrade banner if the cache reports a newer version.

    Called from the global CLI callback when the daily fetch is throttled.
    The banner uses a generic ``colab update`` install hint because the
    cache does not record which source supplied the version; the disable
    hint is shown because this is unsolicited output.
    """
    if not settings.latest_version:
        return
    current = get_app_version()
    if not _is_newer(settings.latest_version, current):
        return
    announce_upgrade(
        settings.latest_version,
        current,
        "colab update",
        show_disable_hint=True,
    )


def run_background_check() -> None:
    """Entry point for the global CLI callback.

    Performs either the daily fetch (which writes the cache) or, if
    throttled, surfaces the cached banner. Honors the
    ``enable_update_check`` master switch.
    """
    settings = state.settings_store.load()
    if not settings.enable_update_check:
        return
    if _is_throttled(settings):
        maybe_show_cached_banner(settings)
    else:
        check_for_updates(quiet=True)


# ---------- Self-install ------------------------------------------------


def self_install() -> None:
    """Install the fork's latest release wheel in place, detecting uv vs pip."""
    import sys

    wheel_url = state.settings_store.load().latest_wheel_url
    if not wheel_url:
        typer.echo(
            f"[colab] No release wheel is known; download it from {RELEASES_PAGE}.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Installed via `uv tool install`: use `uv` to replace it.
    if _is_uv_tool_install():
        cmd = ["uv", "tool", "install", "--force", wheel_url]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-U", wheel_url]

    typer.echo(f"[colab] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
