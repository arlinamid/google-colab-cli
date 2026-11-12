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

# Integration Test: `colab job` background jobs (docs/08_agent_feedback.md, B)
#
# On a live CPU runtime:
#   1. a job with real tqdm progress and colab_feedback.report() starts and
#      returns at once; status shows progress and metrics while it runs;
#   2. `colab exec` still works while the job runs (the kernel is free);
#   3. `colab job wait` ends with exit 0 and the metric trend;
#   4. a failing job -> exit 1 and diagnosis missing_module;
#   5. `colab job stop` -> state stopped, wait exits 130;
#   6. `colab job list --json` lists all three.
# Allocates one CPU runtime and stops it.

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
NAME="repro-job-$(date +%s)"
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

# check NAME EXPECTED_EXIT ACTUAL_EXIT JSON_FILE PYTHON_EXPR (sees `recs`, `r` = last)
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
r = recs[-1]
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

job_id() { uv run python -c "import json,sys; print(json.loads(open(sys.argv[1]).read())['id'])" "$1"; }

echo "[*] Creating runtime '$NAME' (REAL API CALL)..."
if ! $COLAB new -s "$NAME" > /dev/null 2>&1; then
    echo "[FAILURE] colab new failed."
    exit 1
fi

echo "[*] 1. Job with tqdm progress and metrics"
cat > "$TMP_DIR/train.py" <<'PY'
import time
from tqdm import tqdm
from colab_feedback import report, note
print("training", flush=True)
for i in tqdm(range(30)):
    time.sleep(0.7)
    report(step=i, loss=round(1 / (i + 1), 4))
note("finished")
print("done", flush=True)
PY
$COLAB job start -s "$NAME" -f "$TMP_DIR/train.py" --name train --json > "$TMP_DIR/start.json" 2> "$TMP_DIR/start.err"
check "start returns at once" 0 $? "$TMP_DIR/start.json" "r['type'] == 'job' and r['state'] == 'starting'"
JOB1=$(job_id "$TMP_DIR/start.json")
sleep 10
$COLAB job status -s "$NAME" "$JOB1" --json > "$TMP_DIR/status.json" 2> "$TMP_DIR/status.err"
check "status while running" 0 $? "$TMP_DIR/status.json" \
    "r['state'] == 'running' and r['progress'] and r['progress']['total'] == 30 and 0 < r['progress']['percent'] < 100 and r['metrics']['count'] > 0"

echo "[*] 2. The kernel stays free"
OUT=$(echo 'print(6 * 7)' | $COLAB exec -s "$NAME" 2>/dev/null)
if [ "$OUT" = "42" ]; then echo "  [PASS] exec during the job"; else echo "  [FAIL] exec during the job: '$OUT'"; FAILURES=$((FAILURES + 1)); fi

echo "[*] 3. Wait for it"
$COLAB job wait -s "$NAME" "$JOB1" --interval 5 --json > "$TMP_DIR/wait.json" 2> "$TMP_DIR/wait.err"
check "wait succeeds" 0 $? "$TMP_DIR/wait.json" \
    "r['state'] == 'succeeded' and r['exit_code'] == 0 and r['metrics']['trend']['loss']['last'] < r['metrics']['trend']['loss']['first'] and r['tail'][-1] == 'done'"

echo "[*] 4. A failing job"
$COLAB job start -s "$NAME" --json -- 'python3 -c "import surely_missing_mod_xyz"' > "$TMP_DIR/start2.json" 2>/dev/null
JOB2=$(job_id "$TMP_DIR/start2.json")
$COLAB job wait -s "$NAME" "$JOB2" --interval 3 --json > "$TMP_DIR/wait2.json" 2> "$TMP_DIR/wait2.err"
check "failed job diagnosis" 1 $? "$TMP_DIR/wait2.json" \
    "r['state'] == 'failed' and r['diagnosis']['code'] == 'missing_module'"

echo "[*] 5. Stop a job"
$COLAB job start -s "$NAME" --json -- 'sleep 300' > "$TMP_DIR/start3.json" 2>/dev/null
JOB3=$(job_id "$TMP_DIR/start3.json")
sleep 5
$COLAB job stop -s "$NAME" "$JOB3" > /dev/null 2>&1
$COLAB job wait -s "$NAME" "$JOB3" --interval 3 --timeout 60 --json > "$TMP_DIR/wait3.json" 2> "$TMP_DIR/wait3.err"
check "stopped job" 130 $? "$TMP_DIR/wait3.json" "r['state'] == 'stopped'"

echo "[*] 6. List"
$COLAB job list -s "$NAME" --json > "$TMP_DIR/list.json" 2> "$TMP_DIR/list.err"
check "list" 0 $? "$TMP_DIR/list.json" \
    "[x['state'] for x in recs] == ['succeeded', 'failed', 'stopped']"

if [ "$FAILURES" -ne 0 ]; then
    echo "[FAILURE] $FAILURES check(s) failed."
    exit 1
fi
echo "[SUCCESS] colab job works end to end."
