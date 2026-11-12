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

"""Turn a failure into a fixed code and concrete next steps for an agent.

Each rule matches the exception name, its message and the traceback text,
and yields ``{"code", "summary", "next_actions"}``. ``next_actions`` are
real ``colab`` commands (``cmd``) with a reason (``why``); ``cmd`` is None
when the step is not a single command, for example after ``colab run`` has
already stopped its runtime. See docs/08_agent_feedback.md.
"""

import re
from typing import Dict, List, Optional

# Import name -> pip package, for the common cases where they differ.
_PIP_NAMES = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "Crypto": "pycryptodome",
    "docx": "python-docx",
    "fitz": "pymupdf",
}

_MODULE_RE = re.compile(r"No module named '([\w.]+)'")
_PATH_RE = re.compile(r"No such file or directory: '([^']*)'")


def _action(cmd: Optional[str], why: str) -> Dict[str, Optional[str]]:
    return {"cmd": cmd, "why": why}


def _result(code: str, summary: str, actions: Optional[List] = None) -> Dict:
    return {"code": code, "summary": summary, "next_actions": actions or []}


def _rerun(session: Optional[str], file: Optional[str]) -> List[Dict]:
    if not session:
        return []
    if file and not file.startswith("<"):
        return [_action(f"colab exec -s {session} -f {file} --json", "run it again")]
    return [_action(None, f"run the same code again on session '{session}'")]


def _missing_module(m, session, file, **_):
    module = m.group(1).split(".")[0]
    package = _PIP_NAMES.get(module, module)
    summary = f"The runtime has no '{module}' package (pip: {package})."
    if not session:
        return _result("missing_module", summary, [
            _action(
                None,
                f"install '{package}' at the start of the script (for example "
                f"subprocess.run([sys.executable, '-m', 'pip', 'install', "
                f"'{package}'])), or rerun with --keep and use "
                f"`colab install -s NAME {package}`",
            )
        ])
    return _result("missing_module", summary, [
        _action(f"colab install -s {session} {package}", "install the missing package"),
        *_rerun(session, file),
    ])


def _cuda_oom(m, session, **_):
    actions = [_action(None, "lower the batch size or the model/input size")]
    if session:
        actions.append(_action(f"colab inspect -s {session} --gpu", "see GPU memory use"))
    actions.append(
        _action("colab new --gpu A100", "or use a runtime with a larger GPU")
    )
    return _result("cuda_oom", "The GPU ran out of memory.", actions)


def _no_gpu(m, session, **_):
    return _result("no_gpu", "The code needs a GPU, but this runtime has none.", [
        _action("colab new --gpu T4", "create a GPU runtime and run the code there"),
    ])


def _disk_full(m, session, **_):
    actions = []
    if session:
        actions = [
            _action(f"colab inspect -s {session} --disk", "see what uses the disk"),
            _action(f"colab ls -s {session} /content", "find files to remove"),
        ]
    return _result("disk_full", "The runtime's disk is full.", actions)


def _file_not_found(m, session, **_):
    path = m.group(1) if m else ""
    summary = f"File not found on the runtime: '{path}'." if path else "File not found."
    actions = []
    if session:
        remote = path if path.startswith("/") else f"/content/{path}"
        actions = [
            _action(f"colab ls -s {session} /content", "check what is on the runtime"),
            _action(
                f"colab upload -s {session} LOCAL_FILE {remote}",
                "upload the file (relative paths start in /content)",
            ),
        ]
    return _result("file_not_found", summary, actions)


def _syntax_error(m, session, file, **_):
    actions = [_action(None, "fix the code at error.location")]
    if file and not file.startswith("<"):
        actions.append(
            _action(f"colab exec --check-only -f {file}", "check the syntax locally")
        )
    return _result("syntax_error", "The code does not parse.", actions)


def _keyboard_interrupt(m, session, **_):
    return _result("keyboard_interrupt", "The code was interrupted.")


def _script_exit(m, session, evalue="", **_):
    return _result("script_exit", f"The script exited with code {evalue or '?'}.")


# (name, ename matcher, text pattern or None, handler). Order matters: the
# first match wins, so specific causes come before generic ones.
_RULES: List[tuple] = [
    ("syntax", lambda n: n in ("SyntaxError", "IndentationError", "TabError"), None, _syntax_error),
    ("missing_module", lambda n: n in ("ModuleNotFoundError", "ImportError"), _MODULE_RE, _missing_module),
    ("cuda_oom", lambda n: n == "OutOfMemoryError", None, _cuda_oom),
    ("cuda_oom_text", lambda n: True, re.compile(r"CUDA out of memory"), _cuda_oom),
    (
        "no_gpu",
        lambda n: True,
        re.compile(
            r"Torch not compiled with CUDA enabled|No CUDA GPUs are available"
            r"|Found no NVIDIA driver",
            re.IGNORECASE,
        ),
        _no_gpu,
    ),
    ("disk_full", lambda n: True, re.compile(r"\[Errno 28\]|No space left on device"), _disk_full),
    ("file_not_found", lambda n: n == "FileNotFoundError", None, _file_not_found),
    ("interrupt", lambda n: n == "KeyboardInterrupt", None, _keyboard_interrupt),
    ("exit", lambda n: n == "SystemExit", None, _script_exit),
]


def diagnose(
    ename: str,
    evalue: str,
    traceback: str = "",
    *,
    session: Optional[str] = None,
    file: Optional[str] = None,
) -> Dict:
    """Diagnose a kernel error. ``traceback`` is plain text (no ANSI)."""
    text = f"{evalue}\n{traceback}"
    for _name, ename_ok, pattern, handler in _RULES:
        if not ename_ok(ename or ""):
            continue
        m = None
        if pattern is not None:
            m = pattern.search(text)
            if not m:
                continue
        elif handler is _file_not_found:
            m = _PATH_RE.search(evalue or "")
        return handler(m, session=session, file=file, evalue=evalue)
    return _result("unknown", f"{ename}: {evalue}".strip(": "))


def diagnose_failure(
    kind: str, *, session: Optional[str] = None, timeout=None, budget=None
) -> Dict:
    """Diagnose a failure of the CLI itself, not of the user's code."""
    if kind == "idle_timeout":
        return _result(
            "idle_timeout",
            f"No output for {timeout:g} s (--timeout); the kernel was interrupted."
            if timeout
            else "The run timed out; the kernel was interrupted.",
            [
                _action(None, "raise --timeout, or print progress more often"),
                _action(None, "for long jobs, run them detached (colab job, planned)"),
            ],
        )
    if kind == "budget_reached":
        return _result(
            "budget_reached",
            f"The compute budget ({budget:g} CU, estimated from the hourly "
            "rate) was reached; the code was stopped."
            if budget
            else "The compute budget was reached; the code was stopped.",
            [
                _action(None, "raise --budget, or make the run cheaper (less data, a smaller model, or a CPU runtime)"),
                _action("colab usage --json", "check the balance and the hourly rate"),
            ],
        )
    if kind == "session_lost":
        actions = [_action("colab new", "create a new runtime")]
        if session:
            actions.insert(
                0, _action(f"colab status -s {session}", "check whether the runtime still runs")
            )
        return _result(
            "session_lost",
            "The connection to the runtime was lost; it may have been stopped.",
            actions,
        )
    return _result("unknown", kind)
