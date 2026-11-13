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

"""The tray process (docs/09): icon, panel, refresh, notifications, actions.

One Qt event loop. A QTimer starts collect_snapshot() on a worker thread
(the only thread that uses the CLI state); its result comes back as a
signal. Every action runs one of the CLI's subcommands as a child process
(`colab_argv()`): `colab job stop`, `colab stop`, `colab profile run`, and
`colab job tail` / `colab ssh` in a new terminal.
"""

import os
import subprocess
import time
import webbrowser
from typing import Any, Dict, List, Optional, Set

from PySide6.QtCore import QLocale, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSystemTrayIcon,
    QVBoxLayout,
)

from colab_cli.common import colab_argv
from colab_cli.profiles import ProfileError, load_profiles, profile_summary, profiles_path
from colab_cli.tray import actions
from colab_cli.tray.i18n import fmt_age, pick_language, t
from colab_cli.tray.icon import status_icon
from colab_cli.tray.panel import Panel
from colab_cli.tray_model import (
    card_model,
    collect_snapshot,
    cu_model,
    events,
    icon_state,
    merge_starts,
    sessions_to_stop,
    start_progress,
)


START_REFRESH_MS = 10_000  # refresh this often while a profile's runtime is being created
_RUN_LOG_PART = 64 * 1024


def _read_run_log(path: str, offset: int) -> str:
    """A profile run's output since ``offset``: its start and its newest part."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(min(offset, size))
            if size - offset <= 2 * _RUN_LOG_PART:
                data = f.read()
            else:
                head = f.read(_RUN_LOG_PART)
                f.seek(size - _RUN_LOG_PART)
                data = head + b"\n" + f.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


class _Signals(QObject):
    done = Signal(dict)


class _Refresh(QRunnable):
    def __init__(self, state, prev, signals):
        super().__init__()
        self.state, self.prev, self.signals = state, prev, signals

    def run(self):
        self.signals.done.emit(collect_snapshot(self.state, prev=self.prev))


class TrayApp(QObject):
    def __init__(self, state, interval: float):
        super().__init__()
        self.state = state
        self.settings_store = state.settings_store
        self.settings = self.settings_store.load()
        self.interval = self.settings.tray_interval or interval
        self.lang = pick_language(self.settings.tray_language, QLocale.system().name())
        self.snapshot: Optional[Dict[str, Any]] = None
        self.busy = False
        self.stopping: Set[str] = set()
        self.children: Dict[str, Dict[str, Any]] = {}  # profile -> {proc, log, session, mode}
        self.profiles = {}
        self.log_dir = os.path.join(os.path.dirname(profiles_path(state.config_path)), "tray-logs")

        self.signals = _Signals()
        self.signals.done.connect(self.on_snapshot)
        self.tray = QSystemTrayIcon(status_icon("#9aa0a6"))
        self.tray.activated.connect(self._activated)
        menu = QMenu()
        for key, slot in (("refresh_now", self.refresh), ("settings", self.open_settings), ("quit", self.quit)):
            action = QAction(t(self.lang, key), menu)
            action.triggered.connect(slot)
            menu.addAction(action)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(t(self.lang, "tooltip.prefix"))

        self.panel = Panel()
        self.panel.log_clicked.connect(self.open_log)
        self.panel.ssh_clicked.connect(lambda c: self.in_terminal(actions.ssh_argv(c)))
        self.panel.open_clicked.connect(self.open_service)
        self.panel.stop_clicked.connect(self.ask_stop)
        self.panel.profile_clicked.connect(self.start_profile)
        self.panel.refresh_clicked.connect(self.refresh)
        self.panel.settings_clicked.connect(self.open_settings)
        self.panel.edit_profiles_clicked.connect(self.open_profiles)
        self.windows = []  # open log / editor windows (kept alive here)
        self.panel.quit_clicked.connect(self.quit)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.ticker = QTimer(self)  # "Updated: 12 s ago" and stale detection
        self.ticker.timeout.connect(self.redraw)

    def start(self):
        self.tray.show()
        self.timer.start(int(self.interval * 1000))
        self.ticker.start(5000)
        self.refresh()

    # --- refresh ---

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        QThreadPool.globalInstance().start(_Refresh(self.state, self.snapshot, self.signals))

    def on_snapshot(self, snap: Dict[str, Any]):
        self.busy = False
        prev, self.snapshot = self.snapshot, snap
        self._reap_children()
        if self.settings.tray_notifications:
            for event in events(prev, snap, low_balance_hours=self.settings.tray_low_balance_hours):
                self.notify(actions.notification(event, self.lang),
                            QSystemTrayIcon.Critical if event["kind"] in ("job_failed", "session_gone")
                            else QSystemTrayIcon.Information)
        for name in sessions_to_stop(snap, self.stopping):
            self.stopping.add(name)
            self.notify(t(self.lang, "notify.stopping", session=name))
            self.run_cli(["stop", "-s", name])
        if any(s["progress"]["phase"] != "ready" for s in self.starts()):
            # A runtime is being created: show it as soon as it is there.
            QTimer.singleShot(START_REFRESH_MS, self.refresh)
        self.redraw()

    def starts(self) -> List[Dict[str, Any]]:
        """The profile runs this tray started that still run, with their progress."""
        out = []
        for name, child in self.children.items():
            if child["proc"].poll() is not None:
                continue
            out.append({
                "profile": name, "session": child.get("session"), "hardware": child.get("hardware"),
                "mode": child["mode"], "started_at": child.get("started_at") or time.time(),
                "progress": start_progress(_read_run_log(child["log"], child.get("offset", 0))),
            })
        return out

    def services(self) -> Set[str]:
        return {c["session"] for c in self.children.values()
                if c["mode"] == "kernel" and c["proc"].poll() is None and c["session"]}

    def _reap_children(self):
        for name, child in list(self.children.items()):
            code = child["proc"].poll()
            if code is None:
                continue
            del self.children[name]
            if code != 0:
                self.notify(t(self.lang, "notify.profile_failed", name=name, code=code, log=child["log"]),
                            QSystemTrayIcon.Warning)

    # --- drawing ---

    def view(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        snap = self.snapshot or {"sessions": [], "ok": True, "account": None, "last_ok_at": None}
        services = self.services()
        icon = icon_state(snap, now=now, interval=self.interval, services=services,
                          low_balance_hours=self.settings.tray_low_balance_hours,
                          idle_warn_sec=self.settings.tray_idle_warn_minutes * 60)
        starts = self.starts()
        cards = merge_starts([card_model(e, now, services) for e in snap.get("sessions", [])], starts, now)
        self._load_profiles()
        open_by_session = {}
        for e in snap.get("sessions", []):
            p = self.profiles.get(e.get("profile"))
            if p and p.open:
                open_by_session[e["name"]] = p.open
        for c in cards:
            if c.get("open_url") and c["name"] not in open_by_session:
                open_by_session[c["name"]] = c["open_url"]
        status = "stale" if icon["level"] == "stale" else ("error" if not snap.get("ok") else "connected")
        status_text = {"stale": t(self.lang, "stale"), "error": t(self.lang, "refresh_failed"),
                       "connected": t(self.lang, "connected")}[status]
        if snap.get("last_ok_at") is None:
            updated = t(self.lang, "updated", age="…")
        elif now - snap["last_ok_at"] < 5:
            updated = t(self.lang, "updated_now")
        else:
            updated = t(self.lang, "updated", age=fmt_age(now - snap["last_ok_at"], self.lang))
        return {
            "lang": self.lang,
            "icon": icon,
            "status": status,
            "status_text": status_text,
            "cards": [{
                "card": c,
                "lines": actions.card_lines(c, self.lang),
                "open_url": open_by_session.get(c["name"]),
                "has_service": c["name"] in services,
            } for c in cards],
            "cu": cu_model(snap.get("account")),
            "profiles": list(self.profiles),
            "profile_states": {s["profile"]: "running" if s["progress"]["phase"] == "ready" else "starting"
                               for s in starts},
            "last_profile": self.settings.tray_last_profile,
            "updated_text": updated,
            "tooltip": actions.status_text(icon, cards, self.lang),
        }

    def redraw(self):
        view = self.view()
        self.tray.setIcon(status_icon(view["icon"]["color"]))
        self.tray.setToolTip(view["tooltip"][:2000])
        if self.panel.isVisible():
            self.panel.render(view)

    def _load_profiles(self):
        try:
            self.profiles = load_profiles(profiles_path(self.state.config_path))
        except ProfileError:
            self.profiles = {}

    # --- panel ---

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.toggle_panel()

    def toggle_panel(self):
        if self.panel.isVisible():
            self.panel.hide()
            return
        self.panel.render(self.view())
        self._place_panel()
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    def _place_panel(self):
        self.panel.adjustSize()
        size = self.panel.size()
        geo = self.tray.geometry()
        screen = QGuiApplication.screenAt(geo.center()) if geo.isValid() else None
        screen = screen or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        if geo.isValid() and geo.width() > 0:
            x = geo.center().x() - size.width() // 2
            y = geo.top() - size.height() if geo.center().y() > area.center().y() else geo.bottom()
        else:
            x, y = area.right() - size.width(), area.bottom() - size.height()
        x = max(area.left(), min(x, area.right() - size.width()))
        y = max(area.top(), min(y, area.bottom() - size.height()))
        self.panel.move(x, y)

    # --- actions ---

    def run_cli(self, args, log: Optional[str] = None) -> subprocess.Popen:
        return subprocess.Popen([*colab_argv(self.state), *args], **actions.background_kwargs(log))

    def in_terminal(self, args):
        try:
            cmd, kw = actions.terminal_command(actions.held([*colab_argv(self.state), *args], self.lang),
                                               custom=self.settings.tray_terminal)
            subprocess.Popen(cmd, **kw)
        except OSError as e:
            self.notify(str(e), QSystemTrayIcon.Warning)

    def _show_window(self, window):
        self.windows = [w for w in self.windows if w.isVisible()]
        self.windows.append(window)
        self.panel.hide()
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    def open_log(self, card):
        """The log window: the job's output (followed) or the session's history."""
        from colab_cli.tray.windows import LogWindow

        base = colab_argv(self.state)
        if card.get("job_id"):
            job = card["job_id"]
            help_text = t(self.lang, "log.help_job", job=job, name=card["name"])

            def command(follow):
                return [*base, *actions.log_argv(card, follow=follow)]
        else:
            help_text = t(self.lang, "log.help_session", name=card["name"])

            def command(follow):
                return [*base, *actions.log_argv(card)]
        return self._show_window(LogWindow(
            t(self.lang, "log.title", name=card["name"]), help_text, command, self.lang,
            can_follow=bool(card.get("job_id") and card.get("can_stop_job")),
        ))

    def open_profiles(self):
        from colab_cli.tray.windows import ProfileEditor

        for w in self.windows:
            if isinstance(w, ProfileEditor) and w.isVisible():
                w.raise_()
                w.activateWindow()
                return w
        return self._show_window(ProfileEditor(
            profiles_path(self.state.config_path), self.lang, on_start=self.start_profile,
            on_text=lambda: self.in_terminal(["profile", "edit"]),
        ))

    def open_service(self, card):
        p = self.profiles.get(card.get("profile"))
        url = (p.open if p and p.open else None) or card.get("open_url")
        if url:
            webbrowser.open(url)

    def choose_stop(self, card, options) -> Optional[str]:
        """The stop dialog; returns "job", "runtime" or None (cancel)."""
        dialog = QDialog()
        dialog.setWindowTitle(t(self.lang, "stop.title", name=card["name"]))
        box = QVBoxLayout(dialog)
        box.addWidget(QLabel(t(self.lang, "stop.title", name=card["name"])))
        radios = []
        for i, (key, text) in enumerate(options):
            r = QRadioButton(text)
            r.setChecked(i == 0)
            r.setProperty("choice", key)
            radios.append(r)
            box.addWidget(r)
        buttons = QDialogButtonBox()
        buttons.addButton(QPushButton(t(self.lang, "stop")), QDialogButtonBox.AcceptRole)
        buttons.addButton(QPushButton(t(self.lang, "cancel")), QDialogButtonBox.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        if self.run_dialog(dialog) != QDialog.Accepted:
            return None
        return next(r.property("choice") for r in radios if r.isChecked())

    def run_dialog(self, dialog) -> int:
        """Show a dialog above the panel and wait for it.

        The panel is a frameless, always-on-top tool window: a dialog owned by
        it could open behind it and inherited its dark style sheet. Dialogs
        are top-level and always on top instead, centred on the panel (or the
        screen), and the panel stays open meanwhile.
        """
        dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        dialog.adjustSize()
        anchor = self.panel.frameGeometry() if self.panel.isVisible() else (
            QGuiApplication.primaryScreen().availableGeometry())
        dialog.move(anchor.center() - dialog.rect().center())

        def bring_up():
            dialog.raise_()
            dialog.activateWindow()

        self.panel.keep_open = True
        try:
            QTimer.singleShot(0, bring_up)
            return dialog.exec()
        finally:
            self.panel.keep_open = False

    def ask_stop(self, card):
        has_service = card["name"] in self.services()
        choice = self.choose_stop(card, actions.stop_options(card, has_service, self.lang))
        if not choice:
            return
        if choice == "runtime":
            self.stopping.add(card["name"])
            for name, child in list(self.children.items()):
                if child["session"] == card["name"] and child["proc"].poll() is None:
                    child["proc"].terminate()
        self.run_cli(actions.stop_argv(card, choice))
        QTimer.singleShot(3000, self.refresh)

    def confirm(self, title: str, body: str) -> bool:
        box = QMessageBox(QMessageBox.Question, title, body)
        start = box.addButton(t(self.lang, "start"), QMessageBox.AcceptRole)
        box.addButton(t(self.lang, "cancel"), QMessageBox.RejectRole)
        self.run_dialog(box)
        return box.clickedButton() is start

    def start_profile(self, name: str):
        child = self.children.get(name)
        if child and child["proc"].poll() is None:
            # Creating a runtime takes minutes; a second click would create a second one.
            self.notify(t(self.lang, "notify.already_starting", name=name))
            return
        self._load_profiles()
        p = self.profiles.get(name)
        if p is None:
            return
        rate = (cu_model((self.snapshot or {}).get("account")) or {}).get("rate_cu_per_hour")
        if self.settings.tray_confirm_start and not self.confirm(*actions.profile_confirm(name, p, rate, self.lang)):
            return
        log = os.path.join(self.log_dir, f"{name}.log")
        try:  # the log is appended to: this run's lines start here
            offset = os.path.getsize(log)
        except OSError:
            offset = 0
        proc = self.run_cli(["profile", "run", name], log=log)
        self.children[name] = {
            "proc": proc, "log": log, "offset": offset, "session": p.session, "mode": p.mode,
            "started_at": time.time(), "hardware": profile_summary(p)["hardware"],
        }
        self.settings.tray_last_profile = name
        self.settings_store.save(self.settings)
        self.notify(t(self.lang, "notify.starting", name=name))
        self.redraw()
        QTimer.singleShot(5000, self.refresh)

    def notify(self, text: str, icon=QSystemTrayIcon.Information):
        self.tray.showMessage("Colab CLI", text, icon, 8000)

    def open_settings(self):
        dialog = QDialog()
        dialog.setWindowTitle(t(self.lang, "settings.title"))
        form = QFormLayout(dialog)
        interval = QDoubleSpinBox(minimum=10, maximum=3600, decimals=0, value=self.interval)
        notifications = QCheckBox(checked=self.settings.tray_notifications)
        language = QComboBox()
        for key, label in (("auto", t(self.lang, "settings.auto")), ("hu", "Magyar"), ("en", "English")):
            language.addItem(label, key)
        language.setCurrentIndex(max(0, language.findData(self.settings.tray_language)))
        form.addRow(t(self.lang, "settings.interval"), interval)
        form.addRow(t(self.lang, "settings.notifications"), notifications)
        form.addRow(t(self.lang, "settings.language"), language)
        from colab_cli.tray import autostart

        try:
            started_at_login = autostart.status() is not None
        except OSError:
            started_at_login = False
        login = QCheckBox(t(self.lang, "settings.autostart"), objectName="autostart", checked=started_at_login)
        login.setToolTip(t(self.lang, "settings.autostart_help"))
        form.addRow("", login)
        confirm = QCheckBox(t(self.lang, "settings.confirm_start"), objectName="confirm_start",
                            checked=self.settings.tray_confirm_start)
        confirm.setToolTip(t(self.lang, "settings.confirm_start_help"))
        form.addRow("", confirm)
        low = QDoubleSpinBox(objectName="low_balance", minimum=0, maximum=1000, decimals=1,
                             value=self.settings.tray_low_balance_hours)
        low.setSuffix(" " + t(self.lang, "settings.hours_unit"))
        low.setToolTip(t(self.lang, "settings.low_balance_help"))
        form.addRow(t(self.lang, "settings.low_balance"), low)
        idle = QDoubleSpinBox(objectName="idle_warn", minimum=5, maximum=600, decimals=0,
                              value=self.settings.tray_idle_warn_minutes)
        idle.setSuffix(" " + t(self.lang, "settings.minutes_unit"))
        idle.setToolTip(t(self.lang, "settings.idle_warn_help"))
        form.addRow(t(self.lang, "settings.idle_warn"), idle)
        terminal = QComboBox(objectName="terminal", editable=True)
        auto_label = t(self.lang, "settings.terminal_auto")
        terminal.addItem(auto_label)
        for label, template in actions.terminal_presets():
            terminal.addItem(template)
            terminal.setItemData(terminal.count() - 1, label, Qt.ToolTipRole)
        if self.settings.tray_terminal:
            terminal.setEditText(self.settings.tray_terminal)
        terminal.setToolTip(t(self.lang, "settings.terminal_help"))
        form.addRow(t(self.lang, "settings.terminal"), terminal)
        buttons = QDialogButtonBox()
        buttons.addButton(QPushButton(t(self.lang, "save")), QDialogButtonBox.AcceptRole)
        buttons.addButton(QPushButton(t(self.lang, "cancel")), QDialogButtonBox.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if self.run_dialog(dialog) != QDialog.Accepted:
            return
        self.settings.tray_interval = interval.value()
        self.settings.tray_notifications = notifications.isChecked()
        self.settings.tray_language = language.currentData()
        self.settings.tray_confirm_start = confirm.isChecked()
        self.settings.tray_low_balance_hours = low.value()
        self.settings.tray_idle_warn_minutes = idle.value()
        text = terminal.currentText().strip()
        self.settings.tray_terminal = "" if text in ("", auto_label) else text
        self.settings_store.save(self.settings)
        try:  # kept by the operating system, not settings.json (tray/autostart.py)
            if login.isChecked():
                autostart.enable([*colab_argv(self.state), "tray"])  # also refreshes a moved install
            elif started_at_login:
                autostart.disable()
        except OSError as e:
            self.notify(t(self.lang, "settings.autostart_failed", error=e), QSystemTrayIcon.Warning)
        self.interval = interval.value()
        self.timer.setInterval(int(self.interval * 1000))
        self.lang = pick_language(self.settings.tray_language, QLocale.system().name())
        self.redraw()

    def quit(self):
        self.tray.hide()
        QApplication.instance().quit()


def run_tray(state, interval: float) -> int:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Colab CLI")
    if not QSystemTrayIcon.isSystemTrayAvailable():
        import typer

        typer.echo("[colab] This desktop has no system tray (on GNOME, enable an AppIndicator extension).", err=True)
        return 2
    tray = TrayApp(state, interval)
    tray.start()
    return app.exec()
