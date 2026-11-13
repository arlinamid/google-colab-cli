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

"""``python -m colab_cli.tray.hold [--lang hu] -- CMD...``: run CMD, then wait for Enter.

The tray opens `colab log`, `colab job tail --follow` and `colab ssh` in a
new terminal window; on Windows that window closes the moment the command
exits, before its output can be read. This keeps it open. Ctrl+C ends the
command (e.g. a follow), not the window.
"""

import subprocess
import sys

PROMPT = {
    "en": "[colab] Done (exit {code}). Press Enter to close this window.",
    "hu": "[colab] Kész (kilépési kód: {code}). Az ablak bezárásához nyomj Entert.",
}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    lang = "en"
    if argv[:1] == ["--lang"] and len(argv) > 1:
        lang, argv = argv[1], argv[2:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    try:
        code = subprocess.call(argv) if argv else 0
    except KeyboardInterrupt:
        code = 130
    except OSError as e:
        print(f"[colab] {e}")
        code = 127
    try:
        input("\n" + PROMPT.get(lang, PROMPT["en"]).format(code=code))
    except (EOFError, KeyboardInterrupt):
        pass
    return code


if __name__ == "__main__":
    sys.exit(main())
