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

"""The Colab logo with a status dot, drawn with Qt.

colab.svg: the Colab logo from homarr-labs/dashboard-icons (Apache-2.0); the
logo is a trademark of Google.
"""

from importlib.resources import files

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer

_SVG = None


def _renderer() -> QSvgRenderer:
    global _SVG
    if _SVG is None:
        data = files("colab_cli.tray").joinpath("colab.svg").read_bytes()
        _SVG = QSvgRenderer(QByteArray(data))
    return _SVG


def logo_pixmap(height: int) -> QPixmap:
    """The logo, ``height`` pixels high (it is about 1.7 times as wide)."""
    r = _renderer()
    size = r.defaultSize()
    width = round(height * size.width() / max(size.height(), 1))
    pix = QPixmap(width, height)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p, QRectF(0, 0, width, height))
    p.end()
    return pix


def status_icon(color: str, size: int = 64) -> QIcon:
    """A square tray icon: the logo, and a dot of ``color`` in the corner."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    r = _renderer()
    ds = r.defaultSize()
    w = size
    h = size * ds.height() / max(ds.width(), 1)
    r.render(p, QRectF(0, (size - h) / 2, w, h))
    d = size * 0.42
    p.setPen(QPen(QColor("#202124"), size * 0.06))
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(size - d - 1, 1, d, d))
    p.end()
    return QIcon(pix)
