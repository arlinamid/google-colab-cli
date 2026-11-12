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

# Integration Test: GPU feedback (docs/08_agent_feedback.md)
#
#   1. On a T4 runtime: `colab inspect --gpu` names the GPU and, once torch
#      is loaded, its CUDA memory; an oversized allocation is diagnosed as
#      cuda_oom with `colab inspect -s NAME --gpu` among the next actions.
#   2. On a CPU runtime: CUDA code is diagnosed as no_gpu.
# Allocates one T4 runtime (GPU, billed; a few minutes) and one CPU runtime,
# and stops both.

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
GPU="repro-gpu-$(date +%s)"
CPU="repro-nogpu-$(date +%s)"
COLAB="${COLAB_CMD:-uv run colab} $AUTH_FLAGS --config $SESSION_FILE"
FAILURES=0

cleanup() {
    echo "[*] Cleaning up..."
    $COLAB stop -s "$GPU" > /dev/null 2>&1 || true
    $COLAB stop -s "$CPU" > /dev/null 2>&1 || true
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

echo "[*] Creating T4 runtime '$GPU' (REAL API CALL, GPU billing)..."
if ! $COLAB new -s "$GPU" --gpu T4 > "$TMP_DIR/new.log" 2>&1; then
    echo "[FAILURE] colab new --gpu T4 failed:"; cat "$TMP_DIR/new.log"
    exit 1
fi

echo "[*] 1. GPU inspect and cuda_oom"
echo 'import torch; x = torch.zeros((1024, 1024), device="cuda")' | $COLAB exec -s "$GPU" > /dev/null 2>&1
$COLAB inspect -s "$GPU" --gpu --json > "$TMP_DIR/1.json" 2>/dev/null
check "inspect --gpu" 0 $? "$TMP_DIR/1.json" \
    "'T4' in r['gpu']['devices'][0]['name'] and r['gpu']['devices'][0]['memory_total_mb'] > 10000 and r['gpu']['torch']['allocated_mb'] >= 4"
echo 'import torch; big = torch.empty((80, 1024, 1024, 1024), dtype=torch.uint8, device="cuda")' | \
    $COLAB exec -s "$GPU" --json > "$TMP_DIR/2.json" 2>/dev/null
check "cuda_oom" 1 $? "$TMP_DIR/2.json" \
    "recs[0]['diagnosis']['code'] == 'cuda_oom' and 'colab inspect -s $GPU --gpu' in [a['cmd'] for a in recs[0]['diagnosis']['next_actions']]"
$COLAB stop -s "$GPU" > /dev/null 2>&1

echo "[*] 2. no_gpu on a CPU runtime (REAL API CALL)"
if ! $COLAB new -s "$CPU" > /dev/null 2>&1; then
    echo "[FAILURE] colab new failed."
    exit 1
fi
echo 'import torch; torch.zeros(1).cuda()' | $COLAB exec -s "$CPU" --json > "$TMP_DIR/3.json" 2>/dev/null
check "no_gpu" 1 $? "$TMP_DIR/3.json" \
    "recs[0]['diagnosis']['code'] == 'no_gpu' and any('--gpu T4' in (a['cmd'] or '') for a in recs[0]['diagnosis']['next_actions'])"

if [ "$FAILURES" -ne 0 ]; then
    echo "[FAILURE] $FAILURES check(s) failed."
    exit 1
fi
echo "[SUCCESS] GPU feedback matches real CUDA errors."
