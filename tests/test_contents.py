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

import base64
import io
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests
from colab_cli.contents import (
    REQUEST_TIMEOUT,
    UPLOAD_CHUNK_SIZE,
    ContentsClient,
    IncompleteUploadError,
    _upload_payload,
    fits_in_one_request,
)
from requests import HTTPError, Response

from colab_cli.state import SessionState


@pytest.fixture
def session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://fake-endpoint.colab.dev",
        endpoint="endpoint",
    )


@pytest.fixture
def client(session):
    return ContentsClient(session)


@patch("colab_cli.contents.requests.Session.request")
def test_list_dir(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "content",
        "type": "directory",
        "content": [
            {"name": "file.txt", "type": "file"},
            {"name": "dir", "type": "directory"},
        ],
    }
    mock_request.return_value = mock_resp

    res = client.list_dir("content")

    mock_request.assert_called_once_with(
        "GET",
        "https://fake-endpoint.colab.dev/api/contents/content",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        json=None,
        timeout=REQUEST_TIMEOUT,
    )
    assert res["type"] == "directory"
    assert len(res["content"]) == 2


@patch("colab_cli.contents.requests.Session.request")
def test_rm_file(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 204
    mock_request.return_value = mock_resp

    client.rm("content/file.txt")

    mock_request.assert_called_once_with(
        "DELETE",
        "https://fake-endpoint.colab.dev/api/contents/content/file.txt",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        json=None,
        timeout=REQUEST_TIMEOUT,
    )


@patch("colab_cli.contents.requests.Session.request")
def test_404_error(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 404
    mock_request.return_value = mock_resp

    with pytest.raises(FileNotFoundError):
        client.list_dir("nonexistent")


@patch("colab_cli.contents.requests.Session.request")
def test_download_file(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200

    # Mocking a base64 encoded response
    content_bytes = b"Hello world!"
    b64_content = base64.b64encode(content_bytes).decode("ascii")

    mock_resp.json.return_value = {
        "name": "test.txt",
        "type": "file",
        "format": "base64",
        "content": b64_content,
    }
    mock_request.return_value = mock_resp

    local_file = tmp_path / "test.txt"
    client.download("content/test.txt", str(local_file))

    mock_request.assert_called_once_with(
        "GET",
        "https://fake-endpoint.colab.dev/api/contents/content/test.txt",
        params={
            "authuser": "0",
            "colab-runtime-proxy-token": "test-token",
            "content": "1",
        },
        json=None,
        timeout=REQUEST_TIMEOUT,
    )

    assert local_file.read_bytes() == content_bytes


@patch("colab_cli.contents.requests.Session.request")
def test_download_notebook_json_format(mock_request, client, tmp_path):
    """Downloading a format=json payload serializes valid 2-space indented JSON."""
    notebook_doc = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# Demo"]},
            {
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {},
                "outputs": [],
                "source": ["x = 42\n", "print(x)"],
            },
        ],
        "metadata": {"kernelspec": {"display_name": "Python 3", "name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "demo.ipynb",
        "type": "notebook",
        "format": "json",
        "content": notebook_doc,
    }
    mock_request.return_value = mock_resp

    local_file = tmp_path / "demo.ipynb"
    client.download("content/demo.ipynb", str(local_file))

    raw = local_file.read_text(encoding="utf-8")
    assert json.loads(raw) == notebook_doc
    assert raw == json.dumps(notebook_doc, indent=2)


@patch("colab_cli.contents.requests.Session.request")
def test_upload_file(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    local_file = tmp_path / "test.txt"
    content_bytes = b"Hello upload!"
    local_file.write_bytes(content_bytes)

    client.upload(str(local_file), "content/test.txt")

    expected_b64 = base64.b64encode(content_bytes).decode("ascii")

    mock_request.assert_called_once_with(
        "PUT",
        "https://fake-endpoint.colab.dev/api/contents/content/test.txt",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        json={
            "name": "test.txt",
            "path": "content/test.txt",
            "type": "file",
            "format": "base64",
            "content": expected_b64,
            "chunk": 1,
        },
        timeout=REQUEST_TIMEOUT,
    )


def _http_error_response(status, reason):
    from requests import Response as RealResponse

    resp = RealResponse()
    resp.status_code = status
    resp.reason = reason
    resp.url = (
        "https://fake-endpoint.colab.dev/api/contents/up.txt"
        "?authuser=0&colab-runtime-proxy-token=SECRET-PROXY-TOKEN"
    )
    return resp


@patch("colab_cli.contents.requests.Session.request")
def test_http_error_message_hides_the_proxy_token(mock_request, client):
    """requests puts the full URL, query string included, in HTTPError. The
    runtime proxy token is a query parameter, so it must not reach the
    message that `colab upload` and friends print."""
    mock_request.return_value = _http_error_response(500, "Internal Server Error")

    with pytest.raises(Exception) as excinfo:

        client.list_dir("up.txt")

    message = str(excinfo.value)
    assert "SECRET-PROXY-TOKEN" not in message
    assert "500" in message
    assert "up.txt" in message


@patch("colab_cli.contents.requests.Session.request")
def test_read_text_handles_text_and_base64(mock_request, client):
    resp = MagicMock(spec=Response)
    resp.status_code = 200
    resp.json.return_value = {"type": "file", "format": "text", "content": "a\nb\n"}
    mock_request.return_value = resp
    assert client.read_text("/content/x.txt") == "a\nb\n"

    resp.json.return_value = {
        "type": "file", "format": "base64",
        "content": base64.b64encode("ünnep".encode()).decode(),
    }
    assert client.read_text("/content/x.bin") == "ünnep"


@patch("colab_cli.contents.requests.Session.request")
def test_write_text_puts_a_text_file(mock_request, client):
    resp = MagicMock(spec=Response)
    resp.status_code = 200
    resp.json.return_value = {}
    mock_request.return_value = resp

    client.write_text("/content/.colab-jobs/j/stop", "")

    method, url = mock_request.call_args.args
    assert method == "PUT" and url.endswith("/api/contents/content/.colab-jobs/j/stop")
    payload = mock_request.call_args.kwargs["json"]
    assert payload["format"] == "text" and payload["content"] == "" and payload["name"] == "stop"


@patch("colab_cli.contents.requests.Session.request")
def test_mkdir(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 201
    mock_request.return_value = mock_resp

    client.mkdir("content/new_dir")

    mock_request.assert_called_once_with(
        "PUT",
        "https://fake-endpoint.colab.dev/api/contents/content/new_dir",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        json={"type": "directory"},
        timeout=REQUEST_TIMEOUT,
    )


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    (src / "empty").mkdir()
    (src / "root.txt").write_text("root", encoding="utf-8")
    (src / "sub" / "bin.dat").write_bytes(b"\x00\x01\xff")

    count = client.upload_dir(str(src), "content/proj")

    assert count == 2
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://fake-endpoint.colab.dev/api/contents/content/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/empty",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/sub",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/root.txt",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/sub/bin.dat",
    ]
    assert mock_request.call_args_list[0].kwargs["json"] == {"type": "directory"}
    assert mock_request.call_args_list[1].kwargs["json"] == {"type": "directory"}
    assert mock_request.call_args_list[2].kwargs["json"] == {"type": "directory"}
    assert (
        mock_request.call_args_list[4].kwargs["json"]["path"]
        == "content/proj/sub/bin.dat"
    )


@patch("colab_cli.contents.requests.Session.request")
def test_download_directory_raises_with_hint(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"name": "mydir", "type": "directory", "content": []}
    mock_request.return_value = mock_resp

    with pytest.raises(IsADirectoryError, match="-r/--recursive"):
        client.download("content/mydir", str(tmp_path / "mydir"))


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir(mock_request, client, tmp_path):
    responses = {
        "https://fake-endpoint.colab.dev/api/contents/content/proj": {
            "name": "proj",
            "type": "directory",
            "content": [
                {"name": "root.txt", "path": "content/proj/root.txt", "type": "file"},
                {
                    "name": "nb.ipynb",
                    "path": "content/proj/nb.ipynb",
                    "type": "notebook",
                },
                {"name": "sub", "path": "content/proj/sub", "type": "directory"},
                {"name": "empty", "path": "content/proj/empty", "type": "directory"},
            ],
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/root.txt": {
            "name": "root.txt",
            "type": "file",
            "format": "text",
            "content": "hello",
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/nb.ipynb": {
            "name": "nb.ipynb",
            "type": "notebook",
            "format": "json",
            "content": {"cells": [], "nbformat": 4},
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/sub": {
            "name": "sub",
            "type": "directory",
            "content": [
                {"name": "bin.dat", "path": "content/proj/sub/bin.dat", "type": "file"}
            ],
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/sub/bin.dat": {
            "name": "bin.dat",
            "type": "file",
            "format": "base64",
            "content": base64.b64encode(b"\x00\x02").decode("ascii"),
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/empty": {
            "name": "empty",
            "type": "directory",
            "content": [],
        },
    }

    def side_effect(method, url, **kwargs):
        resp = MagicMock(spec=Response)
        resp.status_code = 200
        resp.json.return_value = responses[url]
        return resp

    mock_request.side_effect = side_effect

    dest = tmp_path / "out"
    count = client.download_dir("content/proj", str(dest))

    assert count == 3
    assert (dest / "root.txt").read_text(encoding="utf-8") == "hello"
    assert json.loads((dest / "nb.ipynb").read_text(encoding="utf-8")) == {
        "cells": [],
        "nbformat": 4,
    }
    assert (dest / "sub" / "bin.dat").read_bytes() == b"\x00\x02"
    assert (dest / "empty").is_dir()


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_single_file(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "solo.txt",
        "type": "file",
        "format": "text",
        "content": "solo",
    }
    mock_request.return_value = mock_resp

    dest = tmp_path / "nested" / "solo.txt"
    count = client.download_dir("content/solo.txt", str(dest))

    assert count == 1
    assert dest.read_text(encoding="utf-8") == "solo"


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_rejects_unsafe_entry_name(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "dir",
        "type": "directory",
        "content": [{"name": "../escape", "path": "content/escape", "type": "file"}],
    }
    mock_request.return_value = mock_resp

    with pytest.raises(ValueError, match="Unsafe"):
        client.download_dir("content/dir", str(tmp_path / "out"))


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_creates_missing_remote_parents(mock_request, client, tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")

    attempts = {}

    def side_effect(method, url, **kwargs):
        attempts[url] = attempts.get(url, 0) + 1
        resp = MagicMock(spec=Response)
        if url.endswith("/content/a/b/proj") and attempts[url] == 1:
            resp.status_code = 500
            resp.raise_for_status.side_effect = requests.HTTPError("500", response=resp)
            return resp
        if url.endswith("/content/a/b") and attempts[url] == 1:
            resp.status_code = 500
            resp.raise_for_status.side_effect = requests.HTTPError("500", response=resp)
            return resp
        resp.status_code = 200
        return resp

    mock_request.side_effect = side_effect

    count = client.upload_dir(str(src), "content/a/b/proj")

    assert count == 1
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://fake-endpoint.colab.dev/api/contents/content/a/b/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/a/b",
        "https://fake-endpoint.colab.dev/api/contents/content/a",
        "https://fake-endpoint.colab.dev/api/contents/content/a/b",
        "https://fake-endpoint.colab.dev/api/contents/content/a/b/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/a/b/proj/a.txt",
    ]


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_does_not_recurse_on_403(mock_request, client, tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")

    resp = MagicMock(spec=Response)
    resp.status_code = 403
    resp.raise_for_status.side_effect = requests.HTTPError("403", response=resp)
    mock_request.return_value = resp

    with pytest.raises(requests.HTTPError):
        client.upload_dir(str(src), "content/a/proj")

    assert mock_request.call_count == 1


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_does_not_mkdir_content_root_on_500(mock_request, client, tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")

    resp = MagicMock(spec=Response)
    resp.status_code = 500
    resp.raise_for_status.side_effect = requests.HTTPError("500", response=resp)
    mock_request.return_value = resp

    with pytest.raises(requests.HTTPError):
        client.upload_dir(str(src), "content/proj")

    assert mock_request.call_count == 1


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_skips_symlinked_directories(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    ext = tmp_path / "ext_dir"
    ext.mkdir()
    (ext / "secret.txt").write_text("secret", encoding="utf-8")

    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    (src / "linked_dir").symlink_to(ext, target_is_directory=True)

    count = client.upload_dir(str(src), "content/proj")

    assert count == 1
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://fake-endpoint.colab.dev/api/contents/content/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/a.txt",
    ]


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_ignores_untrusted_item_path(mock_request, client, tmp_path):
    responses = {
        "https://fake-endpoint.colab.dev/api/contents/content/proj": {
            "name": "proj",
            "type": "directory",
            "content": [
                {
                    "name": "safe.txt",
                    "path": "other/untrusted.txt",
                    "type": "file",
                }
            ],
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/safe.txt": {
            "name": "safe.txt",
            "type": "file",
            "format": "text",
            "content": "trusted",
        },
    }

    def side_effect(method, url, **kwargs):
        resp = MagicMock(spec=Response)
        resp.status_code = 200
        resp.json.return_value = responses[url]
        return resp

    mock_request.side_effect = side_effect

    dest = tmp_path / "out"
    count = client.download_dir("content/proj", str(dest))

    assert count == 1
    assert (dest / "safe.txt").read_text(encoding="utf-8") == "trusted"


def test_reuses_http_session_across_requests(client, tmp_path):
    assert isinstance(client._session, requests.Session)
    local_file = tmp_path / "a.txt"
    local_file.write_text("hi", encoding="utf-8")

    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    with patch.object(client._session, "request", return_value=mock_resp) as m_req:
        client.mkdir("content/proj")
        client.upload(str(local_file), "content/proj/a.txt")

    assert m_req.call_count == 2
    assert m_req.call_args_list[0].args[0] == "PUT"
    assert m_req.call_args_list[1].args[0] == "PUT"


@patch("colab_cli.contents.requests.Session.request")
def test_http_error_omits_runtime_proxy_token(mock_request, client):
    resp = Response()
    resp.status_code = 400
    resp.reason = "Bad Request"
    resp.url = (
        "https://fake-endpoint.colab.dev/api/contents/content/x"
        "?authuser=0&colab-runtime-proxy-token=SECRET-JWT"
    )
    mock_request.return_value = resp

    with pytest.raises(requests.HTTPError) as exc_info:
        client.list_dir("content/x")

    assert exc_info.value.response.status_code == 400
    assert "SECRET-JWT" not in str(exc_info.value)
    assert "colab-runtime-proxy-token" not in str(exc_info.value)


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_skips_symlinked_files_and_broken_symlinks(
    mock_request, client, tmp_path
):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    src = tmp_path / "proj"
    src.mkdir()
    (src / "real.txt").write_text("ok", encoding="utf-8")
    (src / "link.txt").symlink_to(secret)
    (src / "broken.txt").symlink_to(tmp_path / "missing.txt")

    count = client.upload_dir(str(src), "content/proj")

    assert count == 1
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://fake-endpoint.colab.dev/api/contents/content/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/real.txt",
    ]


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_raises_on_non_directory_or_walk_error(
    mock_request, client, tmp_path, mocker
):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("hi", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        client.upload_dir(str(not_a_dir), "content/proj")
    mock_request.assert_not_called()

    src = tmp_path / "proj"
    src.mkdir()

    def failing_walk(*args, onerror=None, **kwargs):
        onerror(PermissionError("denied"))
        return iter([])

    mocker.patch("colab_cli.contents.os.walk", side_effect=failing_walk)
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    with pytest.raises(PermissionError, match="denied"):
        client.upload_dir(str(src), "content/proj")


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_normalizes_remote_dir(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")

    client.upload_dir(str(src), "./content//proj/")
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://fake-endpoint.colab.dev/api/contents/content/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/a.txt",
    ]

    with pytest.raises(ValueError, match="Unsafe"):
        client.upload_dir(str(src), "../escape")


@pytest.mark.parametrize(
    "bad_name",
    [
        pytest.param(
            name,
            marks=pytest.mark.skipif(
                sys.platform != "win32",
                reason="a drive prefix only means something on Windows",
            ),
        )
        for name in ("C:evil.txt", "D:evil.txt")
    ]
    + [123, "bad\x00name", "", ".", "..", "a\\b", "a/b"],
)
@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_rejects_unsafe_entry_variants(
    mock_request, client, tmp_path, bad_name
):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "dir",
        "type": "directory",
        "content": [{"name": bad_name, "type": "file"}],
    }
    mock_request.return_value = mock_resp

    with pytest.raises(ValueError, match="Unsafe"):
        client.download_dir("content/dir", str(tmp_path / "out"))


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_rejects_local_symlink(mock_request, client, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "file.txt").symlink_to(outside)

    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "dir",
        "type": "directory",
        "content": [{"name": "file.txt", "type": "file"}],
    }
    mock_request.return_value = mock_resp

    with pytest.raises(ValueError, match="symlink"):
        client.download_dir("content/dir", str(dest))

    assert outside.read_text(encoding="utf-8") == "keep"


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_single_file_into_existing_or_trailing_slash_dir(
    mock_request, client, tmp_path
):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "solo.txt",
        "type": "file",
        "format": "text",
        "content": "solo",
    }
    mock_request.return_value = mock_resp

    existing = tmp_path / "existing"
    existing.mkdir()
    assert client.download_dir("content/solo.txt", str(existing)) == 1
    assert (existing / "solo.txt").read_text(encoding="utf-8") == "solo"

    trailing = tmp_path / "new_dir"
    assert client.download_dir("content/solo.txt", f"{trailing}/") == 1
    assert (trailing / "solo.txt").read_text(encoding="utf-8") == "solo"


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_max_depth_guard(mock_request, client, tmp_path):
    def side_effect(method, url, **kwargs):
        resp = MagicMock(spec=Response)
        resp.status_code = 200
        resp.json.return_value = {
            "name": "loop",
            "type": "directory",
            "content": [{"name": "loop", "type": "directory"}],
        }
        return resp

    mock_request.side_effect = side_effect

    with pytest.raises(RecursionError, match="Maximum directory depth"):
        client.download_dir("content/loop", str(tmp_path / "out"), _max_depth=3)


@patch("colab_cli.contents.requests.Session.request")
def test_download_none_content_raises(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "f.txt",
        "type": "file",
        "format": "text",
        "content": None,
    }
    mock_request.return_value = mock_resp

    with pytest.raises(ValueError, match="Missing file content"):
        client.download("content/f.txt", str(tmp_path / "f.txt"))


def test_context_manager_closes_session(session):
    c = ContentsClient(session)
    with patch.object(c._session, "close") as m_close:
        with c as entered:
            assert entered is c
        m_close.assert_called_once_with()


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_refuses_a_symlinked_destination_with_a_trailing_slash(
    mock_request, client, tmp_path
):
    """os.path.islink("link/") is False: the slash resolves the link."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need extra rights here")
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "solo.txt", "type": "file", "format": "text", "content": "x",
    }
    mock_request.return_value = mock_resp

    with pytest.raises(ValueError, match="symlink"):
        client.download_dir("content/solo.txt", f"{link}/")
    assert not (outside / "solo.txt").exists()


def test_request_times_out_when_server_stops_responding(client, monkeypatch):
    # A server that accepts the connection but never answers (e.g. the network
    # dropped mid-transfer) must not make upload/download hang forever.
    import socket
    import threading

    import requests

    import colab_cli.contents as contents

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client.base_url = f"http://127.0.0.1:{server.getsockname()[1]}"
    monkeypatch.setattr(contents, "REQUEST_TIMEOUT", (0.01, 0.01))

    errors = []

    def call():
        try:
            client.list_dir("content")
        except Exception as e:
            errors.append(e)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(1)
    server.close()

    assert not worker.is_alive(), "request hung with no timeout"
    assert isinstance(errors[0], requests.exceptions.Timeout)


class FakeContentsServer:
    """Minimal stand-in for the runtime's Contents API, as observed on Colab.

    PUT follows the Jupyter chunked protocol: chunk 1 replaces the file and
    other chunks are appended. `fail` maps a chunk number to an HTTP status
    (or True for 500) to answer that PUT with; a failed request changes
    nothing, and, like requests, its error text carries the URL and token.
    """

    REASONS = {200: "OK", 403: "Forbidden", 404: "Not Found", 500: "Server Error"}

    def __init__(self, files=None, fail=None):
        self.files = dict(files or {})
        self.fail = fail
        self.calls = []
        self.tokens = []

    def __call__(self, method, url, params=None, json=None, timeout=None):
        assert method == "PUT"
        path = url.split("/api/contents/", 1)[1]
        chunk = json["chunk"]
        self.calls.append((path, chunk))
        self.tokens.append(params["colab-runtime-proxy-token"])
        status = self.fail(chunk) if self.fail else None
        if status:
            status = 500 if status is True else status
            token = params["colab-runtime-proxy-token"]
            return self._response(
                status, error=f"{status} Error for url: {url}?token={token}"
            )
        data = base64.b64decode(json["content"])
        old = b"" if chunk == 1 else self.files.get(path, b"")
        self.files[path] = old + data
        return self._response(200, {"type": "file"})

    @classmethod
    def _response(cls, status, body=None, error=None):
        resp = MagicMock(spec=Response)
        resp.status_code = status
        resp.reason = cls.REASONS[status]
        resp.json.return_value = body
        if error:
            resp.raise_for_status.side_effect = HTTPError(error, response=resp)
        return resp


@pytest.fixture
def small_requests():
    """Shrinks the request limit so that, for content/big.bin, files of up to
    6 bytes fit in one request and larger ones do not. 4-byte chunks fit,
    also as chunk -1, whose envelope is one byte longer than chunk 1's."""
    envelope = len(json.dumps(_upload_payload("content/big.bin", "", 1)))
    with patch("colab_cli.contents.MAX_REQUEST_BODY", envelope + 9):
        yield


def _upload(client, tmp_path, server, content, chunk_size=4):
    local_file = tmp_path / "big.bin"
    local_file.write_bytes(content)
    with patch("colab_cli.contents.requests.Session.request", side_effect=server):
        client.upload(str(local_file), "content/big.bin", chunk_size=chunk_size)


def test_upload_large_file_in_chunks(client, tmp_path, small_requests):
    """Files that do not fit in one request are sent as chunks 1, 2, ..., -1."""
    server = FakeContentsServer({"content/big.bin": b"old contents"})

    _upload(client, tmp_path, server, b"0123456789")

    assert server.files == {"content/big.bin": b"0123456789"}
    assert server.calls == [
        ("content/big.bin", 1),
        ("content/big.bin", 2),
        ("content/big.bin", -1),
    ]


def test_upload_exact_multiple_of_chunk_size(client, tmp_path, small_requests):
    """The last full chunk is marked -1; no empty trailing request is sent."""
    server = FakeContentsServer()

    _upload(client, tmp_path, server, b"01234567")

    assert server.files == {"content/big.bin": b"01234567"}
    assert server.calls == [("content/big.bin", 1), ("content/big.bin", -1)]


@pytest.mark.parametrize("content", [b"", b"0123", b"012345"])
def test_upload_that_fits_is_one_request(client, tmp_path, small_requests, content):
    """A file that fits in one request is sent as a single chunk=1 request,
    even when it is larger than one chunk. A lone chunk=-1 would append to
    the old file instead of replacing it."""
    server = FakeContentsServer({"content/big.bin": b"old contents"})

    _upload(client, tmp_path, server, content)

    assert server.files == {"content/big.bin": content}
    assert server.calls == [("content/big.bin", 1)]


def test_one_request_limit_matches_colab():
    """Pinned to a live Colab measurement for this path: a 78,643,113-byte file
    (a 104,857,600-byte request body) uploaded in one request, and larger
    files were refused with HTTP 400."""
    assert fits_in_one_request("/content/p_edge.bin", 78_643_113)
    assert not fits_in_one_request("/content/p_edge.bin", 78_643_114)


def test_default_chunk_fits_in_one_request():
    long_path = "content/" + "x" * 1000
    assert fits_in_one_request(long_path, UPLOAD_CHUNK_SIZE)


@pytest.mark.parametrize("status, error", [(404, FileNotFoundError), (403, HTTPError)])
def test_rejected_first_chunk_raises_original_error(
    client, tmp_path, small_requests, status, error
):
    """A runtime that refuses chunk 1 wrote nothing, so the plain error is kept."""
    server = FakeContentsServer(
        {"content/big.bin": b"old contents"},
        fail=lambda chunk: status if chunk == 1 else None,
    )

    with pytest.raises(error):
        _upload(client, tmp_path, server, b"0123456789")

    assert server.files == {"content/big.bin": b"old contents"}


def test_failed_single_request_raises_original_error(client, tmp_path):
    """A one-request upload either replaces the file or leaves it as it was."""
    server = FakeContentsServer(
        {"content/big.bin": b"old contents"}, fail=lambda chunk: True
    )

    with pytest.raises(HTTPError):
        _upload(client, tmp_path, server, b"0123")

    assert server.files == {"content/big.bin": b"old contents"}


@pytest.mark.parametrize("failed_chunk", [1, 2])
def test_failed_chunk_reports_possibly_incomplete_file(
    client, tmp_path, small_requests, failed_chunk
):
    """Any failed chunk may have been stored (e.g. the response was lost), so
    the error says the file may be incomplete. The HTTPError text has the
    token, so it must reach neither the message nor a chained traceback."""
    server = FakeContentsServer(fail=lambda chunk: chunk == failed_chunk)

    with pytest.raises(IncompleteUploadError) as excinfo:
        _upload(client, tmp_path, server, b"0123456789")

    message = str(excinfo.value)
    assert "content/big.bin" in message
    sent = failed_chunk - 1
    assert f"after {sent} of its chunks were sent (HTTPError 500 Server Error)" in (
        message
    )
    assert "may be incomplete" in message
    assert "test-token" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert len(server.calls) == failed_chunk


def test_later_chunks_use_a_refreshed_token(session, tmp_path, small_requests):
    """Long uploads can outlast the proxy token, so every chunk after the first
    asks the session for a possibly renewed one."""
    renewed = session.model_copy(update={"token": "renewed-token"})
    client = ContentsClient(session, refresh_session=lambda: renewed)
    server = FakeContentsServer()

    _upload(client, tmp_path, server, b"0123456789")

    assert server.tokens == ["test-token", "renewed-token", "renewed-token"]


def test_failed_token_refresh_reports_possibly_incomplete_file(
    session, tmp_path, small_requests
):
    """Refreshing can end in sys.exit (e.g. failed ADC credentials) after chunk 1
    replaced the remote file, so it is reported like a failed chunk."""

    def refresh_session():
        raise SystemExit(1)

    client = ContentsClient(session, refresh_session=refresh_session)
    server = FakeContentsServer()

    with pytest.raises(
        IncompleteUploadError, match=r"after 1 of its chunks.*SystemExit"
    ):
        _upload(client, tmp_path, server, b"0123456789")

    assert server.calls == [("content/big.bin", 1)]


@pytest.mark.parametrize("content", [b"", b"0123"])
def test_file_that_shrank_after_its_size_was_read(
    client, tmp_path, small_requests, content
):
    """If the file is truncated between the size check and the first read, the
    rest is sent as one chunk=1 request instead of failing."""
    server = FakeContentsServer({"content/big.bin": b"old contents"})

    with patch("colab_cli.contents.os.fstat", return_value=MagicMock(st_size=10**9)):
        _upload(client, tmp_path, server, content)

    assert server.files == {"content/big.bin": content}
    assert server.calls == [("content/big.bin", 1)]


class FailingReader(io.BytesIO):
    """A local file whose reads fail after the first `ok_reads`."""

    def __init__(self, data, ok_reads):
        super().__init__(data)
        self.ok_reads = ok_reads

    def fileno(self):
        return -1

    def read(self, size=-1):
        if self.ok_reads == 0:
            raise OSError(5, "Input/output error")
        self.ok_reads -= 1
        return super().read(size)


def test_failed_local_read_reports_possibly_incomplete_file(client, small_requests):
    """Chunk 1 already replaced the remote file, so a local read error later
    on must also say the remote file may be incomplete."""
    server = FakeContentsServer({"content/big.bin": b"old contents"})
    reader = FailingReader(b"0123456789", ok_reads=2)

    with (
        patch("colab_cli.contents.open", create=True, return_value=reader),
        patch("colab_cli.contents.os.fstat", return_value=MagicMock(st_size=10)),
        patch("colab_cli.contents.requests.Session.request", side_effect=server),
        pytest.raises(IncompleteUploadError) as excinfo,
    ):
        client.upload("big.bin", "content/big.bin", chunk_size=4)

    assert "Input/output error" in str(excinfo.value)
    assert "may be incomplete" in str(excinfo.value)
    assert server.files == {"content/big.bin": b"0123"}


@pytest.mark.parametrize("chunk_size", [0, -1, 7])
def test_upload_rejects_bad_chunk_size(client, tmp_path, small_requests, chunk_size):
    """Chunks must be positive and fit in one request (7 bytes do not here)."""
    with pytest.raises(ValueError):
        _upload(client, tmp_path, FakeContentsServer(), b"0123", chunk_size)


class DroppingContentsServer(FakeContentsServer):
    """Like FakeContentsServer, but `drops` maps a PUT's call number (from 1)
    to "before" (the connection drops before the server stores the chunk) or
    "after" (the chunk is stored, then the response is lost), as a network
    that cuts long uploads off with an SSL EOF does. GET with content=0
    answers the file's size."""

    def __init__(self, drops=None, files=None):
        super().__init__(files=files)
        self.drops = dict(drops or {})
        self.puts = 0

    def __call__(self, method, url, params=None, json=None, timeout=None):
        path = url.split("/api/contents/", 1)[1]
        if method == "GET":
            self.calls.append((path, "size"))
            if path not in self.files:
                return self._response(404)
            return self._response(200, {"type": "file", "size": len(self.files[path])})
        self.puts += 1
        drop = self.drops.get(self.puts)
        if drop == "before":
            self.calls.append((path, json["chunk"]))
            raise requests.exceptions.SSLError("EOF occurred in violation of protocol")
        resp = super().__call__(method, url, params=params, json=json, timeout=timeout)
        if drop == "after":
            raise requests.exceptions.ConnectionError("Connection aborted")
        return resp


def test_a_dropped_first_chunk_is_sent_again(client, tmp_path, small_requests):
    """Chunk 1 replaces the file, so sending it again is always safe."""
    server = DroppingContentsServer(drops={1: "before"})
    _upload(client, tmp_path, server, b"0123456789")
    assert server.files["content/big.bin"] == b"0123456789"
    assert [c for _, c in server.calls] == [1, 1, 2, -1]


def test_a_dropped_later_chunk_that_was_not_stored_is_sent_again(
    client, tmp_path, small_requests
):
    server = DroppingContentsServer(drops={2: "before"})
    _upload(client, tmp_path, server, b"0123456789")
    assert server.files["content/big.bin"] == b"0123456789"
    assert [c for _, c in server.calls] == [1, 2, "size", 2, -1]


def test_a_later_chunk_stored_before_the_drop_is_not_appended_twice(
    client, tmp_path, small_requests
):
    server = DroppingContentsServer(drops={2: "after"})
    _upload(client, tmp_path, server, b"0123456789")
    assert server.files["content/big.bin"] == b"0123456789"
    assert [c for _, c in server.calls] == [1, 2, "size", -1]


def test_a_single_request_upload_is_sent_again_after_a_drop(client, tmp_path):
    server = DroppingContentsServer(drops={1: "before"})
    _upload(client, tmp_path, server, b"0123")
    assert server.files["content/big.bin"] == b"0123"


def test_an_upload_that_keeps_dropping_reports_an_incomplete_file(
    client, tmp_path, small_requests
):
    server = DroppingContentsServer(drops={2: "before", 3: "before", 4: "before"})
    with pytest.raises(IncompleteUploadError) as excinfo:
        _upload(client, tmp_path, server, b"0123456789")
    assert "after 1 of its chunks were sent (SSLError)" in str(excinfo.value)
    assert server.puts == 4


def test_an_unexpected_remote_size_stops_the_upload(client, tmp_path, small_requests):
    """Neither the size before nor after the chunk: something else wrote to
    the file, so the upload stops instead of guessing."""
    server = DroppingContentsServer(drops={2: "before"})
    original = server.__call__

    def tampering(method, url, **kwargs):
        if method == "GET":
            server.files["content/big.bin"] += b"x"
        return original(method, url, **kwargs)

    with pytest.raises(IncompleteUploadError):
        _upload(client, tmp_path, tampering, b"0123456789")
