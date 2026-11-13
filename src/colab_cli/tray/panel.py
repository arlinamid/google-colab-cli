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

"""The tray panel (docs/09): header, one card per runtime, compute units,
profile button, footer. It only draws a view model and emits what was
clicked; app.py does the work."""

from typing import Any, Dict, List

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from colab_cli.tray.i18n import fmt_number, t
from colab_cli.tray.icon import logo_pixmap

WIDTH = 400
CARD_HEIGHT = 168
MAX_VISIBLE_CARDS = 3

DOT = {
    "running": "#34a853", "service": "#34a853", "stalled": "#f9ab00", "idle": "#9aa0a6",
    "done": "#4285f4", "failed": "#ea4335", "stopped": "#9aa0a6",
}
STATUS_DOT = {"connected": "#34a853", "error": "#ea4335", "stale": "#f9ab00"}

QSS = """
#panel { background: #202124; border: 1px solid #3c4043; border-radius: 14px; }
* { font-family: "Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Noto Sans", "DejaVu Sans", sans-serif; }
QLabel { color: #e8eaed; font-size: 13px; }
#title { font-size: 20px; font-weight: 600; }
#muted, #section { color: #9aa0a6; }
#section { font-size: 12px; font-weight: 600; letter-spacing: 1px; }
#badge { color: #fdd663; background: #3b3416; border: 1px solid #8a6d00; border-radius: 8px; padding: 2px 8px; }
#card, #cu { background: #2b2c2f; border: 1px solid #3c4043; border-radius: 10px; }
#metric { color: #e8eaed; }
#estimate { color: #fdd663; background: #3b3416; border: 1px solid #8a6d00; border-radius: 6px; padding: 3px 8px; }
QProgressBar { background: #3c4043; border: none; border-radius: 4px; height: 8px; }
QProgressBar::chunk { border-radius: 4px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #f9ab00, stop:1 #e8710a); }
QPushButton#action { color: #e8eaed; background: #2b2c2f; border: 1px solid #5f6368; border-radius: 6px; padding: 6px 10px; }
QPushButton#action:hover { background: #35363a; }
QPushButton#danger { color: #f28b82; background: #2b2c2f; border: 1px solid #ea4335; border-radius: 6px; padding: 6px 10px; }
QPushButton#danger:hover { background: #3c2a29; }
QPushButton#primary { color: #ffffff; background: #e8710a; border: none; border-top-left-radius: 8px; border-bottom-left-radius: 8px; padding: 10px; font-size: 15px; font-weight: 600; }
QPushButton#primary:hover { background: #f9ab00; }
QPushButton#primary:disabled { background: #5f6368; color: #bdc1c6; }
QToolButton#more::menu-indicator { image: none; width: 0; }
QToolButton#more { color: #ffffff; background: #e8710a; border: none; border-left: 1px solid #b85c00; border-top-right-radius: 8px; border-bottom-right-radius: 8px; padding: 10px; }
QPushButton#row { color: #e8eaed; background: transparent; border: none; text-align: left; padding: 8px 4px; font-size: 14px; }
QPushButton#row:hover, QPushButton#flat:hover, QToolButton#gear:hover { background: #2b2c2f; border-radius: 6px; }
QPushButton#flat { color: #bdc1c6; background: transparent; border: none; padding: 4px 6px; }
QToolButton#gear { color: #e8eaed; background: transparent; border: none; font-size: 18px; padding: 4px; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 0 2px 2px; }
QScrollBar::handle:vertical { background: #5f6368; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QMenu { background: #2b2c2f; color: #e8eaed; border: 1px solid #5f6368; }
QMenu::item:selected { background: #3c4043; }
"""


def _label(text: str = "", name: str = "") -> QLabel:
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    return label


def _dot(color: str, size: int = 10) -> QLabel:
    dot = QLabel()
    dot.setFixedSize(size, size)
    dot.setStyleSheet(f"background: {color}; border-radius: {size // 2}px;")
    return dot


class Panel(QWidget):
    """Signals carry what was clicked; ``card`` is a tray_model card dict."""

    log_clicked = Signal(dict)
    ssh_clicked = Signal(dict)
    open_clicked = Signal(dict)
    stop_clicked = Signal(dict)
    profile_clicked = Signal(str)
    refresh_clicked = Signal()
    settings_clicked = Signal()
    quit_clicked = Signal()

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(QSS)
        self.setFixedWidth(WIDTH + 24)
        self.keep_open = False  # while a dialog of the panel is open
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        self.frame = QFrame(objectName="panel")
        shadow = QGraphicsDropShadowEffect(blurRadius=24, offset=QPoint(0, 4))
        shadow.setColor(QColor(0, 0, 0, 160))
        self.frame.setGraphicsEffect(shadow)
        outer.addWidget(self.frame)
        self.body = QVBoxLayout(self.frame)
        self.body.setContentsMargins(16, 14, 16, 12)
        self.body.setSpacing(10)
        self._build_static()
        self.cards: List[QFrame] = []

    # --- fixed parts ---

    def _build_static(self):
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(logo_pixmap(30))
        head.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(_label("Colab CLI", "title"))
        status = QHBoxLayout()
        status.setSpacing(6)
        self.status_dot = _dot(STATUS_DOT["connected"])
        self.status_text = _label("", "muted")
        status.addWidget(self.status_dot)
        status.addWidget(self.status_text)
        status.addStretch()
        titles.addLayout(status)
        head.addLayout(titles, 1)
        gear = QToolButton(objectName="gear", text="⚙")
        gear.clicked.connect(self.settings_clicked)
        head.addWidget(gear, 0, Qt.AlignTop)
        self.body.addLayout(head)

        section = QHBoxLayout()
        self.section_label = _label("", "section")
        self.count_badge = _label("", "badge")
        section.addWidget(self.section_label)
        section.addStretch()
        section.addWidget(self.count_badge)
        self.body.addLayout(section)

        self.scroll = QScrollArea(widgetResizable=True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(8)
        self.scroll.setWidget(self.cards_host)
        self.body.addWidget(self.scroll)

        self.cu_frame = QFrame(objectName="cu")
        cu = QVBoxLayout(self.cu_frame)
        row = QHBoxLayout()
        left = QVBoxLayout()
        self.cu_title = _label("", "")
        self.cu_title.setStyleSheet("font-weight: 600; font-size: 14px;")
        self.cu_balance = _label("", "muted")
        left.addWidget(self.cu_title)
        left.addWidget(self.cu_balance)
        row.addLayout(left, 1)
        right = QVBoxLayout()
        self.cu_usage_label = _label("", "muted")
        self.cu_usage = _label("", "")
        self.cu_usage.setStyleSheet("font-weight: 600;")
        right.addWidget(self.cu_usage_label)
        right.addWidget(self.cu_usage)
        row.addLayout(right)
        cu.addLayout(row)
        self.cu_estimate = _label("", "estimate")
        self.cu_estimate.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        cu.addWidget(self.cu_estimate)
        self.body.addWidget(self.cu_frame)

        split = QHBoxLayout()
        split.setSpacing(0)
        self.profile_button = QPushButton(objectName="primary")
        self.profile_button.clicked.connect(self._start_main_profile)
        self.profile_more = QToolButton(objectName="more", text="▾")
        self.profile_more.setPopupMode(QToolButton.InstantPopup)
        self.profile_menu = QMenu(self)
        self.profile_more.setMenu(self.profile_menu)
        split.addWidget(self.profile_button, 1)
        split.addWidget(self.profile_more)
        self.body.addLayout(split)

        self.refresh_row = QPushButton(objectName="row")
        self.refresh_row.clicked.connect(self.refresh_clicked)
        self.settings_row = QPushButton(objectName="row")
        self.settings_row.clicked.connect(self.settings_clicked)
        self.body.addWidget(self.refresh_row)
        self.body.addWidget(self.settings_row)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #3c4043;")
        self.body.addWidget(line)
        foot = QHBoxLayout()
        self.updated = _label("", "muted")
        self.quit_button = QPushButton(objectName="flat")
        self.quit_button.clicked.connect(self.quit_clicked)
        foot.addWidget(self.updated)
        foot.addStretch()
        foot.addWidget(self.quit_button)
        self.body.addLayout(foot)
        self._main_profile = None

    def _start_main_profile(self):
        if self._main_profile:
            self.profile_clicked.emit(self._main_profile)

    # --- the view model ---

    def render(self, view: Dict[str, Any]) -> None:
        """Draw ``view`` (built by app.py from a snapshot)."""
        lang = view["lang"]
        self.status_dot.setStyleSheet(
            f"background: {STATUS_DOT[view['status']]}; border-radius: 5px;")
        self.status_text.setText(view["status_text"])
        self.section_label.setText(t(lang, "active_sessions"))
        self.count_badge.setText(t(lang, "n_active", n=len(view["cards"])))
        self._render_cards(view)
        self._render_cu(view)
        self._render_profiles(view)
        self.refresh_row.setText("⟳   " + t(lang, "refresh_now"))
        self.settings_row.setText("⚙   " + t(lang, "settings"))
        self.updated.setText(view["updated_text"])
        self.updated.setStyleSheet("color: #fdd663;" if view["status"] == "stale" else "")
        self.quit_button.setText(t(lang, "quit") + "  ⇥")
        self.adjustSize()

    def _render_cards(self, view):
        for card in self.cards:
            card.deleteLater()
        self.cards = []
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        lang = view["lang"]
        if not view["cards"]:
            empty = _label(t(lang, "no_sessions"), "muted")
            self.cards_layout.addWidget(empty)
        for item in view["cards"]:
            frame = self._card(item, lang)
            self.cards.append(frame)
            self.cards_layout.addWidget(frame)
        shown = max(1, min(len(view["cards"]), MAX_VISIBLE_CARDS))
        self.scroll.setFixedHeight(shown * CARD_HEIGHT + (shown - 1) * 8 if view["cards"] else 40)

    def _card(self, item, lang) -> QFrame:
        card, lines = item["card"], item["lines"]
        frame = QFrame(objectName="card")
        frame.setFixedHeight(CARD_HEIGHT)
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 10, 14, 10)
        top = QHBoxLayout()
        top.addWidget(_dot(DOT.get(card["state"], "#9aa0a6"), 12), 0, Qt.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(0)
        name = _label(card["name"])
        name.setStyleSheet("font-size: 16px; font-weight: 600;")
        names.addWidget(name)
        names.addWidget(_label(lines["subtitle"], "muted"))
        top.addLayout(names, 1)
        if lines["metric"]:
            top.addWidget(_label(lines["metric"], "metric"), 0, Qt.AlignTop)
        box.addLayout(top)
        if card["percent"] is not None and card["state"] in ("running", "stalled"):
            bar_row = QHBoxLayout()
            bar = QProgressBar(objectName="progress", textVisible=False)
            bar.setRange(0, 1000)
            bar.setValue(round(card["percent"] * 10))
            bar.setFixedHeight(8)
            bar_row.addWidget(bar, 1)
            bar_row.addWidget(_label(f"{card['percent']:.0f}%"))
            box.addLayout(bar_row)
        detail = _label(lines["detail"] or "", "muted")
        detail.setObjectName("detail")
        box.addWidget(detail)
        buttons = QHBoxLayout()
        enabled = card["local"]
        for key, signal, obj in (("log", self.log_clicked, "action"), ("ssh", self.ssh_clicked, "action")):
            b = QPushButton(t(lang, key), objectName=obj)
            b.setEnabled(enabled)
            b.clicked.connect(lambda _=False, c=card, s=signal: s.emit(c))
            buttons.addWidget(b)
        if item.get("open_url"):
            b = QPushButton(t(lang, "open"), objectName="action")
            b.clicked.connect(lambda _=False, c=card: self.open_clicked.emit(c))
            buttons.addWidget(b)
        stop = QPushButton("■  " + t(lang, "stop"), objectName="danger")
        stop.setProperty("session", card["name"])
        stop.clicked.connect(lambda _=False, c=card: self.stop_clicked.emit(c))
        buttons.addWidget(stop)
        box.addLayout(buttons)
        return frame

    def _render_cu(self, view):
        lang, cu = view["lang"], view["cu"]
        self.cu_frame.setVisible(cu is not None)
        if cu is None:
            return
        self.cu_title.setText("◈  " + t(lang, "compute_units"))
        self.cu_balance.setText(f"{t(lang, 'balance')} {fmt_number(cu['balance_cu'] or 0, lang)} CU")
        self.cu_usage_label.setText(t(lang, "usage"))
        self.cu_usage.setText(t(lang, "per_hour", rate=fmt_number(cu["rate_cu_per_hour"] or 0, lang)))
        if cu["hours_left"] is not None:
            self.cu_estimate.setText(f"{t(lang, 'hours_left')}  {t(lang, 'hours', h=fmt_number(cu['hours_left'], lang, 0))}")
        else:
            self.cu_estimate.setText(t(lang, "no_estimate"))

    def _render_profiles(self, view):
        lang = view["lang"]
        names = view["profiles"]
        self.profile_menu.clear()
        for name in names:
            action = self.profile_menu.addAction(name)
            action.triggered.connect(lambda _=False, n=name: self.profile_clicked.emit(n))
        main = view.get("last_profile") if view.get("last_profile") in names else (names[0] if names else None)
        self._main_profile = main
        if main:
            self.profile_button.setText(f"＋  {t(lang, 'start_profile')}: {main}")
        else:
            self.profile_button.setText(t(lang, "no_profiles"))
        self.profile_button.setEnabled(bool(main))
        self.profile_more.setEnabled(bool(names))

    # --- behaviour ---

    def event(self, e):
        if e.type() == QEvent.WindowDeactivate and not self.keep_open:
            self.hide()
        return super().event(e)
