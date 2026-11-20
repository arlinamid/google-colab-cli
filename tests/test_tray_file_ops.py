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


"""The file manager's actions as CLI argv (no Qt)."""

import json
import os

import pytest

from colab_cli.tray import file_ops as f


def test_ls_argv():
    assert f.ls_argv("s1", "content/data") == ["ls", "-s", "s1", "--json", "content/data"]


def test_copy_argv_upload_a_folder_into_the_remote_folder():
    argv = f.copy_argv("s1", os.path.join("C:", "work", "data"), "content", upload=True, is_dir=True, via="contents")
    assert argv == [
        "upload", "-s", "s1", "-r", "--via", "contents", "--progress", os.path.join("C:", "work", "data"), "content/data",
    ]


def test_copy_argv_download_a_file_over_ssh(tmp_path):
    argv = f.copy_argv("s1", "content/out/model.pt", str(tmp_path), upload=False, is_dir=False, via="ssh")
    assert argv == ["download", "-s", "s1", "--via", "ssh", "content/out/model.pt", os.path.join(str(tmp_path), "model.pt")]


def test_mkdir_and_rm_argv():
    assert f.mkdir_argv("s1", "content/new") == ["mkdir", "-s", "s1", "content/new"]
    assert f.rm_argv("s1", "content/x.txt", is_dir=False) == ["rm", "-s", "s1", "content/x.txt"]
    assert f.rm_argv("s1", "content/d", is_dir=True) == ["rm", "-s", "s1", "-r", "content/d"]


def test_remote_paths():
    assert f.remote_join("", "content") == "content"
    assert f.remote_join("content", "a b") == "content/a b"
    assert f.remote_parent("content/data/") == "content"
    assert f.remote_parent("content") == ""


def test_command_over_ssh_runs_in_the_folder():
    argv, stdin = f.command_argv("s1", "content/my dir", "ls -la", via="ssh")
    assert argv == ["ssh", "-s", "s1", "--", "sh", "-c", "cd '/content/my dir' && ls -la"]
    assert stdin is None


def test_command_over_contents_runs_through_exec():
    argv, stdin = f.command_argv("s1", "content", "echo 'hi' $HOME", via="contents")
    assert argv == ["exec", "-s", "s1"]
    assert "'/content'" in stdin and repr("echo 'hi' $HOME") in stdin
    compile(stdin, "<cmd>", "exec")


def test_parse_listing_takes_the_json_line():
    payload = {"path": "content", "items": [{"name": "a", "type": "file", "size": 1, "last_modified": None}]}
    text = "\n[colab] some notice\n" + json.dumps(payload) + "\n"
    assert f.parse_listing(text) == payload["items"]


def test_parse_listing_errors():
    with pytest.raises(ValueError, match="not found"):
        f.parse_listing(json.dumps({"path": "x", "error": "File or directory not found: x"}))
    with pytest.raises(ValueError):
        f.parse_listing("Traceback ...")


@pytest.mark.parametrize("size,text", [(None, ""), (0, "0 B"), (999, "999 B"), (2048, "2.0 KB"), (5 * 1024**3, "5.0 GB")])
def test_human_size(size, text):
    assert f.human_size(size) == text


def test_parse_progress():
    assert f.parse_progress("[colab] progress 3/10 content/d/sub/b.txt\n") == (3, 10, "content/d/sub/b.txt")
    assert f.parse_progress("[colab] progress 7/? content/x") == (7, None, "content/x")
    assert f.parse_progress("[colab] Uploaded 'x' to 'y'") is None
