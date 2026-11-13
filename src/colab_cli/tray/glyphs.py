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

"""Line icons for the tray panel, drawn here as small SVGs (24x24, stroke 2).

GLYPHS holds the SVG body of each icon; icon()/pixmap() colour and render it.
"""

import math

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


def _cog() -> str:
    """A gear: 8 teeth around a ring, with a hole."""
    points = []
    for i in range(8):
        a = i * math.pi / 4
        for da, r in ((-0.22, 7.2), (-0.12, 10.0), (0.12, 10.0), (0.22, 7.2)):
            points.append((12 + r * math.cos(a + da), 12 + r * math.sin(a + da)))
    d = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in points) + " Z"
    return f'<path d="{d}"/><circle cx="12" cy="12" r="3"/>'


GLYPHS = {
    "log": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
           '<path d="M14 3v5h5"/><path d="M9 13h6M9 17h6M9 9h2"/>',
    "terminal": '<path d="M5 7l5 5-5 5"/><path d="M12 18h7"/>',
    "stop": '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    "open": '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/>'
            '<path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "layers": '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/><path d="M3 17.5l9 5 9-5"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/><path d="M4 3v5h5"/>'
               '<path d="M4 13a8 8 0 0 0 14.3 4.9L20 16"/><path d="M20 21v-5h-5"/>',
    "settings": _cog(),
    "chevron_right": '<path d="M9 6l6 6-6 6"/>',
    "chevron_down": '<path d="M6 9l6 6 6-6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
    "quit": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/>',
}


def svg(name: str, color: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f"{GLYPHS[name]}</svg>"
    )


def pixmap(name: str, color: str, size: int = 18, ratio: float = 2.0) -> QPixmap:
    """The icon at ``size`` logical pixels (drawn at ``ratio`` for HiDPI)."""
    px = round(size * ratio)
    pix = QPixmap(px, px)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg(name, color).encode())).render(p, QRectF(0, 0, px, px))
    p.end()
    pix.setDevicePixelRatio(ratio)
    return pix


def icon(name: str, color: str, size: int = 18) -> QIcon:
    return QIcon(pixmap(name, color, size))
