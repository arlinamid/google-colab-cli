#!/bin/bash
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

# Integration Test: profiles, progress and the tray snapshot (docs/09)
#
# On one live CPU runtime:
#   1. a job profile (`colab profile add` / `run`) starts a script that
#      reports epochs; `colab tray --snapshot --json` shows it running with
#      overall progress and the epoch, then finished;
#   2. a notebook runs as a background job; its status names the cell;
#   3. a kernel profile reuses the runtime and sets a Python global (vars).
# Allocates one CPU runtime and stops it.

has_oauth2_token() {
    timeout 10 uv run python -c "import sys, keyring; sys.exit(0 if keyring.get_password('google-colab-cli', 'oauth_token') else 1)" > /dev/null 2>&1
}
if [ -n "$COLAB_AUTH" ]; then
    AUTH_FLAGS="--auth=$COLAB_AUTH"
elif has_oauth2_token; then
    AUTH_FLAGS="--auth=oauth2"
elif command -v gcloud > /dev/null && gcloud auth application-default print-access-token > /dev/null 2>&1; then
    AUTH_FLAGS="--auth=adc"
else
    echo "Error: No usable auth provider found."
    exit 1
fi
echo "[*] Using $AUTH_FLAGS"

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
NAME="repro-tray-$(date +%s)"
COLAB="${COLAB_CMD:-uv run colab} $AUTH_FLAGS --config $SESSION_FILE"
FAILURES=0

cleanup() {
    echo "[*] Cleaning up..."
    $COLAB stop -s "$NAME" > /dev/null 2>&1 || true
    $COLAB sessions 2>&1 | grep -v "new version\|colab update\|enable_update" || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# check NAME EXPECTED_EXIT ACTUAL_EXIT FILE PYTHON_EXPR (sees `recs`, `r` = last, `text`)
check() {
    local name="$1" want="$2" got="$3" file="$4" expr="$5"
    if [ "$got" != "$want" ]; then
        echo "  [FAIL] $name: exit $got, expected $want"
        sed 's/^/    | /' "$file"
        FAILURES=$((FAILURES + 1))
        return
    fi
    if uv run python - "$file" "$expr" <<'PY'
import json, sys
text = open(sys.argv[1], encoding="utf-8").read()
recs = []
for line in text.splitlines():
    try:
        recs.append(json.loads(line))
    except ValueError:
        pass
r = recs[-1] if recs else None
sys.exit(0 if eval(sys.argv[2]) else 1)
PY
    then
        echo "  [PASS] $name"
    else
        echo "  [FAIL] $name: $expr"
        sed 's/^/    | /' "$file"
        FAILURES=$((FAILURES + 1))
    fi
}

cat > "$TMP_DIR/train.py" <<'PY'
import time
from colab_feedback import report
for epoch in range(1, 4):
    for step in range(5):
        time.sleep(1)
    report(epoch=epoch, epochs=3, loss=1.0 / epoch)
    print("epoch", epoch, flush=True)
PY

echo "[*] 1. Job profile, snapshot while running and after (REAL API CALL)"
$COLAB profile add tp --session "$NAME" --keep-runtime --description "repro" -- "$TMP_DIR/train.py" > "$TMP_DIR/add.log" 2>&1
check "profile add" 0 $? "$TMP_DIR/add.log" "'Saved profile' in text"
$COLAB profile list --json > "$TMP_DIR/list.json" 2>/dev/null
check "profile list" 0 $? "$TMP_DIR/list.json" "r['profiles'][0]['name'] == 'tp' and r['profiles'][0]['mode'] == 'job'"
$COLAB profile run tp > "$TMP_DIR/run.log" 2>&1
check "profile run" 0 $? "$TMP_DIR/run.log" "'Started job' in text"

seen_running=0
for _ in $(seq 1 30); do
    $COLAB tray --snapshot --json > "$TMP_DIR/snap.json" 2>/dev/null
    if uv run python -c "
import json, sys
r = json.load(open(sys.argv[1], encoding='utf-8'))
c = [c for c in r['cards'] if c['name'] == sys.argv[2]][0]
sys.exit(0 if c['state'] == 'running' and c['epoch'] and c['percent'] is not None else 1)
" "$TMP_DIR/snap.json" "$NAME" 2>/dev/null; then
        seen_running=1
        break
    fi
    sleep 2
done
if [ "$seen_running" = 1 ]; then
    check "snapshot running" 0 0 "$TMP_DIR/snap.json" \
        "[c for c in r['cards'] if c['name'] == '$NAME'][0]['profile'] == 'tp' and r['icon']['level'] == 'running' and r['cu']['estimate'] is True"
else
    echo "  [FAIL] snapshot never showed the job running with an epoch"
    sed 's/^/    | /' "$TMP_DIR/snap.json"
    FAILURES=$((FAILURES + 1))
fi
$COLAB job wait -s "$NAME" --interval 5 --timeout 180 --json > "$TMP_DIR/wait.json" 2>/dev/null
check "job finished" 0 $? "$TMP_DIR/wait.json" "r['state'] == 'succeeded' and r['overall_percent'] == 100.0 and r['epoch'] == {'current': 3, 'total': 3}"
$COLAB tray --snapshot --json > "$TMP_DIR/snap2.json" 2>/dev/null
check "snapshot done" 0 $? "$TMP_DIR/snap2.json" \
    "[c for c in r['cards'] if c['name'] == '$NAME'][0]['state'] == 'done'"

echo "[*] 2. A notebook as a background job"
uv run python - "$TMP_DIR/nb.ipynb" <<'PY'
import sys, nbformat
nb = nbformat.v4.new_notebook()
nb.cells = [
    nbformat.v4.new_markdown_cell("# demo"),
    nbformat.v4.new_code_cell("#@title Setup\nx = 20"),
    nbformat.v4.new_code_cell("!echo shell-ok"),
    nbformat.v4.new_code_cell("print('answer', x + 22)"),
]
nbformat.write(nb, sys.argv[1])
PY
$COLAB job start -s "$NAME" -f "$TMP_DIR/nb.ipynb" > /dev/null 2>&1
$COLAB job wait -s "$NAME" --interval 3 --timeout 120 --json > "$TMP_DIR/nb.json" 2>/dev/null
check "notebook job" 0 $? "$TMP_DIR/nb.json" \
    "r['state'] == 'succeeded' and r['cell'] == {'index': 3, 'total': 3, 'label': 'cell 3'} and any('answer 42' in l for l in r['tail']) and any('shell-ok' in l for l in r['tail'])"

echo "[*] 3. A kernel profile reuses the runtime and sets vars"
printf 'print("vars:", CONFIG["access"], CONFIG["n"] + 1)\n' > "$TMP_DIR/kernel.py"
PROFILES=$($COLAB profile path)
cat >> "$PROFILES" <<EOF

[kp]
script = "kernel.py"
mode = "kernel"
session = "$NAME"
vars = { CONFIG = { access = "forward", n = 41 } }
stop_when_done = false
EOF
cp "$TMP_DIR/kernel.py" "$(dirname "$PROFILES")/kernel.py" 2>/dev/null || true
$COLAB profile run kp > "$TMP_DIR/kp.log" 2>&1
check "kernel profile" 0 $? "$TMP_DIR/kp.log" "'Reusing runtime' in text and 'vars: forward 42' in text"

if [ "$FAILURES" -ne 0 ]; then
    echo "[FAILURE] $FAILURES check(s) failed."
    exit 1
fi
echo "[SUCCESS] profiles, progress and the tray snapshot work end to end."
