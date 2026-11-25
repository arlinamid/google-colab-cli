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
import json
import os
import posixpath
from typing import Callable, Optional
from urllib.parse import quote

import requests

from colab_cli.state import SessionState
from colab_cli.utils import get_status_code

# (connect, read) seconds. The read timeout bounds the wait between bytes, not
# the whole transfer, so large uploads/downloads still finish; a dropped
# connection fails instead of hanging forever.
REQUEST_TIMEOUT = (30, 300)


def _write_file_content(data: dict, local_path: str) -> None:
    content = data.get("content")
    if content is None:
        raise ValueError("Missing file content in server response")
    fmt = data.get("format")

    if fmt == "base64":
        content_bytes = base64.b64decode(content)
    elif fmt == "json":
        content_bytes = json.dumps(content, indent=2).encode("utf-8")
    else:
        content_bytes = str(content).encode("utf-8")

    with open(local_path, "wb") as f:
        f.write(content_bytes)


def _normalize_remote_path(remote_path: str) -> str:
    cleaned = remote_path.strip("/")
    if not cleaned:
        return ""
    normalized = posixpath.normpath(cleaned)
    if normalized == ".":
        return ""
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"Unsafe remote path: {remote_path!r}")
    return normalized


def _validate_entry_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or os.path.splitdrive(name)[0]
    ):
        raise ValueError(f"Unsafe entry name in remote directory: {name!r}")
    return name


def _is_local_symlink(path: str) -> bool:
    """islink() is False for "link/" on POSIX: the trailing slash resolves it."""
    return os.path.islink(path.rstrip("/" + os.sep) or path)


def _assert_safe_local_child(real_root: str, child_local: str) -> None:
    if _is_local_symlink(child_local):
        raise ValueError(f"Refusing to overwrite local symlink: {child_local}")
    real_child = os.path.realpath(child_local)
    try:
        common = os.path.commonpath([real_root, real_child])
    except ValueError as e:
        raise ValueError(f"Unsafe destination path: {child_local}") from e
    if common != real_root or real_child == real_root:
        raise ValueError(f"Unsafe destination path: {child_local}")


# Colab runtimes reject request bodies over 100 MiB with HTTP 400. Files are
# sent base64-encoded (4/3 the size) inside JSON, so a file that does not fit
# in one request goes up in chunks of UPLOAD_CHUNK_SIZE (about 85 MiB encoded).
MAX_REQUEST_BODY = 100 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 64 * 1024 * 1024


class IncompleteUploadError(IOError):
    """A chunked upload failed partway, so the remote file may hold only part of
    the local file."""


def describe_error(e: Exception) -> str:
    """Describes an error without the request URL, which has the proxy token."""
    if isinstance(e, requests.RequestException):
        status = get_status_code(e)
        reason = getattr(getattr(e, "response", None), "reason", None)
        details = [str(part) for part in (status, reason) if part]
        return " ".join([type(e).__name__, *details])
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _upload_payload(remote_path: str, content: str, chunk: int) -> dict:
    return {
        "name": remote_path.split("/")[-1],
        "path": remote_path,
        "type": "file",
        "format": "base64",
        "content": content,
        "chunk": chunk,
    }


def fits_in_one_request(remote_path: str, size: int, chunk: int = 1) -> bool:
    """Whether `size` bytes can be uploaded to remote_path as `chunk` in one
    request, i.e. whether its JSON body stays within MAX_REQUEST_BODY."""
    envelope = len(json.dumps(_upload_payload(remote_path, "", chunk)))
    return envelope + 4 * -(-size // 3) <= MAX_REQUEST_BODY


class ContentsClient:
    def __init__(
        self,
        session_state: SessionState,
        refresh_session: Optional[Callable[[], SessionState]] = None,
    ):
        """refresh_session, if given, is called before every chunk after the
        first to pick up a renewed runtime proxy token, so that uploads that
        outlast the token do not fail partway."""
        self.base_url = session_state.url.rstrip("/")
        self.token = session_state.token
        self._refresh_session = refresh_session
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _request(
        self, method: str, path: str, params: dict = None, json_data: dict = None
    ):
        # Quote the path, but don't encode slashes so directory paths stay intact
        quoted_path = quote(path.strip("/"), safe="/")
        url = f"{self.base_url}/api/contents/{quoted_path}"

        req_params = {"authuser": "0", "colab-runtime-proxy-token": self.token}
        if params:
            req_params.update(params)

        response = self._session.request(
            method, url, params=req_params, json=json_data, timeout=REQUEST_TIMEOUT
        )

        if get_status_code(response) == 404:
            raise FileNotFoundError(f"File or directory not found: {path}")

        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            # requests names the full URL, and the runtime proxy token is a
            # query parameter of it: report the status and path only.
            resp = e.response or response
            reason = getattr(resp, "reason", None) or "Error"
            raise requests.HTTPError(
                f"{resp.status_code} {reason} for /api/contents/{quoted_path}",
                response=resp,
            ) from None

        # DELETE doesn't return JSON
        if method == "DELETE":
            return None

        return response.json()

    def list_dir(self, path: str):
        return self._request("GET", path)

    def upload(
        self, local_path: str, remote_path: str, chunk_size: int = UPLOAD_CHUNK_SIZE
    ):
        """Uploads a file, in chunks if it does not fit in one request.

        A file that fits is sent as before: one request with chunk 1, which
        replaces the remote file in a single save, so the file is either
        replaced completely or left as it was. A larger file uses the Jupyter
        Contents API chunked protocol: chunk 1 replaces the file, later chunks
        are appended, and -1 marks the last one. A lone -1 would append to an
        existing file, so it is only ever sent after chunk 1.

        If a chunk, or refresh_session before it, fails, IncompleteUploadError
        is raised: the remote file may be partly written (even a failed chunk 1
        may have been stored if only the response was lost) and the upload has
        to be run again. A chunk 1 the runtime refused (404 or another 4xx)
        wrote nothing, so that error is raised unchanged. IncompleteUploadError
        is raised outside the `except` block so that it carries no reference
        to the requests error, whose text includes the proxy token. An upload
        interrupted with Ctrl-C after chunk 1 also leaves a partial file.

        The stored size is not compared with the local one: the Contents API
        reports a symlink's own size (lstat), so an upload that wrote through
        a symlinked destination would look incomplete.
        """
        if chunk_size <= 0 or not fits_in_one_request(remote_path, chunk_size, -1):
            raise ValueError(f"chunk_size {chunk_size} does not fit in one request")
        with open(local_path, "rb") as f:
            if fits_in_one_request(remote_path, os.fstat(f.fileno()).st_size):
                return self._put_chunk(remote_path, f.read(), 1)
            data = f.read(chunk_size)
            next_data = f.read(chunk_size)
            if not next_data:
                return self._put_chunk(remote_path, data, 1)
            sent = 0
            failure = None
            try:
                while data:
                    if sent:
                        self._refresh_token()
                    model = self._put_chunk(
                        remote_path, data, sent + 1 if next_data else -1
                    )
                    sent += 1
                    data, next_data = next_data, f.read(chunk_size)
            except (Exception, SystemExit) as e:
                status = get_status_code(e)
                rejected = isinstance(e, FileNotFoundError) or (
                    status is not None and 400 <= status < 500
                )
                if not sent and rejected:
                    raise
                failure = (
                    f"Uploading {remote_path} failed after {sent} of its chunks "
                    f"were sent ({describe_error(e)}); "
                    "the remote file may be incomplete."
                )
        if failure:
            raise IncompleteUploadError(failure)
        return model

    def _refresh_token(self):
        if self._refresh_session:
            session_state = self._refresh_session()
            self.base_url = session_state.url.rstrip("/")
            self.token = session_state.token

    def _put_chunk(self, remote_path: str, data: bytes, chunk: int):
        content = base64.b64encode(data).decode("ascii")
        payload = _upload_payload(remote_path, content, chunk)
        return self._request("PUT", remote_path, json_data=payload)

    def mkdir(self, remote_path: str):
        return self._request("PUT", remote_path, json_data={"type": "directory"})

    def _ensure_dir(self, remote_path: str) -> None:
        try:
            self.mkdir(remote_path)
        except (FileNotFoundError, requests.HTTPError) as e:
            if isinstance(e, requests.HTTPError) and get_status_code(e) != 500:
                raise
            parent = posixpath.dirname(remote_path)
            if not parent or parent == remote_path or parent == "content":
                raise
            self._ensure_dir(parent)
            self.mkdir(remote_path)

    def upload_dir(self, local_dir: str, remote_dir: str, on_file=None) -> int:
        """Upload a tree; ``on_file(done, total, remote_path)`` after each file."""
        if not os.path.isdir(local_dir):
            raise NotADirectoryError(f"Not a directory: {local_dir}")
        base_remote = _normalize_remote_path(remote_dir)
        if base_remote:
            self._ensure_dir(base_remote)

        def _raise_walk_error(err: OSError) -> None:
            raise err

        total = None
        if on_file is not None:
            total = sum(
                1
                for root, dirs, files in os.walk(local_dir, followlinks=False)
                for f in files
                if not os.path.islink(os.path.join(root, f)) and os.path.isfile(os.path.join(root, f))
            )
        count = 0
        for root, dirs, files in os.walk(
            local_dir, followlinks=False, onerror=_raise_walk_error
        ):
            dirs[:] = sorted(
                d for d in dirs if not os.path.islink(os.path.join(root, d))
            )
            files.sort()
            rel = os.path.relpath(root, local_dir)
            rel_posix = "" if rel == "." else rel.replace(os.sep, "/")
            current_remote = posixpath.join(base_remote, rel_posix)
            for d in dirs:
                self.mkdir(posixpath.join(current_remote, d))
            for f in files:
                local_file = os.path.join(root, f)
                if not os.path.islink(local_file) and os.path.isfile(local_file):
                    remote_file = posixpath.join(current_remote, f)
                    self.upload(local_file, remote_file)
                    count += 1
                    if on_file is not None:
                        on_file(count, total, remote_file)
        return count

    def download(self, remote_path: str, local_path: str):
        data = self._request("GET", remote_path, params={"content": "1"})

        if data.get("type") == "directory":
            raise IsADirectoryError(
                f"Cannot download a directory: {remote_path}. "
                "Use -r/--recursive to download directories."
            )

        _write_file_content(data, local_path)

    def download_dir(
        self,
        remote_path: str,
        local_path: str,
        *,
        on_file=None,
        _depth: int = 0,
        _max_depth: int = 64,
        _done=None,
    ) -> int:
        """Download a tree; ``on_file(done, None, remote_path)`` after each file."""
        _done = _done if _done is not None else [0]
        if _depth > _max_depth:
            raise RecursionError(
                f"Maximum directory depth ({_max_depth}) exceeded while downloading {remote_path!r}"
            )
        base_remote = _normalize_remote_path(remote_path)
        data = self._request("GET", base_remote, params={"content": "1"})
        if data.get("type") != "directory":
            if _is_local_symlink(local_path):
                raise ValueError(f"Refusing to overwrite local symlink: {local_path}")
            if os.path.isdir(local_path) or local_path.endswith(("/", os.sep)):
                target_name = _validate_entry_name(
                    data.get("name") or posixpath.basename(base_remote)
                )
                os.makedirs(local_path, exist_ok=True)
                target = os.path.join(local_path, target_name)
                _assert_safe_local_child(os.path.realpath(local_path), target)
                _write_file_content(data, target)
            else:
                parent = os.path.dirname(local_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                _write_file_content(data, local_path)
            return 1

        if _is_local_symlink(local_path):
            raise ValueError(f"Refusing to traverse local symlink: {local_path}")
        os.makedirs(local_path, exist_ok=True)
        real_root = os.path.realpath(local_path)
        count = 0
        for item in data.get("content") or []:
            item_name = _validate_entry_name(item.get("name", ""))
            child_remote = (
                posixpath.join(base_remote, item_name) if base_remote else item_name
            )
            child_local = os.path.join(local_path, item_name)
            _assert_safe_local_child(real_root, child_local)
            if item.get("type") == "directory":
                count += self.download_dir(
                    child_remote,
                    child_local,
                    on_file=on_file,
                    _depth=_depth + 1,
                    _max_depth=_max_depth,
                    _done=_done,
                )
            else:
                self.download(child_remote, child_local)
                count += 1
                _done[0] += 1
                if on_file is not None:
                    on_file(_done[0], None, child_remote)
        return count

    def read_text(self, remote_path: str) -> str:
        """The content of a remote file as text (UTF-8)."""
        data = self._request("GET", remote_path, params={"content": "1"})
        if data.get("type") == "directory":
            raise IsADirectoryError(f"Not a file: {remote_path}")
        content = data.get("content", "")
        if data.get("format") == "base64":
            return base64.b64decode(content).decode("utf-8", errors="replace")
        if data.get("format") == "json" or not isinstance(content, str):
            return json.dumps(content)
        return content

    def write_text(self, remote_path: str, text: str):
        """Create or replace a remote text file."""
        payload = {
            "name": remote_path.split("/")[-1],
            "path": remote_path,
            "type": "file",
            "format": "text",
            "content": text,
        }
        return self._request("PUT", remote_path, json_data=payload)

    def rm(self, remote_path: str):
        self._request("DELETE", remote_path)

    def rm_tree(self, remote_path: str) -> None:
        """Delete a folder and everything in it, children first.

        The contents API does not delete a folder that is not empty.
        """
        data = self.list_dir(remote_path)
        if data.get("type") == "directory":
            for item in data.get("content") or []:
                child = posixpath.join(remote_path, _validate_entry_name(item.get("name")))
                if item.get("type") == "directory":
                    self.rm_tree(child)
                else:
                    self.rm(child)
        self.rm(remote_path)
