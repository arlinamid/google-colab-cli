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

import base64
import functools
import html2text
import logging
import os
import re
import subprocess
import sys
import tempfile
import tokenize
import webbrowser

from typing import Optional, Union

from rich.markdown import Markdown
from rich.text import Text


def read_python_source(path: str) -> str:
    """Reads a Python source file the way Python itself does.

    Honors a PEP 263 coding cookie (``# -*- coding: cp932 -*-``) and a UTF-8
    BOM, and otherwise assumes UTF-8 (PEP 3120). A plain ``open()`` would use
    the Windows ANSI code page instead (cp1252, cp1250, cp932, ...), and fail
    or garble any non-ASCII text for most non-English users.

    Raises:
      UnicodeDecodeError, SyntaxError: If the file does not decode with its
        declared (or the default UTF-8) encoding.
    """
    with tokenize.open(path) as f:
        return f.read()


def utf8_text_stream(stream):
    """Makes a redirected text stream read and write UTF-8; returns it.

    When stdin/stdout/stderr is a pipe or a file, Python uses the locale
    encoding, which on Windows is the ANSI code page: printing Japanese (or
    any text outside it) then raised UnicodeEncodeError, and piped-in code
    was decoded in the wrong charset. Python 3.15 makes UTF-8 the default
    (PEP 686); until then, switch redirected streams to UTF-8, replacing
    anything that does not decode instead of failing. A real console is left
    alone: Python writes to it through the Unicode console API already.
    """
    if stream is None or not hasattr(stream, "reconfigure"):
        return stream
    try:
        if stream.isatty():
            return stream
    except (OSError, ValueError):
        return stream
    if (stream.encoding or "").lower().replace("-", "") != "utf8":
        stream.reconfigure(encoding="utf-8", errors="replace")
    return stream


_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


@functools.lru_cache(maxsize=1)
def _msys_root() -> Optional[str]:
    """Windows path of the Git Bash (MSYS) root, like ``C:/Program Files/Git``."""
    root = ""
    try:
        root = subprocess.run(
            ["cygpath", "-m", "/"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        # git-bash.exe sets EXEPATH to the install root or its bin folder.
        exepath = os.environ.get("EXEPATH", "").rstrip("\\/")
        if os.path.basename(exepath).lower() == "bin":
            exepath = os.path.dirname(exepath)
        root = exepath
    return root.replace("\\", "/").rstrip("/") or None


def undo_msys_path(path: str) -> str:
    """Turns a remote path that Git Bash rewrote back into a POSIX path.

    Git Bash (MSYS) converts arguments that look like POSIX paths before a
    native Windows program sees them, so ``colab ls /content`` arrives as
    ``C:/Program Files/Git/content``. Remote paths are never Windows paths,
    so under Git Bash one inside the MSYS root is turned back into
    ``/content``. Anything else, and every path on Linux and macOS, is
    returned unchanged.
    """
    if (
        sys.platform != "win32"
        or not os.environ.get("MSYSTEM")
        or not _DRIVE_PATH.match(path)
    ):
        return path
    root = _msys_root()
    if not root:
        return path
    normalized = path.replace("\\", "/")
    if normalized.rstrip("/").lower() == root.lower():
        return "/"
    if normalized.lower().startswith(root.lower() + "/"):
        return normalized[len(root):]
    return path


def get_status_code(e: Exception) -> Optional[int]:
    """Safely extracts status code from various exception types."""
    if hasattr(e, "response") and e.response is not None:
        if hasattr(e.response, "status_code"):
            return e.response.status_code
    if hasattr(e, "status_code"):
        return e.status_code
    return None


def is_terminal_error(e: Exception) -> bool:
    """Checks if an exception indicates a lost session (404/401)."""
    code = get_status_code(e)
    if code in (404, 401):
        return True
    # Some exceptions from jupyter-kernel-client might wrap the real one or be different
    err_msg = str(e)
    if "404" in err_msg or "401" in err_msg:
        return True
    return False


def open_url_in_browser(url: str) -> bool:
    """Open ``url`` in the user's default browser.

    Returns ``True`` when a browser was launched and ``False`` when no opener
    is available (headless SSH hosts, containers, CI) or the opener fails.
    Callers must keep showing the URL so those environments can still complete
    the flow manually.
    """
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def print_kitty(image_bytes: bytes):
    """
    Outputs an image using the Kitty Graphics Protocol.
    Expects PNG bytes.

    No-op when stdout is not a TTY: the escape sequence is meaningless to a
    file/pipe and visually corrupts captured output (e.g. when piping
    `colab exec` into a shell tool, redirecting to a log file, or running
    under non-Kitty terminals). Callers still get the image via
    `handle_image`'s file-write path.
    """
    if not sys.stdout.isatty():
        return
    try:
        b64_data = base64.b64encode(image_bytes).decode("ascii")
        sys.stdout.write("\n\033_Ga=T,f=100;")
        sys.stdout.write(b64_data)
        sys.stdout.write("\033\\\n")
        sys.stdout.flush()
    except Exception:
        logging.exception("Kitty rendering failed")


def render_display_data(data: dict) -> Union[Markdown, Text, None]:
    """Extract the best text representation from a display_data dict.

    Priority: text/markdown > text/html (via html2text) > text/plain.
    Returns a Rich renderable (Markdown or Text) or None when no text mime
    type is present.  Callers can pass the result directly to Console.print().
    """
    if "text/markdown" in data:
        return Markdown(data["text/markdown"])
    if "text/html" in data:
        return Markdown(html2text.html2text(data["text/html"]))
    if "text/plain" in data:
        return Text.from_ansi(data["text/plain"])
    return None


def handle_image(image_b64: str, mime_type: str = "image/png", target_path: str = None):
    image_bytes = base64.b64decode(image_b64)
    # Print inline using Kitty protocol
    print_kitty(image_bytes)

    if target_path:
        # If a target path is specified, save it there
        with open(target_path, "wb") as f:
            f.write(image_bytes)
        print(f"\n[Image saved to: {target_path}]")
    else:
        # Save to temp file as fallback
        ext = mime_type.split("/")[-1]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
        tmp.write(image_bytes)
        tmp.close()
        print(f"\n[Image saved to: {tmp.name}]")
