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

# Integration Test: `colab stop --all` next to the fork's `colab stop --orphans`
# On live CPU runtimes:
#   1. `colab stop --all --orphans` refuses (exit 2) and stops nothing;
#   2. `colab stop --all` stops both local sessions and leaves the orphan
#      (a runtime only another sessions.json knows) running;
#   3. `colab stop --orphans --yes` stops the orphan. Skipped, and the orphan
#      stopped by name instead, when runtimes not started by this test were
#      already on the server: --orphans would end those too.
# Allocates three CPU runtimes and stops them.

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

COLAB="colab $AUTH_FLAGS"
TS=$(date +%s)
A="repro-stopall-a-$TS"
B="repro-stopall-b-$TS"
O="repro-stopall-orphan-$TS"
TMP_DIR=$(mktemp -d)
OTHER_CONFIG=$(cygpath -m "$TMP_DIR/sessions.json" 2>/dev/null || echo "$TMP_DIR/sessions.json")
FAILED=0

cleanup() {
    echo "[*] Cleaning up..."
    $COLAB stop -s "$A" > /dev/null 2>&1
    $COLAB stop -s "$B" > /dev/null 2>&1
    $COLAB --config "$OTHER_CONFIG" stop -s "$O" > /dev/null 2>&1
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

fail() { echo "[FAILURE] $*"; FAILED=1; }

on_server() {
    $COLAB sessions 2>/dev/null > "$TMP_DIR/sessions.txt"
    cat "$TMP_DIR/sessions.txt"
}

endpoint_of() {
    uv run python -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]['endpoint'])" "$1" "$2"
}

echo "[*] 0. Runtimes on the server before the test"
BEFORE=$(on_server | grep -c ' | Hardware: ')
echo "    $BEFORE runtime(s)"

echo "[*] Allocating $A, $B and the orphan $O (REAL API CALLS)"
$COLAB new -s "$A" > /dev/null || { echo "[FAILURE] colab new $A failed."; exit 1; }
$COLAB new -s "$B" > /dev/null || { echo "[FAILURE] colab new $B failed."; exit 1; }
$COLAB --config "$OTHER_CONFIG" new -s "$O" > /dev/null || { echo "[FAILURE] colab new $O failed."; exit 1; }
O_EP=$(endpoint_of "$OTHER_CONFIG" "$O")
echo "    orphan endpoint: $O_EP"

on_server > /dev/null
grep -q "^\[?\] $O_EP" "$TMP_DIR/sessions.txt" || fail "the orphan is not listed as [?]"
grep -q "^\[$A\]" "$TMP_DIR/sessions.txt" || fail "$A is not listed"

echo "[*] 1. colab stop --all --orphans refuses"
OUT=$($COLAB stop --all --orphans 2>&1); RC=$?
[ "$RC" = 2 ] || fail "expected exit 2, got $RC: $OUT"
echo "$OUT" | grep -q -- "--all alone" || fail "no usage message: $OUT"
on_server > /dev/null
[ "$(grep -c ' | Hardware: ' "$TMP_DIR/sessions.txt")" = "$((BEFORE + 3))" ] || fail "a runtime was stopped by the refused command"

echo "[*] 2. colab stop --all stops the two local sessions, not the orphan"
OUT=$($COLAB stop --all 2>&1); RC=$?
echo "$OUT" | sed 's/^/    /'
[ "$RC" = 0 ] || fail "stop --all exited $RC"
echo "$OUT" | grep -q "Stopped 2 session(s)" || fail "expected 'Stopped 2 session(s)'"
on_server > /dev/null
grep -q "^\[$A\]\|^\[$B\]" "$TMP_DIR/sessions.txt" && fail "a local session survived stop --all"
grep -q "$O_EP" "$TMP_DIR/sessions.txt" || fail "stop --all also stopped the orphan"

echo "[*] 3. colab stop --orphans --yes stops the orphan"
if [ "$BEFORE" = 0 ]; then
    OUT=$($COLAB stop --orphans --yes 2>&1); RC=$?
    echo "$OUT" | sed 's/^/    /'
    [ "$RC" = 0 ] || fail "stop --orphans exited $RC"
    echo "$OUT" | grep -q "Stopped $O_EP" || fail "the orphan was not stopped"
else
    echo "    [SKIP] $BEFORE runtime(s) were already running; stopping the orphan by name."
    $COLAB --config "$OTHER_CONFIG" stop -s "$O" > /dev/null
fi
on_server > /dev/null
grep -q "$O_EP" "$TMP_DIR/sessions.txt" && fail "the orphan is still on the server"

if [ "$FAILED" = 0 ]; then
    echo "[SUCCESS] stop --all and stop --orphans work side by side."
else
    exit 1
fi
