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

"""Drive credential propagation.

Pins the fixes for upstream issues:
- #113 mount failed after exactly 120s (kernel-side drive.mount timeout)
- #103 HTTP 400 when the browser's active account is not the CLI account
- #108 open the consent URL instead of copy-paste only
"""

import json
from unittest.mock import MagicMock

from colab_cli.drive_auth import (
    DRIVE_MOUNT_TIMEOUT_MS,
    explain_propagation_failure,
    login_hint,
    mount_code,
    perform_drive_authorization,
    with_authuser,
)


def test_mount_code_extends_kernel_timeout_past_120s():
    """google.colab.drive.mount defaults timeout_ms to 120000. That budget
    covers the browser consent wait, so a normal OAuth click-through makes
    the kernel give up and raise ``ValueError: mount failed`` (#113).
    """
    code = mount_code("/content/drive")
    assert "drive.mount('/content/drive'" in code
    assert f"timeout_ms={DRIVE_MOUNT_TIMEOUT_MS}" in code
    assert DRIVE_MOUNT_TIMEOUT_MS >= 600_000


def test_mount_code_quotes_path_with_repr():
    code = mount_code("/tmp/drive's")
    assert (
        "drive.mount('/tmp/drive\\'s'" in code or 'drive.mount("/tmp/drive\'s"' in code
    )


def test_login_hint_is_decoded():
    uri = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        "?login_hint=user%40example.com&response_type=none+gsession"
    )
    assert login_hint(uri) == "user@example.com"
    assert login_hint("") is None
    assert login_hint("https://accounts.google.com/o/oauth2/v2/auth") is None


def test_with_authuser_appends_and_replaces():
    uri = "https://accounts.google.com/o/oauth2/v2/auth?login_hint=a@b.c&authuser=0"
    rewritten = with_authuser(uri, "6")
    assert "authuser=6" in rewritten
    assert "authuser=0" not in rewritten
    assert "login_hint=a%40b.c" in rewritten or "login_hint=a@b.c" in rewritten
    assert with_authuser(uri, None) == uri


def test_explain_400_is_actionable_and_not_a_raw_html_dump():
    html = (
        "<!DOCTYPE html><html><head><title>Error 400 (Bad Request)!!1</title>"
        "</head><body>" + ("x" * 8000) + "</body></html>"
    )
    text = explain_propagation_failure(400, html)
    assert "400" in text
    assert "account" in text.lower()
    assert "login_hint" in text or "chooser" in text.lower()
    assert "https://github.com/googlecolab/google-colab-cli/issues/103" in text
    assert "<html" not in text.lower()
    assert "x" * 500 not in text


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        if isinstance(payload, str):
            self.text = payload
        else:
            self.text = ")]}'\n" + json.dumps(payload)


def _http(responses):
    http = MagicMock()
    http.request.side_effect = responses
    return http


def _ws():
    ws = MagicMock()
    ws.session.msg.side_effect = lambda *a, **k: {"msg_type": a[0], "content": a[1]}
    return ws


_CONSENT = (
    "https://accounts.google.com/o/oauth2/v2/auth"
    "?login_hint=user%40example.com&redirect_uri=https%3A%2F%2Fcolab.research.google.com"
    "%2Ftun%2Fm%2Fauthorize-for-drive-credentials-ephem"
)


def test_perform_prompts_opens_browser_and_replies_on_success():
    http = _http(
        [
            _Resp(200, {"token": "tok"}),
            _Resp(200, {"success": False, "unauthorized_redirect_uri": _CONSENT}),
            _Resp(200, {"success": True}),
        ]
    )
    ws = _ws()
    opened = []
    lines = []
    echoes = []
    events = []

    perform_drive_authorization(
        colab_domain="https://colab.research.google.com",
        endpoint="gpu-t4-1",
        http=http,
        wsclient=ws,
        deserialize_msg={"header": {"msg_id": "parent"}},
        msg_id=7,
        open_browser=lambda url: opened.append(url) or True,
        read_line=lambda prompt="": lines.append(prompt) or "\n",
        echo=echoes.append,
        on_event=lambda name, payload: events.append(name),
    )

    assert opened == [_CONSENT]
    assert lines and "Enter" in lines[0]
    assert any("user@example.com" in msg for msg in echoes)
    assert any("Credentials propagated" in msg for msg in echoes)
    sent = ws.stdin_channel.send.call_args.args[0]
    assert sent["content"]["value"]["type"] == "colab_reply"
    assert sent["content"]["value"]["colab_msg_id"] == 7
    assert "error" not in sent["content"]["value"]
    assert sent["parent_header"]["msg_id"] == "parent"
    assert "drive_auth_needed" in events
    assert "drive_auth_success" in events
    # Final propagation is dryrun=false.
    last_params = http.request.call_args_list[-1].kwargs["params"]
    assert last_params["dryrun"] == "false"
    assert last_params["authuser"] == "0"


def test_perform_skips_prompt_when_already_authorized():
    http = _http(
        [
            _Resp(200, {"token": "tok"}),
            _Resp(200, {"success": True}),
            _Resp(200, {"success": True}),
        ]
    )
    ws = _ws()
    read_line = MagicMock()
    open_browser = MagicMock()
    perform_drive_authorization(
        colab_domain="https://colab.research.google.com",
        endpoint="e",
        http=http,
        wsclient=ws,
        deserialize_msg={"header": {}},
        msg_id=1,
        open_browser=open_browser,
        read_line=read_line,
        echo=lambda *_: None,
    )
    read_line.assert_not_called()
    open_browser.assert_not_called()
    assert ws.stdin_channel.send.called
    assert "error" not in ws.stdin_channel.send.call_args.args[0]["content"]["value"]


def test_perform_400_explains_account_mismatch_and_unblocks_kernel():
    html = "<html><title>Error 400 (Bad Request)!!1</title>" + ("y" * 5000)
    http = _http(
        [
            _Resp(200, {"token": "tok"}),
            _Resp(200, {"success": False, "unauthorized_redirect_uri": _CONSENT}),
            _Resp(400, html),
        ]
    )
    ws = _ws()
    echoes = []
    perform_drive_authorization(
        colab_domain="https://colab.research.google.com",
        endpoint="e",
        http=http,
        wsclient=ws,
        deserialize_msg={"header": {}},
        msg_id=4,
        open_browser=lambda url: False,
        read_line=lambda prompt="": "\n",
        echo=echoes.append,
    )
    blob = "\n".join(echoes)
    assert "400" in blob
    assert "user@example.com" in blob
    assert "y" * 400 not in blob
    assert "<html" not in blob.lower()
    value = ws.stdin_channel.send.call_args.args[0]["content"]["value"]
    assert value["colab_msg_id"] == 4
    assert "error" in value


def test_perform_explicit_authuser_is_added_to_consent_url_only():
    """Issue #103: changing the propagation request's authuser does not fix
    the 400. Steering the *browser* URL is the unverified but requested
    lever, and it must not happen unless the user opted in — appending
    authuser=0 by default can pin the chooser to the wrong account.
    """
    http = _http(
        [
            _Resp(200, {"token": "tok"}),
            _Resp(200, {"success": False, "unauthorized_redirect_uri": _CONSENT}),
            _Resp(200, {"success": True}),
        ]
    )
    opened = []
    perform_drive_authorization(
        colab_domain="https://colab.research.google.com",
        endpoint="e",
        http=http,
        wsclient=_ws(),
        deserialize_msg={"header": {}},
        msg_id=1,
        authuser="6",
        rewrite_auth_url=True,
        open_browser=lambda url: opened.append(url) or True,
        read_line=lambda prompt="": "\n",
        echo=lambda *_: None,
    )
    assert "authuser=6" in opened[0]
    assert http.request.call_args_list[-1].kwargs["params"]["authuser"] == "6"


def test_default_consent_url_is_not_rewritten():
    http = _http(
        [
            _Resp(200, {"token": "tok"}),
            _Resp(200, {"success": False, "unauthorized_redirect_uri": _CONSENT}),
            _Resp(200, {"success": True}),
        ]
    )
    opened = []
    perform_drive_authorization(
        colab_domain="https://colab.research.google.com",
        endpoint="e",
        http=http,
        wsclient=_ws(),
        deserialize_msg={"header": {}},
        msg_id=1,
        open_browser=lambda url: opened.append(url) or True,
        read_line=lambda prompt="": "\n",
        echo=lambda *_: None,
    )
    assert opened == [_CONSENT]
    assert "authuser=" not in opened[0]
