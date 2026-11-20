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


"""CLI pieces the tray's file manager runs (docs/09): ls --json, mkdir,
rm -r, and upload/download --via ssh (scp through `colab ssh --proxy-mode`)."""

import json
from unittest.mock import MagicMock, call

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import files as files_module
from colab_cli.contents import ContentsClient

runner = CliRunner()


@pytest.fixture
def session(mock_common_state):
    s = MagicMock(url="https://u", token="t", endpoint="ep")
    s.name = "s1"
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.get_session.return_value = s
    return s


@pytest.fixture
def contents(mocker, session):
    return mocker.patch.object(files_module, "ContentsClient").return_value


def test_ls_json_lists_folders_first_with_sizes(contents):
    contents.list_dir.return_value = {
        "type": "directory",
        "path": "content",
        "content": [
            {"name": "b.txt", "type": "file", "size": 3, "last_modified": "2026-11-20T01:00:00Z"},
            {"name": "data", "type": "directory", "size": None, "last_modified": "2026-11-19T01:00:00Z"},
            {"name": "a.ipynb", "type": "notebook", "size": 10, "last_modified": "2026-11-20T02:00:00Z"},
        ],
    }
    result = runner.invoke(app, ["ls", "-s", "s1", "--json", "content"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["path"] == "content"
    assert [i["name"] for i in data["items"]] == ["data", "a.ipynb", "b.txt"]
    assert data["items"][0] == {
        "name": "data", "type": "directory", "size": None, "last_modified": "2026-11-19T01:00:00Z",
    }


def test_ls_json_error_is_json_too(contents):
    contents.list_dir.side_effect = FileNotFoundError("File or directory not found: nope")
    result = runner.invoke(app, ["ls", "-s", "s1", "--json", "nope"])
    assert result.exit_code == 1
    assert "not found" in json.loads(result.stdout)["error"]


def test_mkdir(contents):
    result = runner.invoke(app, ["mkdir", "-s", "s1", "content/new"])
    assert result.exit_code == 0, result.output
    contents.mkdir.assert_called_once_with("content/new")


def test_rm_r_removes_a_tree(contents):
    result = runner.invoke(app, ["rm", "-s", "s1", "-r", "content/data"])
    assert result.exit_code == 0, result.output
    contents.rm_tree.assert_called_once_with("content/data")
    contents.rm.assert_not_called()


def test_rm_tree_deletes_children_before_their_folder():
    client = ContentsClient.__new__(ContentsClient)
    listings = {
        "d": {"type": "directory", "content": [
            {"name": "f.txt", "type": "file"}, {"name": "sub", "type": "directory"},
        ]},
        "d/sub": {"type": "directory", "content": [{"name": "g.txt", "type": "file"}]},
    }
    client.list_dir = MagicMock(side_effect=lambda p: listings[p])
    client.rm = MagicMock()
    client.rm_tree("d")
    assert client.rm.call_args_list == [call("d/f.txt"), call("d/sub/g.txt"), call("d/sub"), call("d")]


@pytest.fixture
def scp(mocker, session, tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("private")
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA")
    monkeypatch.setenv("COLAB_CLI_SSH_IDENTITY", str(key))
    mocker.patch("colab_cli.commands.ssh._proxy_command", return_value="PROXY")
    popen = mocker.patch("colab_cli.commands.ssh.subprocess.Popen")
    popen.return_value.wait.return_value = 0
    popen.return_value.poll.return_value = 0
    return popen


def test_upload_via_ssh_runs_scp(scp, tmp_path):
    folder = tmp_path / "dir"
    folder.mkdir()
    result = runner.invoke(app, ["upload", "-s", "s1", "-r", "--via", "ssh", str(folder), "content/dir"])
    assert result.exit_code == 0, result.output
    argv = scp.call_args.args[0]
    assert "scp" in argv[0] and "-r" in argv and "ProxyCommand=PROXY" in argv
    assert argv[-2:] == [str(folder), "root@colab-runtime:/content/dir"]


def test_download_via_ssh_runs_scp(scp, tmp_path):
    result = runner.invoke(app, ["download", "-s", "s1", "--via", "ssh", "content/a.txt", str(tmp_path / "a.txt")])
    assert result.exit_code == 0, result.output
    argv = scp.call_args.args[0]
    assert "-r" not in argv
    assert argv[-2:] == ["root@colab-runtime:/content/a.txt", str(tmp_path / "a.txt")]


def test_scp_failure_exits_with_its_code(scp, tmp_path):
    scp.return_value.wait.return_value = 1
    result = runner.invoke(app, ["download", "-s", "s1", "--via", "ssh", "content/a.txt", str(tmp_path / "a")])
    assert result.exit_code == 1


def test_unknown_via(session, tmp_path):
    result = runner.invoke(app, ["download", "-s", "s1", "--via", "ftp", "content/a", str(tmp_path / "a")])
    assert result.exit_code == 2


def test_upload_dir_reports_each_file_with_the_total(tmp_path):
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "a.txt").write_text("a")
    (tmp_path / "d" / "sub" / "b.txt").write_text("b")
    client = ContentsClient.__new__(ContentsClient)
    client._ensure_dir = MagicMock()
    client.mkdir = MagicMock()
    client.upload = MagicMock()
    seen = []
    client.upload_dir(str(tmp_path / "d"), "content/d", on_file=lambda *a: seen.append(a))
    assert seen == [(1, 2, "content/d/a.txt"), (2, 2, "content/d/sub/b.txt")]


def test_download_dir_counts_across_subfolders(tmp_path):
    client = ContentsClient.__new__(ContentsClient)
    listings = {
        "d": {"type": "directory", "content": [{"name": "a", "type": "file"}, {"name": "s", "type": "directory"}]},
        "d/s": {"type": "directory", "content": [{"name": "b", "type": "file"}]},
    }
    client._request = MagicMock(side_effect=lambda m, p, params=None, json_data=None: listings[p])
    client.download = MagicMock()
    seen = []
    client.download_dir("d", str(tmp_path / "out"), on_file=lambda *a: seen.append(a))
    assert seen == [(1, None, "d/a"), (2, None, "d/s/b")]


def test_progress_lines(contents, tmp_path):
    folder = tmp_path / "dir"
    folder.mkdir()

    def fake_upload_dir(local, remote, on_file=None):
        on_file(1, 2, "content/dir/a")
        on_file(2, 2, "content/dir/b")
        return 2

    contents.upload_dir.side_effect = fake_upload_dir
    result = runner.invoke(app, ["upload", "-s", "s1", "-r", "--progress", str(folder), "content/dir"])
    assert result.exit_code == 0, result.output
    assert "[colab] progress 1/2 content/dir/a" in result.stdout
    assert "[colab] progress 2/2 content/dir/b" in result.stdout
