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

# Integration Test: `colab inspect` and cost feedback (docs/08, phases C and D)
#
# On a live CPU runtime:
#   1. inspect describes variables (shape, length) but never prints values;
#   2. a package installed after the first inspect shows up as added;
#   3. a busy kernel is reported, not queued behind;
#   4. usage --json and the cost block of exec --json;
#   5. exec --budget worth ~10 s stops a 60 s cell: exit 5, budget_reached;
#   6. job start --budget worth ~10 s is stopped on the runtime.
# Allocates one CPU runtime and stops it.

has_oauth2_token() {
    timeout 10 uv run python -c "import sys, keyring; sys.exit(0 if keyring.get_password('google-colab-cli', 'oauth_token') else 1)" > /dev/null 2>&1
}
if has_oauth2_token; then
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
NAME="repro-inspect-$(date +%s)"
COLAB="uv run colab $AUTH_FLAGS --config $SESSION_FILE"
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
text = open(sys.argv[1], encoding="utf-8").read()
recs = [json.loads(line) for line in text.splitlines() if line.strip()]
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

echo "[*] Creating runtime '$NAME' (REAL API CALL)..."
if ! $COLAB new -s "$NAME" > /dev/null 2>&1; then
    echo "[FAILURE] colab new failed."
    exit 1
fi

echo "[*] 1. Variables without values"
printf 'import numpy as np\narr = np.zeros((100, 3))\ntoken = "hf_supersecret_xyz"\nitems = [1, 2]\n' | $COLAB exec -s "$NAME" > /dev/null 2>&1
$COLAB inspect -s "$NAME" --json > "$TMP_DIR/1.json" 2> "$TMP_DIR/1.err"
check "inspect vars" 0 $? "$TMP_DIR/1.json" \
    "{v['name']: v for v in r['vars']}['arr']['shape'] == [100, 3] and {v['name']: v for v in r['vars']}['token']['len'] == 18 and 'supersecret' not in text and r['disk']['free_gb'] > 0 and r['gpu']['devices'] == [] and r['packages']['count'] > 100"

echo "[*] 2. A package installed since the first inspect"
echo '!pip install -q tinydb' | $COLAB exec -s "$NAME" > /dev/null 2>&1
$COLAB inspect -s "$NAME" --packages --json > "$TMP_DIR/2.json" 2> "$TMP_DIR/2.err"
check "package added" 0 $? "$TMP_DIR/2.json" \
    "'tinydb' in [p['name'] for p in r['packages']['added']]"

echo "[*] 3. A busy kernel is not queued behind"
echo 'import time; time.sleep(40)' | $COLAB exec -s "$NAME" > /dev/null 2>&1 &
BG=$!
sleep 12
$COLAB inspect -s "$NAME" --json > "$TMP_DIR/3.json" 2> "$TMP_DIR/3.err"
check "busy kernel" 0 $? "$TMP_DIR/3.json" \
    "r['kernel'] == 'busy' and r['vars'] == {'skipped': 'kernel busy'}"
wait $BG

echo "[*] 4. Usage and the cost block"
$COLAB usage --json > "$TMP_DIR/4.json" 2> "$TMP_DIR/4.err"
check "usage --json" 0 $? "$TMP_DIR/4.json" "r['type'] == 'usage' and r['rate_cu_per_hour'] >= 0"
RATE=$(uv run python -c "import json,sys; print(json.load(open(sys.argv[1]))['rate_cu_per_hour'])" "$TMP_DIR/4.json")
echo 'print(1)' | $COLAB exec -s "$NAME" --json > "$TMP_DIR/4b.json" 2> "$TMP_DIR/4b.err"
check "exec cost block" 0 $? "$TMP_DIR/4b.json" "r['cost'] is not None and r['cost']['rate_cu_per_hour'] == $RATE"

if uv run python -c "import sys; sys.exit(0 if float(sys.argv[1]) > 0 else 1)" "$RATE"; then
    BUDGET=$(uv run python -c "import sys; print(float(sys.argv[1]) * 10 / 3600)" "$RATE")
    echo "[*] 5. exec --budget $BUDGET CU (about 10 s at $RATE CU/h)"
    START=$(date +%s)
    echo 'import time; time.sleep(60)' | $COLAB exec -s "$NAME" --json --budget "$BUDGET" > "$TMP_DIR/5.json" 2> "$TMP_DIR/5.err"
    RC=$?
    ELAPSED=$(( $(date +%s) - START ))
    check "exec budget" 5 $RC "$TMP_DIR/5.json" \
        "recs[0]['diagnosis']['code'] == 'budget_reached' and r['exit_code'] == 5 and $ELAPSED < 50"

    echo "[*] 6. job start --budget"
    $COLAB job start -s "$NAME" --budget "$BUDGET" --json -- 'sleep 300' > "$TMP_DIR/6a.json" 2>/dev/null
    JOB=$(uv run python -c "import json,sys; print(json.load(open(sys.argv[1]))['id'])" "$TMP_DIR/6a.json")
    $COLAB job wait -s "$NAME" "$JOB" --interval 5 --timeout 90 --json > "$TMP_DIR/6.json" 2> "$TMP_DIR/6.err"
    check "job budget" 130 $? "$TMP_DIR/6.json" \
        "r['state'] == 'stopped' and r['stop_reason'] == 'budget' and r['diagnosis']['code'] == 'budget_reached' and r['elapsed_s'] < 40"
else
    echo "  [SKIP] 5-6: the hourly rate is 0, so a budget cannot be converted to time."
fi

if [ "$FAILURES" -ne 0 ]; then
    echo "[FAILURE] $FAILURES check(s) failed."
    exit 1
fi
echo "[SUCCESS] inspect and cost feedback work end to end."
