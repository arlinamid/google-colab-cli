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

# Integration Test: `--json` result records for agents (docs/08_agent_feedback.md)
#
# On a live CPU runtime, checks against real kernel tracebacks:
#   1. a missing module -> diagnosis missing_module + `colab install` action;
#   2. an error on line 4 of a file run with --env -> location.line == 4;
#   3. a notebook -> one record per cell, location.cell of the failing cell;
#   4. a plot -> the image is saved and reported by path;
#   5. --timeout -> exit 4 and diagnosis idle_timeout;
#   6. `colab run --json` with sys.exit(3) -> exit 3 and diagnosis script_exit.
# Allocates two CPU runtimes (one for exec, one for run) and stops both.

# ---------- Auth detection ----------------------------------------------------
has_oauth2_token() {
    timeout 10 uv run python -c "import sys, keyring; sys.exit(0 if keyring.get_password('google-colab-cli', 'oauth_token') else 1)" > /dev/null 2>&1
}
# COLAB_AUTH=oauth2|adc skips the detection (e.g. a token.json without a keyring).
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
NAME="repro-json-$(date +%s)"
# COLAB_CMD runs another build, e.g. the standalone binary: COLAB_CMD=dist/bin/colab
COLAB="${COLAB_CMD:-uv run colab} $AUTH_FLAGS --config $SESSION_FILE"
FAILURES=0

cleanup() {
    echo "[*] Cleaning up..."
    $COLAB stop -s "$NAME" > /dev/null 2>&1 || true
    $COLAB sessions 2>&1 | grep -v "new version\|colab update\|enable_update" || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# check NAME EXPECTED_EXIT ACTUAL_EXIT JSON_FILE PYTHON_EXPR
# PYTHON_EXPR sees `recs` (all records) and `cells` (cell records only).
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
recs = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
cells = [r for r in recs if r["type"] == "cell"]
assert all(r["schema"] == "colab.v1" for r in recs), "schema"
assert recs[-1]["type"] == "summary", "summary last"
ok = eval(sys.argv[2])
sys.exit(0 if ok else 1)
PY
    then
        echo "  [PASS] $name"
    else
        echo "  [FAIL] $name: $expr"
        sed 's/^/    | /' "$file"
        FAILURES=$((FAILURES + 1))
    fi
}

echo "[*] Creating runtime '$NAME' (REAL API CALL)..."
if ! $COLAB new -s "$NAME" > /dev/null 2>&1; then
    echo "[FAILURE] colab new failed."
    exit 1
fi

echo "[*] 1. Missing module"
echo 'import surely_not_a_module_xyz' | $COLAB exec -s "$NAME" --json > "$TMP_DIR/1.json" 2> "$TMP_DIR/1.err"
check "missing_module" 1 $? "$TMP_DIR/1.json" \
    "cells[0]['diagnosis']['code'] == 'missing_module' and cells[0]['diagnosis']['next_actions'][0]['cmd'] == 'colab install -s $NAME surely_not_a_module_xyz'"

echo "[*] 2. File line through the --env prelude"
printf 'import os\nprint(os.environ["A"])\nx = 1\n1/0\n' > "$TMP_DIR/job.py"
$COLAB exec -s "$NAME" -f "$TMP_DIR/job.py" --env A=hello --json > "$TMP_DIR/2.json" 2> "$TMP_DIR/2.err"
check "location line 4" 1 $? "$TMP_DIR/2.json" \
    "cells[0]['error']['location']['line'] == 4 and cells[0]['error']['location']['code'] == '1/0' and cells[0]['stdout'] == 'hello\n'"

echo "[*] 3. Notebook"
uv run python - "$TMP_DIR/nb.ipynb" <<'PY'
import sys, nbformat
nb = nbformat.v4.new_notebook()
nb.cells = [nbformat.v4.new_code_cell("print('a')"), nbformat.v4.new_code_cell("y = 2\nraise ValueError('nb')")]
nbformat.write(nb, sys.argv[1])
PY
$COLAB exec -s "$NAME" -f "$TMP_DIR/nb.ipynb" --json > "$TMP_DIR/3.json" 2> "$TMP_DIR/3.err"
check "notebook cells" 1 $? "$TMP_DIR/3.json" \
    "len(cells) == 2 and cells[0]['status'] == 'ok' and cells[1]['error']['location']['cell'] == 2 and cells[1]['error']['location']['line'] == 2"

echo "[*] 4. Plot saved by path"
printf 'import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.show()\n' | \
    $COLAB exec -s "$NAME" --json --output-dir "$TMP_DIR/img" > "$TMP_DIR/4.json" 2> "$TMP_DIR/4.err"
check "image path" 0 $? "$TMP_DIR/4.json" \
    "len(cells[0]['images']) == 1 and __import__('os').path.getsize(cells[0]['images'][0]) > 1000"

echo "[*] 5. Idle timeout"
echo 'import time; time.sleep(30)' | $COLAB exec -s "$NAME" --json --timeout 3 > "$TMP_DIR/5.json" 2> "$TMP_DIR/5.err"
check "idle_timeout" 4 $? "$TMP_DIR/5.json" \
    "cells[0]['diagnosis']['code'] == 'idle_timeout' and recs[-1]['exit_code'] == 4"

echo "[*] 6. colab run --json keeps the script's exit code (REAL API CALL)"
printf '#!/usr/bin/env -S colab run\nimport sys\nprint("bye")\nsys.exit(3)\n' > "$TMP_DIR/exit.py"
$COLAB run --json "$TMP_DIR/exit.py" > "$TMP_DIR/6.json" 2> "$TMP_DIR/6.err"
check "run exit 3" 3 $? "$TMP_DIR/6.json" \
    "cells[0]['exit_code'] == 3 and cells[0]['diagnosis']['code'] == 'script_exit' and cells[0]['stdout'] == 'bye\n' and cells[0]['session'] is None"

if [ "$FAILURES" -ne 0 ]; then
    echo "[FAILURE] $FAILURES check(s) failed."
    exit 1
fi
echo "[SUCCESS] --json records match real kernel output."
