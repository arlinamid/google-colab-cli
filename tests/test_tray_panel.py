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

"""The tray panel and controller, on Qt's offscreen platform (docs/09)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

try:  # no PySide6, or no GL/xkb libraries for Qt on a bare Linux box
    from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QPushButton

    from colab_cli.tray import app as tray_app
    from colab_cli.tray.panel import MAX_VISIBLE_CARDS
except ImportError as e:
    pytest.skip(f"Qt is not usable here: {e}", allow_module_level=True)

from colab_cli.state import Settings  # noqa: E402

NOW = 1_000_000.0


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _entry(name, job=None, **kw):
    e = {"name": name, "endpoint": f"ep-{name}", "hardware": "T4", "local": True, "profile": None,
         "stop_when_done": None, "running": None, "activity": None, "last_execution_at": None,
         "jobs_checked_at": NOW, "job": job}
    e.update(kw)
    return e


def _job(state="running", percent=45.0, **kw):
    j = {"id": "train-1", "state": state, "overall_percent": percent, "progress": {"eta_s": 720},
         "epoch": {"current": 2, "total": 5}, "cell": None, "last_output_age_s": 4.0, "stalled": False,
         "metrics": {"trend": {"loss": {"first": 0.5, "last": 0.31, "min": 0.31, "max": 0.5}}},
         "diagnosis": None, "stop_reason": None, "ended_at": None}
    j.update(kw)
    return j


def _snap(sessions, ok=True):
    return {"type": "tray_snapshot", "taken_at": NOW, "last_ok_at": NOW, "ok": ok, "error": None if ok else "x",
            "account": {"balance_cu": 87.0, "rate_cu_per_hour": 1.9, "assignments": 1, "fetched_at": NOW},
            "sessions": sessions}


@pytest.fixture
def tray(qapp, tmp_path, mocker):
    state = MagicMock()
    state.config_path = str(tmp_path / "sessions.json")
    state.settings_store.load.return_value = Settings(tray_language="hu")
    mocker.patch.object(tray_app, "colab_argv", return_value=["colab"])
    popen = mocker.patch.object(tray_app.subprocess, "Popen")
    popen.return_value.poll.return_value = None
    t = tray_app.TrayApp(state, interval=30)
    t.popen = popen
    t.notify = MagicMock()
    yield t
    t.panel.hide()


def _render(tray, sessions):
    tray.snapshot = _snap(sessions)
    view = tray.view(now=NOW)
    tray.panel.render(view)
    return view


def test_one_card_as_in_the_design(tray):
    view = _render(tray, [_entry("train-t4", _job())])

    assert len(tray.panel.cards) == 1
    texts = [w.text() for w in tray.panel.cards[0].findChildren(QLabel)]
    assert "train-t4" in texts and "NVIDIA T4 · Fut" in texts
    assert any("loss 0,31" in x and "#34a853" in x for x in texts)
    assert "2 / 5 epoch · kb. 12 perc hátra" in texts
    assert tray.panel.cards[0].findChildren(QProgressBar)[0].value() == 450
    buttons = [b.text() for b in tray.panel.cards[0].findChildren(QPushButton)]
    assert buttons == ["Napló", "SSH", "Leállítás"]
    assert tray.panel.count_badge.text() == "1 aktív"
    assert tray.panel.status_text.text() == "Kapcsolódva"
    assert "Becsült hátralévő idő" in tray.panel.cu_estimate.text()
    assert "87,0 CU" in tray.panel.cu_balance.text()
    assert view["tooltip"].startswith("Colab CLI – Fut")


def test_many_runtimes_scroll(tray):
    _render(tray, [_entry(f"s{i}", _job()) for i in range(5)])

    assert len(tray.panel.cards) == 5
    first = tray.panel.cards[:MAX_VISIBLE_CARDS]
    assert tray.panel.scroll.height() == sum(c.sizeHint().height() for c in first) + 8 * (len(first) - 1)
    assert tray.panel.cards_host.sizeHint().height() > tray.panel.scroll.height()


def test_an_orphan_card_has_no_actions(tray):
    _render(tray, [_entry("?", None, local=False)])
    card = tray.panel.cards[0]
    assert card.findChildren(QPushButton) == []
    texts = [w.text() for w in card.findChildren(QLabel)]
    assert "leállítás: colab stop --orphans" in texts


def test_no_progress_shows_the_last_output(tray):
    _render(tray, [_entry("a", _job(percent=None, epoch=None, progress=None, metrics={"trend": {}},
                                    last_output_age_s=180))])

    card = tray.panel.cards[0]
    assert card.findChildren(QProgressBar) == []
    assert "utolsó kimenet: 3 perce" in [w.text() for w in card.findChildren(QLabel)]


def test_stale_and_error_states(tray):
    tray.snapshot = _snap([_entry("a", _job())])
    view = tray.view(now=NOW + 61)
    assert view["status"] == "stale" and view["icon"]["level"] == "stale"
    assert "Elavult" in view["tooltip"]

    tray.snapshot = {**_snap([]), "ok": False, "error": "boom"}
    view = tray.view(now=NOW)
    assert view["status"] == "error" and "boom" in view["tooltip"]


def test_stop_job_only(tray, mocker):
    _render(tray, [_entry("train-t4", _job())])
    mocker.patch.object(tray, "choose_stop", return_value="job")

    tray.ask_stop(tray.view(now=NOW)["cards"][0]["card"])

    assert tray.popen.call_args.args[0] == ["colab", "job", "stop", "-s", "train-t4", "train-1"]


def test_stop_the_runtime_closes_its_service(tray, mocker):
    service = MagicMock()
    service.poll.return_value = None
    tray.children["ui"] = {"proc": service, "log": "l", "session": "train-t4", "mode": "kernel"}
    _render(tray, [_entry("train-t4", None)])
    choose = mocker.patch.object(tray, "choose_stop", return_value="runtime")

    tray.ask_stop(tray.view(now=NOW)["cards"][0]["card"])

    options = dict(choose.call_args.args[1])
    assert set(options) == {"runtime"} and "forward" in options["runtime"]
    service.terminate.assert_called_once()
    assert tray.popen.call_args.args[0] == ["colab", "stop", "-s", "train-t4"]


def test_cancelled_stop_does_nothing(tray, mocker):
    _render(tray, [_entry("train-t4", _job())])
    mocker.patch.object(tray, "choose_stop", return_value=None)
    tray.ask_stop(tray.view(now=NOW)["cards"][0]["card"])
    tray.popen.assert_not_called()


def test_start_profile_after_confirmation(tray, tmp_path, mocker):
    (tmp_path / "profiles.toml").write_text(
        '[train]\nscript = "t.py"\ngpu = "T4"\n\n[ui]\nscript = "s.py"\nmode = "kernel"\n'
        'session = "ui"\nopen = "http://127.0.0.1:8188"\n', encoding="utf-8")
    confirm = mocker.patch.object(tray, "confirm", return_value=True)
    _render(tray, [])
    assert tray.panel.profile_button.text().endswith("train")
    assert [a.text() for a in tray.panel.profile_menu.actions()] == ["train", "ui"]

    tray.start_profile("ui")

    title, body = confirm.call_args.args
    assert "ui" in title and "1,9 CU / óra" in body
    assert tray.popen.call_args.args[0] == ["colab", "profile", "run", "ui"]
    assert tray.children["ui"]["session"] == "ui"
    assert tray.settings.tray_last_profile == "ui"

    # The running kernel profile is a service with an Open button.
    _render(tray, [_entry("ui", None, profile="ui")])
    assert [b.text() for b in tray.panel.cards[0].findChildren(QPushButton)] == [
        "Napló", "SSH", "Megnyitás", "Leállítás"]


def test_declined_profile_does_not_start(tray, tmp_path, mocker):
    (tmp_path / "profiles.toml").write_text('[train]\nscript = "t.py"\n', encoding="utf-8")
    mocker.patch.object(tray, "confirm", return_value=False)
    tray.start_profile("train")
    tray.popen.assert_not_called()


def test_snapshot_notifies_and_stops_when_done(tray):
    first = _snap([_entry("t", _job(), profile="p", stop_when_done=True)])
    done = _snap([_entry("t", _job(state="succeeded", ended_at="1970-01-12T13:46:40+00:00"),
                         profile="p", stop_when_done=True)])
    tray.on_snapshot(first)
    tray.popen.assert_not_called()

    tray.on_snapshot(done)

    texts = [c.args[0] for c in tray.notify.call_args_list]
    assert "Kész a feladat: t" in texts and "Leállítom: t (stop_when_done)" in texts
    assert tray.popen.call_args.args[0] == ["colab", "stop", "-s", "t"]
    tray.on_snapshot(done)
    assert tray.popen.call_count == 1  # asked once


def test_failed_profile_run_is_reported(tray):
    proc = MagicMock()
    proc.poll.return_value = 2
    tray.children["x"] = {"proc": proc, "log": "/tmp/x.log", "session": None, "mode": "job"}
    tray.on_snapshot(_snap([]))
    assert "x" not in tray.children
    assert "2" in tray.notify.call_args.args[0] and "/tmp/x.log" in tray.notify.call_args.args[0]


def test_status_icon_draws(qapp):
    from colab_cli.tray.icon import logo_pixmap, status_icon

    assert not status_icon("#34a853").isNull()
    pix = logo_pixmap(30)
    assert pix.height() == 30 and pix.width() > 40


def test_dialogs_open_on_top_of_the_panel_without_its_style(tray, mocker):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog

    seen = {}

    def fake_exec(dialog):
        seen["flags"] = dialog.windowFlags()
        seen["parent"] = dialog.parent()
        seen["keep_open"] = tray.panel.keep_open
        return QDialog.Rejected

    mocker.patch.object(QDialog, "exec", fake_exec)
    _render(tray, [_entry("train-t4", _job())])
    tray.panel.show()

    tray.open_settings()

    assert seen["flags"] & Qt.WindowStaysOnTopHint
    assert seen["parent"] is None
    assert seen["keep_open"] is True and tray.panel.keep_open is False
