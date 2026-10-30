"""SK-25 контракты: привязка задача↔вкладка.

Мост (живой, mock-расширение): резолв цели (явный tab > вкладка задачи > активная),
биндинг по ответу open_grouped/use_tab (первая привязка выигрывает / rebind),
push task_tab, tab_gone роняет биндинг, инъекция вкладки в ОБА лега action.do,
аудит с tab. Расширение: чистая машина состояний URL-свидетеля (tabGate) под node +
статические контракты (TASKS, task_tab, tab_focus, чип вкладки). agentd: конверт с
указанием task, параллельные задачи (не более одной на чат, курсор до самого раннего
незакрытого).
"""
import asyncio
import json
import pathlib
import re
import shutil
import subprocess
import time
import uuid

import pytest
import websockets

from conftest import kill_bridge, start_bridge, token_of

ROOT = pathlib.Path(__file__).resolve().parent.parent
URI = "ws://127.0.0.1:8799"

BG = (ROOT / "extension/background.js").read_text(encoding="utf-8")
FEED = (ROOT / "extension/feed.js").read_text(encoding="utf-8")
SB = (ROOT / "extension/sidebar.js").read_text(encoding="utf-8")
CSS = (ROOT / "extension/sidebar.css").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

import sys
sys.path.insert(0, str(ROOT / "agentd"))
import sidekick_agentd as A  # noqa: E402


def rpc_call(home, method, params=None, rid=None):
    rid = rid or str(uuid.uuid4())
    req = {"v": 1, "id": rid, "method": method, "params": params or {}}
    with open(home / "rpc_in.jsonl", "a") as f:
        f.write(json.dumps(req, ensure_ascii=False) + "\n")
    return rid


def read_rpc_out(home, rid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = home / "rpc_out.jsonl"
        if p.exists():
            for ln in p.read_text().splitlines():
                try:
                    obj = json.loads(ln)
                except ValueError:
                    continue
                if obj.get("id") == rid:
                    return obj
        time.sleep(0.05)
    return None


def run(coro):
    return asyncio.run(coro)


class ExtSession:
    """Mock-расширение: одно WS, несколько rpc-ногов, collect task_tab-рамок."""

    def __init__(self, home):
        self.home = home
        self.task_tabs = []

    async def __aenter__(self):
        self.c = await websockets.connect(URI, open_timeout=3)
        await self.c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(self.home)}))
        ack = json.loads(await asyncio.wait_for(self.c.recv(), 3))
        assert ack.get("ok") is True, ack
        return self

    async def __aexit__(self, *a):
        await self.c.close()

    async def pump(self, timeout=1.0):
        """Съесть одну рамку (любого типа), классифицируя task_tab; timeout — молчание."""
        try:
            raw = await asyncio.wait_for(self.c.recv(), timeout)
        except asyncio.TimeoutError:
            return None
        msg = json.loads(raw)
        if msg.get("type") == "task_tab":
            self.task_tabs.append(msg)
        return msg

    async def wait_task_tab(self, pred, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for tt in self.task_tabs:
                if pred(tt):
                    return tt
            await self.pump(max(0.05, deadline - time.time()))
        return None

    async def wait_rpc(self, method=None, timeout=5.0):
        """Ждёт rpc-рамку (опционально нужного метода); task_tab собирает попутно."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = await asyncio.wait_for(self.c.recv(), max(0.1, deadline - time.time()))
            msg = json.loads(raw)
            if msg.get("type") == "task_tab":
                self.task_tabs.append(msg)
                continue
            if msg.get("type") != "rpc":
                continue
            if method and msg.get("method") != method:
                continue
            return msg
        raise AssertionError(f"no rpc frame ({method or 'any'}) in time")

    async def reply(self, rid, ok=True, result=None, err=None):
        frame = {"v": 1, "type": "rpc_result", "id": rid, "ok": ok}
        if ok:
            frame["result"] = result or {}
        else:
            frame["err"] = err or "err"
        await self.c.send(json.dumps(frame, ensure_ascii=False))

    async def call(self, home, method, params, respond=None, err=None, timeout=5.0):
        """rpc_call + дождаться доставки расширению + ответить + вернуть (frame, out)."""
        rid = rpc_call(home, method, params)
        frame = await self.wait_rpc(timeout=timeout)
        assert frame["method"] == method, frame
        if respond is not None or err is not None:
            await self.reply(frame["id"], ok=err is None, result=respond, err=err)
        return frame, rid


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


DOM = {"url": "http://x/", "title": "t", "elements": [], "truncated": False}


# ------------------------------------------------------------- bridge: цель ---

def test_open_grouped_binds_and_page_targets_bound_tab(home):
    async def t():
        async with ExtSession(home) as x:
            f, rid = await x.call(home, "tabs.open_grouped",
                                  {"task": "T1", "url": "http://site/a"},
                                  respond={"tabId": 42, "windowId": 7, "url": "http://site/a"})
            assert read_rpc_out(home, rid)["ok"] is True
            # push task_tab — карточка сайдбара должна видеть вкладку
            tt = await x.wait_task_tab(lambda m: m["task"] == "T1" and m["tab"] == 42)
            assert tt == {"v": 1, "type": "task_tab", "task": "T1", "tab": 42,
                          "window": 7, "url": "http://site/a"}
            # page.dom с task — бьёт строго по привязанной вкладке
            f2, rid2 = await x.call(home, "page.dom", {"task": "T1"}, respond=DOM)
            assert f2["params"].get("tab") == 42
            assert read_rpc_out(home, rid2)["ok"] is True
    run(t())


def test_explicit_tab_wins_legacy_has_no_tab(home):
    async def t():
        async with ExtSession(home) as x:
            _, rid = await x.call(home, "tabs.open_grouped", {"task": "T1", "url": "http://a/"},
                                  respond={"tabId": 42, "url": "http://a/"})
            read_rpc_out(home, rid)
            f, rid = await x.call(home, "page.text", {"task": "T1", "tab": 99},
                                  respond={"text": "x"})
            assert f["params"]["tab"] == 99  # явный tab > биндинг
            read_rpc_out(home, rid)
            f, rid = await x.call(home, "page.dom", {}, respond=DOM)  # легаси — без tab
            assert "tab" not in f["params"]
            read_rpc_out(home, rid)
            # page.active — «что у человека»; вкладка-цель противоречит смыслу
            rid = rpc_call(home, "page.active", {"tab": 5})
            out = read_rpc_out(home, rid, timeout=3.0)
            assert out and out["ok"] is False and out["err"] == "bad_request"
    run(t())


def test_first_binding_wins_open_grouped(home):
    async def t():
        async with ExtSession(home) as x:
            _, rid = await x.call(home, "tabs.open_grouped", {"task": "T1", "url": "http://a/"},
                                  respond={"tabId": 42})
            read_rpc_out(home, rid)
            _, rid = await x.call(home, "tabs.open_grouped", {"task": "T1", "url": "http://b/"},
                                  respond={"tabId": 55})
            read_rpc_out(home, rid)  # задача владеет ОДНОЙ вкладкой — вторая не перетирает
            f, rid = await x.call(home, "page.dom", {"task": "T1"}, respond=DOM)
            assert f["params"].get("tab") == 42
    run(t())


def test_use_tab_rebinds_task(home):
    async def t():
        async with ExtSession(home) as x:
            _, rid = await x.call(home, "tabs.open_grouped", {"task": "T1", "url": "http://a/"},
                                  respond={"tabId": 42})
            read_rpc_out(home, rid)
            # use_tab {task,tabId} — глагол перепривязки (восстановление после tab_moved)
            f, rid = await x.call(home, "tabs.use_tab", {"task": "T1", "tabId": 43},
                                  respond={"tabId": 43, "url": "http://c/"})
            read_rpc_out(home, rid)
            f, rid = await x.call(home, "page.dom", {"task": "T1"}, respond=DOM)
            assert f["params"].get("tab") == 43
    run(t())


def test_tab_gone_kills_task_until_rebind(home):
    async def t():
        async with ExtSession(home) as x:
            _, rid = await x.call(home, "tabs.open_grouped", {"task": "T1", "url": "http://a/"},
                                  respond={"tabId": 42})
            read_rpc_out(home, rid)
            # расширение доложило tab_gone — ошибка уходит агенту, запись помечена мёртвой
            _, rid = await x.call(home, "page.dom", {"task": "T1"},
                                  err="tab_gone: вкладка задачи закрыта")
            out = read_rpc_out(home, rid)
            assert out["ok"] is False and out["err"].startswith("tab_gone")
            tt = await x.wait_task_tab(lambda m: m["task"] == "T1" and m["tab"] is None)
            assert tt is not None  # карточка погашена (tab:null)
            # мёртвая задача НЕ падает в легаси-активную: каждый rpc — tab_gone локально,
            # без раунд-трипа к браузеру (упрямый агент не ударит по чужой странице)
            rid = rpc_call(home, "page.dom", {"task": "T1"})
            out = read_rpc_out(home, rid, timeout=3.0)
            assert out and out["ok"] is False and out["err"].startswith("tab_gone")
            rid = rpc_call(home, "action.do", {"task": "T1", "selector": "#a", "action": "click"})
            out = read_rpc_out(home, rid, timeout=3.0)
            assert out and out["ok"] is False and out["err"].startswith("tab_gone")
            # open_grouped оживляет мёртвую запись (иначе задача была бы похоронена навсегда)
            _, rid = await x.call(home, "tabs.open_grouped", {"task": "T1", "url": "http://b/"},
                                  respond={"tabId": 77})
            read_rpc_out(home, rid)
            f, rid = await x.call(home, "page.dom", {"task": "T1"}, respond=DOM)
            assert f["params"].get("tab") == 77
    run(t())


def test_bad_tab_local_no_browser(home):
    # неверный tab отсекается локально, до раунд-трипа (расширение не нужно)
    for bad in ("x", -1, True, 1.5):
        rid = rpc_call(home, "page.dom", {"task": "T", "tab": bad})
        out = read_rpc_out(home, rid, timeout=3.0)
        assert out and out["ok"] is False and out["err"] == "bad_request", bad


def test_action_do_injects_tab_into_both_legs(home):
    async def t():
        async with ExtSession(home) as x:
            _, rid = await x.call(home, "tabs.open_grouped", {"task": "T1", "url": "http://a/"},
                                  respond={"tabId": 42, "url": "http://a/"})
            read_rpc_out(home, rid)
            rid = rpc_call(home, "action.do", {"task": "T1", "selector": "#buy", "action": "click"})
            pv = await x.wait_rpc("action.preview")   # гейт-превью — на вкладке задачи
            assert pv["params"].get("tab") == 42
            await x.reply(pv["id"], result={"shown": True, "cancelled": False})
            ex = await x.wait_rpc("action.do")        # исполнение — та же вкладка
            assert ex["params"].get("tab") == 42
            await x.reply(ex["id"], result={"done": True, "executed": True, "synthetic": True})
            out = read_rpc_out(home, rid)
            assert out["ok"] is True and out["result"]["executed"] is True
            # аудит: обе ноги знают вкладку
            await asyncio.sleep(0.3)
            lines = [json.loads(ln) for ln in (home / "audit.jsonl").read_text().splitlines()]
            prev = [a for a in lines if a.get("ev") == "preview" and a.get("via") == "action.do"]
            assert prev and prev[-1].get("tab") == 42
    run(t())


def test_bind_without_task_is_inert(home):
    async def t():
        async with ExtSession(home) as x:
            _, rid = await x.call(home, "tabs.open_grouped", {"url": "http://a/"},
                                  respond={"tabId": 42})
            assert read_rpc_out(home, rid)["ok"] is True
            await asyncio.sleep(0.3)
            assert not x.task_tabs  # легаси-вызов без task ничего не привязывает
            f, rid = await x.call(home, "page.dom", {}, respond=DOM)
            assert "tab" not in f["params"]
    run(t())


# ------------------------------------------- расширение: машина URL-свидетеля ---

NODE_TABGATE = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[1], "utf8"));
const out = {};
out.no_track = tabGate(null, 1000);
out.grace = tabGate({ url: "u", moved: false, graceUntil: 2000 }, 1000);
out.ok = tabGate({ url: "u", moved: false, graceUntil: 500 }, 1000);
out.moved_sticky = tabGate({ url: "u", moved: true, graceUntil: 9e15 }, 1000); // moved липкий
console.log(JSON.stringify(out));
"""


def test_tabgate_state_machine_node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    m = re.search(r"(function tabGate\(track, now\) \{.*?\n\})", BG, re.S)
    assert m, "чистая функция tabGate исчезла из background.js"
    block = tmp_path / "sk25_tabgate.js"
    block.write_text(m.group(1), encoding="utf-8")
    r = subprocess.run([node, "-e", NODE_TABGATE, str(block)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node: {r.stderr}"
    assert json.loads(r.stdout) == {"no_track": "fresh", "grace": "grace",
                                    "ok": "ok", "moved_sticky": "moved"}


# ------------------------------------------------- расширение: статика п.2-4 ---

def test_background_multitask_and_watcher_contracts():
    assert "const TASKS = new Map()" in BG                      # п.3: ленты задач не путаются
    assert "function taskOf(" in BG                             # снапшот — массив задач
    assert "tasks }" in BG and "const tasks = [];" in BG        # tlPush: tasks-массив аддитивно
    assert 'msg.type === "task_tab"' in BG                      # п.4: bind/rebind/unbind
    assert "function resolveTargetTab(" in BG                   # п.2: цель — конкретная вкладка
    assert "ERR_TAB_GONE" in BG and "tab_gone" in BG
    assert "ERR_TAB_MOVED" in BG and "tab_moved" in BG
    assert "NAV_GRACE_MS" in BG and "graceUntil" in BG          # grace: агент vs человека
    # SK-44: решение переехало в чистую witnessNav (объект на выходе вместо присваиваний) —
    # пин tab_returned: возврат на эталонный URL обязан снимать moved
    assert "tr.moved ? { ...tr, moved: false } : tr" in BG      # tab_returned: возврат снял претензию
    # стендовый обжон (S3b): moved-ветка перетирала tr.url — эталон агента — hijack-URL,
    # и возврат на эталонный URL уже не мог совпасть: tab_returned не срабатывал никогда.
    assert not re.search(r"tr\.moved = true; tr\.url", BG), \
        "уход человека НЕ перезаписывает эталонный URL вкладки задачи"
    assert "browser.tabs.onRemoved" in BG and "tabGone = true" in BG
    assert 'm.type === "tab_focus"' in BG                       # п.4: чип = фокус вкладки
    assert "browser.tabs.update(e.tab, { active: true })" in BG


def test_feed_card_tab_chip_contracts():
    assert "tktab" in FEED and "onFocusTab" in FEED
    assert "stopPropagation" in FEED                            # чип ≠ сворачивание шагов
    assert "вкладка закрыта" in FEED                            # tabGone-состояние чипа
    assert "function buildCard(doc, task, onFocusTab)" in FEED


def test_sidebar_focus_and_per_task_render():
    assert 'port.postMessage({ type: "tab_focus", task })' in SB
    assert "function renderCardFor(" in SB                      # п.3: карточка на задачу
    assert "Array.isArray(TL.tasks)" in SB                      # легаси-фолбэк снапшота
    assert ".tktab" in CSS and ".tktab.gone" in CSS             # тема токенами, без inline


def test_agents_md_item9():
    assert "SK-25" in AGENTS and "tab_gone" in AGENTS and "tab_moved" in AGENTS
    assert '"task":"<tid>"' in AGENTS


# ---------------------------------------------------------------- agentd п.3 ---

def test_child_envelope_demands_task_param():
    msg = A.build_child_message("sk42-abcd", "сделай")
    assert '"task\\":\\"sk42-abcd' in json.dumps(msg) or '"task\":\"sk42-abcd' in msg
    assert "tab_gone" in msg and "tab_moved" in msg  # п.6: агент знает про стоп-ошибки


def test_agentd_parallel_tasks_contracts():
    src = (ROOT / "agentd/sidekick_agentd.py").read_text(encoding="utf-8")
    assert "MAX_ACTIVE" in src and "SIDEKICK_AGENTD_MAX" in src
    assert 'actives: dict[str, Active] = {}' in src
    # не более одной задачи на чат — сообщение чата с живой задачей «направляет» её
    assert 'a.chat == msg["chat"]' in src
    # курсор — до самого раннего незакрытого сообщения (падение не «съедает» хвост)
    assert "def commit_cursor(" in src
    assert "a.src_end < bound" in src
    # hub/stop — каждому живому потомку (цикл по actives, а не одному active)
    assert "for a in list(actives.values()):" in src


def test_active_carries_own_old_proc():
    a = A.Active("default", "x", 10, "t-old", None)
    assert a.old_proc is None and a.old_since == 0.0
