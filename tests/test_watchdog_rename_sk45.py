"""SK-45 контракты: watchdog зависания хода (3 мин) + честный retry rename сессий.

Аудит №2 22.10, проблемы 3 и 9: (а) sk79745 висела в thinking РОВНО 10 минут без
единого rpc/stdout и умерла только от ручного Stop (idle-close 600 с — единственный
страж, и тот не успел/поздн); (б) rc=1 «No session» на каждой новой сессии —
rename успевал до коммита строки hermes, именование SK-29 держалось на случайных
ретраях. Здесь — чистые предикаты, расписание чекпоинтов и контракт «тишины»;
живой SIGSTOP-прогон — харнесс tests/sk45_acc.py, не pytest.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agentd"))
import sidekick_agentd as A  # noqa: E402


# ------------------------------------------------------------- watchdog -----

def test_hang_expired_requires_both_channels_silent():
    now = 10_000.0
    wd = 180.0
    # оба канала молчат — зависание
    assert A.hang_expired(now - 181, now - 181, now, False, wd)
    assert A.hang_expired(now - 500, now - 181, now, False, wd)   # rpc старше
    assert A.hang_expired(now - 181, now - 500, now, False, wd)   # stdout старше
    # один канал жив — НЕ зависание (модель думает / идёт rpc)
    assert not A.hang_expired(now - 5, now - 500, now, False, wd)   # stdout стримит
    assert not A.hang_expired(now - 500, now - 5, now, False, wd)   # rpc живой
    # граница: тишина ровно порога — ещё не просрочка
    assert not A.hang_expired(now - 180, now - 180, now, False, wd)
    # ждём человека (hub/paused, висячий вердикт гейта) — не зависание
    assert not A.hang_expired(now - 500, now - 500, now, True, wd)


def test_hang_expired_zero_disables_and_default_threshold():
    assert A.WATCHDOG_S == 180.0
    assert not A.hang_expired(0.0, 0.0, 10_000.0, False, 0)      # выключен
    assert not A.hang_expired(0.0, 0.0, 10_000.0, False, -1)
    # rpc ещё не было (0.0) — часы считаются по stdout спавна, а не от «нуля»
    assert not A.hang_expired(10_000.0 - 10, 0.0, 10_000.0, False, 180)
    assert A.hang_expired(10_000.0 - 200, 0.0, 10_000.0, False, 180)


def test_hang_text_shape():
    t = A.hang_text("🔎 search_files · rpc_in", 180)
    assert "зависла (3 мин тишины)" in t
    assert "вот что было последним: 🔎 search_files · rpc_in" in t
    assert "Напиши реплику" in t                     # резюм — репликой человека
    assert "молчал" in A.hang_text("", 180)           # ничего не видел — честно
    assert "45 с тишины" in A.hang_text("x", 45)      # порог <минуты — в секундах
    assert "60 с" not in A.hang_text("x", 120)        # >=минуты — в минутах


def test_active_carries_watchdog_clocks():
    a = A.Active("c1", "текст", 0, "t1", None, new_task=True)
    assert a.last_out == pytest.approx(a.last_event, abs=2)
    assert a.last_rpc == 0.0            # rpc ещё не было — max() считает по stdout
    assert a.last_seen
    assert a.hang_kill_at == 0.0 and a.hang_killed is False


def test_watchdog_wired_into_main_loop():
    src = (ROOT / "agentd" / "sidekick_agentd.py").read_text(encoding="utf-8")
    for needle in (
            "hang_expired(a.last_out, a.last_rpc",   # предикат в pump
            "hang_text(a.last_seen)",                # явная строка в чат
            "hang_kill_at",                          # эскалация SIGKILL
            "a.last_event = a.last_out",             # stdout-строка = жизнь
            ".last_rpc = ts_",                       # rpc задачи = жизнь
            "SIDEKICK_AGENTD_WATCHDOG_S",            # порог — конфиг
    ):
        assert needle in src, needle
    # think-дельты — тоже вывод (иначе «долго думает» убивалось бы на ровном месте)
    assert 'a.last_seen = "thinking: "' in src


# -------------------------------------------------------------- rename ------

def test_rename_checkpoints_default_schedule():
    assert A.RENAME_BACKOFF_S == [0.5, 1.0, 2.0, 4.0]
    assert A.RENAME_MAX_WAIT_S == 10.0
    # спека: 0.5/1/2/4 с, суммарно до 10 с; последний чекпоинт — ровно бюджет
    assert A.rename_checkpoints() == [0.0, 0.5, 1.5, 3.5, 7.5, 10.0]


def test_rename_checkpoints_custom_and_budget_cap():
    assert A.rename_checkpoints([1, 2], 10) == [0.0, 1.0, 3.0, 10.0]
    assert A.rename_checkpoints([5], 3) == [0.0, 3.0]        # шаг за бюджетом
    assert A.rename_checkpoints([2, 3], 5) == [0.0, 2.0, 5.0]  # впритык — без дубля


def test_rename_waits_for_visibility_then_renames_once(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(A.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(A, "_cli_sync",
                        lambda *a, **k: calls.append((a, k)) or 0)
    seen = {"n": 0}

    def vis():
        seen["n"] += 1
        return seen["n"] >= 3          # строка появилась к третьему чекпоинту

    assert A._rename_when_visible("sid-N", "sidekick/cN", visible=vis) is True
    assert len(calls) == 1             # rename ровно один — после видимости, не вслепую
    assert calls[0][0] == ("sessions", "rename", "sid-N", "sidekick/cN")
    assert calls[0][1].get("quiet") is True          # rc=1 не протекает в journal
    assert sleeps == [0.5, 1.0]        # backoff, а не слепые удары с паузой 1 с


def test_rename_exhaustion_is_quiet_note_only(monkeypatch, tmp_path):
    calls, logs = [], []
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    monkeypatch.setattr(A, "_cli_sync", lambda *a, **k: calls.append(a) or 1)
    monkeypatch.setattr(A, "log", lambda *x: logs.append(" ".join(map(str, x))))
    out = tmp_path / "outbox.jsonl"
    monkeypatch.setattr(A, "OUTBOX", str(out))
    # строка не появилась вовсе: CLI вслепую не долбим, в чат не пишем,
    # в journal — одна пометка без «rc=1»
    assert A._rename_when_visible("sid-X", "sidekick/cX", visible=lambda: False) is False
    assert calls == []
    assert not out.exists()
    assert len(logs) == 1 and "не появилась" in logs[0] and "rc=1" not in logs[0]


def test_rename_visible_but_cli_error_gives_up_quietly(monkeypatch):
    # строка видна, но rename упорно падает (не гонка) — по попытке на чекпоинт,
    # затем тихая пометка; rc=1-спам в journal не выходит (quiet=True везде)
    calls = []
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    monkeypatch.setattr(A, "_cli_sync", lambda *a, **k: calls.append(a) or 1)
    monkeypatch.setattr(A, "log", lambda *x: None)
    assert A._rename_when_visible("sid-Y", "t", visible=lambda: True) is False
    assert len(calls) == len(A.rename_checkpoints())


def test_default_visible_probe_reads_state_db(monkeypatch):
    calls = []
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    monkeypatch.setattr(A, "_cli_sync", lambda *a, **k: calls.append(a) or 0)
    monkeypatch.setattr(A, "session_titles_map",
                        lambda db=None: {"sid-Z": "sidekick/cZ"})
    assert A._rename_when_visible("sid-Z", "sidekick/cZ") is True
    assert calls == [("sessions", "rename", "sid-Z", "sidekick/cZ")]
    # строки нет в state — вслепую rename не дёргаем (тот самый rc=1 из аудита)
    calls.clear()
    monkeypatch.setattr(A, "session_titles_map", lambda db=None: {})
    monkeypatch.setattr(A, "log", lambda *x: None)
    assert A._rename_when_visible("sid-W", "sidekick/cW") is False
    assert calls == []
