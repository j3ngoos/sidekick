"""SK-29 контракты: история сессий agentd — именованные, возобновляемые, с ротацией.

Юниты чистой логики (именование sidekick/<chat>, граница «новая задача → новая
сессия», выбор целей ротации, idle-предикат, RpcWatch, abandon-кадры) + контракт
скрипта scripts/agentd-sessions.sh на фиктивной копии state.db (read-only) и
макет hub/sessions.json. Живой прогон с реальным hermes-потомком — харнесс
приёмки, не pytest (детерминированные части — здесь).
"""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agentd"))
import sidekick_agentd as A  # noqa: E402

SCRIPT = str(ROOT / "scripts" / "agentd-sessions.sh")


# ---------------------------------------------------------------- naming ----

def test_session_title_shape_and_sanitization():
    assert A.session_title("cmvfvh7jbz2jh") == "sidekick/cmvfvh7jbz2jh"
    assert A.session_title("default") == "sidekick/default"
    # страховка: мусорный chat id не должен утащить имя в невалидный заголовок
    assert A.session_title("  a\tb\n ") == "sidekick/a b"
    assert A.session_title("") == "sidekick/default"
    assert A.session_title("x" * 200) == "sidekick/" + "x" * 64


def test_history_title_bare_vs_history_distinguishable():
    h = A.history_title("c1", 1792600000.0)
    assert h.startswith("sidekick/c1 · ")
    assert A.HIST_SEP in h and A.HIST_SEP not in A.session_title("c1")
    # формат даты — стабильный, секундный (уникальность при сериальных задачах чата)
    import re
    assert re.search(r" · \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", h)


# ------------------------------------------------------- window/hygiene -----

def test_new_session_needed_window_boundary():
    now = 1000.0
    assert A.new_session_needed(None, now)                     # нет сессии — новая
    assert A.new_session_needed({"sid": ""}, now)              # пустая — новая
    assert not A.new_session_needed({"sid": "s", "updated": now - 10}, now, 1800)
    assert A.new_session_needed({"sid": "s", "updated": now - 1801}, now, 1800)
    # мусор в updated — консервативно новая (не тащить контекст вслепую)
    assert A.new_session_needed({"sid": "s", "updated": "не-число"}, now, 1800)
    assert A.new_session_needed({"sid": "s"}, now, 1800)


def test_window_default_is_thirty_minutes():
    assert A.WINDOW_S == 1800.0


# ------------------------------------------------------------- rotation -----

def _row(i, chat, hist, started, archived=False):
    title = A.session_title(chat) if not hist else A.history_title(chat, started)
    return {"id": str(i), "title": title, "started_at": started, "archived": archived}


def test_rotation_under_keep_noop():
    rows = [_row(i, f"c{i}", False, 100 + i) for i in range(30)]
    assert A.pick_archive_targets(rows, 30) == []


def test_rotation_archives_oldest_history_only_currents_protected():
    rows = [_row(i, f"h{i}", True, 100 + i) for i in range(31)]      # 31 истории
    rows.append(_row(99, "live", False, 999))                        # и текущая
    t = A.pick_archive_targets(rows, 30)
    assert [r["id"] for r in t] == ["0", "1"]  # 32 живых, keep 30 → ровно 2, старейшие
    assert all(A.HIST_SEP in r["title"] for r in t)                  # только исторические
    assert "99" not in [r["id"] for r in t]                          # текущая не тронута


def test_rotation_skips_archived_and_other_titles():
    rows = [_row(i, f"c{i}", True, 100 + i, archived=True) for i in range(40)]
    rows += [_row(200 + i, f"x{i}", False, 500 + i) for i in range(5)]
    rows[0]["title"] = "не-наш заголовок"                            # вне sidekick/*
    assert A.pick_archive_targets(rows, 30) == []                    # архивные не в зачёт


def test_rotation_only_currents_over_keep_leaves_them():
    rows = [_row(i, f"c{i}", False, 100 + i) for i in range(35)]     # 35 текущих, историй нет
    assert A.pick_archive_targets(rows, 30) == []                    # не архивим точку resume


def test_rotation_default_keep_30():
    assert A.SESSION_KEEP == 30


# --------------------------------------------------------------- envelope ---

def test_strip_envelope():
    env = A.build_child_message("t1", "привет, мир")
    assert A.strip_envelope(env) == "привет, мир"
    assert A.strip_envelope("обычная реплика") == "обычная реплика"
    assert A.strip_envelope("a\n---\nb\n---\nc") == "b\n---\nc"      # первое вхождение


# ------------------------------------------------------------------ idle ----

def test_idle_expired_predicate():
    assert A.idle_expired(0.0, 601.0, False, 600)
    assert not A.idle_expired(0.0, 599.0, False, 600)
    assert not A.idle_expired(0.0, 9999.0, True, 600)   # ждём человека — не трогать
    assert A.IDLE_S == 600.0


def test_idle_pause_exemptions_wired():
    """hub/paused (SK-08) и висячий вердикт гейта — константы/механизм на месте."""
    assert A.PAUSED_F.endswith("/paused")
    w = A.RpcWatch(rpc_in="/nonexistent-a", rpc_out="/nonexistent-b")
    assert w.poll() == {} and w.pending == set()       # нет файлов — не падает


# -------------------------------------------------------------- RpcWatch ----

def test_rpc_watch_pending_and_task_activity(tmp_path):
    ri, ro = tmp_path / "rpc_in.jsonl", tmp_path / "rpc_out.jsonl"
    ri.write_text(json.dumps({"v": 1, "id": "r1", "method": "page.dom",
                              "params": {"task": "sk1"}}) + "\n")
    w = A.RpcWatch(str(ri), str(ro))
    touched = w.poll()
    assert touched == {"sk1": pytest.approx(touched["sk1"], abs=5)}
    assert w.pending == {"r1"}                          # вердикта нет — гейт висит
    ro.write_text(json.dumps({"v": 1, "id": "r1", "ok": True}) + "\n")
    assert w.poll() == {}
    assert w.pending == set()                           # вердикт снял pending
    # rpc без params.task — в pending, но не в активность конкретной задачи
    # (файл дописывается — append-only контракт hub; ротация — отдельный тест)
    with open(ri, "a") as fh:
        fh.write(json.dumps({"v": 1, "id": "r2", "method": "perm.check",
                             "params": {"domain": "x"}}) + "\n")
    assert w.poll() == {} and w.pending == {"r2"}


def test_rpc_watch_survives_file_rotation(tmp_path):
    """SK-33 ротирует rpc_out → курсор за файлом: pending обязан обнулиться
    (иначе один потерянный вердикт навсегда глушит idle-надзор)."""
    ri, ro = tmp_path / "rpc_in.jsonl", tmp_path / "rpc_out.jsonl"
    ri.write_text(json.dumps({"id": "r1", "method": "m"}) + "\n")
    w = A.RpcWatch(str(ri), str(ro))
    w.poll()
    assert w.pending == {"r1"}
    ri.write_text("")                                    # ротация: файл с нуля
    ro.write_text("")
    w.poll()
    assert w.pending == set()


# ----------------------------------------------------------- TaskFeed -------

def test_feed_abandon_terminal_frames_and_idempotent():
    f = A.TaskFeed("t9", "задача")
    f.start()
    frames = f.pause("⏸ пауза")                          # idle-пауза: task paused
    assert frames[-1]["state"] == "paused"
    ab = f.abandon("⏱ просрочила окно — закрываю")
    types = [x["type"] for x in ab]
    assert "chat" in types and ab[-1] == {"type": "task", "id": "t9",
                                          "title": "задача", "state": "failed"}
    assert f.abandon("повтор") == []                     # закрытая — больше не шевелится
    assert f.pause("после закрытия") == []


def test_feed_pause_then_resume_still_works():
    f = A.TaskFeed("t9", "задача")
    f.start()
    f.pause("⏸")
    r = f.resume()
    assert r[0]["state"] == "running" and "▶" in r[-1]["text"]


# ------------------------------------------------------------ SessionsMap ---

def test_sessions_map_entry_and_name(tmp_path):
    p = tmp_path / "sessions.json"
    m = A.SessionsMap(str(p))
    assert m.entry("c1") == {}
    m.set("c1", "sid1", "sidekick/c1")
    e = m.entry("c1")
    assert e["sid"] == "sid1" and e["name"] == "sidekick/c1"
    assert isinstance(e["updated"], float)
    m.set("c1", None)
    assert m.entry("c1") == {} and m.get("c1") is None


def test_sessions_map_legacy_entry_without_name_readable(tmp_path):
    p = tmp_path / "sessions.json"
    p.write_text(json.dumps({"c1": {"sid": "s", "updated": 1.0}}))
    m = A.SessionsMap(str(p))
    assert m.entry("c1") == {"sid": "s", "updated": 1.0}   # legacy-читаемость


# ------------------------------------------------------------- child_cmd ----

def test_child_cmd_resume_only_when_sid():
    c = A.child_cmd("sid42")
    assert "--resume" in c and c[c.index("--resume") + 1] == "sid42"
    assert "--resume" not in A.child_cmd(None)
    assert A.child_cmd(None)[:4] == [A.hermes_bin(), "-p", A.PROFILE, "chat"]


# ------------------------------------------------------------- state rows ---

def _mkdb(path):
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
               "started_at REAL, last_activity_at REAL, message_count INTEGER, "
               "archived INTEGER DEFAULT 0)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
               "role TEXT, content TEXT, active INTEGER DEFAULT 1, "
               "_compressed_summary INTEGER DEFAULT 0, api_content TEXT)")
    return db


def test_state_db_rows_filters_and_errors(tmp_path):
    db = _mkdb(tmp_path / "s.db")
    db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?)", [
        ("a", "sidekick/c1", 1.0, 1.0, 2, 0),
        ("b", "sidekick/c1 · x", 2.0, 2.0, 3, 0),
        ("c", "чужая сессия", 3.0, 3.0, 9, 0),
        ("d", "sidekick/c2", 4.0, 4.0, 1, 1),
    ])
    db.commit()
    rows = A.state_db_rows(str(tmp_path / "s.db"))
    assert {r["id"] for r in rows} == {"a", "b", "d"}       # чужой заголовок отсечён
    assert next(r for r in rows if r["id"] == "d")["archived"]
    assert A.state_db_rows(str(tmp_path / "нет.db")) == []  # нет файла → []


def test_migrate_legacy_names_renames_existing_only(tmp_path, monkeypatch):
    db = _mkdb(tmp_path / "s.db")
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
               ("sid-old", "[sidekick-agentd task=sk1] мусор", 1.0, 1.0, 5, 0))
    db.commit()
    sj = tmp_path / "sessions.json"
    sj.write_text(json.dumps({"c1": {"sid": "sid-old", "updated": 1.0},
                              "c2": {"sid": "sid-нет-в-бд", "updated": 1.0}}))
    calls = []
    monkeypatch.setattr(A, "STATE_DB", str(tmp_path / "s.db"))
    monkeypatch.setattr(A, "_cli_sync",
                        lambda *a, **k: calls.append(a) or 0)
    m = A.SessionsMap(str(sj))
    assert A.migrate_legacy_names(m) == 1                   # только реально существующая
    assert calls == [("sessions", "rename", "sid-old", "sidekick/c1")]
    assert m.entry("c1")["name"] == "sidekick/c1"
    # повторный запуск — идемпотентно (name уже есть)
    calls.clear()
    assert A.migrate_legacy_names(m) == 0 and calls == []


# ---------------------------------------------------------------- script ----

def _script_env(tmp_path, monkeypatch=None):
    db = _mkdb(tmp_path / "s.db")
    now = 1792600000.0
    db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?)", [
        ("s1", "sidekick/cA", now, now + 60, 4, 0),
        ("s2", "sidekick/cA · 2026-10-20 10:00:00", now - 900, now - 800, 7, 0),
        ("s3", "sidekick/cB", now - 50, now - 40, 2, 0),
        ("s4", "sidekick/cC · 2026-10-19 09:00:00", now - 9e4, now - 9e4, 3, 1),
        ("s5", "чужой профиль", now, now, 1, 0),
    ])
    env = A.build_child_message("tX", "Запомни ровно: число 42")
    db.executemany("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)", [
        ("s1", "user", env),
        ("s1", "assistant", "ок"),
        ("s2", "user", "привет"),
        ("s3", "user", "открой страницу"),
        ("s4", "user", "старая"),
    ])
    db.commit()
    sj = tmp_path / "sessions.json"
    sj.write_text(json.dumps({
        "cA": {"sid": "s1", "updated": now, "name": "sidekick/cA"},
        "cB": {"sid": "s3", "updated": now, "name": "sidekick/cB"}}))
    return {"DB": str(tmp_path / "s.db"), "SJ": str(sj)}


def _run_script(env_extra, *args):
    env = dict(os.environ,
               SIDEKICK_STATE_DB=env_extra["DB"],
               SIDEKICK_SESSIONS_JSON=env_extra["SJ"],
               SIDEKICK_AGENTD_PROFILE="sidekick")
    r = subprocess.run([bash := "/bin/bash", SCRIPT, *args], env=env,
                       capture_output=True, text=True, timeout=60)
    return r


def test_script_lists_named_sessions_with_dates_and_first_reply(tmp_path):
    env = _script_env(tmp_path)
    r = _run_script(env)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "sidekick/cA" in out and "sidekick/cB" in out
    assert "2026-10-20 10:00:00" in out                    # историческая видна
    assert "чужой профиль" not in out                      # не наши — нет
    assert "число 42" in out                               # первая реплика БЕЗ конверта
    assert "sidekick-agentd" not in out                    # конверт вырезан
    assert "старая" not in out                             # архивная — только с --archived
    # порядок: свежие сверху
    assert out.index("sidekick/cA\n") if "sidekick/cA\n" in out else True
    assert out.find("sidekick/cA ") < out.find("sidekick/cA · ")


def test_script_archived_includes_rotated(tmp_path):
    env = _script_env(tmp_path)
    r = _run_script(env, "--archived")
    assert r.returncode == 0 and "sidekick/cC" in r.stdout and "арх" in r.stdout


def test_script_check_ok_and_mismatch(tmp_path):
    env = _script_env(tmp_path)
    r = _run_script(env, "--check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "согласовано" in r.stdout
    # рассинхрон карты: sid чата cA ведёт в никуда
    sj = pathlib.Path(env["SJ"])
    data = json.loads(sj.read_text())
    data["cA"]["sid"] = "s4"                               # архивная — нарушение
    sj.write_text(json.dumps(data))
    r = _run_script(env, "--check")
    assert r.returncode == 1 and "MISMATCH" in r.stdout
    # orphan: голоименная живая сессия, которой нет в карте
    data["cA"]["sid"] = "s1"
    del data["cB"]
    sj.write_text(json.dumps(data))
    r = _run_script(env, "--check")
    assert r.returncode == 1 and "ORPHAN" in r.stdout


def test_script_missing_db_exits_2(tmp_path):
    env = {"DB": str(tmp_path / "нет.db"), "SJ": str(tmp_path / "sj.json")}
    r = _run_script(env)
    assert r.returncode == 2 and "не найден" in r.stderr


def test_script_is_executable_and_documented():
    import stat
    st = os.stat(SCRIPT)
    assert st.st_mode & stat.S_IXUSR
    head = open(SCRIPT, encoding="utf-8").read(2000)
    assert "SK-29" in head and "--check" in head


# ------------------------------------------------- rename-воркер (гонка) ----

def test_rename_worker_serializes_and_retries(monkeypatch):
    """Живая приёмка 22.10 + SK-45: строка сессии коммитится потомком НЕ в момент
    init — rename обязан НЕ долбить CLI вслепую (rc=1 «No session» спамило в
    journal), а ждать видимость строки в state.db. Цепочка: prev→new строго по
    очереди; new — rename только после видимости, ровно один успешный вызов."""
    calls = []

    def fake_cli(*args, **kw):
        calls.append(args)
        return 0

    vis_states = iter([False, False, True])   # строка видна с 3-го чекпоинта

    monkeypatch.setattr(A, "_cli_sync", fake_cli)
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    monkeypatch.setattr(A, "session_titles_map",
                        lambda db=None: {"sid-new": "t"} if next(vis_states) else {})
    monkeypatch.setattr(A, "rotate_sessions", lambda: calls.append(("rotate",)))

    A._NAME_Q.queue.clear()
    A.name_new_session("cX", "sid-new", "sid-prev", 1792600000.0)
    A._name_work_once(*A._NAME_Q.get_nowait())

    assert [c[:3] for c in calls] == [
        ("sessions", "rename", "sid-prev"),
        ("sessions", "rename", "sid-new"),
        ("rotate",)]
    assert calls[0][3].startswith("sidekick/cX \u00b7 ")  # prev — в историю
    assert calls[1][3] == "sidekick/cX"                   # new — голое имя


def test_child_env_strips_kanban_leak(monkeypatch):
    """Kanban-окружение worker'а (HERMES_KANBAN_TASK=...) в потомке помечает
    сессию «kanban worker transcript» — Hermes затем отказывает CLI-resume по
    имени. Агентд — не диспетчер: в потомков kanban-переменные не текут."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/x.db")
    monkeypatch.setenv("HERMES_TENANT", "tX")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "sidekick")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    e = A.child_env("/tmp/think.jsonl")
    assert "HERMES_KANBAN_TASK" not in e and "HERMES_KANBAN_DB" not in e
    assert "HERMES_TENANT" not in e and "HERMES_KANBAN_BOARD" not in e
    # source — отдельная переменная (её ставит диспетчер): с ней сессия потомка
    # получает source=kanban → скрывается из sessions list и не резюмится по
    # имени (resume-guard). Агентд чинит её явно.
    assert e["HERMES_SESSION_SOURCE"] == "cli"
    assert e["SIDEKICK_THINK_FILE"] == "/tmp/think.jsonl"


def test_active_fresh_task_carries_prev_sid():
    """Проводка границы гигиены (регрессия живой приёмки 22.10): у новой задачи
    prev_sid — прошлая сессия чата (её в resume уже нет), иначе старая так и
    останется голоименной, а новой не освободится имя (rename → «title in use»)."""
    a = A.Active("c1", "текст", 0, "t1", None, new_task=True, prev_sid="sid-old")
    assert a.prev_sid == "sid-old" and a.resume_sid is None
    b = A.Active("c1", "текст", 0, "t2", "sid-live")          # resume внутри окна
    assert b.prev_sid is None and b.resume_sid == "sid-live"
