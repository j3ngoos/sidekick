"""SK-32 контракты: tabs.list / tabs.close / tabs.activate в мосту.

Мост (живой, mock-расширение): инвентаризация с пометкой protected/task; валидация
закрытия локально (bad_request и без браузера); защищённые вкладки SK-25 — отказ до
переспроса, force — пропускает; ОДНО человеческое разрешение на пачку (perm_ask
close_tabs: once -> закрытие уходит на rid агента, deny -> успешный {denied:true},
always -> grant «вкладки» и дальше без переспроса, ask_timeout -> denied);
tabs.activate: tabId / task-резолв (dead -> tab_gone, цели нет -> bad_request);
PAUSED-гейт SK-08 на всех трёх; аудит (method-строки + ev:perm).
Статика: роутинг расширения, AGENTS.md п.6 (три глагола + запрет обхода),
русские подписи сайдбара, документация контракта в шапке моста.
"""
import asyncio
import json
import pathlib
import time
import uuid

import pytest
import websockets

from conftest import kill_bridge, start_bridge, token_of

ROOT = pathlib.Path(__file__).resolve().parent.parent
URI = "ws://127.0.0.1:8799"

BG = (ROOT / "extension/background.js").read_text(encoding="utf-8")
SB = (ROOT / "extension/sidebar.js").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
BRIDGE = (ROOT / "bridge/sidekick_bridge.py").read_text(encoding="utf-8")


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


def audit_lines(home):
    out = []
    try:
        for ln in (home / "audit.jsonl").read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    except FileNotFoundError:
        pass
    return out


class ExtSession:
    """Mock-расширение: rpc-рамки + perm_ask-рамки, ответы по выбору теста."""

    def __init__(self, home):
        self.home = home

    async def __aenter__(self):
        self.c = await websockets.connect(URI, open_timeout=3)
        await self.c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(self.home)}))
        ack = json.loads(await asyncio.wait_for(self.c.recv(), 3))
        assert ack.get("ok") is True, ack
        return self

    async def __aexit__(self, *a):
        await self.c.close()

    async def recv(self, timeout=1.0):
        try:
            return json.loads(await asyncio.wait_for(self.c.recv(), timeout))
        except asyncio.TimeoutError:
            return None

    async def wait_rpc(self, method=None, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = await self.recv(max(0.05, deadline - time.time()))
            if msg is None:
                break
            if msg.get("type") == "rpc" and (not method or msg.get("method") == method):
                return msg
        raise AssertionError(f"no rpc frame ({method or 'any'}) in time")

    async def wait_frame(self, ptype, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = await self.recv(max(0.05, deadline - time.time()))
            if msg is None:
                break
            if msg.get("type") == ptype:
                return msg
        return None

    async def reply(self, rid, ok=True, result=None, err=None):
        frame = {"v": 1, "type": "rpc_result", "id": rid, "ok": ok}
        if ok:
            frame["result"] = result or {}
        else:
            frame["err"] = err or "err"
        await self.c.send(json.dumps(frame, ensure_ascii=False))

    async def call(self, home, method, params, respond=None, err=None, timeout=5.0):
        rid = rpc_call(home, method, params)
        frame = await self.wait_rpc(timeout=timeout)
        assert frame["method"] == method, frame
        if respond is not None or err is not None:
            await self.reply(frame["id"], ok=err is None, result=respond, err=err)
        return frame, rid

    async def perm_answer(self, ask_id, choice):
        await self.c.send(json.dumps({"v": 1, "type": "perm_answer",
                                      "id": ask_id, "choice": choice}))

    async def bind(self, home, task, tab_id, url="http://x/"):
        """open_grouped {task} -> ответ с tabId: задача привязана к вкладке."""
        await self.call(home, "tabs.open_grouped", {"task": task, "url": url},
                        respond={"tabId": tab_id, "windowId": 1, "url": url, "grouped": True})
        return tab_id


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


def run(coro):
    return asyncio.run(coro)


async def no_close_leg(ext, secs=1.2):
    """Ни одна rpc-рамка tabs.close не прошла (state/task_tab — не в счёт)."""
    deadline = time.time() + secs
    while time.time() < deadline:
        msg = await ext.recv(max(0.05, deadline - time.time()))
        if msg and msg.get("type") == "rpc" and msg.get("method") == "tabs.close":
            return False
    return True


# ------------------------------------------------------------- tabs.list -----

def test_tabs_list_forwards_and_annotates_protected(home):
    async def scenario():
        async with ExtSession(home) as ext:
            await ext.bind(home, "T1", 5)
            rid = rpc_call(home, "tabs.list", {})
            frame = await ext.wait_rpc("tabs.list")
            await ext.reply(frame["id"], result={"tabs": [
                {"tabId": 5, "url": "http://x/", "title": "A", "active": True,
                 "pinned": False, "groupId": None, "windowId": 1, "index": 0},
                {"tabId": 6, "url": "http://y/", "title": "B", "active": False,
                 "pinned": False, "groupId": 3, "windowId": 1, "index": 1}]})
            out = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid)
            assert out and out.get("ok"), out
            tabs = {t["tabId"]: t for t in out["result"]["tabs"]}
            assert tabs[5].get("protected") is True and tabs[5].get("task") == "T1"
            assert "protected" not in tabs[6] and "task" not in tabs[6]
    run(scenario())


def test_tabs_list_no_annotation_without_bindings(home):
    async def scenario():
        async with ExtSession(home) as ext:
            rid = rpc_call(home, "tabs.list", {})
            frame = await ext.wait_rpc("tabs.list")
            await ext.reply(frame["id"], result={"tabs": [{"tabId": 9}]})
            out = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid)
            assert out.get("ok") and "protected" not in out["result"]["tabs"][0]
    run(scenario())


# ------------------------------------------------------- tabs.close: валид. --

@pytest.mark.parametrize("params", [
    {},                                             # цели нет
    {"tabIds": []},                                 # пустая пачка
    {"tabId": 3, "tabIds": [3, 4]},                 # оба поля — неоднозначно
    {"tabIds": ["3"]},                              # не целые
    {"tabIds": [3, True]},                          # bool — не int
    {"tabIds": [-1]},                               # отрицательный
    {"tabIds": list(range(100))},                   # кап TABS_CLOSE_MAX
    {"tabId": 1, "force": "yes"},                   # force — только bool
    {"tabId": 1, "force": 1},
])
def test_tabs_close_bad_request_local(home, params):
    """Валидация — локально: bad_request и без расширения (порядок в dispatch)."""
    rid = rpc_call(home, "tabs.close", params)
    out = read_rpc_out(home, rid)
    assert out and not out.get("ok") and out.get("err") == "bad_request", out


def test_tabs_close_no_extension(home):
    rid = rpc_call(home, "tabs.close", {"tabIds": [1, 2]})
    out = read_rpc_out(home, rid)
    assert out and not out.get("ok") and out.get("err") == "no_extension", out


# ------------------------------------------------ tabs.close: гейт человека --

def test_tabs_close_once_forwards_deduped_on_agent_rid(home):
    async def scenario():
        async with ExtSession(home) as ext:
            rid = rpc_call(home, "tabs.close", {"tabIds": [7, 7, 8], "hint": "мусор после задачи"})
            ask = await ext.wait_frame("perm_ask")
            assert ask and ask["action"] == "close_tabs", ask
            assert ask["domain"] == "вкладки"
            assert "мусор после задачи" in ask.get("hint", "")
            assert ask["id"] == rid  # переспрашиваем под rid агента: ответ — результат
            await ext.perm_answer(rid, "once")
            frame = await ext.wait_rpc("tabs.close")
            assert frame["id"] == rid, "закрытие уходит на rid агента, не отдельным легам"
            assert frame["params"]["tabIds"] == [7, 8], frame["params"]  # дедуп на мосту
            assert "force" not in frame["params"]
            await ext.reply(frame["id"], result={"closed": [7, 8], "skipped": [], "force": False})
            out = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid)
            assert out and out.get("ok") and out["result"]["closed"] == [7, 8], out
            # аудит: method-строка tabs.close + перм-вердикт человека
            lines = audit_lines(home)
            assert any(l.get("method") == "tabs.close" and l.get("ok") for l in lines)
            assert any(l.get("ev") == "perm" and l.get("action") == "close_tabs"
                       and l.get("decision") == "allow" and l.get("via") == "human_once"
                       for l in lines)
    run(scenario())


def test_tabs_close_hint_default_lists_count(home):
    async def scenario():
        async with ExtSession(home) as ext:
            rpc_call(home, "tabs.close", {"tabIds": [1, 2, 3]})
            ask = await ext.wait_frame("perm_ask")
            assert ask and "3" in ask.get("hint", ""), ask
    run(scenario())


def test_tabs_close_deny_is_successful_denied_answer(home):
    async def scenario():
        async with ExtSession(home) as ext:
            rid = rpc_call(home, "tabs.close", {"tabIds": [4, 5]})
            ask = await ext.wait_frame("perm_ask")
            await ext.perm_answer(ask["id"], "deny")
            out = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid)
            assert out and out.get("ok"), out  # человеческое «нет» — не ошибка rpc
            assert out["result"] == {"closed": [], "denied": True, "reason": "user_denied"}
            # закрытие НЕ должно уйти расширению (state-рамки не в счёт)
            assert await no_close_leg(ext), "после deny лег закрытия всё же ушёл"
    run(scenario())


def test_tabs_close_always_grants_and_skips_next_ask(home):
    async def scenario():
        async with ExtSession(home) as ext:
            rid = rpc_call(home, "tabs.close", {"tabId": 11})
            ask = await ext.wait_frame("perm_ask")
            await ext.perm_answer(ask["id"], "always")
            frame = await ext.wait_rpc("tabs.close")
            assert frame["params"] == {"tabIds": [11]}, frame["params"]
            await ext.reply(frame["id"], result={"closed": [11], "skipped": []})
            out = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid)
            assert out.get("ok") and out["result"]["closed"] == [11]
            grants = json.loads((home / "grants.json").read_text())
            assert grants.get("вкладки", {}).get("level") == "allow"
            # второй вызов — без переспроса (grant), сразу лег закрытия
            rid2 = rpc_call(home, "tabs.close", {"tabIds": [12]})
            frame2 = await ext.wait_rpc("tabs.close")
            assert frame2["id"] == rid2
            await ext.reply(frame2["id"], result={"closed": [12], "skipped": []})
            out2 = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid2)
            assert out2.get("ok") and out2["result"]["closed"] == [12]
    run(scenario())


def test_tabs_close_ask_timeout_denies(tmp_path):
    p = start_bridge(tmp_path, {"SIDEKICK_PERM_TIMEOUT": "1"})
    try:
        async def scenario():
            async with ExtSession(tmp_path) as ext:
                rid = rpc_call(tmp_path, "tabs.close", {"tabIds": [1]})
                ask = await ext.wait_frame("perm_ask")
                assert ask, "perm_ask не пришёл"
                out = await asyncio.get_event_loop().run_in_executor(
                    None, read_rpc_out, tmp_path, rid, 5.0)
                assert out and out.get("ok"), out
                assert out["result"]["denied"] is True
                assert out["result"]["reason"] == "ask_timeout", out
                assert await no_close_leg(ext, 0.5), "после ask_timeout закрытие всё же ушло"
        run(scenario())
    finally:
        kill_bridge(p)


# ------------------------------------------------ tabs.close: защита SK-25 ---

def test_tabs_close_protected_refused_before_ask(home):
    async def scenario():
        async with ExtSession(home) as ext:
            await ext.bind(home, "T9", 42)
            rid = rpc_call(home, "tabs.close", {"tabIds": [40, 42, 43]})
            out = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid)
            assert out and not out.get("ok"), out
            assert str(out["err"]).startswith("protected_tab") and "42" in out["err"], out
            # отказ ДО переспроса: ни perm_ask-вердикта в действии, ни лега закрытия
            assert await no_close_leg(ext), "защищённая вкладка всё равно ушла на закрытие"
    run(scenario())


def test_tabs_close_force_passes_protection(home):
    async def scenario():
        async with ExtSession(home) as ext:
            await ext.bind(home, "T9", 42)
            rid = rpc_call(home, "tabs.close", {"tabIds": [42], "force": True})
            ask = await ext.wait_frame("perm_ask")
            assert ask, "force снимает только защиту SK-25, не разрешение человека"
            await ext.perm_answer(ask["id"], "once")
            frame = await ext.wait_rpc("tabs.close")
            assert frame["params"] == {"tabIds": [42], "force": True}, frame["params"]
            await ext.reply(frame["id"], result={"closed": [42], "skipped": [], "force": True})
            out = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid)
            assert out.get("ok") and out["result"]["closed"] == [42]
    run(scenario())


def test_tabs_close_dead_binding_not_protected(home):
    """tab_gone роняет биндинг — вкладка (если ещё жива для браузера) перестаёт быть
    защищённой: задача мертва, умирать вместе с ней её вкладке уже не зачем."""
    async def scenario():
        async with ExtSession(home) as ext:
            await ext.bind(home, "TD", 77)
            # вкладки нет: page.text отвечает tab_gone -> биндинг мёртв
            frame, _ = await ext.call(home, "page.text", {"task": "TD"},
                                      err="tab_gone: закрыта")
            rid = rpc_call(home, "tabs.close", {"tabIds": [77]})
            ask = await ext.wait_frame("perm_ask")
            assert ask, "мёртвый биндинг не должен защищать вкладку"
            await ext.perm_answer(ask["id"], "once")
            f2 = await ext.wait_rpc("tabs.close")
            await ext.reply(f2["id"], result={"closed": [77], "skipped": []})
            out = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid)
            assert out.get("ok") and out["result"]["closed"] == [77]
    run(scenario())


# ---------------------------------------------------------- tabs.activate ----

def test_tabs_activate_tabid_forwards(home):
    async def scenario():
        async with ExtSession(home) as ext:
            frame, rid = await ext.call(home, "tabs.activate", {"tabId": 8},
                                        respond={"activated": True, "tabId": 8,
                                                 "windowId": 1, "url": "http://z/",
                                                 "title": "Z"})
            assert frame["params"]["tabId"] == 8
            out = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid)
            assert out.get("ok") and out["result"]["activated"] is True
    run(scenario())


def test_tabs_activate_task_binding_injected(home):
    async def scenario():
        async with ExtSession(home) as ext:
            await ext.bind(home, "TA", 33)
            frame, rid = await ext.call(home, "tabs.activate", {"task": "TA"},
                                        respond={"activated": True, "tabId": 33})
            assert frame["params"]["tabId"] == 33, "вкладка задачи инжектируется мостом"
    run(scenario())


def test_tabs_activate_dead_task_tab_gone(home):
    async def scenario():
        async with ExtSession(home) as ext:
            await ext.bind(home, "TD", 77)
            await ext.call(home, "page.text", {"task": "TD"}, err="tab_gone: закрыта")
            rid = rpc_call(home, "tabs.activate", {"task": "TD"})
            out = await asyncio.get_event_loop().run_in_executor(
                None, read_rpc_out, home, rid)
            assert out and not out.get("ok") and str(out["err"]).startswith("tab_gone"), out
    run(scenario())


@pytest.mark.parametrize("params", [
    {},                          # ни tabId, ни task — активную не угадываем
    {"task": "nope"},            # задача без вкладки
    {"tabId": "3"},
    {"tabId": True},
    {"tabId": -2},
])
def test_tabs_activate_bad_request(home, params):
    rid = rpc_call(home, "tabs.activate", params)
    out = read_rpc_out(home, rid)
    assert out and not out.get("ok") and out.get("err") == "bad_request", out


# ------------------------------------------------------------- PAUSED гейт ---

@pytest.mark.parametrize("method,params", [
    ("tabs.list", {}),
    ("tabs.close", {"tabIds": [1]}),
    ("tabs.activate", {"tabId": 1}),
])
def test_tabs_methods_refused_during_handoff(home, method, params):
    """SK-08: рука у человека — вкладочные глаголы отвечают paused_for_human и пишут
    ev:handoff_block (аудит доказывает, что в браузер ничего не ушло)."""
    (home / "paused").touch()
    rid = rpc_call(home, method, params)
    out = read_rpc_out(home, rid)
    assert out and not out.get("ok") and out.get("err") == "paused_for_human", out
    assert any(l.get("ev") == "handoff_block" and l.get("method") == method
               for l in audit_lines(home))


# ----------------------------------------------------------------- статика ---

def test_extension_routes_tab_methods():
    for m in ("tabs.list", "tabs.close", "tabs.activate"):
        assert f'method === "{m}"' in BG, m
    for fn in ("async function tabsList", "async function tabsClose", "async function tabsActivate"):
        assert fn in BG, fn
    assert "last_in_window" in BG  # последняя вкладка окна без force не закрывается
    assert "browser.tabs.remove" in BG


def test_agents_md_documents_tabs_and_bypass_ban():
    p6 = AGENTS.split("7. **Исполнение через мост")[0]
    for needle in ("tabs.list", "tabs.close", "tabs.activate", "protected_tab",
                   "last_in_window", "denied", "Обход моста запрещён",
                   "CDP", "Marionette", "selenium", "geckodriver"):
        assert needle in p6, needle


def test_sidebar_russian_labels_for_close_tabs():
    assert "close_tabs" in SB
    assert "закрыть вкладки" in SB


def test_bridge_contract_documents_sk32():
    head = BRIDGE.split("import asyncio", 1)[0]
    for needle in ("tabs.list", "tabs.close", "tabs.activate", "protected",
                   "TABS_CLOSE_MAX", "ask_timeout", "last_in_window"):
        assert needle in head, needle
