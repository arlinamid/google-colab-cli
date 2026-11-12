# Colab CLI

A command-line interface for Google Colab. Provision high-performance CPU, GPU, and TPU runtimes, execute local code, manage remote files, and orchestrate automated cloud pipelines — directly from your terminal.

Designed to support seamless developer productivity, headless automation, and AI agent integrations.

> [!IMPORTANT]
> **This is a fork** of Google's [googlecolab/google-colab-cli](https://github.com/googlecolab/google-colab-cli), not the official release. It tracks Google's code and stays as close to it as possible; the differences are:
>
> * **Windows fixes:** Drive mount consent, `colab edit` temp files and `$EDITOR` paths, `colab ssh`/`colab forward` from Git Bash (Windows OpenSSH), ssh `ProxyCommand` quoting.
> * **Any language/code page:** scripts are read like Python source (coding cookie or UTF-8), and redirected output is UTF-8, so non-ASCII text works on every Windows system.
> * **`colab exec`:** `--cells`, `--stop-on-error`, `--check`/`--check-only`, exit code 1 on errors; `--timeout` (exec, run) has no default limit, and when set it interrupts the kernel after that many seconds without output.
> * **New commands:** `colab secret`, `--secret`, `colab ssh -L`/`colab forward`, `colab share` (cloudflared/ngrok), `colab keepalive`, `colab hf`, `colab clone`.
> * **Updates:** `colab update` offers and installs this fork's releases; a newer Google release is only announced, so an update never replaces the fork with Google's package.
>
> Versions are numbered after the Google release they are based on: **0.7.5.N** is Google's 0.7.5 plus this fork's changes. Releases are published on [GitHub](https://github.com/arlinamid/google-colab-cli/releases), not on PyPI (the `google-colab-cli` package there is Google's). See [CHANGELOG.md](CHANGELOG.md).

[Demo](https://github.com/user-attachments/assets/656226a9-af13-4fdb-8eda-d7de747336a2)

> [!NOTE]
> **Platform support:** Linux, macOS, and Windows (PowerShell and cmd.exe). `colab ssh --proxy-mode` reads stdin with a blocking read so it works as an OpenSSH ProxyCommand under Windows OpenSSH.

> [!TIP]
> Looking for in-notebook, interactive agent-assisted coding instead of a terminal workflow? See the [Colab MCP Server](https://github.com/googlecolab/colab-mcp).

---

## Key Features

* **Instant VM Provisioning:** Spin up CPU, GPU (T4, L4, G4, H100, A100), or TPU (v5e1, v6e1) runtimes in seconds.
* **Robust Code Execution:** Run local Python scripts, Jupyter Notebooks (`.ipynb`), or piped `stdin` code; launch interactive REPLs or SSH shells (`colab ssh`).
* **Ephemeral Job Runner (`colab run`):** Provision a fresh VM, execute a local script with forwarded arguments, retrieve output files, and automatically tear down the runtime in a single command.
* **Activity-Based Liveness:** Sessions stay alive as long as the kernel is active, with the Colab backend automatically maintaining liveness.
* **Seamless Workspace Automation:** Mount Google Drive, authenticate Google Cloud Platform (GCP) credentials, and install dependencies with high-performance `uv` package management.
* **State & Log Archival:** Inspect local session states or export interactive history logs to standard Jupyter Notebooks, Markdown, or structured JSONL.

---

## Installation

Install this fork from its [GitHub release](https://github.com/arlinamid/google-colab-cli/releases) using `uv` (recommended) or `pip`. The wheel is pure Python and works on Linux, macOS and Windows:

```bash
# Using uv (recommended)
uv tool install https://github.com/arlinamid/google-colab-cli/releases/download/v0.7.5.2/google_colab_cli-0.7.5.2-py3-none-any.whl

# Using pip
pip install https://github.com/arlinamid/google-colab-cli/releases/download/v0.7.5.2/google_colab_cli-0.7.5.2-py3-none-any.whl
```

`uv tool install google-colab-cli` / `pip install google-colab-cli` install Google's official release from PyPI instead, without this fork's changes.

**Standalone binary (no Python needed):** each release also has a single-file `colab` program: `colab-<version>-windows-x64.exe`, `colab-<version>-linux-x64` and `colab-<version>-macos-x64` (Intel; runs on Apple silicon through Rosetta 2). Download it from the [releases page](https://github.com/arlinamid/google-colab-cli/releases), rename it to `colab` (`colab.exe` on Windows), put it on your `PATH`, and on Linux/macOS run `chmod +x colab`. `colab update` tells you when a new one is out; download it the same way (`colab update --install` only upgrades pip/uv installs). To build one yourself: `uv run --with pyinstaller python packaging/build_binary.py`.

---

## Quick Start

Run a CPU-based VM runtime, execute some code, and clean up:

```bash
# 1. Provision a new session
colab new

# 2. Execute code from stdin
echo 'print("Hello from Google Colab!")' | colab exec

# 3. Stop and release the VM resource
colab stop
```

> [!NOTE]
> When only one session is active, you can omit the `-s, --session` option;
> the CLI automatically knows it.


---

## Command Index

Run `colab <command> --help` to view specific options, defaults, and detailed help.

### Session Management
| Command | Description |
| --- | --- |
| `colab new [-s NAME] [--gpu GPU] [--tpu TPU] [--high-mem]` | Allocate a new CPU, GPU, or TPU VM runtime (optionally high-RAM) |
| `colab sessions` | List all active sessions currently active on the backend |
| `colab status [-s NAME]` | Display hardware, machine shape, status, and local metadata for active sessions |
| `colab restart-kernel [-s NAME]` | Restart the active session's Jupyter kernel |
| `colab stop [-s NAME]` | Terminate a session VM and release its resources |
| `colab stop --orphans [--yes]` | List server-side runtimes with no local session (`[?]` in `colab sessions`) and stop them after confirmation |
| `colab url [-s NAME] [--open]` | Print or open a browser URL connecting to the active session |

### Execution
| Command | Description |
| --- | --- |
| `colab run [--gpu GPU] [--tpu TPU] [--high-mem] [--keep] [--secret SPEC] [--json] SCRIPT [ARGS...]` | Run a local script on a fresh VM, forwarding arguments, then release it |
| `colab exec [-s NAME] [-f FILE] [--output-image PATH] [--secret SPEC] [--cells SPEC] [--stop-on-error] [--check \| --check-only] [--json \| --json-stream]` | Execute Python code from stdin, a local `.py` file, or a `.ipynb` notebook (exits 1 if the code or any cell raised an error) |
| `colab repl [-s NAME] [--output-image PATH]` | Start an interactive Python REPL on the VM (exits cleanly on piped EOF) |
| `colab ssh [-s NAME] [--proxy-mode] [-i KEY] [--gpu GPU] [--tpu TPU] [--high-mem] [-L PORT [-N]] [-- CMD ...]` | Open an SSH shell to the runtime over WebSocket, run a one-shot remote command (`-- CMD`), or act as an OpenSSH `ProxyCommand` bridge for IDE remote-dev |
| `colab job start [-s NAME] [--name N] [--env K=V] [--secret SPEC] (-f SCRIPT [-- ARGS] \| -- CMD)` | Start a long job in the background on the runtime and return at once; Python jobs can report metrics with `colab_feedback.report()` |
| `colab job status\|tail\|wait\|stop\|list [-s NAME] [ID] [--json]` | Follow a job: state, tqdm progress, metrics trend, last output, stall flag and a diagnosis of failures; `wait` keeps the runtime alive and exits with the job's code |
| `colab inspect [-s NAME] [--vars] [--gpu] [--disk] [--packages] [--files PATH] [--json]` | Show the kernel's variables (types and shapes, never values), GPU, disk, memory and packages installed since the first inspect; a busy kernel is not queued behind |
| `colab history errors\|stats [-s NAME] [--since 7d] [--json]` | Learn from earlier runs: recurring failures and what fixed them last time; runs, success rate, usual duration and cost per file (local history only) |
| `colab forward [-s NAME] PORT...` | Forward local ports to the runtime over ssh (private; keeps the runtime alive until Ctrl+C) |
| `colab share [-s NAME] PORT [--via cloudflared\|ngrok] [--detach]` | Share a runtime port at a public URL (keeps the runtime alive until Ctrl+C; `--list`, `--stop`) |

### File Operations
| Command | Description |
| --- | --- |
| `colab ls [-s NAME] [PATH]` | List remote files on the VM |
| `colab upload [-s NAME] LOCAL REMOTE` | Upload a local file to the VM filesystem |
| `colab download [-s NAME] REMOTE LOCAL` | Download a remote file from the VM filesystem |
| `colab rm [-s NAME] PATH` | Delete a remote file on the VM filesystem |
| `colab edit [-s NAME] PATH` | Edit a remote file in-place using your local `$EDITOR` |

### Automation & Utilities
| Command | Description |
| --- | --- |
| `colab auth [-s NAME]` | Authenticate the VM for GCP services (BigQuery, GCS, etc.) |
| `colab drivemount [-s NAME] [--authuser N] [PATH]` | Mount Google Drive on the VM (default: `/content/drive`) |
| `colab install [-s NAME] [-r FILE \| PKG...]` | Install packages on the VM using `uv` (falls back to `pip`) |
| `colab secret set [-s NAME] NAME[=SOURCE]...` | Set secrets in the kernel's environment; values never go on the command line or into the history log |
| `colab hf login [-s NAME] [--token SOURCE]` | Log the runtime in to Hugging Face |
| `colab hf download [-s NAME] REPO_ID [--repo-type T] [--revision R] [--local-dir DIR]` | Download a Hugging Face model, dataset or space onto the runtime |
| `colab clone [-s NAME] REPO [DEST] [--branch B] [--depth N] [--token SOURCE]` | Clone a git repository (GitHub `owner/name` or https URL) onto the runtime |
| `colab keepalive [-s NAME] [--minutes N]` | Keep a runtime alive without running code (e.g. behind a detached share) |
| `colab log [-s NAME] [-n N] [-o FILE]` | View or export session history (`.ipynb`, `.md`, `.txt`, `.jsonl`) |
| `colab usage` | Show account compute-unit usage rate and balance |
| `colab pay` | Open the Colab subscription page to manage compute units |
| `colab version` | Print the installed version of the CLI |
| `colab update [--install]` | Check for a newer release (and optionally upgrade the CLI in place) |

### Secrets, Tunnels and Integrations

Tokens are passed as `NAME[=SOURCE]`, never as values: `SOURCE` is `env` (the local `NAME` variable, the default), `env:VAR`, `prompt` (hidden), `hf-cli` (`hf auth token`) or `gh-cli` (`gh auth token`). Values are written as `***` in the history log.

```bash
colab secret set -s s1 HF_TOKEN WANDB_API_KEY=prompt
colab exec -s s1 --secret OPENAI_API_KEY -f job.py
colab hf login -s s1 && colab hf download -s s1 google/gemma-3-1b-it
colab clone -s s1 me/private-repo            # uses GH_TOKEN or your gh login
colab forward -s s1 7860                     # http://localhost:7860, only for you
colab share -s s1 7860                       # public https://...trycloudflare.com
colab share -s s1 7860 --via ngrok           # needs NGROK_AUTHTOKEN
```

The Colab backend only counts kernel activity as use, so `colab forward`, `colab ssh -L` and `colab share` keep the runtime alive while they run. See [docs/07_secrets_tunnels_integrations.md](docs/07_secrets_tunnels_integrations.md).

### Global Options
* `--auth {oauth2,adc}` — Authentication strategy for the Colab API (default: `adc`).
* `-c, --client-oauth-config PATH` — Path to public OAuth client credentials configuration (default: `~/.colab-cli-oauth-config.json`).
* `--config PATH` — Path to local session metadata storage (default: `~/.config/colab-cli/sessions.json`).
* `--logtostderr` — Direct debug logging output to stderr.

---

## Practical Examples

### Accelerator Training with Checkpoint Retrieval

Provision an A100 GPU, install requirements, run a local training script, retrieve the resulting model weights, and terminate the VM:

```bash
colab new -s trainer --gpu A100
colab install -s trainer torch transformers
colab exec -s trainer -f train.py
colab download -s trainer checkpoints/model.bin ./model.bin
colab stop -s trainer
```

### Workspace Notebook Execution with Drive Integration

Mount Google Drive, run a local notebook against the VM kernel (outputs are written back into `report_output.ipynb`), export a Markdown log of the execution, and clean up:

```bash
colab new -s analysis
colab drivemount -s analysis
colab exec -s analysis -f report.ipynb
colab log -s analysis -o execution_log.md
colab stop -s analysis
```

### Choosing Notebook Cells, and Checking Before Running

`colab exec` runs every code cell top to bottom by default. A cell that raises an error does not stop the run, but the command exits with code 1 and lists the failed cells. You can change this with the following options:

* `--cells SPEC`: run only some cells, in the order given. `SPEC` is a comma-separated list of code-cell numbers (counted from 1, without markdown cells), ranges such as `2-5`, `#@title` values or cell ids. An entry that looks like a number or range is always read as one, so select a cell whose title or id is numeric by its number.
* `--stop-on-error`: stop at the first failing cell.
* `--check`: check the selected cells locally before anything runs: the notebook structure, `--cells`, and Python syntax (IPython `!cmd`, `%magic`, `x = !cmd` and top-level `await` are accepted; `%%` cell magics are skipped). If a check fails, nothing runs.
* `--check-only`: run the same checks and print which cells would run, without a session.

```bash
colab exec -f report.ipynb --check-only --cells "Setup,4-6"
colab exec -s analysis -f report.ipynb --check --cells "Setup,4-6" --stop-on-error
```

---

## Usage Notes

* **Machine shape:** Use `--high-mem` with `colab new`, `colab run`, or `colab ssh` (when auto-creating a runtime) to request a high-RAM machine shape. Requires Colab Pro or Pro+ entitlement for supported accelerators (CPU, T4, A100, etc.). L4 and TPU runtimes ignore this flag because they only offer one shape. Machine shape is shown in `colab sessions` and `colab status`.
* **TTY Requirements:** `colab repl` and interactive `colab ssh` expect a local TTY. When running inside automated scripts or pipelines, pipe stdin to `colab repl` (e.g., `echo "print(1)" | colab repl`) to trigger its non-interactive mode, or run one-shot shell commands with `colab ssh -s NAME -- CMD` (e.g., `colab ssh -s NAME -- ls /content`); pass untrusted values as separate arguments (after `--` where the program accepts it), since a single argument is a remote shell line. `colab ssh` needs a local `ssh` client and key, so where it can't work (no client or key, a tool not on the minimal ssh environment's `PATH`, or the runtime's concurrent SSH connections in use), pipe the commands to `colab exec` with IPython's `!` prefix (e.g., `echo '!ls /content' | colab exec`); each `!` line is its own shell and IPython expands `{expr}` and `$name` in it, so start a multi-line script with `%%bash` and keep untrusted text out of `!` lines.
* **Drive mount and multiple Google accounts:** `colab drivemount` prints a consent URL (and opens it in the browser when it can) and waits up to 10 minutes. On the account chooser, select the same account the CLI authenticated as (`colab whoami` / the `login_hint` in the URL). A browser that is signed into several accounts otherwise attributes the Drive session to the active account, and Colab rejects the mount with HTTP 400. `--authuser N` appends that index to the consent URL when the chooser keeps opening the wrong account.
* **Git Bash on Windows:** Git Bash rewrites arguments that look like POSIX paths, so `/content/x` reaches Windows programs as `C:/Program Files/Git/content/x`. `colab ls`, `rm`, `upload`, `download` and `edit` turn such remote paths back. For other commands that take remote paths, such as `colab ssh -s NAME -- ls /content`, run `MSYS_NO_PATHCONV=1 colab ...` or write `//content`.
* **Results for AI agents (`--json`):** `colab exec --json` and `colab run --json` print one JSON record per cell and a summary on stdout (everything else goes to stderr). A failed cell's record names the failing line in your local file, a diagnosis code (e.g. `missing_module`, `cuda_oom`, `file_not_found`) and the `colab` commands to try next:

  ```bash
  $ echo 'import torchvision' | colab exec -s demo --json
  {"schema": "colab.v1", "type": "cell", "status": "error", "error": {"ename": "ModuleNotFoundError", "location": {"file": "<stdin>", "line": 1, ...}, ...}, "diagnosis": {"code": "missing_module", "next_actions": [{"cmd": "colab install -s demo torchvision", "why": "install the missing package"}, ...]}, ...}
  {"schema": "colab.v1", "type": "summary", "status": "error", "cells_run": 1, "cells_failed": 1, "exit_code": 1, ...}
  ```

  Exit codes: 1 code error, 3 lost session, 4 timeout, 5 `--budget` reached (`run` keeps the script's own code). A failure seen before carries `diagnosis.seen_before` with what fixed it last time (from the local history, see `colab history`). The summary's `cost` block estimates the compute units used; `--budget CU` (also on `colab job start`) stops a run before it costs more, and `colab usage --json` gives balance and rate. `COLAB_CLI_JSON=1` turns `--json` on everywhere; `--json-stream` also streams output events. See [docs/08_agent_feedback.md](docs/08_agent_feedback.md).
* **Transparent Code Execution:** When calling `colab exec -f file.py`, the CLI reads the file locally and transmits its content to the remote kernel. You do not need to manually upload files before execution.
* **Storage & State Paths:** Session tokens and metadata are stored at `~/.config/colab-cli/sessions.json`. Global CLI settings are located at `~/.config/colab-cli/settings.json`. These can be customized or isolated via the global `--config` flag.

### Ephemeral Accelerator Jobs

Use `colab run` to run a local script on dedicated hardware without manual session lifecycle management. The CLI handles provisioning, script execution, and immediate VM teardown automatically:

```bash
# Run train.py on a T4 GPU and release the VM on completion
colab run --gpu T4 train.py
```

### Shebang Execution Support

To execute a local file directly on a remote accelerator, place the `colab run` interpreter in the shebang line:

```python
#!/usr/bin/env -S colab run --gpu L4 --keep
import torch

print("L4 GPU Available:", torch.cuda.is_available())
print("Device Name:", torch.cuda.get_device_name(0))
```

Make the script executable (`chmod +x script.py`) and run it: `./script.py`. The `--keep` option tells the CLI to preserve the session VM on completion so you can re-execute or inspect logs.

---

## Deep Dive Documentation

For comprehensive architectural overviews and deep-dives into specific CLI sub-systems, refer to the detailed documentation:

* [Session Management Architecture](docs/01_session_management.md)
* [Interactive & Non-Interactive Execution Design](docs/02_execution_and_interactive.md)
* [File Management & Jupyter Contents API](docs/03_file_management.md)
* [Authentication Providers & VM Automation](docs/04_automation_and_utility.md)
* [Ephemeral Job Runner Design](docs/05_run_command.md)

To view interactive walkthroughs of eleven real-world automated scenarios, check out the [Demo Walkthroughs](docs/demos.md).

---

## Contributing

Feedback and contributions are welcome! Please read [`CONTRIBUTING.md`](./CONTRIBUTING.md) for details.

---

## Legal & Privacy

See Colab's [Terms of Service](https://research.google.com/colaboratory/tos_v6.html) and the [Google Privacy Policy](https://policies.google.com/privacy), which apply to usage of this CLI.
