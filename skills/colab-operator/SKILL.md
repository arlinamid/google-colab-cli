---
name: colab-operator
description: Operate Google Colab environments via the `colab` CLI. Use when asked to create or manage GPU/TPU sessions, run Python/shell on a remote Colab VM, sync files, automate environment setup (packages, auth, Drive), or export session history.
---

# Skill: Colab Session Operator

Operate Google Colab environments via the `colab` CLI: provision GPU/TPU sessions, run Python/shell on the VM, sync files, and capture work as notebooks.

## Installation

If the user does not already have the `colab` tool installed, it can be acquired
by running `uv tool install google-colab-cli` or `pip install google-colab-cli`.

## When to activate
- Creating or managing TPU/GPU sessions.
- Running Python or shell on a remote Colab VM.
- Syncing files between local and remote.
- Automating environment setup (packages, auth, Drive).
- Exporting session history as a Jupyter notebook.

## Mental model (read this first)
- **A session == a live Jupyter kernel on a rented VM.** `colab new` allocates a billable VM; `colab stop` releases it. Sessions stay alive as long as the kernel is active, with the Colab backend automatically maintaining liveness.
- **Kernel state PERSISTS across `colab exec` / `colab repl` calls in the same session.** Each invocation reattaches to the *same* kernel (the kernel ID is cached in local state) and only closes the websocket on exit — it does **not** shut the kernel down. So imports, variables, and defined functions survive between separate `colab exec` commands. Build up state incrementally; don't re-import everything each call. (`colab stop` and `colab restart-kernel` are what actually reset it.)
- **Default working directory is `/content`.** Every `exec`/`repl`/`run` `cd`s there first; prefer absolute paths (`/content/...`) for file work. For `colab ls/rm/upload/download`, absolute `/content/...` paths work and the default `ls` path is `content` (VM root).
- **`colab` is fire-and-forget.** Each command authenticates, does one thing, and exits.

## Authentication (the #1 thing that blocks agents)
- The global flag is `--auth={adc,oauth2}` and the **default is `adc`** (Application Default Credentials). It must come *before* the subcommand: `colab --auth=adc new -s x`.
- **ADC setup** (most reliable for headless/agent use). The Colab backends need a specific scope set, so re-mint ADC with all four scopes:
  ```bash
  gcloud auth application-default login \
    --scopes=openid,\
  https://www.googleapis.com/auth/cloud-platform,\
  https://www.googleapis.com/auth/userinfo.email,\
  https://www.googleapis.com/auth/colaboratory
  ```
  Why all four: `userinfo.email` (session backend `colab.research.google.com`, else 401), `colaboratory` (Colab features/APIs), `openid`+`cloud-platform` (mandated by gcloud itself; it rejects scope lists missing `cloud-platform`).
- **oauth2 setup**: `colab --auth=oauth2 <anything>` triggers a browser consent flow on first use (token cached in the OS keyring via `keyring`). Requires a client config at `~/.colab-cli-oauth-config.json` (or `-c PATH`), falling back to the bundled config. The browser step means it usually needs a human; prefer ADC for agents.
- **Verify auth in one shot**: `colab sessions` (read-only, lists server assignments) or `colab whoami` (hidden debug command: prints the active email, scopes, audience, and expiry). When any call 401s or 403s, the cause is almost always missing credentials or scopes — `colab whoami` shows it instantly.
- **Do NOT confuse `colab auth` with CLI authentication.** `colab auth` injects *VM-side* GCP credentials into the running kernel (so notebook code can call BigQuery/GCS); it is orthogonal to how the CLI itself authenticates. Never suggest "run `colab auth`" to fix a CLI 401/403 — that's a scope/identity problem fixed via the `gcloud` command above.

## Workflow

### Provision
- `colab new -s <name>` (CPU). Add `--gpu A100` or `--tpu v6e1` for accelerators. **Always pass `-s <name>`** — an omitted name is auto-generated as a random 6-hex string, which makes later commands ambiguous.
- Supported `--gpu`: `T4`, `L4`, `G4`, `H100`, `A100`. Supported `--tpu`: `v5e1`, `v6e1`.
- **Gotcha**: an unrecognized `--gpu` value silently falls back to **A100** (which then usually fails the next step). A `400` on `colab new` with an accelerator means no quota/entitlement for it on this account — fall back to `--gpu T4` or omit the flag for CPU.
- Accelerator availability is tier-gated; most accounts can only get CPU. Don't assume a GPU/TPU will allocate.

### Execute
- **Preferred**: `colab exec -s <name> -f <script.py>` runs a local script on the remote VM (read locally, sent to the kernel — no manual upload needed).
- **Piped code**: `echo "print(1)" | colab exec -s <name>` or `cat script.py | colab exec -s <name>`.
- **Notebooks**: `colab exec -s <name> -f nb.ipynb` runs each code cell and writes results to `<basename>_output.ipynb` next to the input. A `# @title Foo` first line labels the cell in progress output.
- **Plots/images**: PNG/JPEG outputs are intercepted. Use `--output-image <path>` on `exec`/`repl` to save to a known location (otherwise a temp path is printed). Inline terminal-image escapes are auto-suppressed when stdout isn't a TTY, so piped/captured output stays clean.
- **Shell**: run a one-shot shell command with `colab ssh -s <name> -- <cmd>`: it runs in `/content` and exits with the command's status. Output is byte-exact when stdout is piped or redirected; ssh allocates a PTY only when stdin and stdout are both terminals, so a shell tool that runs commands in a terminal should add `</dev/null` and redirect the output to a file (a pipe keeps `colab ssh`'s exit status only under `set -o pipefail`). One argument is a shell command line (quote it for pipes or globs: `colab ssh -s <name> -- 'nvidia-smi | head -5'`); several arguments are an exact argv, so pass untrusted values as separate arguments after `--` where the program accepts it (e.g. `colab ssh -s <name> -- ls -- "$f"`), never inside a shell line or a piped script. Pipe a multi-line script to `colab ssh -s <name> -- bash`. Use `colab exec` for Python code. When ssh can't work (no local `ssh` client, no `~/.ssh/id_ed25519` or `id_ecdsa` key and no `-i KEY`, a tool not on the minimal ssh environment's `PATH` such as `gcloud` or `node`, or the runtime's limited concurrent SSH connections in use, which the server answers with 429, e.g. while a person or an IDE is connected), fall back to `exec` with IPython's `!` prefix: `echo '!df -h /content' | colab exec -s <name>` (single quotes stop your local shell expanding `!`). Each `!` line runs in its own shell with no input (a command that prompts hangs), and IPython evaluates `{expr}` as Python even inside quotes and expands `$name` outside single quotes. Start a multi-line script with a `%%bash` line (not with `--env`, which prepends Python lines), use non-interactive flags such as `apt-get -y`, and never put untrusted text in a `%%bash` script or a `!` line (even via `{var}` or `$var`): pass it with `--env NAME=VALUE` and run `subprocess.run(["cmd", "--", os.environ["NAME"]])` from Python. For an interactive shell on the VM, a human runs `colab ssh -s <name>`.
- **Never run `colab repl`, `colab ssh` without `-- CMD`, `colab auth`, or `colab drivemount` interactively from an agent** — `repl`, `auth` and `drivemount` expect a TTY and will hang or fail, and bare `colab ssh` with no saved session creates a billable runtime; an interactive shell is for a human. `repl` accepts piped stdin and exits on EOF; `auth`/`drivemount` genuinely require a human at the terminal.

### Machine-readable results (`--json`)
Prefer `--json` whenever you parse the result; set `COLAB_CLI_JSON=1` once to turn it on for every command that supports it (`exec`, `run`).
- **Output**: stdout carries only JSON, one object per line (NDJSON): one `"type": "cell"` record per executed block (stdin or a `.py` file is one block, a notebook one per code cell), then one `"type": "summary"`. Everything else (`[colab]` progress, notebook messages) goes to stderr. Every object has `"schema": "colab.v1"`.
- **Cell record**: `status` (`ok`/`error`), `stdout`, `stderr` (last 64 KiB; `*_truncated` says when cut), `data` (`text/plain`, `application/json`, `text/markdown`), `images` (absolute paths of saved PNG/JPEG files, in `--output-dir` or a temp folder), `error`, `diagnosis`, `duration_s`, `session`.
- **`error.location`**: `{"file", "cell", "line", "code"}` is the failing line in YOUR local file (or notebook cell number + line), already corrected for the `--env` prelude and a stripped shebang. Edit that line; don't count lines in the traceback yourself.
- **`diagnosis`**: `{"code", "summary", "next_actions"}`. Codes: `missing_module`, `cuda_oom`, `no_gpu`, `disk_full`, `file_not_found`, `syntax_error`, `keyboard_interrupt`, `script_exit`, `idle_timeout`, `session_lost`, `unknown`. Each `next_actions` item is `{"cmd", "why"}`: run `cmd` as given (e.g. `colab install -s demo torchvision`) when it is not null; when `cmd` is null, `why` describes a code change instead. `unknown` has no actions: read `error` and fix the code.
- **Exit codes with `--json`**: `exec`: 0 ok, 1 code error, 2 usage error (text on stderr, no JSON), 3 lost session, 4 idle timeout. `run`: the script's own exit code (as `python script.py`), 3 lost session, 4 timeout; its records have `exit_code`, and `session` is null when the runtime was already stopped (use `--keep` to get `-s NAME` commands in `next_actions`).
- **`colab run --json` goes before the script path** (`colab run --json job.py --flag`); anything after the script is the script's own argument.
- **Long cells**: `--json-stream` adds `{"type": "output", "cell_index", "stream", "text"}` events while code runs, before the cell record.

### Ephemeral one-shot jobs (`colab run`)
- `colab run [--gpu T4] [--tpu v6e1] [--keep] [-s NAME] script.py [args...]` = `new` + `exec` + `stop` in one command. It provisions a fresh VM, runs the script with `sys.argv` and `__name__ == "__main__"` set like native `python script.py args`, then tears the VM down (unless `--keep`).
- **Long runs**: `colab run --detach [--gpu T4] [--budget CU] -s NAME script.py [args]` creates the runtime, starts the script as a background job (see `colab job`) and returns at once with the job id; follow it with `colab job status|wait -s NAME`, then `colab stop -s NAME` (the runtime is kept).
- **Exit codes propagate**: an uncaught exception or `sys.exit(N)` in the script makes `colab run` exit non-zero (CPython semantics: `sys.exit()`/`sys.exit(0)` → 0, `sys.exit(N)` → N, `sys.exit("msg")` → 1).
- **Stream separation**: `colab run` writes its own `[colab] ...` chatter to **stderr** and the script's output to **stdout** — so `colab run job.py > out.txt` captures only the script's stdout. (`colab exec` streams the script's stdout/stderr live to your stdout/stderr.)
- Works as a shebang: `#!/usr/bin/env -S colab run --gpu T4` makes a `chmod +x`'d `.py` a self-contained "rent a GPU, run, clean up" script. After editing CLI behavior, reinstall before testing shebangs — they resolve `colab` via `$PATH`, not the editable install.
- A nonexistent script path exits non-zero **before** allocating a VM (no wasted compute).

### Long jobs in the background (`colab job`)
Use this instead of a long `colab exec` (which blocks you and can hit `--timeout`): training, downloads, anything over a few minutes.
- **Start and return at once**: `colab job start -s <name> -f train.py [--name train] [--env K=V] [--secret NAME] [-- script args]`, or a shell command: `colab job start -s <name> -- 'bash command'`. It prints the job id (`--json`: a `"type": "job"` record). The job runs as its own process on the VM, apart from the kernel, so `colab exec`/`inspect` stay usable, and it keeps running after the command exits (verified for 100 minutes with no client connected).
- **Report progress from Python**: `from colab_feedback import report, note` (already on the job's path), then `report(step=i, loss=l, val_acc=a)` and `note("switched lr")`. Outside a job they do nothing. tqdm bars are read automatically.
- **Check without blocking**: `colab job status -s <name> [ID] --json` (default: the newest job) gives `state` (`starting`/`running`/`stopping`/`succeeded`/`failed`/`stopped`), `progress` (`percent`, `n`, `total`, `eta_s`), `metrics.last` and `metrics.trend` (first/last/min/max per metric), `tail` (last lines of output), `silent_s`, `stalled` (true after `--stall-after`, default 600 s, without output) and `diagnosis`. A failed job gets the same diagnosis codes and `next_actions` as `--json` exec results (read from the traceback in its output); a stalled one gets code `stalled`.
- **Block until done**: `colab job wait -s <name> ID [--interval 30] [--timeout S] --json` keeps the runtime alive while waiting and exits with the job's own code (130 if stopped, 4 if `--timeout` passed; the job keeps running).
- **Other**: `colab job tail -s <name> ID [-n 50] [--follow]`, `colab job stop -s <name> ID` (SIGTERM, SIGKILL after 10 s), `colab job list -s <name> [--json]`. Full output: `colab download -s <name> /content/.colab-jobs/ID/output.log out.log`.
- **Loop for agents**: start, then poll `status --json` every few minutes; act on `stalled`, a falling/rising metric trend, or `state: failed` + `diagnosis.next_actions`; `stop` and restart with a fix if needed.

### Look before you run (`colab inspect`)
- `colab inspect -s <name> --json` (or `--vars`, `--gpu`, `--disk`, `--packages`, `--files PATH`) returns one `"type": "inspect"` record: `vars` (name, type, `len`, `shape`, `dtype`, `device`, `nbytes`; never values, so it is safe with secrets in variables), `gpu.devices` (nvidia-smi) and `gpu.torch` memory, `disk`/`memory` in GB, `packages` (`added`/`removed`/`changed` since the first inspect of that runtime) and `files`.
- Use it to see what is already loaded instead of re-running setup, to check GPU memory after a `cuda_oom`, or disk after `disk_full`.
- If `kernel` is `busy` (a long `exec` is running), the kernel sections are `{"skipped": "kernel busy"}`; `--files` still works. Jobs do not make the kernel busy.

### Cost and budgets
- `colab usage --json`: `balance_cu`, `rate_cu_per_hour` (all active runtimes together), `hours_left_at_rate`.
- Every `--json` summary of `exec`/`run`, and `job status|wait --json`, has `cost`: `rate_cu_per_hour`, `balance_cu`, `used_cu_estimate` (rate x duration; high, never low, with several runtimes), `hours_left_at_rate`.
- `--budget CU` on `exec`, `run` (before the script path) and `job start`: the run is stopped once the estimate reaches the budget. `exec`/`run` exit 5 with diagnosis `budget_reached`; a job ends `stopped` with `stop_reason: "budget"` (enforced on the runtime, so it works with no client connected). Set a budget for anything that could run away on a GPU.

### Learn from earlier runs (`colab history`)
- A failed cell's `diagnosis.seen_before` (in `--json` results) is null for a new error, or `{"count", "last_seen", "sessions", "last_fix"}`. `last_fix` is what made the same failure go away last time: `steps` (`{"install": [...]}` from `colab install` or `!pip install`, `{"upload": PATH}`), `code_changed`, `attempts`, `after_s`. Try those steps first before inventing a new fix.
- `colab history errors [--since 7d] [-s NAME] --json`: recurring failures across all sessions (`"type": "error_group"`), each with `count`, `where`, `sessions` and `last_fix`.
- `colab history stats [FILE] --json`: per file `runs`, `success_rate`, `median_duration_s`, `max_duration_s`, `median_cost_cu`, `top_errors` (`"type": "file_stats"`). Check it before a long or expensive run to pick a sensible `--timeout`, `--budget` or `colab job`.
- All of this is read from the local history only; nothing is sent anywhere.

### Automate
- `colab auth -s <name>` — VM-side GCP creds, needed before in-VM GCS/BigQuery calls (interactive; not agent-runnable).
- `colab drivemount -s <name> [--authuser N] [PATH]` — mounts Drive at `/content/drive` by default (interactive; not agent-runnable). The user must approve the printed consent URL in a browser as the same account `colab whoami` shows; a multi-account browser rejects the mount with HTTP 400 unless that account is the one selected on the chooser. The kernel waits 10 minutes, not 2.
- `colab install -s <name> pkg1 pkg2` — installs via `uv pip install --system`, falling back to `pip`. Also `colab install -s <name> -r requirements.txt`.
- **Secrets:** never put a token value on the command line. Use `NAME[=SOURCE]` specs, SOURCE = `env` (default) / `env:VAR` / `prompt` / `hf-cli` / `gh-cli`: `colab secret set -s <name> HF_TOKEN`, or `colab exec -s <name> --secret HF_TOKEN -f job.py`. Values are logged as `***`. `prompt` needs a terminal (not agent-runnable); prefer `env:`.
- `colab hf login -s <name>` / `colab hf download -s <name> REPO_ID [--local-dir DIR]` — Hugging Face on the runtime.
- `colab clone -s <name> owner/repo [DEST]` — clone onto the runtime (`/content/<repo>`); private GitHub repos use `GH_TOKEN` or the local `gh` login.
- `colab forward -s <name> PORT...` / `colab share -s <name> PORT [--via ngrok] [--detach]` — private localhost forward or public URL for a web UI on the runtime. Both run until Ctrl+C and keep the runtime alive; for agents use `colab share --detach` (then `colab share --stop PORT`) or `colab ssh -s <name> -L PORT -- CMD`.

### Inspect & report
- `colab help` (or `colab help <cmd>`) lists/explains commands; the listing is alphabetical.
- `colab sessions` lists server-side assignments and auto-prunes stale local entries. Orphans with no local record show as `[?]`.
- `colab status [-s <name>]` shows hardware, IDLE/BUSY, and last execution.
- `colab log -s <name> [-n 20] [-t TYPE]` shows recent structured events; invaluable when a task fails.
- `colab log -s <name> -o summary.ipynb` exports the session as a notebook (also `.md`, `.txt`, `.jsonl` by suffix).
- `colab url -s <name>` prints a browser URL that attaches the Colab web UI to your existing CLI session instead of allocating a new VM (add `--open` to launch it).
- `colab skill` / `colab readme` print this skill and the README (handy for self-discovery).

## Safety
- **Always `colab stop -s <name>` when done** — idle VMs burn compute units. `colab run` (without `--keep`) self-cleans even if the script errors.
- Local state lives in `~/.config/colab-cli/sessions.json` (settings in `settings.json`, history in `history/*.jsonl`). Don't edit by hand.
- **Isolate parallel/agent runs** with the global `--config <path>` flag to point session state at a scratch file (e.g. `colab --config /tmp/agent.json new -s job`).

## Recovery
- "Session not found" / 404 / 401 on exec: the backend pruned the VM. `colab exec`/`repl` detect this and clean up local state automatically — run `colab sessions` and re-create with `colab new`.
- Execution timeout or wedged kernel: `colab restart-kernel -s <name>` (keeps the VM, resets the kernel), or `colab stop` then `colab new`.
