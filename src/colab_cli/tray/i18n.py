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

"""The tray's texts in English and Hungarian, and number/time formatting.

The CLI itself stays English; only the tray panel follows the system
language (or the language chosen in its settings).
"""

from typing import Optional

STRINGS = {
    "en": {
        "connected": "Connected",
        "refresh_failed": "Refresh failed",
        "stale": "Stale data",
        "active_sessions": "ACTIVE SESSIONS",
        "n_active": "{n} active",
        "no_sessions": "No active runtimes.",
        "state.running": "Running",
        "state.stalled": "No output",
        "state.done": "Finished",
        "state.failed": "Failed",
        "state.stopped": "Stopped",
        "state.idle": "Idle",
        "state.service": "Service",
        "state.starting": "Starting",
        "settings.autostart": "Start with the computer",
        "settings.confirm_start": "Ask before a profile starts",
        "settings.confirm_start_help": "Show the hardware and the current rate before a profile starts a runtime.",
        "settings.low_balance": "Low balance warning under",
        "settings.low_balance_help": "Warn when the compute units last less than this at the current rate (0: never).",
        "settings.idle_warn": "Idle runtime warning after",
        "settings.idle_warn_help": "Warn when a runtime has been idle this long; Colab reclaims idle runtimes at about 90 minutes.",
        "settings.terminal": "Terminal for Log and SSH",
        "settings.terminal_auto": "Automatic",
        "settings.terminal_help": "A command with {cmd} where the command goes, e.g. wt -w new {cmd} or alacritty -e {cmd}. Automatic: the system's terminal.",
        "settings.hours_unit": "h",
        "settings.minutes_unit": "min",
        "settings.autostart_help": "Start the tray when you sign in (per user; Windows, macOS and Linux).",
        "settings.autostart_failed": "Could not change the start with the computer: {error}",
        "phase.launching": "Launching…",
        "phase.allocating": "Getting a runtime (1–3 min)…",
        "phase.setup": "Running the script…",
        "phase.waiting_ready": "Waiting for the service…",
        "elapsed": "{time} elapsed",
        "profile_starting": "{name} starting…",
        "profile_running": "{name} running",
        "notify.starting": "{name} is starting: getting a runtime takes 1–3 minutes; the panel shows how far it is.",
        "notify.already_starting": "{name} is already starting or running; wait for it, or stop it first.",
        "orphan": "not in the local session list",
        "orphan_hint": "stop it with: colab stop --orphans",
        "forwarding": "forward: {ports}",
        "not_answering": "{port} (not answering)",
        "epoch": "{cur} / {total} epoch",
        "cell": "cell {cur}/{total} · {label}",
        "eta": "about {time} left",
        "last_output": "last output: {age}",
        "no_output_yet": "no output yet",
        "log": "Log",
        "ssh": "SSH",
        "open": "Open",
        "stop": "Stop",
        "compute_units": "Compute Units",
        "balance": "Balance:",
        "usage": "Usage:",
        "per_hour": "{rate} CU / hour",
        "hours_left": "Estimated time left:",
        "hours": "{h} hours",
        "no_estimate": "no estimate (rate 0)",
        "start_profile": "Start profile",
        "no_profiles": "No profiles yet (colab profile add)",
        "create_profile": "Create a profile…",
        "edit_profiles": "Edit profiles…",
        "refresh_now": "Refresh now",
        "settings": "Settings",
        "updated": "Updated: {age}",
        "updated_now": "Updated: now",
        "quit": "Quit",
        "stop.title": "Stop {name}?",
        "stop.job": "Stop only the job ({job}). The Colab runtime keeps running and using compute units.",
        "stop.runtime": "Stop the whole Colab runtime ({name}, {hw}). Unsaved files on it are lost.",
        "stop.service": "The forward / service started by the tray is closed too.",
        "cancel": "Cancel",
        "start.title": "Start profile {name}?",
        "start.body": "{mode} on {hw}: {script}",
        "start.rate": "Current account usage: {rate} CU / hour.",
        "start.reuse": "It reuses the runtime '{session}' if it is running.",
        "mode.job": "Background job",
        "mode.kernel": "Kernel / service",
        "start": "Start",
        "notify.job_done": "Job finished on {session}",
        "notify.job_failed": "Job failed on {session}",
        "notify.job_stopped": "Job stopped on {session}",
        "notify.budget": "Budget reached: job stopped on {session}",
        "notify.session_gone": "Runtime {session} is gone",
        "notify.low_balance": "Low compute-unit balance",
        "notify.stopping": "Stopping {session} (stop_when_done)",
        "notify.profile_failed": "Profile {name} ended with exit code {code}; see {log}",
        "settings.title": "Colab CLI tray settings",
        "settings.interval": "Refresh every (seconds)",
        "settings.notifications": "Notifications",
        "settings.language": "Language",
        "settings.auto": "System",
        "save": "Save",
        "tooltip.prefix": "Colab CLI",
    },
    "hu": {
        "connected": "Kapcsolódva",
        "refresh_failed": "Frissítési hiba",
        "stale": "Elavult adatok",
        "active_sessions": "AKTÍV MUNKAMENET",
        "n_active": "{n} aktív",
        "no_sessions": "Nincs aktív Colab-gép.",
        "state.running": "Fut",
        "state.stalled": "Nincs kimenet",
        "state.done": "Kész",
        "state.failed": "Hiba",
        "state.stopped": "Leállítva",
        "state.idle": "Tétlen",
        "state.service": "Szolgáltatás",
        "state.starting": "Indul",
        "settings.autostart": "Indítás a számítógéppel",
        "settings.confirm_start": "Kérdezzen profilindítás előtt",
        "settings.confirm_start_help": "A profil indítása előtt mutassa a hardvert és az aktuális óradíjat.",
        "settings.low_balance": "Kevés kredit figyelmeztetés ez alatt",
        "settings.low_balance_help": "Figyelmeztet, ha a Compute Unit az aktuális fogyasztással ennél rövidebb ideig elég (0: soha).",
        "settings.idle_warn": "Tétlen gép figyelmeztetés ennyi után",
        "settings.idle_warn_help": "Figyelmeztet, ha egy gép ennyi ideje tétlen; a Colab kb. 90 perc tétlenség után visszaveszi.",
        "settings.terminal": "Terminál a Naplóhoz és SSH-hoz",
        "settings.terminal_auto": "Automatikus",
        "settings.terminal_help": "Parancs, benne {cmd} a futtatandó parancs helyén, pl. wt -w new {cmd} vagy alacritty -e {cmd}. Automatikus: a rendszer terminálja.",
        "settings.hours_unit": "óra",
        "settings.minutes_unit": "perc",
        "settings.autostart_help": "A tray elindul, amikor bejelentkezel (csak a te fiókodban; Windows, macOS és Linux).",
        "settings.autostart_failed": "Nem sikerült beállítani az indítást a számítógéppel: {error}",
        "phase.launching": "Indítás…",
        "phase.allocating": "Gép foglalása (1–3 perc)…",
        "phase.setup": "A szkript fut…",
        "phase.waiting_ready": "Várakozás a szolgáltatásra…",
        "elapsed": "{time} telt el",
        "profile_starting": "{name} indul…",
        "profile_running": "{name} fut",
        "notify.starting": "{name} indul: a gép elkészítése 1–3 perc, a panel mutatja, hol tart.",
        "notify.already_starting": "{name} már indul vagy fut; várd meg, vagy előbb állítsd le.",
        "orphan": "nincs a helyi munkamenetek között",
        "orphan_hint": "leállítás: colab stop --orphans",
        "forwarding": "továbbítás: {ports}",
        "not_answering": "{port} (nem válaszol)",
        "epoch": "{cur} / {total} epoch",
        "cell": "cella {cur}/{total} · {label}",
        "eta": "kb. {time} hátra",
        "last_output": "utolsó kimenet: {age}",
        "no_output_yet": "még nincs kimenet",
        "log": "Napló",
        "ssh": "SSH",
        "open": "Megnyitás",
        "stop": "Leállítás",
        "compute_units": "Compute Unit",
        "balance": "Egyenleg:",
        "usage": "Fogyasztás:",
        "per_hour": "{rate} CU / óra",
        "hours_left": "Becsült hátralévő idő:",
        "hours": "{h} óra",
        "no_estimate": "nincs becslés (0 fogyasztás)",
        "start_profile": "Profil indítása",
        "no_profiles": "Még nincs profil (colab profile add)",
        "create_profile": "Profil létrehozása…",
        "edit_profiles": "Profilok szerkesztése…",
        "refresh_now": "Frissítés most",
        "settings": "Beállítások",
        "updated": "Frissítve: {age}",
        "updated_now": "Frissítve: most",
        "quit": "Kilépés",
        "stop.title": "Leállítod: {name}?",
        "stop.job": "Csak a feladat ({job}) áll le. A Colab-gép tovább fut, és fogyasztja a kreditet.",
        "stop.runtime": "Az egész Colab-gép ({name}, {hw}) leáll. A rajta lévő, nem mentett fájlok elvesznek.",
        "stop.service": "A tray által indított forward / szolgáltatás is bezárul.",
        "cancel": "Mégse",
        "start.title": "Elindítod a(z) {name} profilt?",
        "start.body": "{mode}, {hw}: {script}",
        "start.rate": "A fiók jelenlegi fogyasztása: {rate} CU / óra.",
        "start.reuse": "Ha a(z) '{session}' gép fut, azt használja újra.",
        "mode.job": "Háttérfeladat",
        "mode.kernel": "Kernel / szolgáltatás",
        "start": "Indítás",
        "notify.job_done": "Kész a feladat: {session}",
        "notify.job_failed": "Hibával leállt a feladat: {session}",
        "notify.job_stopped": "Leállt a feladat: {session}",
        "notify.budget": "Elérte a keretet, leállt a feladat: {session}",
        "notify.session_gone": "Megszűnt a gép: {session}",
        "notify.low_balance": "Kevés a Compute Unit",
        "notify.stopping": "Leállítom: {session} (stop_when_done)",
        "notify.profile_failed": "A(z) {name} profil {code} kóddal ért véget; napló: {log}",
        "settings.title": "Colab CLI tálca – beállítások",
        "settings.interval": "Frissítés (másodperc)",
        "settings.notifications": "Értesítések",
        "settings.language": "Nyelv",
        "settings.auto": "Rendszer",
        "save": "Mentés",
        "tooltip.prefix": "Colab CLI",
    },
}


def pick_language(setting: Optional[str], locale_name: str) -> str:
    """``setting`` ("hu", "en", or "auto"/None for the system's) to a language."""
    if setting in STRINGS:
        return setting
    return "hu" if (locale_name or "").lower().startswith("hu") else "en"


def t(lang: str, key: str, **kw) -> str:
    text = STRINGS.get(lang, STRINGS["en"]).get(key) or STRINGS["en"].get(key, key)
    return text.format(**kw) if kw else text


def fmt_number(value: float, lang: str, digits: int = 1) -> str:
    text = f"{value:.{digits}f}"
    return text.replace(".", ",") if lang == "hu" else text


def fmt_duration(seconds: Optional[float], lang: str) -> str:
    """12 perc / 1 óra 5 perc / 40 mp (hu), 12 min / 1 h 5 min / 40 s (en)."""
    if seconds is None:
        return "?"
    seconds = max(int(seconds), 0)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    units = ("óra", "perc", "mp") if lang == "hu" else ("h", "min", "s")
    if h:
        return f"{h} {units[0]}" + (f" {m} {units[1]}" if m else "")
    if m:
        return f"{m} {units[1]}"
    return f"{s} {units[2]}"


def fmt_age(seconds: Optional[float], lang: str) -> str:
    """How long ago: "3 perce" / "3 min ago"."""
    if seconds is None:
        return "?"
    d = fmt_duration(seconds, lang)
    if lang == "hu":
        head, _, unit = d.rpartition(" ")
        return f"{head} " + {"óra": "órája", "perc": "perce", "mp": "mp-e"}[unit]
    return f"{d} ago"


_HARDWARE = {"T4": "NVIDIA T4", "L4": "NVIDIA L4", "A100": "NVIDIA A100", "H100": "NVIDIA H100", "G4": "NVIDIA G4"}


def hardware_label(hw: str) -> str:
    if hw in _HARDWARE:
        return _HARDWARE[hw]
    if hw.startswith("V") and hw[1:2].isdigit():
        return f"TPU {hw}"
    return hw


STRINGS["en"].update({
    "reason.stale": "stale data (last refresh {minutes} min ago)",
    "reason.refresh_failed": "refresh failed: {error}",
    "reason.job_failed": "job failed: {names}",
    "reason.stalled": "no output: {names}",
    "reason.low_balance": "low balance (about {hours} h left)",
    "reason.idle_long": "idle for long, may be reclaimed: {names}",
    "summary.none": "no runtimes",
    "summary.n": "{n} runtime(s)",
    "level.none": "No runtimes",
    "level.idle": "Idle",
    "level.running": "Running",
    "level.warning": "Warning",
    "level.error": "Error",
    "level.stale": "Stale data",
})
STRINGS["hu"].update({
    "reason.stale": "elavult adatok (utolsó frissítés: {minutes} perce)",
    "reason.refresh_failed": "frissítési hiba: {error}",
    "reason.job_failed": "hibás feladat: {names}",
    "reason.stalled": "nincs kimenet: {names}",
    "reason.low_balance": "kevés kredit (kb. {hours} óra)",
    "reason.idle_long": "régóta tétlen, a Colab visszaveheti: {names}",
    "summary.none": "nincs gép",
    "summary.n": "{n} gép",
    "level.none": "Nincs gép",
    "level.idle": "Tétlen",
    "level.running": "Fut",
    "level.warning": "Figyelmeztetés",
    "level.error": "Hiba",
    "level.stale": "Elavult adatok",
})


# The profile editor and the log window (docs/09).
STRINGS["en"].update({
    "editor.title": "Colab CLI profiles",
    "editor.profiles": "Profiles",
    "editor.new": "New",
    "editor.delete": "Delete",
    "editor.save": "Save",
    "editor.run": "Save and start",
    "editor.as_text": "Edit as text…",
    "editor.name": "Name",
    "editor.saved": "Saved {name} in {path}.",
    "editor.deleted": "Deleted {name}.",
    "editor.bad_name": "Use letters, digits, '-', '_' or '.' in the name.",
    "editor.exists": "A profile named {name} already exists.",
    "editor.confirm_delete": "Delete the profile {name}? Its runtimes are not touched.",
    "editor.unsaved": "The profile has unsaved changes. Save them?",
    "editor.help_title": "Help",
    "editor.intro": "A profile is a saved way to run something. Job: a script or notebook as a background job, with progress. Kernel: a server started in the kernel, then forwarded to this computer. Click a field for help.",
    "mode.job.short": "Background job",
    "mode.kernel.short": "Kernel / service",
    "browse": "Browse…",
    "field.description": "Description",
    "field.mode": "Mode",
    "field.script": "Script / notebook",
    "field.args": "Arguments",
    "field.session": "Session name",
    "field.hardware": "Hardware",
    "field.high_mem": "High RAM",
    "field.budget": "Budget (CU)",
    "field.timeout": "Idle timeout (s)",
    "field.secrets": "Secrets",
    "field.env": "Environment",
    "field.vars": "Python variables",
    "field.forward": "Forward ports",
    "field.ssh_key": "SSH key",
    "field.open": "Open URL",
    "field.ready": "Ready URL",
    "field.stop_when_done": "Stop the runtime when done",
    "help.name": "The profile's name, used in `colab profile run NAME` and the tray menu.",
    "help.description": "One line shown in the profile list and in the start confirmation.",
    "help.mode": "Background job: the script runs on the runtime as its own process (`colab job`); you can close everything, and the tray shows its progress. Kernel / service: the script runs in the notebook kernel (`colab exec`), e.g. to start a web server and return; then the ports below are forwarded.",
    "help.script": "The local .py file or .ipynb notebook to run. A relative path is relative to profiles.toml. A notebook's code cells run in order.",
    "help.args": "Arguments for the script (sys.argv), separated by spaces; quote an argument with spaces: --name \"my run\".",
    "help.session": "Leave empty for a new runtime each time (name-MMDD-HHMMSS). With a fixed name, a runtime of that name that is still running is reused instead of allocating a new one.",
    "help.hardware": "CPU, a GPU or a TPU. A GPU or TPU costs more compute units per hour; a type your account cannot get fails when the runtime is created.",
    "help.high_mem": "A high-RAM machine shape (Colab Pro / Pro+). Ignored for L4 and TPUs.",
    "help.budget": "Stop the job once it has used about this many compute units, estimated from the account's hourly rate when it starts. Enforced on the runtime itself. Empty: no limit.",
    "help.timeout": "Interrupt the kernel code after this many seconds without output. Empty: no limit (a server script usually returns once it started).",
    "help.secrets": "Names of secrets set with `colab secret set`, separated by commas (e.g. HF_TOKEN). They reach the code as environment variables; never type token values here.",
    "help.env": "Environment variables, one KEY=VALUE per line. Not for tokens: use Secrets.",
    "help.vars": "Python globals set before the script runs, as a JSON object, e.g. {\"CONFIG\": {\"access\": \"forward\"}}. Useful for notebooks that read settings from variables (#@param).",
    "help.forward": "Runtime ports to forward to this computer after the script ran, separated by commas: 8188, or LOCAL:REMOTE (8080:80). Needs a passphrase-less SSH key. The forward keeps the runtime alive while it runs.",
    "help.ssh_key": "The private key for the forward. Empty: ~/.ssh/id_ed25519 or id_ecdsa. It must have no passphrase.",
    "help.open": "Opened in the browser once the service answers, e.g. http://127.0.0.1:8188. The tray's Open button uses it too.",
    "help.ready": "The address that tells the service is up (any answer below HTTP 500). Empty: the Open URL.",
    "help.stop_when_done": "Job: the tray stops the runtime once the job has ended (only while the tray runs). Kernel: the runtime stops when the run ends (the forward is closed). Off: the runtime keeps running and using compute units until you stop it.",
    "log.title": "Log: {name}",
    "log.follow": "Follow",
    "log.refresh": "Refresh",
    "log.copy": "Copy",
    "log.help_job": "The output of the job {job} on the runtime: its last lines, then new ones as they come while Follow is on. The full log: colab download -s {name} /content/.colab-jobs/{job}/output.log",
    "log.help_session": "What this CLI ran on {name}: commands, code and outputs from the local history (newest last). A server's own log is on the runtime, e.g. colab ssh -s {name} -- tail -f /content/server.log",
    "log.ended": "[ended, exit code {code}]",
})
STRINGS["hu"].update({
    "editor.title": "Colab CLI profilok",
    "editor.profiles": "Profilok",
    "editor.new": "Új",
    "editor.delete": "Törlés",
    "editor.save": "Mentés",
    "editor.run": "Mentés és indítás",
    "editor.as_text": "Szerkesztés szövegként…",
    "editor.name": "Név",
    "editor.saved": "Mentve: {name} ({path}).",
    "editor.deleted": "Törölve: {name}.",
    "editor.bad_name": "A névben betű, szám, '-', '_' vagy '.' lehet.",
    "editor.exists": "Már van {name} nevű profil.",
    "editor.confirm_delete": "Törlöd a(z) {name} profilt? A gépeit nem érinti.",
    "editor.unsaved": "A profilban nem mentett változás van. Mented?",
    "editor.help_title": "Súgó",
    "editor.intro": "A profil egy elmentett futtatási mód. Háttérfeladat: script vagy notebook a gépen, haladáskövetéssel. Kernel: a kernelben indított szolgáltatás, amit a gépedre továbbítunk. Kattints egy mezőre a súgóért.",
    "mode.job.short": "Háttérfeladat",
    "mode.kernel.short": "Kernel / szolgáltatás",
    "browse": "Tallózás…",
    "field.description": "Leírás",
    "field.mode": "Mód",
    "field.script": "Script / notebook",
    "field.args": "Argumentumok",
    "field.session": "Munkamenet neve",
    "field.hardware": "Hardver",
    "field.high_mem": "Sok memória",
    "field.budget": "Keret (CU)",
    "field.timeout": "Tétlenségi idő (mp)",
    "field.secrets": "Titkok",
    "field.env": "Környezeti változók",
    "field.vars": "Python-változók",
    "field.forward": "Továbbított portok",
    "field.ssh_key": "SSH-kulcs",
    "field.open": "Megnyitandó cím",
    "field.ready": "Készenléti cím",
    "field.stop_when_done": "A végén álljon le a gép",
    "help.name": "A profil neve: ezzel indul a `colab profile run NÉV` parancs, és ez látszik a tray menüjében.",
    "help.description": "Egy sor, ami a profillistában és az indítás előtti megerősítésben látszik.",
    "help.mode": "Háttérfeladat: a script a gépen külön folyamatként fut (`colab job`); mindent bezárhatsz, a tray mutatja a haladást. Kernel / szolgáltatás: a script a notebook kernelében fut (`colab exec`), például elindít egy webszervert és visszatér; utána az alábbi portokat továbbítjuk.",
    "help.script": "A futtatandó helyi .py fájl vagy .ipynb notebook. Relatív útvonal a profiles.toml-hoz képest értendő. A notebook kódcellái sorban futnak.",
    "help.args": "A script argumentumai (sys.argv), szóközzel elválasztva; a szóközt tartalmazót tedd idézőjelbe: --name \"my run\".",
    "help.session": "Üresen minden indítás új gépet kap (név-HHNN-ÓÓPPMM). Fix névvel, ha már fut ilyen nevű gép, azt használja újra, nem foglal újat.",
    "help.hardware": "CPU, GPU vagy TPU. A GPU és a TPU óránként több Compute Unitot fogyaszt; ha a fiókod nem kaphat ilyet, a gép létrehozása sikertelen lesz.",
    "help.high_mem": "Sok memóriás gép (Colab Pro / Pro+). L4-nél és TPU-nál nem számít.",
    "help.budget": "A feladat leáll, ha kb. ennyi Compute Unitot elhasznált (a fiók indításkori óradíja alapján becsülve). Magán a gépen érvényesül. Üresen: nincs korlát.",
    "help.timeout": "Ennyi másodperc kimenet nélkül után megszakítja a kernelben futó kódot. Üresen: nincs korlát (egy szerverindító script általában visszatér, ha elindult).",
    "help.secrets": "A `colab secret set`-tel megadott titkok nevei, vesszővel elválasztva (pl. HF_TOKEN). Környezeti változóként érik el a kódot; tokent soha ne írj ide.",
    "help.env": "Környezeti változók, soronként egy KULCS=ÉRTÉK. Tokenhez ne ezt használd, hanem a Titkokat.",
    "help.vars": "A script előtt beállított Python-globálisok JSON-objektumként, pl. {\"CONFIG\": {\"access\": \"forward\"}}. Olyan notebookokhoz jó, amelyek változókból olvassák a beállításaikat (#@param).",
    "help.forward": "A script után a gépedre továbbítandó portok, vesszővel elválasztva: 8188, vagy HELYI:TÁVOLI (8080:80). Jelszó nélküli SSH-kulcs kell hozzá. Amíg a továbbítás fut, ébren tartja a gépet.",
    "help.ssh_key": "A továbbításhoz használt privát kulcs. Üresen: ~/.ssh/id_ed25519 vagy id_ecdsa. Nem lehet jelszava.",
    "help.open": "Ezt nyitja meg a böngészőben, ha a szolgáltatás válaszol, pl. http://127.0.0.1:8188. A tray Megnyitás gombja is ezt használja.",
    "help.ready": "Erről a címről derül ki, hogy a szolgáltatás elindult (bármilyen válasz HTTP 500 alatt). Üresen: a megnyitandó cím.",
    "help.stop_when_done": "Háttérfeladat: a feladat végén a tray leállítja a gépet (csak ha a tray fut). Kernel: a futtatás végén (a továbbítás bezárásakor) áll le a gép. Kikapcsolva a gép tovább fut és fogyaszt, amíg le nem állítod.",
    "log.title": "Napló: {name}",
    "log.follow": "Követés",
    "log.refresh": "Frissítés",
    "log.copy": "Másolás",
    "log.help_job": "A(z) {job} feladat kimenete a gépen: az utolsó sorai, Követés mellett az újak is, ahogy jönnek. A teljes napló: colab download -s {name} /content/.colab-jobs/{job}/output.log",
    "log.help_session": "Amit ez a CLI a(z) {name} gépen futtatott: parancsok, kód és kimenetek a helyi előzményekből (a legújabb alul). Egy szerver saját naplója a gépen van, pl. colab ssh -s {name} -- tail -f /content/server.log",
    "log.ended": "[vége, kilépési kód: {code}]",
})
