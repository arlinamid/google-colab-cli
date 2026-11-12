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

# Integration Test: learning from history (docs/08_agent_feedback.md, phase E)
#
# On a live CPU runtime: a script fails on a missing package, `colab install`
# fixes it, it succeeds; after uninstalling the package the same failure
# comes back, and its --json record must say it was seen before and that
# installing the package fixed it. Then `colab history errors|stats` for
# this session. Allocates one CPU runtime and stops it. Writes to the local
# history like any other run (one file, named after the session).

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
NAME="repro-history-$(date +%s)"
# COLAB_CMD runs another build, e.g. the standalone binary: COLAB_CMD=dist/bin/colab
COLAB="${COLAB_CMD:-uv run colab} $AUTH_FLAGS --config $SESSION_FILE"
FAILURES=0
PKG=tinydb

cleanup() {
    echo "[*] Cleaning up..."
    $COLAB stop -s "$NAME" > /dev/null 2>&1 || true
    $COLAB sessions 2>&1 | grep -v "new version\|colab update\|enable_update" || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

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

echo "[*] Creating runtime '$NAME' (REAL API CALL)..."
if ! $COLAB new -s "$NAME" > /dev/null 2>&1; then
    echo "[FAILURE] colab new failed."
    exit 1
fi
printf 'import %s\nprint("ok", %s.__name__)\n' "$PKG" "$PKG" > "$TMP_DIR/job.py"

echo "[*] 1. Fail, install, succeed"
$COLAB exec -s "$NAME" -f "$TMP_DIR/job.py" --json > "$TMP_DIR/1.json" 2>/dev/null
check "first failure" 1 $? "$TMP_DIR/1.json" "recs[0]['diagnosis']['code'] == 'missing_module'"
$COLAB install -s "$NAME" "$PKG" > /dev/null 2>&1
$COLAB exec -s "$NAME" -f "$TMP_DIR/job.py" --json > "$TMP_DIR/2.json" 2>/dev/null
check "success after install" 0 $? "$TMP_DIR/2.json" "recs[0]['status'] == 'ok'"

echo "[*] 2. The same failure again"
echo "!pip uninstall -y -q $PKG" | $COLAB exec -s "$NAME" > /dev/null 2>&1
# The kernel keeps the imported module; a fresh kernel sees it gone.
$COLAB restart-kernel -s "$NAME" > /dev/null 2>&1
$COLAB exec -s "$NAME" -f "$TMP_DIR/job.py" --json > "$TMP_DIR/3.json" 2>/dev/null
check "seen_before with the fix" 1 $? "$TMP_DIR/3.json" \
    "recs[0]['diagnosis']['seen_before']['count'] >= 1 and '$NAME' in recs[0]['diagnosis']['seen_before']['sessions'] and {'install': ['$PKG']} in recs[0]['diagnosis']['seen_before']['last_fix']['steps']"

echo "[*] 3. colab history"
$COLAB history errors -s "$NAME" --json > "$TMP_DIR/4.json" 2>/dev/null
check "history errors" 0 $? "$TMP_DIR/4.json" \
    "len(recs) == 1 and recs[0]['count'] == 2 and recs[0]['code'] == 'missing_module' and recs[0]['last_fix']['steps'] == [{'install': ['$PKG']}]"
$COLAB history stats job.py -s "$NAME" --json > "$TMP_DIR/5.json" 2>/dev/null
check "history stats" 0 $? "$TMP_DIR/5.json" \
    "r['file'] == 'job.py' and r['runs'] == 3 and r['failures'] == 2 and r['median_duration_s'] is not None"

if [ "$FAILURES" -ne 0 ]; then
    echo "[FAILURE] $FAILURES check(s) failed."
    exit 1
fi
echo "[SUCCESS] history feedback works end to end."
