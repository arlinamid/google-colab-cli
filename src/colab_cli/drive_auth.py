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

"""Google Drive credential propagation for ``colab drivemount``.

The kernel's ``drive.mount()`` blocks in ``google.colab._message.blocking_request``
until the frontend answers the ``dfs_ephemeral`` ``colab_request`` with an
``input_reply`` whose value is ``{"type": "colab_reply", "colab_msg_id": ...}``.
The default ``timeout_ms`` is 120000, and that budget includes the time the
user spends in the browser. This module keeps that wait aligned with the CLI's
interactive timeout and turns the multi-account HTTP 400 into an actionable
message instead of a raw HTML error page.
"""

import json
import re
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from colab_cli.terminal import open_url_in_browser, read_controlling_line

# google.colab.drive.mount defaults to 120s. That is shorter than a normal
# consent click-through, so the kernel gives up, starts DriveFS with no
# credentials, and raises ``ValueError: mount failed`` (upstream issue #113).
# 10 minutes matches the CLI execute() ceiling for this command.
DRIVE_AUTH_TIMEOUT_SEC = 600
DRIVE_MOUNT_TIMEOUT_MS = DRIVE_AUTH_TIMEOUT_SEC * 1000

_XSSI_PREFIX = ")]}'"
_ISSUE_103 = "https://github.com/googlecolab/google-colab-cli/issues/103"
_ISSUE_113 = "https://github.com/googlecolab/google-colab-cli/issues/113"

Echo = Callable[[str], None]
BrowserOpener = Callable[[str], bool]
LineReader = Callable[..., str]
EventLogger = Callable[[str, dict], None]


def mount_code(path: str, timeout_ms: int = DRIVE_MOUNT_TIMEOUT_MS) -> str:
    """Python source that mounts Drive and waits long enough for browser consent.

    ``path`` is embedded with ``repr`` so quotes and backslashes round-trip.
    """
    return (
        "from google.colab import drive\n"
        f"drive.mount({path!r}, timeout_ms={int(timeout_ms)})"
    )


def login_hint(uri: str) -> Optional[str]:
    """Return the ``login_hint`` query value, or None when it is absent."""
    if not uri:
        return None
    values = parse_qs(urlparse(uri).query).get("login_hint") or []
    if not values or not values[0]:
        return None
    return values[0]


def with_authuser(uri: str, authuser: Optional[str]) -> str:
    """Return ``uri`` with its ``authuser`` query parameter set.

    ``authuser is None`` leaves the URL alone. The server-built consent URL
    already carries ``login_hint``; appending ``authuser=0`` by default can
    pin a multi-account browser to the wrong account.
    """
    if not uri or authuser is None:
        return uri
    parts = urlparse(uri)
    pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "authuser"
    ]
    pairs.append(("authuser", str(authuser)))
    return urlunparse(parts._replace(query=urlencode(pairs)))


def propagation_params(authuser: str, *, dryrun: bool) -> dict:
    """Query parameters for ``/tun/m/credentials-propagation``."""
    return {
        "authuser": str(authuser),
        "authtype": "dfs_ephemeral",
        "version": "2",
        "dryrun": "true" if dryrun else "false",
        "propagate": "true",
        "record": "false",
    }


def parse_colab_json(text: str) -> dict:
    """Parse a Colab JSON body, stripping the XSSI guard prefix when present."""
    if not text:
        return {}
    raw = text
    if raw.startswith(_XSSI_PREFIX):
        raw = raw[len(_XSSI_PREFIX) :]
        if raw.startswith("\n"):
            raw = raw[1:]
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _plain_snippet(body: str, limit: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", body or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def explain_propagation_failure(status: Optional[int], body: str) -> str:
    """Human-readable propagation failure.

    A 400 from this endpoint is the multi-account mismatch described in
    upstream issue #103: the consent screen follows ``login_hint``, but the
    gsession created at the redirect is attributed to the browser's *active*
    account. The response body is a full HTML error page; dumping it does not
    tell the user what to do.
    """
    snippet = _plain_snippet(body)
    guidance = (
        "The browser account that approved access did not match the CLI "
        "account. On the account chooser, select the account named in the "
        "login_hint (printed above, and shown by `colab whoami`). "
        f"{_ISSUE_103}"
    )
    if status == 400 or (body and "Bad Request" in body):
        message = (
            f"[colab] Drive credential propagation was rejected (HTTP {status}). "
            f"{guidance}"
        )
        if snippet:
            message = f"{message}\n{snippet}"
        return message
    suffix = f" {snippet}" if snippet else ""
    return f"[colab] Error propagating: {status}{suffix}"


def send_colab_reply(
    wsclient: Any,
    deserialize_msg: dict,
    msg_id: Any,
    *,
    error: Optional[str] = None,
) -> None:
    """Unblock ``drive.mount`` with a ``colab_reply`` on the stdin channel.

    The kernel matches ``content.value.colab_msg_id`` to the integer it stored
    in the request metadata. An ``error`` field makes ``read_reply_from_input``
    raise ``MessageError`` immediately instead of waiting out the mount timeout
    and then failing with an empty ``ValueError: mount failed``.
    """
    value: dict = {"type": "colab_reply", "colab_msg_id": msg_id}
    if error:
        value["error"] = error
    reply = wsclient.session.msg("input_reply", {"value": value})
    header = deserialize_msg.get("header")
    if header:
        reply["parent_header"] = header
    wsclient.stdin_channel.send(reply)


def _json_ok(resp: Any) -> dict:
    if getattr(resp, "status_code", None) != 200:
        return {}
    return parse_colab_json(getattr(resp, "text", "") or "")


def perform_drive_authorization(
    *,
    colab_domain: str,
    endpoint: str,
    http: Any,
    wsclient: Any,
    deserialize_msg: dict,
    msg_id: Any,
    authuser: str = "0",
    rewrite_auth_url: bool = False,
    open_browser: BrowserOpener = open_url_in_browser,
    read_line: LineReader = read_controlling_line,
    echo: Optional[Echo] = None,
    on_event: Optional[EventLogger] = None,
) -> None:
    """Run the Drive credential-propagation handshake and reply to the kernel.

    ``http`` is an authorized ``requests`` session (``get_credentials``).
    ``rewrite_auth_url`` appends ``authuser`` to the consent URL. It is off
    unless the user passed ``--authuser``: issue #103 showed that changing the
    propagation *request* parameter does not fix a multi-account 400, and
    forcing ``authuser=0`` onto the browser URL can select the wrong account.
    """
    if echo is None:
        import typer

        echo = typer.echo

    def emit(message: str) -> None:
        echo(message)

    def event(name: str, payload: dict) -> None:
        if on_event:
            on_event(name, payload)

    url = f"{colab_domain}/tun/m/credentials-propagation/{endpoint}"
    emit(f"\n[colab] Intercepted Drive Auth Request. Connecting to {colab_domain}...")
    params = propagation_params(authuser, dryrun=True)
    token_resp = http.request("GET", url, params=params)
    token = _json_ok(token_resp).get("token")
    headers = {"x-goog-colab-token": token} if token else {}
    files = {"file_id": (None, "empty.ipynb")}
    probe = http.request("POST", url, params=params, headers=headers, files=files)
    if getattr(probe, "status_code", None) != 200:
        message = explain_propagation_failure(
            getattr(probe, "status_code", None), getattr(probe, "text", "") or ""
        )
        emit(message)
        send_colab_reply(
            wsclient,
            deserialize_msg,
            msg_id,
            error="Drive credential propagation failed before consent.",
        )
        return

    data = _json_ok(probe)
    if not data.get("success"):
        uri = data.get("unauthorized_redirect_uri") or ""
        if rewrite_auth_url:
            uri = with_authuser(uri, authuser)
        if not uri:
            emit("[colab] Drive auth did not return a consent URL.")
            emit(
                explain_propagation_failure(
                    getattr(probe, "status_code", None),
                    getattr(probe, "text", "") or "",
                )
            )
            send_colab_reply(
                wsclient,
                deserialize_msg,
                msg_id,
                error="Drive auth did not return a consent URL.",
            )
            return
        account = (
            login_hint(uri) or "the account this CLI authenticated as (colab whoami)"
        )
        emit("[colab] Google Drive authorization needed.")
        emit(f"Sign in as {account}.")
        emit(
            "If the browser is signed into more than one Google account, pick "
            "this account on the chooser. login_hint alone is not enough: Colab "
            "attributes the Drive session to the browser's active account and "
            f"rejects a mismatch with HTTP 400 ({_ISSUE_103})."
        )
        emit("")
        emit(uri)
        emit("")
        event("drive_auth_needed", {"uri": uri})
        if open_browser(uri):
            emit("[colab] Opened that link in your browser.")
        else:
            emit("[colab] Could not launch a browser here. Open the link above.")
        emit(f"[colab] The kernel waits up to 10 minutes for this step ({_ISSUE_113}).")
        read_line("Press Enter after you have granted access... ")

    emit("[colab] Authorizing VM...")
    params = propagation_params(authuser, dryrun=False)
    final = http.request("POST", url, params=params, headers=headers, files=files)
    if getattr(final, "status_code", None) == 200:
        emit("[colab] Credentials propagated. Resuming mount...")
        event("drive_auth_success", {})
        send_colab_reply(wsclient, deserialize_msg, msg_id)
        return

    message = explain_propagation_failure(
        getattr(final, "status_code", None), getattr(final, "text", "") or ""
    )
    emit(message)
    send_colab_reply(
        wsclient,
        deserialize_msg,
        msg_id,
        error=message,
    )
