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

"""The tray's own windows (docs/09): the profile editor and the log viewer.

Both have a help text: the editor explains the field you are in, the log
window what it shows and where the full log is. The editor reads and writes
profiles.toml through colab_cli.profiles; the log window runs a CLI
subcommand (`colab job tail` / `colab log`) with QProcess and shows its
output as it comes.
"""

import codecs
import re
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QEvent, QObject, QProcess, QProcessEnvironment, Qt
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from colab_cli.profiles import ProfileError, load_profiles, remove_profile, save_profile
from colab_cli.tray.glyphs import icon as glyph_icon
from colab_cli.tray.i18n import t
from colab_cli.tray.profile_form import FIELDS, FormError, from_form, hardware_choices, to_form, valid_name

WINDOW_QSS = """
QWidget#window { background: #202124; }
QWidget { color: #e8eaed; font-family: "Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Noto Sans", sans-serif; font-size: 13px; }
QLabel#muted { color: #9aa0a6; }
QLabel#title { font-size: 16px; font-weight: 600; }
QFrame#help { background: #2b2c2f; border: 1px solid #3c4043; border-radius: 8px; }
QLabel#helptitle { color: #fdd663; font-weight: 600; }
QLabel#error { color: #f28b82; }
QLabel#ok { color: #81c995; }
QLineEdit, QPlainTextEdit, QComboBox, QListWidget {
  background: #2b2c2f; border: 1px solid #5f6368; border-radius: 6px; padding: 4px 6px;
  selection-background-color: #8a4b08; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: #f9ab00; }
QLineEdit[invalid="true"], QPlainTextEdit[invalid="true"] { border-color: #ea4335; }
QComboBox QAbstractItemView { background: #2b2c2f; selection-background-color: #3c4043; }
QListWidget::item { padding: 6px; }
QListWidget::item:selected { background: #3c4043; color: #ffffff; }
QPushButton { background: #2b2c2f; border: 1px solid #5f6368; border-radius: 6px; padding: 6px 12px; }
QPushButton:hover { background: #35363a; }
QPushButton#primary { background: #e8710a; border: none; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #f9ab00; }
QPushButton#danger { color: #f28b82; border-color: #ea4335; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #5f6368; border-radius: 4px; background: #2b2c2f; }
QCheckBox::indicator:checked { background: #e8710a; border-color: #e8710a; }
QScrollBar:vertical, QScrollBar:horizontal { background: transparent; width: 8px; height: 8px; }
QScrollBar::handle { background: #5f6368; border-radius: 3px; min-height: 24px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QPlainTextEdit#log { font-family: "Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", monospace; font-size: 12px; }
"""

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


class _FocusHelp(QObject):
    """Shows a field's help when one of its widgets gets the focus."""

    def __init__(self, show: Callable[[str], None], field: str):
        super().__init__()
        self.show, self.field = show, field

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            self.show(self.field)
        return False


class ProfileEditor(QWidget):
    """A form for profiles.toml: list on the left, fields and help on the right."""

    def __init__(self, path: str, lang: str, on_start: Optional[Callable[[str], None]] = None,
                 on_text: Optional[Callable[[], None]] = None):
        super().__init__(None, Qt.Window)
        self.path, self.lang = path, lang
        self.on_start, self.on_text = on_start, on_text
        self.current: Optional[str] = None
        self._filters: List[_FocusHelp] = []
        self.setObjectName("window")
        self.setStyleSheet(WINDOW_QSS)
        self.setWindowTitle(t(lang, "editor.title"))
        self.resize(860, 640)

        root = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(QLabel(t(lang, "editor.profiles"), objectName="title"))
        self.list = QListWidget()
        self.list.currentTextChanged.connect(self._select)
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.new_button = QPushButton(t(lang, "editor.new"))
        self.new_button.setIcon(glyph_icon("plus", "#e8eaed"))
        self.new_button.clicked.connect(self.new_profile)
        self.delete_button = QPushButton(t(lang, "editor.delete"), objectName="danger")
        self.delete_button.clicked.connect(self.delete_profile)
        row.addWidget(self.new_button)
        row.addWidget(self.delete_button)
        left.addLayout(row)
        root.addLayout(left, 1)

        right = QVBoxLayout()
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.name = QLineEdit()
        self._watch(self.name, "name")
        self.form.addRow(t(lang, "editor.name"), self.name)
        self.widgets: Dict[str, QWidget] = {}
        self.rows: Dict[str, int] = {}
        for field, kind, _ in FIELDS:
            widget, focus = self._widget(field, kind)
            self.widgets[field] = focus
            self.rows[field] = self.form.rowCount()
            self.form.addRow(t(lang, f"field.{field}"), widget)
        right.addLayout(self.form)

        self.help_frame = QFrame(objectName="help")
        help_box = QVBoxLayout(self.help_frame)
        self.help_title = QLabel(t(lang, "editor.help_title"), objectName="helptitle")
        self.help_text = QLabel(t(lang, "editor.intro"), wordWrap=True)
        self.help_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        help_box.addWidget(self.help_title)
        help_box.addWidget(self.help_text)
        right.addWidget(self.help_frame)

        self.status = QLabel("", objectName="muted", wordWrap=True)
        right.addWidget(self.status)
        buttons = QHBoxLayout()
        self.text_button = QPushButton(t(lang, "editor.as_text"))
        self.text_button.clicked.connect(lambda: self.on_text and self.on_text())
        buttons.addWidget(self.text_button)
        buttons.addStretch()
        self.save_button = QPushButton(t(lang, "editor.save"))
        self.save_button.clicked.connect(self.save)
        self.start_button = QPushButton(t(lang, "editor.run"), objectName="primary")
        self.start_button.clicked.connect(self.save_and_start)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.start_button)
        right.addLayout(buttons)
        root.addLayout(right, 3)
        self.reload()

    # --- widgets ---

    def _watch(self, widget: QWidget, field: str) -> None:
        f = _FocusHelp(self.show_help, field)
        widget.installEventFilter(f)
        self._filters.append(f)
        widget.setToolTip(t(self.lang, f"help.{field}"))

    def _widget(self, field: str, kind: str):
        if kind == "choice":
            box = QComboBox()
            if field == "mode":
                box.addItem(t(self.lang, "mode.job.short"), "job")
                box.addItem(t(self.lang, "mode.kernel.short"), "kernel")
                box.currentIndexChanged.connect(self._mode_changed)
            else:
                for choice in hardware_choices():
                    box.addItem(choice, choice)
            self._watch(box, field)
            return box, box
        if kind == "check":
            box = QCheckBox()
            self._watch(box, field)
            return box, box
        if kind == "text":
            edit = QPlainTextEdit()
            edit.setTabChangesFocus(True)
            edit.setFixedHeight(64)
            self._watch(edit, field)
            return edit, edit
        edit = QLineEdit()
        self._watch(edit, field)
        if kind != "file":
            return edit, edit
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        browse = QPushButton(t(self.lang, "browse"))
        browse.clicked.connect(lambda _=False, e=edit, f=field: self._browse(e, f))
        row.addWidget(browse)
        return holder, edit

    def _browse(self, edit: QLineEdit, field: str) -> None:
        pattern = "Python / notebook (*.py *.ipynb);;*" if field == "script" else "*"
        path, _ = QFileDialog.getOpenFileName(self, t(self.lang, f"field.{field}"), edit.text(), pattern)
        if path:
            edit.setText(path)

    def show_help(self, field: str) -> None:
        label = t(self.lang, "editor.name") if field == "name" else t(self.lang, f"field.{field}")
        self.help_title.setText(f"{t(self.lang, 'editor.help_title')}: {label}")
        self.help_text.setText(t(self.lang, f"help.{field}"))

    def _mode_changed(self, *_):
        mode = self.widgets["mode"].currentData()
        for field, _, modes in FIELDS:
            self.form.setRowVisible(self.rows[field], mode in modes)

    # --- data ---

    def reload(self, select: Optional[str] = None) -> None:
        try:
            self.profiles = load_profiles(self.path)
            self._set_status("")
        except ProfileError as e:
            self.profiles = {}
            self._set_status(str(e), "error")
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems(list(self.profiles))
        self.list.blockSignals(False)
        names = list(self.profiles)
        target = select if select in self.profiles else (names[0] if names else None)
        if target:
            self.list.setCurrentRow(names.index(target))
            self._select(target)
        else:
            self.new_profile()

    def _select(self, name: str) -> None:
        if not name or name not in self.profiles:
            return
        self.current = name
        self.name.setText(name)
        self.set_values(to_form(self.profiles[name]))

    def set_values(self, values: Dict) -> None:
        for field, kind, _ in FIELDS:
            w = self.widgets[field]
            value = values[field]
            if kind == "choice":
                w.setCurrentIndex(max(0, w.findData(value)))
            elif kind == "check":
                w.setChecked(bool(value))
            elif kind == "text":
                w.setPlainText(value)
            else:
                w.setText(value)
            w.setProperty("invalid", False)
            w.style().polish(w)
        self._mode_changed()

    def values(self) -> Dict:
        out = {}
        for field, kind, _ in FIELDS:
            w = self.widgets[field]
            if kind == "choice":
                out[field] = w.currentData()
            elif kind == "check":
                out[field] = w.isChecked()
            elif kind == "text":
                out[field] = w.toPlainText()
            else:
                out[field] = w.text()
        return out

    def _set_status(self, text: str, kind: str = "muted") -> None:
        self.status.setObjectName(kind)
        self.status.setText(text)
        self.status.style().polish(self.status)

    def new_profile(self) -> None:
        self.current = None
        self.list.clearSelection()
        self.name.setText("")
        self.set_values(to_form(None))
        self.name.setFocus()
        self.help_title.setText(t(self.lang, "editor.help_title"))
        self.help_text.setText(t(self.lang, "editor.intro"))

    def save(self) -> Optional[str]:
        """Validate and write; returns the saved name, or None (error shown)."""
        name = self.name.text().strip()
        if not valid_name(name):
            self._set_status(t(self.lang, "editor.bad_name"), "error")
            self.show_help("name")
            self.name.setFocus()
            return None
        if name != self.current and name in self.profiles:
            self._set_status(t(self.lang, "editor.exists", name=name), "error")
            return None
        try:
            profile = from_form(self.values())
        except FormError as e:
            w = self.widgets.get(e.field)
            if w is not None:
                w.setProperty("invalid", True)
                w.style().polish(w)
                w.setFocus()
            self.show_help(e.field)
            self._set_status(f"{t(self.lang, 'field.' + e.field)}: {e.message}", "error")
            return None
        if self.current and self.current != name:
            remove_profile(self.path, self.current)
        save_profile(self.path, name, profile)
        self.reload(select=name)
        self._set_status(t(self.lang, "editor.saved", name=name, path=self.path), "ok")
        return name

    def save_and_start(self) -> None:
        name = self.save()
        if name and self.on_start:
            self.on_start(name)

    def delete_profile(self) -> None:
        if not self.current:
            return
        answer = QMessageBox.question(self, t(self.lang, "editor.delete"),
                                      t(self.lang, "editor.confirm_delete", name=self.current))
        if answer != QMessageBox.Yes:
            return
        name = self.current
        remove_profile(self.path, name)
        self.reload()
        self._set_status(t(self.lang, "editor.deleted", name=name), "ok")


class LogWindow(QWidget):
    """Runs a CLI command and shows its output as it comes, with a help line.

    ``command(follow)`` returns the argv; with ``can_follow`` a Follow switch
    restarts it with or without following.
    """

    def __init__(self, title: str, help_text: str, command: Callable[[bool], List[str]],
                 lang: str, can_follow: bool = False):
        super().__init__(None, Qt.Window)
        self.command, self.lang = command, lang
        self.setObjectName("window")
        self.setStyleSheet(WINDOW_QSS)
        self.setWindowTitle(title)
        self.resize(900, 560)
        box = QVBoxLayout(self)
        help_frame = QFrame(objectName="help")
        hb = QVBoxLayout(help_frame)
        hb.addWidget(QLabel(t(lang, "editor.help_title"), objectName="helptitle"))
        self.help = QLabel(help_text, wordWrap=True)
        self.help.setTextInteractionFlags(Qt.TextSelectableByMouse)
        hb.addWidget(self.help)
        box.addWidget(help_frame)
        self.text = QPlainTextEdit(objectName="log", readOnly=True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setMaximumBlockCount(20000)
        fixed = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.text.setFont(QFont(fixed.family(), 10))
        box.addWidget(self.text, 1)
        row = QHBoxLayout()
        self.follow = QCheckBox(t(lang, "log.follow"))
        self.follow.setChecked(can_follow)
        self.follow.setVisible(can_follow)
        self.follow.toggled.connect(lambda _: self.start())
        row.addWidget(self.follow)
        row.addStretch()
        refresh = QPushButton(t(lang, "log.refresh"))
        refresh.setIcon(glyph_icon("refresh", "#e8eaed"))
        refresh.clicked.connect(self.start)
        copy = QPushButton(t(lang, "log.copy"))
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text.toPlainText()))
        row.addWidget(refresh)
        row.addWidget(copy)
        box.addLayout(row)
        self.proc: Optional[QProcess] = None
        self.start()

    def start(self) -> None:
        self.stop()
        self.text.clear()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        argv = self.command(self.follow.isChecked() and self.follow.isVisible())
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        for k, v in (("NO_COLOR", "1"), ("PYTHONIOENCODING", "utf-8"), ("COLUMNS", "200"), ("TERM", "dumb")):
            env.insert(k, v)
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self._read)
        proc.finished.connect(self._finished)
        self.proc = proc
        proc.start(argv[0], argv[1:])

    def _read(self) -> None:
        data = bytes(self.proc.readAllStandardOutput())
        self.append(self._decoder.decode(data))

    def append(self, text: str) -> None:
        text = _ANSI.sub("", text).replace("\r\n", "\n")
        text = "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))  # progress bars
        bar = self.text.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        cursor = self.text.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        if at_end:
            bar.setValue(bar.maximum())

    def _finished(self, code, _status=None) -> None:
        self.append("\n" + t(self.lang, "log.ended", code=code) + "\n")

    def stop(self) -> None:
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.proc.finished.disconnect(self._finished)
            self.proc.kill()
            self.proc.waitForFinished(3000)
        self.proc = None

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)
