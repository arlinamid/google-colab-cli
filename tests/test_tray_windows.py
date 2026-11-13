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

"""The tray's profile editor and log window, on Qt's offscreen platform (docs/09)."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from colab_cli.tray import windows
except ImportError as e:
    pytest.skip(f"Qt is not usable here: {e}", allow_module_level=True)

from colab_cli.profiles import load_profiles  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    # Delete what the tests made while Qt is still up: widgets and QObjects left
    # for interpreter shutdown were destroyed after Qt and crashed the process
    # on Windows (access violation after "N passed", exit code != 0).
    from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool

    QThreadPool.globalInstance().waitForDone(5000)
    for w in QApplication.topLevelWidgets():
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "profiles.toml"
    p.write_text('[train]\nscript = "t.py"\ngpu = "T4"\nbudget = 2.0\n\n'
                 '[ui]\nscript = "s.py"\nmode = "kernel"\nforward = ["8188"]\nopen = "http://x"\n',
                 encoding="utf-8")
    return str(p)


def test_editor_lists_and_shows_fields_by_mode(qapp, path):
    ed = windows.ProfileEditor(path, "hu")
    assert [ed.list.item(i).text() for i in range(ed.list.count())] == ["train", "ui"]
    assert ed.name.text() == "train" and ed.widgets["hardware"].currentData() == "T4"
    assert ed.widgets["budget"].text() == "2"
    row = lambda f: ed.rows[f]  # noqa: E731
    assert ed.form.isRowVisible(row("budget")) and not ed.form.isRowVisible(row("forward"))

    ed.list.setCurrentRow(1)
    assert ed.name.text() == "ui" and ed.widgets["forward"].text() == "8188"
    assert ed.form.isRowVisible(row("forward")) and not ed.form.isRowVisible(row("budget"))
    ed.close()


def test_editor_help_follows_the_focused_field(qapp, path):
    ed = windows.ProfileEditor(path, "hu")
    ed.show_help("secrets")
    assert "Titkok" in ed.help_title.text() and "colab secret set" in ed.help_text.text()
    assert "colab secret set" in ed.widgets["secrets"].toolTip()
    ed.close()


def test_editor_saves_renames_and_validates(qapp, path):
    started = []
    ed = windows.ProfileEditor(path, "hu", on_start=started.append)
    ed.widgets["budget"].setText("abc")
    assert ed.save() is None
    assert "Keret (CU)" in ed.status.text() and ed.widgets["budget"].property("invalid") is True
    assert "Compute Unit" in ed.help_text.text()

    ed.widgets["budget"].setText("1,5")
    ed.name.setText("train2")
    ed.save_and_start()
    profiles = load_profiles(path)
    assert set(profiles) == {"train2", "ui"} and profiles["train2"].budget == 1.5
    assert started == ["train2"] and "Mentve" in ed.status.text()

    ed.name.setText("ui")  # taken
    assert ed.save() is None and "ui" in ed.status.text()
    ed.close()


def test_editor_new_and_delete(qapp, path, mocker):
    ed = windows.ProfileEditor(path, "en")
    ed.new_profile()
    ed.name.setText("bad name")
    assert ed.save() is None and "letters" in ed.status.text()
    ed.name.setText("svc")
    ed.widgets["mode"].setCurrentIndex(1)
    ed.widgets["script"].setText("srv.py")
    ed.widgets["vars"].setPlainText('{"CONFIG": {"access": "forward"}}')
    assert ed.save() == "svc"
    assert load_profiles(path)["svc"].vars == {"CONFIG": {"access": "forward"}}

    mocker.patch.object(windows.QMessageBox, "question", return_value=QMessageBox.Yes)
    ed.delete_profile()
    assert "svc" not in load_profiles(path)
    ed.close()


def test_editor_with_no_file_starts_a_new_profile(qapp, tmp_path):
    ed = windows.ProfileEditor(str(tmp_path / "none.toml"), "hu")
    assert ed.list.count() == 0 and ed.name.text() == "" and "profil" in ed.help_text.text().lower()
    ed.close()


def test_log_window_shows_output_as_it_comes(qapp, tmp_path):
    script = tmp_path / "out.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n"
        "print('\\x1b[31mred\\x1b[0m')\n"
        "sys.stdout.write('10%\\r50%\\r100%\\n')\n"
        "print('ünnep')\n",
        encoding="utf-8",
    )
    w = windows.LogWindow("t", "help", lambda follow: [sys.executable, str(script)], "hu")
    assert w.proc.waitForFinished(15000)
    qapp.processEvents()
    w._read()
    text = w.text.toPlainText()
    assert "red" in text and "\x1b" not in text
    assert "100%" in text and "10%" not in text
    assert "ünnep" in text
    w.close()
