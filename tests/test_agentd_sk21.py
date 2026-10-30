"""SK-21 контракты: sidekick-agentd — резидент на Hermes API.

Юниты чистой логики агентда (парсер stream-json-событий в кадры ленты, карта
sessions.json, курсор, think-хвост, конверт потомка) + контрактные проверки
юнита systemd, install-скрипта, плагина sidekick-think и сайдбар-заглушки.

Живой прогон (реальный hermes-потомок, hub, мост, hub/stop, рестарт юнита) —
харнесс приёмки, не pytest: здесь детерминированные части.
"""
import json
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT / "agentd"))
import sidekick_agentd as A  # noqa: E402


# ------------------------------------------------------------------ feed ----

def ev(t, **kw):
    return {"type": t, "timestamp": 0, **kw}


def types_of(frames):
    return [f["type"] for f in frames]


def test_feed_happy_path_frames_and_shapes():
    f = A.TaskFeed("t1", "открой ютуб")
    frames = f.start()
    assert frames[0] == {"type": "task", "id": "t1", "title": "открой ютуб", "state": "running"}
    assert frames[1]["type"] == "step" and frames[1]["state"] == "active" and frames[1]["n"] == 1
    assert frames[1]["task"] == "t1"

    frames += f.on_init("sid123")                      # шаг 1 закрыт done
    assert frames[-1] == {"type": "step", "task": "t1", "n": 1,
                          "text": frames[1]["text"], "state": "done"}

    frames += f.on_tool_use("terminal", {"command": "echo hi"})
    n2 = frames[-1]["n"]
    assert n2 == 2 and frames[-1]["state"] == "active"
    frames += f.on_tool_result(False)
    assert frames[-1]["state"] == "done" and frames[-1]["n"] == 2

    frames += f.on_think("думаю ")
    assert frames[-1] == {"type": "think", "task": "t1", "text": "думаю "}

    fin = f.finish(rc=0, text="готово", stopped=False)
    frames += fin
    chat = [x for x in fin if x["type"] == "chat"][-1]
    assert chat["text"] == "готово" and chat["role"] == "agent" and chat["task"] == "t1"
    assert frames[-1] == {"type": "task", "id": "t1", "title": "открой ютуб", "state": "done"}
    # шаг уже закрыт tool_result — финал даёт ровно chat + терминальный task
    assert types_of(fin) == ["chat", "task"]


def test_feed_tool_error_marks_step_failed_and_task_failed():
    f = A.TaskFeed("t2", "x")
    f.start(); f.on_init("s")
    f.on_tool_use("terminal", {"command": "false"})
    fr = f.on_tool_result(True)
    assert fr[-1]["state"] == "failed"
    fr = f.finish(rc=1, text="", stopped=False)
    assert fr[-1]["state"] == "failed"
    assert any(x["type"] == "chat" and "rc=1" in x["text"] for x in fr)


def test_feed_finish_stopped_and_crash_and_idempotent():
    f = A.TaskFeed("t3", "x"); f.start()
    fr = f.finish(rc=130, text="", stopped=True)
    assert any("■ Stop" in x.get("text", "") for x in fr if x["type"] == "chat")
    assert fr[-1]["state"] == "failed"
    assert types_of(fr) == ["step", "chat", "task"]  # открытый шаг закрывается первым
    assert fr[0]["state"] == "failed"
    assert f.finish(rc=130, text="", stopped=True) == []  # повторный финал — пусто

    g = A.TaskFeed("t4", "x"); g.start()
    fr = g.finish(rc=-9, text="", stopped=False, crashed=True)
    assert any("упал" in x.get("text", "") for x in fr if x["type"] == "chat")
    assert fr[-1]["state"] == "failed"


def test_feed_open_step_closes_previous():
    f = A.TaskFeed("t5", "x"); f.start(); f.on_init("s")
    f.on_tool_use("terminal", {"command": "a"})
    fr = f.on_tool_use("terminal", {"command": "b"})
    assert fr[0]["n"] == 2 and fr[0]["state"] == "done"   # предыдущий закрыт
    assert fr[-1]["n"] == 3 and fr[-1]["state"] == "active"


# ---------------------------------------------------------------- humanize --

def test_humanize_rpc_via_terminal_is_labeled():
    # SK-36 (перепин): голые команды из ленты вон — русская фраза, значение
    # селектора приватно и в подпись не попадает; сырьё — отдельное поле raw
    # (его рисует кат «подробнее» в сайдбаре).
    cmd = ("printf '%%s\\n' '{\"v\":1,\"id\":\"ab12\",\"method\":\"action.do\","
           "\"params\":{\"selector\":\"#login\",\"action\":\"click\"}}' >> rpc_in.jsonl")
    cmd = cmd % ()
    s = A.humanize_tool("terminal", {"command": cmd})
    assert s.startswith("кликаю") and "#login" not in s and "📡" not in s
    p = A.step_parts("terminal", {"command": cmd})
    assert p["text"] == s and "action.do" in p["raw"] and not p["weak"]


def test_humanize_plain_terminal_and_tools():
    # SK-36 (перепин): терминальная команда — под кат, в подписи только «что делаю»
    assert A.humanize_tool("terminal", {"command": "echo hello"}) == "🖥 выполняю команду в терминале"
    assert "config.yaml" in A.humanize_tool("read_file", {"path": "/x/config.yaml"})
    assert A.humanize_tool("weird_tool", {}) == "🔧 weird_tool"
    long = A.humanize_tool("terminal", {"command": "x" * 500})
    assert len(long) <= A.STEP_TEXT_MAX + 2


# ----------------------------------------------------------------- envelope -

def test_envelope_forbids_outbox_keeps_text_verbatim():
    text = 'скажи "точно" `так` $(не_раскрывать) && ещё'
    msg = A.build_child_message("tid42", text)
    assert "tid42" in msg and "outbox.jsonl не пиши" in msg
    assert "rpc_in.jsonl" in msg and "hub/stop" in msg
    assert msg.endswith(text)  # пользовательский текст — дословно, последним блоком


# ------------------------------------------------------------- tail_lines ---

def test_tail_lines_partial_and_truncation(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a":1}\n{"b":2}\n')
    lines, off = A.tail_lines(str(p), 0)
    assert lines == ['{"a":1}', '{"b":2}']
    # неполная строка не читается до перевода строки
    with p.open("a") as fh:
        fh.write('{"c":3}')
    lines2, off2 = A.tail_lines(str(p), off)
    assert lines2 == [] and off2 == off
    with p.open("a") as fh:
        fh.write('\n')
    lines3, off3 = A.tail_lines(str(p), off2)
    assert lines3 == ['{"c":3}'] and off3 > off2
    # усечение файла — перезапуск с нуля
    p.write_text('{"z":9}\n')
    lines4, off4 = A.tail_lines(str(p), off3)
    assert lines4 == ['{"z":9}'] and off4 == p.stat().st_size


def test_think_tail_reads_deltas_only(tmp_path):
    p = tmp_path / "think.jsonl"
    t = A.ThinkTail(str(p))                    # файла ещё нет (спавн до ребёнка)
    p.write_text(json.dumps({"kind": "delta", "delta": "привет"}) + "\n"
                 + json.dumps({"kind": "stream_start", "delta": ""}) + "\n"
                 + "не-json\n"
                 + json.dumps({"kind": "delta", "delta": " мир"}) + "\n")
    assert t.read() == ["привет", " мир"]
    assert t.read() == []


def test_think_tail_skips_preexisting_content(tmp_path):
    # ретрай без resume: в тот же файл дописывает новый потомок — старое не повторяем
    p = tmp_path / "think.jsonl"
    p.write_text(json.dumps({"kind": "delta", "delta": "старое"}) + "\n")
    t = A.ThinkTail(str(p))
    assert t.read() == []
    with p.open("a") as fh:
        fh.write(json.dumps({"kind": "delta", "delta": "новое"}) + "\n")
    assert t.read() == ["новое"]


# ----------------------------------------------------------------- sessions -

def test_sessions_map_roundtrip_corrupt_and_clear(tmp_path):
    p = tmp_path / "sessions.json"
    m = A.SessionsMap(str(p))
    assert m.get("default") is None
    m.set("default", "sid1")
    assert m.get("default") == "sid1"
    assert json.loads(p.read_text())["default"]["sid"] == "sid1"
    assert not list(tmp_path.glob("*.tmp*"))          # атомарность: tmp-мусора нет
    p.write_text("{битый")
    assert m.get("default") is None                    # битый файл = пусто, не падает
    m.set("default", "sid2")
    assert m.get("default") == "sid2"
    m.set("default", None)
    assert m.get("default") is None                    # сброс sid (битый resume)


def test_cursor_roundtrip(tmp_path):
    p = tmp_path / "cursor.json"
    c = A.Cursor(str(p))
    assert c.load() is False                 # файла нет — «первая установка»
    assert (c.inbox, c.read_inbox) == (0, 0)
    c.inbox, c.events = 123, 45
    c.save()
    d = A.Cursor(str(p))
    assert d.load() is True
    assert (d.inbox, d.events, d.read_inbox) == (123, 45, 123)
    p.write_text("не json")
    e = A.Cursor(str(p))
    assert e.load() is True                  # битый = старт с нуля, но не «первая установка»
    assert (e.inbox, e.events) == (0, 0)


# ------------------------------------------------------- outbox append ------

def test_out_frame_appends_v1_jsonl(tmp_path, monkeypatch):
    out = tmp_path / "outbox.jsonl"
    monkeypatch.setattr(A, "OUTBOX", str(out))
    A.out_frame(type="think", task="t", text="привет 🧠")
    A.out_frame(type="task", id="t", state="running")
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert lines[0] == {"v": 1, "type": "think", "task": "t", "text": "привет 🧠"}
    assert lines[1]["v"] == 1 and lines[1]["state"] == "running"


# ------------------------------------------------- плагин sidekick-think ----

PLUGIN = ROOT / "agentd" / "think_plugin"


def test_think_plugin_contract():
    assert (PLUGIN / "plugin.yaml").exists() and (PLUGIN / "__init__.py").exists()
    src = (PLUGIN / "__init__.py").read_text()
    assert "def register(ctx)" in src
    for hook in ("on_stream_start", "on_stream_delta", "on_stream_end"):
        assert f'"{hook}"' in src
    assert "SIDEKICK_THINK_FILE" in src  # без env — no-op (обычные прогоны профиля чисты)


def test_think_plugin_writes_only_with_env(tmp_path, monkeypatch):
    import importlib.util
    monkeypatch.delenv("SIDEKICK_THINK_FILE", raising=False)
    spec = importlib.util.spec_from_file_location("sk_think", PLUGIN / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []

    class Ctx:
        def register_hook(self, name, cb):
            calls.append(name)
    mod.register(Ctx())
    assert set(calls) == {"on_stream_start", "on_stream_delta", "on_stream_end"}
    # без env — тихо, без файла
    mod._on_stream_delta(kind="reasoning", delta="x", session_id="s")
    # с env — пишет только reasoning
    f = tmp_path / "t.jsonl"
    monkeypatch.setenv("SIDEKICK_THINK_FILE", str(f))
    mod._on_stream_delta(kind="text", delta="не пишем", session_id="s")
    mod._on_stream_delta(kind="reasoning", delta="думаю", session_id="s",
                         turn_id="t", iteration=1)
    recs = [json.loads(x) for x in f.read_text().splitlines()]
    assert len(recs) == 1 and recs[0]["kind"] == "delta" and recs[0]["delta"] == "думаю"
    assert recs[0]["session_id"] == "s"


# ------------------------------------------------- юнит и install-скрипт ----

UNIT = (ROOT / "agentd" / "sidekick-agentd.service")
INSTALL = ROOT / "scripts" / "install-agentd.sh"


def test_unit_contract():
    t = UNIT.read_text()
    assert "Restart=always" in t
    assert "WantedBy=default.target" in t
    assert "sidekick_agentd.py" in t
    assert "KillMode=control-group" in t  # рестарт демона забирает потомков с собой
    assert "sidekick-bridge.service" in t  # порядок: мост раньше агентда


def test_install_script_contract():
    s = INSTALL.read_text()
    assert "systemctl --user enable --now sidekick-agentd" in s
    assert "daemon-reload" in s
    assert "sidekick-think" in s and "stream_reasoning_deltas" in s
    assert "plugins.enabled" in s  # слияние списка, не перезапись вслепую
    assert INSTALL.stat().st_mode & 0o111


# ------------------------------------------------------ сайдбар-заглушка ----

BG = (ROOT / "extension" / "background.js").read_text()
SB = (ROOT / "extension" / "sidebar.js").read_text()
CSS = (ROOT / "extension" / "sidebar.css").read_text()


def test_background_forwards_think_and_task():
    assert 'msg.type === "think"' in BG
    assert 'type: "think"' in BG
    # chat-кадр пробрасывает task (аддитивно — старыми кадрами игнорируется)
    assert 'role: "hermes", text: msg.text, task: msg.task' in BG


def test_sidebar_stub_renders_think_block():
    assert "function addThink" in SB
    assert 'm.type === "think"' in SB
    assert "THINK_BLOCKS" in SB          # один блок на задачу, не спам строками
    assert "thinkDone" in SB              # финальный chat гасит «думает…»
    # самовылечивание: терминальный tl-кадр тоже закрывает заголовок (chat мог потеряться)
    assert 'm.state === "done" || m.state === "failed"' in SB
    assert ".m.think" in CSS


@pytest.mark.parametrize("f", ["background.js", "sidebar.js"])
def test_js_syntax(f):
    r = subprocess.run(["node", "--check", str(ROOT / "extension" / f)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
