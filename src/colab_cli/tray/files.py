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

"""A two-pane file manager for one runtime (docs/09), in the style of
Total Commander: this computer on the left, the runtime on the right.

Drag and drop or F5 copies between the panes, F7 makes a folder, F8 deletes
(a local file goes to the recycle bin), Enter opens, Backspace goes up, and
the line at the bottom runs a command in the runtime's folder. Every action
runs one of the CLI's own subcommands (file_ops), one after the other; the
transfer goes over the contents API or over ssh (scp), as chosen at the top.
"""

import codecs
import json
import os
import subprocess
import sys
import time
from typing import Callable, List, Optional

from PySide6.QtCore import QDir, QMimeData, QProcess, QProcessEnvironment, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFileSystemModel,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from colab_cli.tray import file_ops
from colab_cli.tray.glyphs import icon as glyph_icon
from colab_cli.tray.i18n import t
from colab_cli.tray.windows import WINDOW_QSS

REMOTE_MIME = "application/x-colab-remote-paths"
REMOTE_ROOT = "content"

Run = Callable[[List[str], Optional[str], str, Callable[[int, str], None]], None]


class _RemoteTree(QTreeWidget):
    """The runtime's folder: drags out its paths, takes local files dropped in."""

    def __init__(self, owner: "FileManager"):
        super().__init__()
        self.owner = owner
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.setRootIsDecorated(False)

    def mimeTypes(self):
        return [REMOTE_MIME]

    def mimeData(self, items):
        data = QMimeData()
        data.setData(REMOTE_MIME, json.dumps(self.owner.remote_entries(items)).encode("utf-8"))
        return data

    def _local_paths(self, event) -> List[str]:
        if not event.mimeData().hasUrls():
            return []
        return [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]

    def dragEnterEvent(self, event):
        if self._local_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._local_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self._local_paths(event)
        if not paths:
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        target = self.owner.remote_dir
        if item is not None and item.data(0, Qt.UserRole + 1) and item.text(0) != "..":
            target = item.data(0, Qt.UserRole)
        event.acceptProposedAction()
        self.owner.upload(paths, target)


class _LocalView(QTreeView):
    """This computer's folder: drags out file URLs, takes the runtime's paths."""

    def __init__(self, owner: "FileManager"):
        super().__init__()
        self.owner = owner
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTreeView.DragDrop)
        self.setSelectionMode(QTreeView.ExtendedSelection)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(REMOTE_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(REMOTE_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(REMOTE_MIME):
            event.ignore()
            return
        entries = json.loads(bytes(event.mimeData().data(REMOTE_MIME)).decode("utf-8"))
        index = self.indexAt(event.position().toPoint())
        target = self.owner.local_dir
        if index.isValid() and self.owner.local_model.isDir(index):
            target = self.owner.local_model.filePath(index)
        event.acceptProposedAction()
        self.owner.download(entries, target)


class FileManager(QWidget):
    def __init__(self, session: str, lang: str, base_argv: List[str], settings_store=None,
                 run: Optional[Run] = None, start_dir: Optional[str] = None):
        super().__init__(None, Qt.Window)
        self.session, self.lang, self.base_argv = session, lang, base_argv
        self.settings_store = settings_store
        self.run = run or self._run_process
        self.queue: List[tuple] = []
        self.proc: Optional[QProcess] = None
        self.remote_dir = REMOTE_ROOT
        self.local_dir = start_dir or os.path.expanduser("~")
        self.active = "remote"
        self.available = True
        self.setObjectName("window")
        self.setStyleSheet(WINDOW_QSS)
        self.setWindowTitle(t(lang, "files.title", session=session))
        self.resize(1100, 680)
        box = QVBoxLayout(self)

        self.banner = QLabel(t(lang, "files.gone", session=session), wordWrap=True)
        self.banner.setStyleSheet(
            "background: #5c1d1d; color: #f6aea9; border: 1px solid #ea4335; border-radius: 6px; padding: 8px;"
        )
        self.banner.hide()
        box.addWidget(self.banner)

        top = QHBoxLayout()
        top.addWidget(QLabel(t(lang, "files.via")))
        self.via = QComboBox()
        self.via.addItem(t(lang, "files.via_contents"), "contents")
        self.via.addItem(t(lang, "files.via_ssh"), "ssh")
        self.via.setCurrentIndex(max(0, self.via.findData(self._saved_via())))
        self.via.currentIndexChanged.connect(self._save_via)
        top.addWidget(self.via)
        top.addStretch()
        self.refresh_button = QPushButton(t(lang, "log.refresh"))
        self.refresh_button.setIcon(glyph_icon("refresh", "#e8eaed"))
        self.refresh_button.clicked.connect(self.refresh_remote)
        top.addWidget(self.refresh_button)
        box.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self.local_path = QLineEdit(self.local_dir)
        self.local_path.returnPressed.connect(lambda: self.open_local(self.local_path.text()))
        self.local_model = QFileSystemModel(self)
        self.local_model.setRootPath("")
        self.local_model.setReadOnly(True)
        self.local_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot | QDir.AllDirs)
        self.local_view = _LocalView(self)
        self.local_view.setModel(self.local_model)
        self.local_view.setColumnWidth(0, 260)
        self.local_view.activated.connect(self._local_activated)
        self.drives = QComboBox()
        self.drives.addItems([QDir.toNativeSeparators(d.absolutePath()) for d in QDir.drives()])
        self.drives.setToolTip(t(lang, "files.drive"))
        self.drives.textActivated.connect(self.open_local)
        split.addWidget(self._pane(t(lang, "files.local"), self.local_path, self.local_view,
                                   self._local_up, lead=self.drives))

        self.remote_path = QLineEdit(self.remote_dir)
        self.remote_path.returnPressed.connect(lambda: self.open_remote(self.remote_path.text().strip("/")))
        self.remote = _RemoteTree(self)
        self.remote.setHeaderLabels([t(lang, "files.name"), t(lang, "files.size"), t(lang, "files.modified")])
        self.remote.setColumnWidth(0, 280)
        self.remote.itemActivated.connect(lambda item, _c: self._remote_activated(item))
        split.addWidget(self._pane(t(lang, "files.remote", session=session), self.remote_path, self.remote,
                                   self._remote_up))
        box.addWidget(split, 3)

        bar = QHBoxLayout()
        self.buttons = {}
        for key, slot in (("files.upload", self.upload_selected), ("files.download", self.download_selected),
                          ("files.mkdir", self.make_folder), ("files.delete", self.delete_selected)):
            button = QPushButton(t(lang, key))
            button.clicked.connect(slot)
            bar.addWidget(button, 1)
            self.buttons[key] = button
        box.addLayout(bar)
        box.addWidget(QLabel(t(lang, "files.keys"), objectName="muted"))
        cmd_row = QHBoxLayout()
        self.prompt = QLabel()
        cmd_row.addWidget(self.prompt)
        self.command = QLineEdit(placeholderText=t(lang, "files.command_hint"))
        self.command.returnPressed.connect(self.run_command)
        cmd_row.addWidget(self.command, 1)
        box.addLayout(cmd_row)
        self.output = QPlainTextEdit(objectName="log", readOnly=True)
        self.output.setMaximumBlockCount(5000)
        fixed = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.output.setFont(QFont(fixed.family(), 10))
        box.addWidget(self.output, 1)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.hide()
        progress_row.addWidget(self.progress, 1)
        self.cancel_button = QPushButton(t(lang, "files.cancel"), objectName="danger")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.hide()
        progress_row.addWidget(self.cancel_button)
        box.addLayout(progress_row)
        self.job_via = None
        self.status = QLabel(objectName="muted")
        box.addWidget(self.status)
        self.job_label, self.job_start, self.job_progress, self._line_buf = "", 0.0, None, ""
        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self._tick)

        for key, slot in ((Qt.Key_F5, self.copy_selected), (Qt.Key_F7, self.make_folder),
                          (Qt.Key_F8, self.delete_selected)):
            QShortcut(QKeySequence(key), self, slot)
        # Enter and double-click arrive as `activated`; Backspace goes up.
        for view, up in ((self.local_view, self._local_up), (self.remote, self._remote_up)):
            QShortcut(QKeySequence(Qt.Key_Backspace), view, up, context=Qt.WidgetShortcut)
        self.local_view.installEventFilter(self)
        self.remote.installEventFilter(self)

        self.open_local(self.local_dir)
        self.open_remote(REMOTE_ROOT)

    def _pane(self, title: str, path: QLineEdit, view: QWidget, up: Callable[[], None],
              lead: Optional[QWidget] = None) -> QWidget:
        pane = QWidget()
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(title, objectName="helptitle"))
        row = QHBoxLayout()
        if lead is not None:
            row.addWidget(lead)
        row.addWidget(path, 1)
        button = QPushButton("⬆ ..")
        button.setToolTip(t(self.lang, "files.up"))
        button.clicked.connect(up)
        row.addWidget(button)
        lay.addLayout(row)
        lay.addWidget(view, 1)
        return pane

    def eventFilter(self, obj, event):
        if event.type() == event.Type.FocusIn:
            self.active = "local" if obj is self.local_view else "remote"
        return False

    # --- settings ---

    def _saved_via(self) -> str:
        try:
            return self.settings_store.load().tray_transfer
        except Exception:
            return "contents"

    def _save_via(self) -> None:
        if self.settings_store is None:
            return
        try:
            settings = self.settings_store.load()
            settings.tray_transfer = self.via.currentData()
            self.settings_store.save(settings)
        except Exception:
            pass

    # --- running CLI commands, one at a time ---

    def submit(self, argv: List[str], label: str, done: Optional[Callable[[int, str], None]] = None,
               stdin: Optional[str] = None) -> None:
        if not self.available:
            self.status.setText(t(self.lang, "files.gone_short"))
            return
        done = done or (lambda code, out: None)
        missing = f"Session '{self.session}' not found"

        def checked(code: int, out: str) -> None:
            done(code, out)
            if code and missing in out:
                self.set_available(False)

        self.run(argv, stdin, label, checked)

    def set_available(self, available: bool) -> None:
        """Called by the tray on each refresh, and when a command finds no session.

        Without the runtime the remote side is read-only history: the banner
        says so, waiting commands are dropped, and only the local pane works.
        """
        if available == self.available:
            return
        self.available = available
        self.banner.setVisible(not available)
        for widget in (self.remote, self.remote_path, self.command, self.via, self.refresh_button,
                       self.buttons["files.upload"], self.buttons["files.download"]):
            widget.setEnabled(available)
        if not available:
            self.queue.clear()
            self.status.setText(t(self.lang, "files.gone_short"))
        else:
            self.refresh_remote()

    def _run_process(self, argv, stdin, label, done) -> None:
        self.queue.append((argv, stdin, label, done))
        if self.proc is None:
            self._next()

    def _next(self) -> None:
        if not self.queue:
            self.proc = None
            self._all_done()
            return
        argv, stdin, label, done = self.queue.pop(0)
        self.job_via = argv[argv.index("--via") + 1] if "--via" in argv else None
        self._job_started(label)
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        for k, v in (("NO_COLOR", "1"), ("PYTHONIOENCODING", "utf-8"), ("TERM", "dumb")):
            env.insert(k, v)
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        chunks: List[str] = []

        def read():
            text = decoder.decode(bytes(proc.readAllStandardOutput()))
            chunks.append(text)
            self._feed_progress(text)

        proc.readyReadStandardOutput.connect(read)

        def finished(code, _status=None):
            chunks.append(decoder.decode(b"", final=True))
            try:
                done(code, "".join(chunks))
            finally:
                proc.deleteLater()
                self._next()

        proc.finished.connect(finished)
        self.proc = proc
        full = [*self.base_argv, *argv]
        proc.start(full[0], full[1:])
        if stdin is not None:
            proc.write(stdin.encode("utf-8"))
        proc.closeWriteChannel()

    # --- progress: a bar, the file being copied, the time so far ---

    def _job_started(self, label: str) -> None:
        self.job_label, self.job_start, self.job_progress, self._line_buf = label, time.monotonic(), None, ""
        self.progress.setRange(0, 0)
        self.progress.show()
        self.cancel_button.show()
        self._tick()
        self.ticker.start(1000)

    def _feed_progress(self, text: str) -> None:
        self._line_buf += text
        *lines, self._line_buf = self._line_buf.split("\n")
        for line in lines:
            parsed = file_ops.parse_progress(line)
            if parsed:
                self._set_progress(*parsed)

    def _set_progress(self, done: int, total: Optional[int], path: str) -> None:
        self.job_progress = (done, total, path)
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self._tick()

    def _tick(self) -> None:
        seconds = int(time.monotonic() - self.job_start)
        parts = [t(self.lang, "files.now", what=self.job_label)]
        if self.job_progress:
            done, total, path = self.job_progress
            parts.append(t(self.lang, "files.of_total", done=done, total=total) if total
                         else t(self.lang, "files.count", done=done))
            parts.append(path)
        parts.append(f"{seconds // 60}:{seconds % 60:02d}")
        if self.queue:
            parts.append(t(self.lang, "files.waiting", left=len(self.queue)))
        self.status.setText(" · ".join(parts))

    def cancel(self) -> None:
        """Stop the running command and drop the waiting ones."""
        dropped = len(self.queue)
        self.queue.clear()
        running = self.proc is not None and self.proc.state() != QProcess.NotRunning
        if running:
            self.log(t(self.lang, "files.cancelled", what=self.job_label, dropped=dropped))
            if self.job_via == "ssh":
                self.log(t(self.lang, "files.cancelled_ssh"))
            elif self.job_via == "contents":
                self.log(t(self.lang, "files.cancelled_contents"))
            self._stop_process_tree(self.proc)
        elif dropped:
            self.log(t(self.lang, "files.cancelled", what="", dropped=dropped))

    @staticmethod
    def _stop_process_tree(proc: QProcess) -> None:
        """Stop the CLI and what it started (scp): killing only the CLI leaves
        scp copying on Windows, where children outlive their parent."""
        pid = proc.processId()
        if sys.platform == "win32" and pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        proc.terminate()
        QTimer.singleShot(5000, lambda: proc.state() != QProcess.NotRunning and proc.kill())

    def _all_done(self) -> None:
        self.ticker.stop()
        self.progress.hide()
        self.cancel_button.hide()
        self.status.setText(t(self.lang, "files.idle" if self.available else "files.gone_short"))

    def log(self, text: str) -> None:
        self.output.appendPlainText(text.rstrip("\n"))

    # --- the remote pane ---

    def open_remote(self, path: str) -> None:
        self.remote_dir = path
        self.remote_path.setText(path)
        self.prompt.setText(f"/{path}$")
        self.submit(file_ops.ls_argv(self.session, path), t(self.lang, "files.listing", path=path),
                    lambda code, out, p=path: self._fill_remote(p, code, out))

    def refresh_remote(self) -> None:
        self.open_remote(self.remote_dir)

    def _fill_remote(self, path: str, code: int, out: str) -> None:
        if path != self.remote_dir:
            return
        try:
            items = file_ops.parse_listing(out)
        except ValueError as e:
            self.log(f"[{path}] {e}")
            return
        self.remote.clear()
        if path:
            self._add_remote_item("..", file_ops.remote_parent(path), True, None, None)
        for item in items:
            is_dir = item.get("type") == "directory"
            self._add_remote_item(item["name"], file_ops.remote_join(path, item["name"]), is_dir,
                                  item.get("size"), item.get("last_modified"))

    def _add_remote_item(self, name, full, is_dir, size, modified) -> None:
        row = QTreeWidgetItem([name + ("/" if is_dir and name != ".." else ""),
                               "" if is_dir else file_ops.human_size(size),
                               (modified or "")[:19].replace("T", " ")])
        row.setData(0, Qt.UserRole, full)
        row.setData(0, Qt.UserRole + 1, is_dir)
        self.remote.addTopLevelItem(row)

    def remote_entries(self, items) -> List[dict]:
        return [{"path": i.data(0, Qt.UserRole), "dir": bool(i.data(0, Qt.UserRole + 1))}
                for i in items if i.text(0) != ".."]

    def _remote_activated(self, item) -> None:
        if item.data(0, Qt.UserRole + 1):
            self.open_remote(item.data(0, Qt.UserRole))

    def _remote_up(self) -> None:
        if self.remote_dir:
            self.open_remote(file_ops.remote_parent(self.remote_dir))

    # --- the local pane ---

    def open_local(self, path: str) -> None:
        if not os.path.isdir(path):
            self.log(t(self.lang, "files.no_folder", path=path))
            return
        self.local_dir = os.path.abspath(path)
        self.local_path.setText(self.local_dir)
        self.local_view.setRootIndex(self.local_model.index(self.local_dir))
        drive = os.path.splitdrive(self.local_dir)[0]
        for i in range(self.drives.count()):
            if drive and self.drives.itemText(i).upper().startswith(drive.upper()):
                self.drives.setCurrentIndex(i)

    def _local_activated(self, index) -> None:
        path = self.local_model.filePath(index)
        if self.local_model.isDir(index):
            self.open_local(path)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _local_up(self) -> None:
        parent = os.path.dirname(self.local_dir.rstrip("/\\"))
        if parent and parent != self.local_dir:
            self.open_local(parent)

    def local_selection(self) -> List[str]:
        rows = self.local_view.selectionModel().selectedRows() if self.local_view.selectionModel() else []
        return [self.local_model.filePath(i) for i in rows]

    # --- actions ---

    def upload(self, paths: List[str], target: str) -> None:
        via = self.via.currentData()
        for path in paths:
            argv = file_ops.copy_argv(self.session, path, target, upload=True, is_dir=os.path.isdir(path), via=via)
            self.submit(argv, t(self.lang, "files.uploading", name=os.path.basename(path.rstrip("/\\"))),
                        self._transfer_done(argv[-1], via))

    def download(self, entries: List[dict], target: str) -> None:
        via = self.via.currentData()
        for entry in entries:
            argv = file_ops.copy_argv(self.session, entry["path"], target, upload=False, is_dir=entry["dir"], via=via)
            self.submit(argv, t(self.lang, "files.downloading", name=entry["path"].rsplit("/", 1)[-1]),
                        self._transfer_done(argv[-1], via))

    def _transfer_done(self, target: str, via: str) -> Callable[[int, str], None]:
        """scp writes as it goes: after a failure the target may be cut short.

        The contents API writes a file whole or not at all, so only files
        already copied stay; nothing needs saying then.
        """
        def done(code: int, out: str) -> None:
            self._after_transfer(code, out)
            if code and via == "ssh":
                self.log(t(self.lang, "files.partial", path=target))
        return done

    def _after_transfer(self, code: int, out: str) -> None:
        self.log(out)
        if self.available:
            self.refresh_remote()

    def upload_selected(self) -> None:
        paths = self.local_selection()
        if not paths:
            self.status.setText(t(self.lang, "files.select_local"))
            return
        self.upload(paths, self.remote_dir)

    def download_selected(self) -> None:
        entries = self.remote_entries(self.remote.selectedItems())
        if not entries:
            self.status.setText(t(self.lang, "files.select_remote"))
            return
        self.download(entries, self.local_dir)

    def copy_selected(self) -> None:
        if self.active == "local":
            self.upload(self.local_selection(), self.remote_dir)
        else:
            self.download(self.remote_entries(self.remote.selectedItems()), self.local_dir)

    def make_folder(self) -> None:
        name, ok = QInputDialog.getText(self, t(self.lang, "files.mkdir_title"), t(self.lang, "files.mkdir_prompt"))
        name = name.strip().strip("/\\")
        if not ok or not name:
            return
        if self.active == "local":
            os.makedirs(os.path.join(self.local_dir, name), exist_ok=True)
        else:
            self.submit(file_ops.mkdir_argv(self.session, file_ops.remote_join(self.remote_dir, name)),
                        t(self.lang, "files.mkdir_title"), self._after_transfer)

    def confirm(self, text: str) -> bool:
        box = QMessageBox(QMessageBox.Question, t(self.lang, "files.delete_title"), text,
                          QMessageBox.Yes | QMessageBox.Cancel, self)
        return box.exec() == QMessageBox.Yes

    def delete_selected(self) -> None:
        if self.active == "local":
            paths = self.local_selection()
            if paths and self.confirm(t(self.lang, "files.delete_local", names=", ".join(map(os.path.basename, paths)))):
                from PySide6.QtCore import QFile

                for path in paths:
                    if not QFile.moveToTrash(path):
                        self.log(t(self.lang, "files.trash_failed", path=path))
            return
        entries = self.remote_entries(self.remote.selectedItems())
        names = ", ".join(e["path"].rsplit("/", 1)[-1] for e in entries)
        if entries and self.confirm(t(self.lang, "files.delete_remote", names=names)):
            for entry in entries:
                self.submit(file_ops.rm_argv(self.session, entry["path"], is_dir=entry["dir"]),
                            t(self.lang, "files.deleting", name=entry["path"]), self._after_transfer)

    def run_command(self) -> None:
        text = self.command.text().strip()
        if not text:
            return
        self.command.clear()
        self.log(f"/{self.remote_dir}$ {text}")
        argv, stdin = file_ops.command_argv(self.session, self.remote_dir, text, via=self.via.currentData())
        self.submit(argv, text, self._after_transfer, stdin=stdin)

    def closeEvent(self, event):
        self.queue.clear()
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(3000)
        super().closeEvent(event)
