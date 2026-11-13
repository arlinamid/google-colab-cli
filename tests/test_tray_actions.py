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

"""Tray texts and button actions, without Qt (docs/09)."""

import pytest

from colab_cli.profiles import Profile
from colab_cli.tray import actions as a
from colab_cli.tray.i18n import fmt_age, fmt_duration, fmt_number, hardware_label, pick_language, t


def _card(**kw):
    card = {"name": "train-t4", "hardware": "T4", "local": True, "profile": None, "state": "running",
            "job_id": "train-1", "can_stop_job": True, "percent": 45.0,
            "metric": {"name": "loss", "value": 0.31, "trend": "down"}, "epoch": (2, 5), "cell": None,
            "eta_s": 720, "last_output_age_s": 4.0, "diagnosis": None}
    card.update(kw)
    return card


def test_language_and_formatting():
    assert pick_language("auto", "hu_HU") == "hu" and pick_language(None, "en_US") == "en"
    assert pick_language("en", "hu_HU") == "en"
    assert fmt_number(87.0, "hu") == "87,0" and fmt_number(1.9, "en") == "1.9"
    assert fmt_duration(720, "hu") == "12 perc" and fmt_duration(3900, "en") == "1 h 5 min"
    assert fmt_age(180, "hu") == "3 perce" and fmt_age(3900, "hu") == "1 óra 5 perce"
    assert fmt_age(40, "hu") == "40 mp-e" and fmt_age(180, "en") == "3 min ago"
    assert hardware_label("T4") == "NVIDIA T4" and hardware_label("V5E1") == "TPU V5E1"
    assert hardware_label("CPU") == "CPU"
    assert set(a.__dict__)  # module imports without Qt


def test_every_key_has_both_languages():
    from colab_cli.tray.i18n import STRINGS

    assert set(STRINGS["en"]) == set(STRINGS["hu"])


def test_card_lines_as_in_the_design():
    lines = a.card_lines(_card(), "hu")
    assert lines["subtitle"] == "NVIDIA T4 · Fut"
    assert lines["metric"] == "loss 0,31 ↓"
    assert lines["detail"] == "2 / 5 epoch · kb. 12 perc hátra"


def test_card_without_progress_names_the_last_output():
    lines = a.card_lines(_card(percent=None, epoch=None, eta_s=None, metric=None, last_output_age_s=180), "hu")
    assert lines["detail"] == "utolsó kimenet: 3 perce"
    assert lines["metric"] is None


def test_orphan_card_says_so():
    lines = a.card_lines(_card(local=False, name="?", state="idle", job_id=None), "en")
    assert "not in the local session list" in lines["subtitle"]
    assert lines["detail"].endswith("stop it with: colab stop --orphans")


def test_stop_options_say_what_stops():
    opts = dict(a.stop_options(_card(), has_service=False, lang="hu"))
    assert set(opts) == {"job", "runtime"}
    assert "Csak a feladat (train-1)" in opts["job"] and "tovább fut" in opts["job"]
    assert "Az egész Colab-gép (train-t4, NVIDIA T4)" in opts["runtime"]

    no_job = dict(a.stop_options(_card(can_stop_job=False), has_service=True, lang="en"))
    assert set(no_job) == {"runtime"} and "forward" in no_job["runtime"]

    assert a.stop_argv(_card(), "job") == ["job", "stop", "-s", "train-t4", "train-1"]
    assert a.stop_argv(_card(), "runtime") == ["stop", "-s", "train-t4"]


def test_log_and_ssh_commands():
    assert a.log_argv(_card()) == ["job", "tail", "-s", "train-t4", "train-1", "--follow"]
    assert a.log_argv(_card(job_id=None)) == ["log", "-s", "train-t4"]
    assert a.ssh_argv(_card()) == ["ssh", "-s", "train-t4"]


def test_profile_confirm_names_cost_and_reuse():
    p = Profile(script="srv.py", mode="kernel", gpu="T4", session="ui", description="ComfyUI")
    title, body = a.profile_confirm("ui", p, 1.9, "hu")
    assert title == "Elindítod a(z) ui profilt?"
    assert "ComfyUI" in body and "NVIDIA T4" in body and "1,9 CU / óra" in body and "'ui'" in body


def test_status_text_in_words():
    icon = {"level": "warning", "reasons": [{"code": "low_balance", "hours": 0.5},
                                             {"code": "stalled", "names": ["a", "b"]}]}
    text = a.status_text(icon, [_card()], "hu")
    assert text.startswith("Colab CLI – Figyelmeztetés")
    assert "train-t4 fut 45%" in text and "kevés kredit (kb. 0.5 óra)" in text and "a, b" in text
    stale = a.status_text({"level": "stale", "reasons": [{"code": "stale", "minutes": 4}]}, [], "en")
    assert "Stale data" in stale and "4 min ago" in stale


def test_notification_texts():
    assert a.notification({"kind": "job_failed", "session": "s", "code": "cuda_oom"}, "en") == \
        "Job failed on s (cuda_oom)"
    assert t("hu", "notify.low_balance") == "Kevés a Compute Unit"


@pytest.mark.parametrize("system, found, first", [
    ("win32", {}, "colab"),
    ("darwin", {}, "osascript"),
    ("linux", {"gnome-terminal": "/usr/bin/gnome-terminal"}, "/usr/bin/gnome-terminal"),
])
def test_terminal_command(system, found, first):
    cmd, kw = a.terminal_command(["colab", "ssh", "-s", "my box"], system=system, which=found.get)
    assert cmd[0] == first
    if system == "darwin":
        assert "'my box'" in cmd[2]
    if system == "linux":
        assert cmd[1] == "--" and cmd[-1] == "my box"


def test_terminal_command_without_a_terminal():
    with pytest.raises(FileNotFoundError):
        a.terminal_command(["colab"], system="linux", which=lambda name: None)


def test_forward_line():
    card = _card(state="service", percent=None, epoch=None, eta_s=None, metric=None, job_id=None,
                 forwards=[{"local_port": 8188, "listening": True}, {"local_port": 7860, "listening": False}])
    assert a.card_lines(card, "hu")["detail"] == "továbbítás: 8188, 7860 (nem válaszol)"
    assert a.card_lines(card, "hu")["subtitle"] == "NVIDIA T4 · Szolgáltatás"
