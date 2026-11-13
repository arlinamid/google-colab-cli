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
