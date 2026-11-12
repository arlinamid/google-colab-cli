---
log:
2026-11-12: Implemented phase A: `src/colab_cli/diagnosis.py` (rules), `src/colab_cli/results.py` (records, line mapping, summary), `--json`/`--json-stream`/`--output-dir` on `exec` and `--json`/`--output-dir` on `run`. Differences from the first design: usage errors (exit 2) and failures before execution (e.g. a refused assign in `run`) still print text on stderr without JSON; `run` keeps the script's own exit code instead of the 0/1 table; `session` in a `run` record is null when the runtime is already stopped, so `next_actions` only name a session with `--keep`. Tracebacks are mapped from the real IPython 7 format Colab uses (`/tmp/ipykernel_N/M.py in <cell line: 0>()`, `---->` marker), plus SyntaxError and IPython 8 forms.
2026-11-12: Initial design of the agent feedback loop (A-D): result envelope with diagnosis and next actions, detached jobs with progress, kernel inspection, and cost feedback. Design only; no code yet.
---

# Design: Agent Feedback Loop (`--json`, `colab job`, `colab inspect`, cost)

## Motivation

AI agents (Claude Code, Codex, Cursor, Gemini CLI) already drive this CLI, but
what they get back is text meant for people: ANSI tracebacks, progress bars,
`[colab]` lines. An agent has to guess whether a run failed, where, why, and
what to try next, and a long run blocks it until the end. That is plain
automation: the agent fires a command and parses whatever comes back.

The goal is a **feedback loop**: every command tells the agent *what happened*,
*where*, *why*, *what it cost*, and *what to do next*, and long work reports
progress while it runs, so the agent can react (wait, fix, stop, scale up)
instead of only reading the end result.

Upstream has an open PR for `colab exec --json` (googlecolab/google-colab-cli#237:
one NDJSON record per cell with `cell_index`, `cell_id`, `status`, `stdout`,
`stderr`, `data`, `images`, `error`). Part A keeps those field names and adds
to them, so a later upstream merge stays a superset instead of a conflict.

## Principles

- **Fire-and-forget stays.** No local daemons or background threads. Long work
  runs **on the VM**; the CLI starts it, exits, and reads its state later.
- **Opt-in, machine-first.** Without `--json` nothing changes. With it, stdout
  carries only JSON; every human line (`[colab] ...`, update banner) goes to
  stderr.
- **Stable schema.** Every JSON object has `"schema": "colab.v1"`. Fields are
  only added within v1; removals or renames need v2.
- **Actionable, not clever.** A diagnosis is a rule match with a fixed `code`
  and concrete `next_actions` (real `colab` commands), never free-form advice.
- **Cross-platform.** Pure Python on the client; VM-side code is Linux only.
  Paths in JSON use the platform's own form for local files and POSIX for
  remote ones. Tests run on Linux, macOS and Windows (CI matrix).
- **TDD and live tests** as in AGENTS.md; every phase ends with an
  integration script under `integration/`.

## Phases

| Phase | Delivers | Depends on |
|-------|----------|------------|
| A | Result envelope, error diagnosis, `next_actions`, local line mapping (**implemented**) | - |
| B | `colab job` detached runs with progress, metrics and wait/stop | A |
| C | `colab inspect` kernel and machine state | A |
| D | Cost block, `--budget`, `colab usage --json` | A (B for budgets on jobs) |

Each phase is its own PR on top of `feat/agent-feedback`.

---

## A. Result envelope, diagnosis and next actions

### Surface

- `colab exec --json` and `colab run --json`: one JSON record per executed
  block (stdin/file = one block, notebook = one per cell), NDJSON on stdout,
  then one `summary` record.
- `colab exec --json-stream`: also emits `output` events while code runs
  (`{"type": "output", "stream": "stdout", "text": "..."}`), for agents that
  watch long cells.
- Env var `COLAB_CLI_JSON=1` turns on `--json` for every command that
  supports it (agents set it once).
- Exit status: 0 all good, 1 code error, 2 usage error, 3 session problem
  (lost, not found), 4 timeout, 5 budget stop (phase D). Today exec exits 1
  for all of these; the codes are new only under `--json` to stay compatible.

### Record

```json
{
  "schema": "colab.v1",
  "type": "cell",
  "cell_index": 0,
  "cell_id": null,
  "status": "error",
  "stdout": "...tail...",
  "stderr": "",
  "stdout_truncated": false,
  "data": [{"mime": "text/plain", "text": "42"}],
  "images": ["C:/work/out/cell0_1.png"],
  "error": {
    "ename": "ModuleNotFoundError",
    "evalue": "No module named 'torchvision'",
    "traceback": "plain text, ANSI stripped",
    "location": {"file": "train.py", "line": 42, "cell": null, "code": "import torchvision"}
  },
  "diagnosis": {
    "code": "missing_module",
    "summary": "The runtime has no 'torchvision' package.",
    "next_actions": [
      {"cmd": "colab install -s demo torchvision", "why": "install the missing package"},
      {"cmd": "colab exec -s demo -f train.py --json", "why": "run again"}
    ]
  },
  "duration_s": 3.4,
  "session": "demo"
}
```

`summary` record: `{"schema", "type": "summary", "status", "cells_run",
"cells_failed", "duration_s", "session", "cost"}` (`cost` from phase D,
`null` until then).

### Line mapping

The CLI prepends `_build_env_prelude(env_vars)` (and the `/content` chdir runs
as its own request). For each block it records `prelude_lines` and the block's
source origin (file + first line, or notebook cell number). Traceback frames
whose filename is the IPython cell (`<ipython-input-N-...>`, `/tmp/ipykernel_*/...py`,
or `Cell In[N], line L`) are mapped back: `line = L - prelude_lines +
first_line - 1`. Frames in library files stay as they are. The innermost frame
in user code becomes `error.location`.

### Diagnosis rules

A table in `src/colab_cli/diagnosis.py`, matched in order on `ename`,
`evalue` and the traceback text. Each rule yields a `code`, a one-line
`summary` and `next_actions` templates (`{session}`, `{module}`, ...):

| code | matches | next_actions |
|------|---------|--------------|
| `missing_module` | `ModuleNotFoundError: No module named 'X'` | `colab install -s S <pip name>` (import name to pip name map for common cases: cv2 -> opencv-python, PIL -> pillow, sklearn -> scikit-learn, yaml -> pyyaml) |
| `cuda_oom` | `torch.OutOfMemoryError`, `CUDA out of memory` | lower batch size; `colab inspect -s S --gpu`; `colab new --gpu A100` |
| `no_gpu` | `Torch not compiled with CUDA`, `no CUDA GPUs are available` on a CPU session | `colab new --gpu T4` |
| `disk_full` | `OSError: [Errno 28]` | `colab inspect -s S --disk`; `colab rm` |
| `file_not_found` | `FileNotFoundError` with a relative path | `colab ls -s S /content`; `colab upload` |
| `session_lost` | `SessionConnectionLost`, 404/401 on the runtime | `colab status -s S`; `colab new` |
| `idle_timeout` | the CLI's `--timeout` fired | raise `--timeout`, or use `colab job` (phase B) |
| `keyboard_interrupt` | `KeyboardInterrupt` | none (interrupted on purpose) |
| `syntax_error` | `SyntaxError`, `IndentationError` | fix at `location`; `colab exec --check` |
| `unknown` | anything else | none |

Rules are data, unit-tested one by one, and easy to extend.

### Implementation notes

- `commands/execution.py`: collect outputs per block (already returned by
  `execute_code`), build records with a new `results.py` (`build_record`,
  `strip_ansi`, `map_location`). Human display stays as is without `--json`.
- Images: write to `--output-dir` (default: a temp dir printed in the
  summary), report paths only; never base64 in JSON.
- `stdout`/`stderr` keep the last 64 KiB each with a `*_truncated` flag; the
  full text stays in the history log.
- `history.log_event` also stores the `diagnosis` code, so `colab log` can show
  it later.

### Tests

`tests/test_results.py` (record shape, ANSI strip, truncation, schema field),
`tests/test_diagnosis.py` (one test per rule, plus `unknown`),
`tests/test_line_mapping.py` (prelude offset, notebook cell, library frames),
`tests/test_exec_json.py` (stdout is only JSON, `[colab]` lines on stderr,
exit codes). Integration: `integration/repro_agent_json/test.sh` runs a
missing-module error, a ZeroDivisionError on line N of a file with `--env`
set, and a notebook, then checks `diagnosis.code` and `location.line`.

---

## B. Detached jobs with progress (`colab job`)

### Surface

```
colab job start -s S [--name NAME] [--env K=V] [--secret ...] (-f script.py | -- CMD...)
colab job status -s S [JOB] [--json]
colab job tail   -s S JOB [-n 50] [--follow]
colab job wait   -s S JOB [--timeout SEC] [--json]
colab job stop   -s S JOB
colab job list   -s S [--json]
```

`colab run --detach script.py` is shorthand for `new` + `job start` and
prints the session and job id.

### How it runs on the VM (no local daemon)

`job start` uploads the script (Contents API) to
`/content/.colab-jobs/<id>/script.py` and starts it from the kernel with
`subprocess.Popen(["setsid", "python", "-u", ...], stdout=log, stderr=log)`,
fully detached from the kernel. A small wrapper writes
`/content/.colab-jobs/<id>/status.json`
(`state`, `pid`, `started_at`, `ended_at`, `exit_code`, `last_output_at`) at
start and in a `finally`/exit handler, and the CLI exits right away.
`status`/`tail`/`wait` read those files over the Contents API: a GET, so no
kernel round trip and no clash with a busy kernel.

A process (not a kernel cell) is the default because it survives the CLI going
away, a kernel restart, and leaves the kernel free for `exec` and `inspect`.
Kernel state is not shared; that is the trade-off, and it is documented.

### Progress and metrics, the feedback channel

- **Automatic:** the wrapper scans output for tqdm-style progress
  (`45%|...| 450/1000 [..<..]`) and stores the last one as
  `progress = {"percent", "n", "total", "eta_s"}` in `status.json`.
- **Explicit:** the job can import a tiny helper the wrapper puts on
  `PYTHONPATH`:

  ```python
  from colab_feedback import report, note
  report(step=120, loss=0.41, val_acc=0.88)   # appends to metrics.jsonl
  note("switched to lr=1e-4")                  # human/agent-readable event
  ```

  Outside a job the helper is a no-op, so scripts stay runnable locally.
- `job status --json` returns `state`, `progress`, the last metrics row, the
  metric trend (first/last/min/max per key), `last_output_at`, and a
  `stalled: true` flag when no output arrived for `--stall-after` seconds
  (default 600). It also adds `diagnosis` for a failed job by running the
  rules from phase A on the log tail.

### Wait and keep-alive

`job wait` polls `status.json` every 30 s until the job ends or `--timeout`
passes, and pings the existing keep-alive endpoint between polls
(`keepalive.wait_with_keep_alive`), in the foreground. Exit code is the job's.

### Open question to verify first (probe before coding)

Whether Colab reclaims a runtime that has a running process but no connected
client. Probe: start a 2-hour `sleep`-and-print job, disconnect, check
`colab sessions` and the log every 20 min, then stop it. If runtimes are
reclaimed, `job start` prints a warning and recommends `colab keepalive` or
`job wait`; the design otherwise stays the same. Record the result here.

### Tests

Unit: wrapper script generation, status parsing, tqdm parser (several bar
formats), metrics trend, stall detection, `wait` polling with a fake clock.
Integration: `integration/repro_job/test.sh` starts a job that prints
progress and calls `report()`, checks `status`/`tail` while it runs, waits,
then checks a failing job's `diagnosis`.

---

## C. Kernel and machine state (`colab inspect`)

### Surface

```
colab inspect -s S [--vars] [--gpu] [--disk] [--packages] [--files PATH] [--json]
```

No flag means all sections except `--files`.

### What it returns

- `vars`: user names from `get_ipython().user_ns` (skipping modules, IPython
  internals and `_`-names): `name`, `type`, and when cheap, `shape`, `dtype`,
  `len`, `device` (torch/numpy/pandas detected by duck typing, without
  importing them), `nbytes`. Never the value's `repr`, so output stays small
  and secrets in variables are not printed.
- `gpu`: `nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu`
  and, if torch is already imported, `torch.cuda.memory_allocated/max_memory_allocated`.
- `disk`: free/total for `/content`; `memory`: from `/proc/meminfo`.
- `packages`: `pip list --format=json` diffed against a baseline captured on the
  first `inspect` of a session (stored in session state), so the agent sees what
  it installed.
- `files`: a listing of PATH (Contents API, no kernel).
- `kernel`: `idle`/`busy`; when busy, `vars` is skipped with
  `"vars": {"skipped": "kernel busy"}` instead of queueing behind the running
  cell.

The introspection snippet is read-only, runs in the kernel as one request, and
prints a single JSON line with a marker the CLI extracts.

### Tests

Unit tests run the snippet against a fake namespace (numpy/pandas/torch-like
duck types, big objects, a module, a secret-looking string) and check shape
detection and that no values leak. Integration: create variables with
`colab exec`, then check `colab inspect --json`.

---

## D. Cost feedback

### Surface

- A `cost` block in every phase A `summary` and in `job status`:
  `{"accelerator", "rate_cu_per_hour", "used_cu", "balance_cu", "hours_left_at_rate"}`.
  `used_cu` is `rate * duration`, labelled as an estimate.
- `colab usage --json` (balance, hourly rate, active assignments).
- `--budget CU` on `exec`, `run` and `job start`/`wait`: stop (interrupt the
  cell, or `job stop`) once the estimate reaches the budget; exit code 5 and
  `diagnosis.code = "budget_reached"`.
- `colab job start` warns on stderr when `hours_left_at_rate` is below the
  job's `--expect-hours`, if given.

### Source of numbers

`ConsumptionUserInfo` (`paid_compute_units_balance`, `consumption_rate_hourly`)
and `CcuInfo` already exist (`src/colab_cli/consumption.py`, used by
`colab usage`). Read the rate once at start; read the balance at start and at
the end. No extra polling.

### Tests

Unit: estimate math, budget stop paths for exec and jobs (fake clock and fake
consumption client), JSON shape. Live: a CPU run with a tiny `--budget`
must stop with exit 5 (CPU rate is low but non-zero; verify the actual rate
first with `colab usage --json`).

---

## Rollout

1. A (foundation; also refresh `SKILL.md` so agents know about `--json`,
   `diagnosis` and `next_actions`).
2. B, after the reclaim probe.
3. C.
4. D.

Each PR updates this document's log, README command index, CHANGELOG
`[Unreleased]`, and `SKILL.md`.

## Out of scope (for now)

- An MCP server mode: Google has a separate Colab MCP server; this CLI stays a
  CLI that agents call, with JSON as the contract.
- Agent-to-human approval prompts.
- Streaming metrics to external trackers (W&B, TensorBoard).
