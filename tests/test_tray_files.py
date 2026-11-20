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


"""The tray's file manager window, on Qt's offscreen platform (docs/09)."""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

try:
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
    from PySide6.QtGui import QDropEvent  # noqa: E402
    from PySide6.QtWidgets import QApplication  # noqa: E402

    from colab_cli.tray import files as files_mod  # noqa: E402
except ImportError as e:
    pytest.skip(f"Qt is not usable here: {e}", allow_module_level=True)

from colab_cli.state import Settings  # noqa: E402

LISTING = {"path": "content", "items": [
    {"name": "data", "type": "directory", "size": None, "last_modified": "2026-11-20T01:02:03Z"},
    {"name": "model.pt", "type": "file", "size": 2048, "last_modified": "2026-11-20T04:05:06Z"},
]}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def fm(qapp, tmp_path):
    calls = []

    def run(argv, stdin, label, done):
        calls.append({"argv": argv, "stdin": stdin, "done": done})

    store = MagicMock()
    store.load.return_value = Settings()
    (tmp_path / "local.txt").write_text("hi")
    window = files_mod.FileManager("s1", "hu", ["colab"], settings_store=store, run=run, start_dir=str(tmp_path))
    window.calls = calls
    window.store = store
    yield window
    window.close()
    window.deleteLater()


def _answer_listing(fm, listing=LISTING):
    fm.calls[-1]["done"](0, "[colab] notice\n" + json.dumps(listing) + "\n")


def _rows(fm):
    return [fm.remote.topLevelItem(i).text(0) for i in range(fm.remote.topLevelItemCount())]


def test_opens_on_content_and_lists_it(fm):
    assert fm.calls[0]["argv"] == ["ls", "-s", "s1", "--json", "content"]
    _answer_listing(fm)
    assert _rows(fm) == ["..", "data/", "model.pt"]
    assert fm.remote.topLevelItem(2).text(1) == "2.0 KB"


def test_entering_a_remote_folder_lists_it(fm):
    _answer_listing(fm)
    fm._remote_activated(fm.remote.topLevelItem(1))
    assert fm.calls[-1]["argv"] == ["ls", "-s", "s1", "--json", "content/data"]
    assert fm.prompt.text() == "/content/data$"


def test_f5_from_the_remote_pane_downloads_into_the_local_folder(fm, tmp_path):
    _answer_listing(fm)
    fm.active = "remote"
    fm.remote.topLevelItem(2).setSelected(True)
    fm.copy_selected()
    assert fm.calls[-1]["argv"] == [
        "download", "-s", "s1", "--via", "contents", "--progress", "content/model.pt",
        os.path.join(str(tmp_path), "model.pt"),
    ]


def test_ssh_transfer_is_remembered_and_used(fm, tmp_path):
    _answer_listing(fm)
    fm.via.setCurrentIndex(fm.via.findData("ssh"))
    assert fm.store.save.call_args.args[0].tray_transfer == "ssh"
    fm.upload([str(tmp_path / "local.txt")], "content")
    assert fm.calls[-1]["argv"][:4] == ["upload", "-s", "s1", "--via"]
    assert fm.calls[-1]["argv"][4] == "ssh"


def test_dropping_local_files_on_the_remote_pane_uploads_them(fm, tmp_path):
    _answer_listing(fm)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path / "local.txt"))])
    event = QDropEvent(QPointF(1, 1), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    fm.remote.dropEvent(event)
    argv = fm.calls[-1]["argv"]
    assert argv[:6] == ["upload", "-s", "s1", "--via", "contents", "--progress"]
    assert os.path.normpath(argv[6]) == os.path.normpath(str(tmp_path / "local.txt"))
    assert argv[7] == "content/local.txt"


def test_dragging_remote_items_carries_their_paths_to_the_local_pane(fm, tmp_path):
    _answer_listing(fm)
    mime = fm.remote.mimeData([fm.remote.topLevelItem(1), fm.remote.topLevelItem(2)])
    entries = json.loads(bytes(mime.data(files_mod.REMOTE_MIME)).decode())
    assert entries == [{"path": "content/data", "dir": True}, {"path": "content/model.pt", "dir": False}]
    event = QDropEvent(QPointF(-5, -5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    fm.local_view.dropEvent(event)
    assert fm.calls[-2]["argv"][:4] == ["download", "-s", "s1", "-r"]
    assert fm.calls[-1]["argv"][-1] == os.path.join(str(tmp_path), "model.pt")


def test_f8_deletes_a_remote_folder_after_asking(fm, mocker):
    _answer_listing(fm)
    fm.active = "remote"
    fm.remote.topLevelItem(1).setSelected(True)
    mocker.patch.object(fm, "confirm", return_value=True)
    fm.delete_selected()
    assert fm.calls[-1]["argv"] == ["rm", "-s", "s1", "-r", "content/data"]


def test_f8_cancelled_deletes_nothing(fm, mocker):
    _answer_listing(fm)
    fm.active = "remote"
    fm.remote.topLevelItem(2).setSelected(True)
    before = len(fm.calls)
    mocker.patch.object(fm, "confirm", return_value=False)
    fm.delete_selected()
    assert len(fm.calls) == before


def test_command_line_runs_in_the_remote_folder(fm):
    _answer_listing(fm)
    fm.command.setText("ls -la")
    fm.run_command()
    assert fm.calls[-1]["argv"] == ["exec", "-s", "s1"]
    assert "'/content'" in fm.calls[-1]["stdin"]
    fm.via.setCurrentIndex(fm.via.findData("ssh"))
    fm.command.setText("du -sh .")
    fm.run_command()
    assert fm.calls[-1]["argv"] == ["ssh", "-s", "s1", "--", "sh", "-c", "cd /content && du -sh ."]


def test_a_listing_error_is_shown_not_raised(fm):
    fm.calls[-1]["done"](1, json.dumps({"path": "content", "error": "File or directory not found: content"}))
    assert "not found" in fm.output.toPlainText()


def test_local_folders_open_with_enter_or_double_click_and_go_up(fm, tmp_path):
    (tmp_path / "inner").mkdir()
    index = fm.local_model.index(str(tmp_path / "inner"))
    fm.local_view.activated.emit(index)
    assert fm.local_dir == str(tmp_path / "inner")
    fm._local_up()
    assert fm.local_dir == str(tmp_path)


def test_the_drive_list_opens_a_drive(fm):
    assert fm.drives.count() >= 1
    first = fm.drives.itemText(0)
    fm.drives.textActivated.emit(first)
    assert os.path.normcase(fm.local_dir) == os.path.normcase(os.path.abspath(first))


def test_remote_folders_open_on_activation(fm):
    _answer_listing(fm)
    fm.remote.itemActivated.emit(fm.remote.topLevelItem(1), 0)
    assert fm.calls[-1]["argv"] == ["ls", "-s", "s1", "--json", "content/data"]


def test_upload_and_download_buttons(fm, tmp_path):
    _answer_listing(fm)
    before = len(fm.calls)
    fm.buttons["files.upload"].click()
    assert len(fm.calls) == before and "bal oldalon" in fm.status.text()

    fm.local_view.selectionModel().select(
        fm.local_model.index(str(tmp_path / "local.txt")),
        fm.local_view.selectionModel().SelectionFlag.Select | fm.local_view.selectionModel().SelectionFlag.Rows,
    )
    fm.buttons["files.upload"].click()
    assert fm.calls[-1]["argv"][0] == "upload" and fm.calls[-1]["argv"][-1] == "content/local.txt"

    fm.remote.topLevelItem(2).setSelected(True)
    fm.buttons["files.download"].click()
    assert fm.calls[-1]["argv"][0] == "download"
    assert fm.calls[-1]["argv"][-1] == os.path.join(str(tmp_path), "model.pt")


def test_a_gone_runtime_shows_a_banner_and_stops_remote_actions(fm, tmp_path):
    _answer_listing(fm)
    fm.queue.append((["upload"], None, "x", lambda c, o: None))
    fm.set_available(False)
    assert fm.banner.isVisibleTo(fm) and fm.queue == []
    assert not fm.buttons["files.upload"].isEnabled() and not fm.buttons["files.download"].isEnabled()
    assert not fm.remote.isEnabled() and not fm.command.isEnabled()
    before = len(fm.calls)
    fm.upload([str(tmp_path / "local.txt")], "content")
    fm.refresh_remote()
    assert len(fm.calls) == before
    assert fm.local_view.isEnabled()

    fm.set_available(True)
    assert not fm.banner.isVisibleTo(fm) and fm.remote.isEnabled()
    assert fm.calls[-1]["argv"] == ["ls", "-s", "s1", "--json", "content"]


def test_a_missing_session_in_the_output_marks_the_runtime_gone(fm):
    fm.calls[-1]["done"](1, "[colab] Session 's1' not found.\n")
    assert fm.banner.isVisibleTo(fm)


def test_a_failed_scp_warns_that_the_target_may_be_partial(fm, tmp_path):
    _answer_listing(fm)
    fm.via.setCurrentIndex(fm.via.findData("ssh"))
    fm.download([{"path": "content/model.pt", "dir": False}], str(tmp_path))
    fm.calls[-1]["done"](1, "[colab] Download over ssh failed (scp exit code 1).")
    text = fm.output.toPlainText()
    assert "csonka" in text and os.path.join(str(tmp_path), "model.pt") in text


def test_a_failed_contents_transfer_does_not_claim_a_partial_file(fm, tmp_path):
    _answer_listing(fm)
    fm.download([{"path": "content/model.pt", "dir": False}], str(tmp_path))
    fm.calls[-1]["done"](1, "[colab] Download failed: boom")
    assert "csonka" not in fm.output.toPlainText()


def test_progress_bar_shows_files_done_and_hides_when_all_done(fm):
    fm._job_started("updir feltöltése")
    assert fm.progress.isVisibleTo(fm) and fm.progress.maximum() == 0
    fm._feed_progress("[colab] progress 3/1")
    fm._feed_progress("0 content/updir/sub/b.txt\n[colab] Uploaded")
    assert (fm.progress.maximum(), fm.progress.value()) == (10, 3)
    assert "3 / 10 fájl" in fm.status.text() and "content/updir/sub/b.txt" in fm.status.text()
    assert "updir feltöltése" in fm.status.text() and "0:0" in fm.status.text()
    fm._all_done()
    assert not fm.progress.isVisibleTo(fm) and fm.status.text() == "Kész."


def test_progress_without_a_total_counts_files(fm):
    fm._job_started("letöltés")
    fm._feed_progress("[colab] progress 4/? content/x\n")
    assert fm.progress.maximum() == 0 and "4 fájl kész" in fm.status.text()


def test_cancel_kills_the_running_copy_and_drops_the_rest(fm, mocker):
    from PySide6.QtCore import QProcess

    fm._job_started("tree feltöltése")
    assert fm.cancel_button.isVisibleTo(fm)
    fm.queue[:] = [(["upload"], None, "a", None), (["upload"], None, "b", None)]
    fm.proc = MagicMock()
    fm.proc.state.return_value = QProcess.Running
    fm.proc.processId.return_value = 4321
    fm.job_via = "ssh"
    run = mocker.patch.object(files_mod.subprocess, "run")
    fm.cancel()
    if files_mod.sys.platform == "win32":
        assert run.call_args.args[0] == ["taskkill", "/PID", "4321", "/T", "/F"]
    else:
        fm.proc.terminate.assert_called_once()
    assert fm.queue == []
    text = fm.output.toPlainText()
    assert "Megszakítva: tree feltöltése (2 várakozó törölve)" in text and "csonka" in text
    fm.proc = None
    fm._all_done()
    assert not fm.cancel_button.isVisibleTo(fm)


def test_cancel_over_contents_says_copied_files_stay(fm, mocker):
    mocker.patch.object(files_mod.subprocess, "run")
    from PySide6.QtCore import QProcess

    fm._job_started("letöltés")
    fm.proc = MagicMock()
    fm.proc.state.return_value = QProcess.Running
    fm.job_via = "contents"
    fm.cancel()
    assert "átment fájlok megmaradnak" in fm.output.toPlainText()
    fm.proc = None
