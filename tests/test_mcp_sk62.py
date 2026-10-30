"""SK-62: тесты MCP-адаптера mcp/sidekick_mcp.py (спека docs/mcp-spec.md).

Слои:
  * tools-декларации (21 tool спеки п.6, схемы, обходные пути НЕ выставлены);
  * stdio: отказ без ключа, handshake, rate-limit -32000, EOF закрывает задачу;
  * in-process гейты с моком rpc: sensitive-реестр → авто-perm.check (только по
    доказанному sensitive:true; «для надёжности» не плодится), ask_timeout streak,
    hub/stop-гейт + resume, лента task/step, idle-закрытие;
  * переживание ротации rpc_out — через настоящий sk-путь (importlib-переиспользование);
  * HTTP: 401 bearer (+WWW-Authenticate), Origin 403, сессии 400/404, GET 405,
    notification 202, DELETE 204, rate-limit 429+Retry-After, bad proto header 400;
  * E2E на настоящем мосту :8799 (conftest) + mock-расширение: page_dom →
    sensitive action_do → perm_ask человеку → allow → executed; кадра ленты
    (task/step) доходят до канала сайдбара; EOF закрывает задачу терминалом.
"""
import importlib.util
import json
import os
import pathlib
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MCP_PATH = str(ROOT / "mcp" / "sidekick_mcp.py")
KEY = "ab" * 32  # фиктивный hex-ключ 64 символа — не секрет, тестовый плейсхолдер

spec = importlib.util.spec_from_file_location("sidekick_mcp_sk62", MCP_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


# ------------------------------------------------------------------ helpers ----

def start_stdio(home, env_extra=None):
    env = dict(os.environ, SIDEKICK_MCP_KEY=KEY, SIDEKICK_HOME=str(home),
               SIDEKICK_MCP_RPM="600", SIDEKICK_MCP_BURST="600")
    env.update(env_extra or {})
    p = subprocess.Popen([sys.executable, MCP_PATH], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, text=True, bufsize=1)
    p._q = queue.Queue()

    def pump():
        for ln in p.stdout:
            p._q.put(json.loads(ln))
    threading.Thread(target=pump, daemon=True).start()
    return p


def send(p, obj):
    p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    p.stdin.flush()


def read_msg(p, timeout=25):
    return p._q.get(timeout=timeout)


def initialize(p, name="pytest"):
    send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": name}}})
    r = read_msg(p)
    send(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return r


def call(p, mid, tool, args=None):
    send(p, {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
             "params": {"name": tool, "arguments": args or {}}})
    return read_msg(p)


def content_json(msg):
    """MCP result из JSON-RPC ответа (stdio/HTTP субпроцессы)."""
    txt = msg["result"]["content"][0]["text"]
    return json.loads(txt)


def direct_json(res):
    """call_tool() возвращает MCP result напрямую (in-process)."""
    return json.loads(res["content"][0]["text"])


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_http(home, rpm="600", burst="600", env_extra=None):
    port = free_port()
    env = dict(os.environ, SIDEKICK_MCP_KEY=KEY, SIDEKICK_HOME=str(home),
               SIDEKICK_MCP_PORT=str(port), SIDEKICK_MCP_RPM=rpm, SIDEKICK_MCP_BURST=burst)
    env.update(env_extra or {})
    p = subprocess.Popen([sys.executable, MCP_PATH, "--http"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return p, port
        except OSError:
            time.sleep(0.1)
    p.terminate()
    pytest.fail("http adapter did not start")


def http(port, body=None, headers=None, method="POST", path="/mcp"):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, dict(r.headers), (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            j = json.loads(raw) if raw else None
        except ValueError:
            j = None
        return e.code, dict(e.headers), j


# ------------------------------------------------------------------ tools ----

def test_tools_table_matches_spec():
    names = [t["name"] for t in mod.TOOLS]
    expected = {"bridge_status", "bridge_caps", "events_poll", "session_info",
                "tabs_list", "tabs_open_grouped", "tabs_use_tab", "tabs_activate", "tabs_close",
                "tabs_highlight_group", "tabs_park_group", "page_dom", "page_screenshot",
                "page_text", "page_media", "action_do", "perm_check", "handoff_request",
                "handoff_status", "chat_send", "task_close"}
    assert set(names) == expected and len(names) == 21
    for t in mod.TOOLS:
        assert len(t["name"]) <= 64
        assert t["desc"], f"{t['name']} без контракта в description"
        sch = t["schema"]
        assert sch["type"] == "object"
        assert isinstance(sch.get("required", []), list)
        for r in sch.get("required", []):
            assert r in sch["properties"]
    # таймауты спеки п.6: handoff 615, tabs_close/perm 90, action_do 91
    by = {t["name"]: t for t in mod.TOOLS}
    assert by["handoff_request"]["timeout"] == 615
    assert by["tabs_close"]["timeout"] == 90 and by["perm_check"]["timeout"] == 90
    assert by["action_do"]["timeout"] == 91


def test_no_bypass_surface():
    """Спека п.10: grants.set/grants.get, audit.tail, chats.*, action.preview, hub/token,
    raw rpc passthrough — НЕ выставлены."""
    banned = ("grant", "audit", "chat.list", "chat.get", "preview", "token", "rpc_raw", "raw_rpc")
    for t in mod.TOOLS:
        blob = (t["name"] + " " + str(t.get("method"))).lower()
        for b in banned:
            assert b not in blob, f"{t['name']} несёт обходную поверхность {b!r}"
    # chat_send — это outbox-рамка (пишем человеку), а не chats.list/chats.get моста
    assert mod.TOOLS_BY["chat_send"]["method"] is None
    assert mod.TOOLS_BY["task_close"]["method"] is None


def test_http_uses_constant_time_compare():
    src = pathlib.Path(MCP_PATH).read_text()
    assert "hmac.compare_digest" in src


# ------------------------------------------------------------------- stdio ----

def test_stdio_refuses_without_key(tmp_path):
    env = dict(os.environ)
    env.pop("SIDEKICK_MCP_KEY", None)
    env["SIDEKICK_HOME"] = str(tmp_path)
    p = subprocess.run([sys.executable, MCP_PATH], env=env, input="",
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 2
    assert "SIDEKICK_MCP_KEY" in p.stderr


def test_stdio_refuses_short_key(tmp_path):
    env = dict(os.environ, SIDEKICK_MCP_KEY="deadbeef", SIDEKICK_HOME=str(tmp_path))
    p = subprocess.run([sys.executable, MCP_PATH], env=env, input="",
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 2


def test_stdio_handshake_negotiates_and_lists(tmp_path):
    p = start_stdio(tmp_path)
    try:
        r = initialize(p)
        assert r["result"]["protocolVersion"] == "2025-06-18"
        assert r["result"]["serverInfo"]["name"] == "sidekick-mcp"
        assert len(r["result"]["instructions"]) > 40
        send(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert len(read_msg(p)["result"]["tools"]) == 21
        send(p, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
        assert read_msg(p)["result"] == {}
        send(p, {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        assert read_msg(p)["error"]["code"] == -32601
    finally:
        p.terminate()


def test_stdio_old_protocol_fallback(tmp_path):
    p = start_stdio(tmp_path)
    try:
        send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05"}})
        assert read_msg(p)["result"]["protocolVersion"] == "2024-11-05"
        send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "1999-01-01"}})
        assert read_msg(p)["result"]["protocolVersion"] == "2025-06-18"
    finally:
        p.terminate()


def test_stdio_rate_limit_jsonrpc_error(tmp_path):
    p = start_stdio(tmp_path, env_extra={"SIDEKICK_MCP_RPM": "1", "SIDEKICK_MCP_BURST": "2"})
    try:
        initialize(p)
        for mid in (10, 11):
            r = call(p, mid, "session_info")
            assert "result" in r, r
        r = call(p, 12, "session_info")
        assert r["error"]["code"] == -32000 and "rate_limited" in r["error"]["message"]
    finally:
        p.terminate()


def test_stdio_unknown_tool_is_jsonrpc_32602(tmp_path):
    p = start_stdio(tmp_path)
    try:
        initialize(p)
        r = call(p, 9, "grants_set", {})
        assert r["error"]["code"] == -32602
    finally:
        p.terminate()


# ------------------------------------------- in-process гейты (мок rpc) ----

CANNED_DOM = {
    "url": "http://127.0.0.1:8899/form.html", "title": "Тест-форма",
    "elements": [
        {"sid": "mail16", "role": "textbox", "name": "Email", "selector": "#email",
         "visible": True, "rect": {"x": 10, "y": 60, "w": 200, "h": 22}, "sensitive": False},
        {"sid": "pass17", "role": "textbox", "name": "Пароль", "selector": "#pass",
         "visible": True, "rect": {"x": 10, "y": 100, "w": 200, "h": 22}, "sensitive": True},
    ],
    "truncated": False,
}


@pytest.fixture
def fake_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEKICK_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def mcp_session(fake_hub):
    return mod.Session("pytest-inline")


class RpcMock:
    def __init__(self, replies):
        self.calls = []
        self.replies = replies  # method -> dict|callable

    def __call__(self, method, params, timeout=None):
        self.calls.append((method, dict(params)))
        r = self.replies.get(method, {"ok": True, "result": {}})
        return r(params) if callable(r) else json.loads(json.dumps(r))

    def methods(self):
        return [m for m, _ in self.calls]


def test_sensitive_registry_autoperm(fake_hub, mcp_session, monkeypatch):
    """Спека п.8.2: input по ДОКАЗАННОЙ sensitive-цели → адаптер сам шлёт perm.check;
    по нечувствительной — пермисс никогда не изобретается (AGENTS п.2)."""
    m = RpcMock({
        "page.dom": {"ok": True, "result": CANNED_DOM},
        "perm.check": {"ok": True, "result": {"decision": "allow", "via": "human"}},
        "action.do": {"ok": True, "result": {"done": True, "executed": True,
                                             "shown": True, "cancelled": False}},
    })
    monkeypatch.setattr(mod, "rpc", m)
    r = mod.call_tool(mcp_session, "page_dom", {})
    assert not r["isError"]
    assert mcp_session.sensitive == {"sel:#pass", "sid:pass17"}
    # sensitive по sid: сначала perm.check, потом action.do
    m.calls.clear()
    r = mod.call_tool(mcp_session, "action_do", {"sid": "pass17", "action": "input", "value": "x"})
    assert not r["isError"]
    assert m.methods() == ["perm.check", "action.do"]
    pm = dict(m.calls[0][1])
    assert pm["perm"] == "sensitive_input" and pm["action"] == "input"
    assert pm["task"] == mcp_session.tid
    assert pm["url"] == CANNED_DOM["url"] and pm["domain"] == "127.0.0.1"
    # SoM-карта несёт тот же sid: marks-карта заполняет тот же реестр (спека п.8.2)
    scr = {"url": CANNED_DOM["url"],
           "marks": [{"n": 3, "sid": "pass17", "selector": "#pass",
                      "tag": "input", "name": "Пароль", "rect": [1, 2, 3, 4]}]}
    mod.reg_update_from_marks(mcp_session, scr)
    m.calls.clear()
    mod.call_tool(mcp_session, "action_do", {"mark": 3, "action": "input", "value": "y"})
    assert m.methods() == ["perm.check", "action.do"]
    # нечувствительная цель — никакого perm.check
    m.calls.clear()
    mod.call_tool(mcp_session, "action_do", {"selector": "#email", "action": "input", "value": "a"})
    assert m.methods() == ["action.do"]
    # click по sensitive — не input/submit, пермисса нет (гейт превью внутри action.do)
    m.calls.clear()
    mod.call_tool(mcp_session, "action_do", {"selector": "#pass", "action": "click"})
    assert m.methods() == ["action.do"]


def test_sensitive_block_and_ask_timeout_streak(fake_hub, mcp_session, monkeypatch):
    """block — действие НЕ исполняется (успешный вердикт); два ask_timeout подряд —
    третий вопрос адаптер не плодит (эмуляция ask_timeout_streak, AGENTS п.2 SK-34)."""
    n = {"i": 0}

    def perm(params):
        n["i"] += 1
        return {"ok": True, "result": {"decision": "block", "via": "ask_timeout"}}

    m = RpcMock({"page.dom": {"ok": True, "result": CANNED_DOM}, "perm.check": perm,
                 "action.do": {"ok": True, "result": {"executed": True}}})
    monkeypatch.setattr(mod, "rpc", m)
    mod.call_tool(mcp_session, "page_dom", {})
    m.calls.clear()
    r1 = mod.call_tool(mcp_session, "action_do", {"selector": "#pass", "action": "input", "value": "x"})
    r2 = mod.call_tool(mcp_session, "action_do", {"selector": "#pass", "action": "input", "value": "x"})
    r3 = mod.call_tool(mcp_session, "action_do", {"selector": "#pass", "action": "input", "value": "x"})
    assert [mm for mm, _ in m.calls] == ["perm.check", "perm.check"]  # третий — локальный block
    for r in (r1, r2):
        gj = direct_json(r)
        assert gj["gate"] == "adapter_sensitive_perm" and gj["decision"] == "block"
        assert not r["isError"]  # отказ гейта — успешный ответ, не ошибка
    gj3 = direct_json(r3)
    assert gj3["via"] == "ask_timeout_streak_adapter"
    assert all(mm != "action.do" for mm, _ in m.calls)  # действие не исполнялось никогда


def test_stop_gate_and_resume(fake_hub, mcp_session, monkeypatch):
    """hub/stop гейтит браузерные вызовы; resume-событие из events.jsonl снимает."""
    m = RpcMock({"page.text": {"ok": True, "result": {"text": "hi"}}})
    monkeypatch.setattr(mod, "rpc", m)
    (fake_hub / "stop").touch()
    r = mod.call_tool(mcp_session, "page_text", {})
    assert r["isError"]
    assert "Stop" in r["content"][0]["text"]
    assert m.calls == []  # мост не тронут
    with open(fake_hub / "events.jsonl", "a") as f:
        f.write(json.dumps({"type": "resume", "task": mcp_session.tid,
                            "ts": int(time.time())}) + "\n")
    r = mod.call_tool(mcp_session, "page_text", {})
    assert not r["isError"]
    assert [mm for mm, _ in m.calls] == ["page.text"]
    assert not (fake_hub / "stop").exists()  # «получил — удалить файл и продолжить»


def test_task_feed_and_chat_frames(fake_hub, mcp_session, monkeypatch):
    m = RpcMock({"page.text": {"ok": True, "result": {"text": "ok"}}})
    monkeypatch.setattr(mod, "rpc", m)
    mod.call_tool(mcp_session, "page_text", {})
    mod.call_tool(mcp_session, "chat_send", {"text": "проверь верстку", "chat": "mcp"})
    frames = [json.loads(ln) for ln in (fake_hub / "outbox.jsonl").read_text().splitlines()]
    types = [(f["type"], f.get("state")) for f in frames]
    assert ("task", "running") in types
    assert ("step", "active") in types and ("step", "done") in types
    assert ("chat", None) in types
    step = [f for f in frames if f["type"] == "step"][0]
    assert step["task"] == mcp_session.tid and step["n"] == 1
    assert step["text"] == "читаю текст страницы"
    task = [f for f in frames if f["type"] == "task"][0]
    assert task["title"].startswith("MCP · pytest") and task.get("long") is None
    # task_close — терминальный кадр
    r = mod.call_tool(mcp_session, "task_close", {"state": "done"})
    assert not r["isError"] and mcp_session.task_open is False
    frames = [json.loads(ln) for ln in (fake_hub / "outbox.jsonl").read_text().splitlines()]
    assert ("task", "done") in [(f["type"], f.get("state")) for f in frames]


def test_paused_for_human_is_not_error(fake_hub, mcp_session, monkeypatch):
    """hub/paused: мост отвечает paused_for_human — адаптер транслирует как успешный
    вердикт-паузу (спека п.6: isError:false)."""
    monkeypatch.setattr(mod, "rpc", lambda meth, p, timeout=None:
                        {"ok": False, "err": "paused_for_human", "id": "x",
                         "hint": "рука человека: верни resumed"})
    r = mod.call_tool(mcp_session, "page_dom", {})
    assert r["isError"] is False
    assert direct_json(r)["err"] == "paused_for_human"


def test_idle_closes_task(fake_hub, mcp_session, monkeypatch):
    m = RpcMock({"page.text": {"ok": True, "result": {}}})
    monkeypatch.setattr(mod, "rpc", m)
    monkeypatch.setitem(mod.CFG, "TASK_IDLE_S", -1.0)
    mod.register(mcp_session)
    try:
        mod.call_tool(mcp_session, "page_text", {})
        assert mcp_session.task_open
        mod.idle_pass()
    finally:
        mod.unregister(mcp_session)
    assert mcp_session.task_open is False
    frames = [json.loads(ln) for ln in (fake_hub / "outbox.jsonl").read_text().splitlines()]
    assert ("task", "done") in [(f["type"], f.get("state")) for f in frames]


def test_rpc_out_rotation_survives(fake_hub):
    """Настоящий sk-путь: мост ротирует rpc_out во время ожидания — вердикт из
    свежего файла находится (переживание ротации inode, sk-семантика)."""
    def responder(hub, rid_seen):
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                lines = (hub / "rpc_in.jsonl").read_text().splitlines()
            except FileNotFoundError:
                time.sleep(0.05)
                continue
            if lines:
                frame = json.loads(lines[-1])
                rid_seen.append(frame["id"])
                time.sleep(0.1)
                # ротация: старый fd остаётся под наблюдением, вердикт — в свежем
                os.rename(hub / "rpc_out.jsonl", hub / "rpc_out.jsonl.1")
                with open(hub / "rpc_out.jsonl", "w") as f:
                    f.write(json.dumps({"v": 1, "id": "decoy", "ok": True,
                                        "result": {"x": 0}}) + "\n")
                    f.write(json.dumps({"v": 1, "id": frame["id"], "ok": True,
                                        "result": {"text": "из-под ротации"}}) + "\n")
                return
            time.sleep(0.05)

    seen = []
    th = threading.Thread(target=responder, args=(fake_hub, seen))
    th.start()
    v = mod.rpc("page.text", {}, timeout=6)
    th.join(5)
    assert v.get("ok") is True
    assert v["result"]["text"] == "из-под ротации"


# ------------------------------------------------------------------- HTTP ----

def test_http_auth_sessions_origin_methods(tmp_path):
    p, port = start_http(tmp_path)
    auth = {"Authorization": f"Bearer {KEY}"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "pytest-http"}}}
    try:
        code, hdr, body = http(port, init)
        assert code == 401 and hdr.get("WWW-Authenticate") == "Bearer"
        assert body["error"]["code"] == -32001
        code, hdr, body = http(port, init, {"Authorization": "Bearer " + "de" * 32})
        assert code == 401  # неверный ключ той же длины — constant-time сравнение
        code, hdr, body = http(port, init, auth)
        assert code == 200 and body["result"]["protocolVersion"] == "2025-06-18"
        sid = hdr.get("Mcp-Session-Id")
        assert sid and len(sid) >= 8
        # без сессии — 400; мёртвая — 404
        code, _, _ = http(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, auth)
        assert code == 400
        code, _, _ = http(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                          {**auth, "Mcp-Session-Id": "deadbeef" * 2})
        assert code == 404
        h2 = {**auth, "Mcp-Session-Id": sid}
        code, hdr, body = http(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, h2)
        assert code == 200 and len(body["result"]["tools"]) == 21
        # Origin: allowlist (дефолт chatgpt.com — ок, evil — 403)
        code, _, _ = http(port, init, {**auth, "Origin": "https://evil.example"})
        assert code == 403
        code, _, _ = http(port, init, {**auth, "Origin": "https://chatgpt.com"})
        assert code == 200
        # заголовок MCP-Protocol-Version: куку — 400
        code, _, _ = http(port, {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                          {**h2, "MCP-Protocol-Version": "1999-01-01"})
        assert code == 400
        # GET → 405 (SSE-поток не эмитим); notification → 202
        code, hdr, _ = http(port, method="GET", headers=auth)
        assert code == 405 and "POST" in hdr.get("Allow", "")
        code, _, _ = http(port, {"jsonrpc": "2.0", "method": "notifications/initialized"}, h2)
        assert code == 202
        # DELETE закрывает сессию (204), дальше — 404
        code, _, _ = http(port, method="DELETE", headers=h2)
        assert code == 204
        code, _, _ = http(port, {"jsonrpc": "2.0", "id": 4, "method": "ping"}, h2)
        assert code == 404
    finally:
        p.terminate()


def test_http_rate_limit_429(tmp_path):
    p, port = start_http(tmp_path, rpm="1", burst="2")
    auth = {"Authorization": f"Bearer {KEY}"}
    try:
        code, hdr, body = http(port, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "2025-06-18"}}, auth)
        sid = hdr.get("Mcp-Session-Id")
        h = {**auth, "Mcp-Session-Id": sid}
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "session_info", "arguments": {}}}
        assert http(port, call, h)[0] == 200
        assert http(port, call, h)[0] == 200
        code, hdr, body = http(port, call, h)
        assert code == 429 and int(hdr.get("Retry-After", "0")) >= 1
        assert body["error"]["code"] == -32000
    finally:
        p.terminate()


# ------------------------------- E2E на настоящем мосту :8799 + мок-расширение ----

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from conftest import kill_bridge, start_bridge  # noqa: E402

CANNED_ACTION = {"done": True, "executed": True, "action": "input", "synthetic": True,
                 "shown": True, "cancelled": False, "url": CANNED_DOM["url"],
                 "hit": {"tag": "input", "text": ""},
                 "post_state": {"before": None, "after": None, "changed": False}}


def test_e2e_real_bridge_sensitive_and_observability(tmp_path):
    import asyncio
    import websockets

    br = start_bridge(tmp_path)
    collected, rpc_seen, perm_seen = [], [], []
    stop = threading.Event()

    async def client():
        async with websockets.connect("ws://127.0.0.1:8799", open_timeout=5) as c:
            await c.send(json.dumps({"v": 1, "type": "hello",
                                     "token": (tmp_path / "token").read_text().strip()}))
            assert json.loads(await asyncio.wait_for(c.recv(), 5))["ok"] is True
            ready.set()
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(c.recv(), 0.3)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "rpc":
                    meth = msg.get("method")
                    rpc_seen.append(meth)
                    result = CANNED_DOM if meth == "page.dom" else CANNED_ACTION
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                             "ok": True, "result": result}))
                elif t == "perm_ask":
                    perm_seen.append(msg)
                    await c.send(json.dumps({"v": 1, "type": "perm_answer",
                                             "id": msg["id"], "choice": "once"}))
                elif t in ("task", "step", "chat"):
                    collected.append(msg)

    def serve():
        asyncio.run(client())

    ready = threading.Event()
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    assert ready.wait(8), "mock extension не подключился"
    p = start_stdio(tmp_path, env_extra={"SIDEKICK_MCP_CLIENT_NAME": "pytest"})
    try:
        r = initialize(p, name="pytest-e2e")
        assert r["result"]["protocolVersion"] == "2025-06-18"
        send(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert len(read_msg(p)["result"]["tools"]) == 21

        # 1) page_dom через настоящий мост → расширение: сид sensitive в реестре адаптера
        r = call(p, 3, "page_dom")
        assert not r["result"]["isError"], r
        dom = content_json(r)
        assert dom["elements"][1]["sensitive"] is True and dom["elements"][1]["sid"] == "pass17"
        assert "page.dom" in rpc_seen

        # 2) input по sensitive-сиду: адаптер САМ заводит perm.check → человеку
        # уходит perm_ask, мок отвечает «once» → bridge-матрица allow → действие.
        r = call(p, 4, "action_do", {"sid": "pass17", "action": "input", "value": "x"})
        assert not r["result"]["isError"], r
        gj = content_json(r)
        assert gj.get("executed") is True, gj
        assert len(perm_seen) == 1 and perm_seen[0]["action"] == "sensitive_input"
        assert perm_seen[0]["domain"] == "127.0.0.1"

        # 3) input по НЕчувствительной цели — никакого нового пермисса
        r = call(p, 5, "action_do", {"selector": "#email", "action": "input", "value": "a"})
        assert not r["result"]["isError"], r
        assert len(perm_seen) == 1

        # 4) человекосооблюдаемость: кадры ленты долетают до WS-канала сайдбара
        call(p, 6, "chat_send", {"text": "проверь верстку"})

        # 5) EOF закрывает задачу терминальным кадром (спека п.8.1, шаг 5):
        # задача после page_dom открыта — закрытие stdin даёт ("task","done")
        p.stdin.close()
        assert p.wait(15) == 0
        deadline = time.time() + 6
        while time.time() < deadline:
            types = [(f["type"], f.get("state")) for f in collected]
            if ("task", "running") in types and ("step", "done") in types \
                    and "chat" in [t for t, _ in types] and ("task", "done") in types:
                break
            time.sleep(0.1)
        types = [(f["type"], f.get("state")) for f in collected]
        assert ("task", "running") in types
        assert ("step", "active") in types and ("step", "done") in types
        assert "chat" in [t for t, _ in types]
        assert ("task", "done") in types, f"EOF не закрыл задачу: {types}"
        tasks = [f for f in collected if f["type"] == "task"]
        assert tasks[0]["title"].startswith("MCP · pytest")
    finally:
        stop.set()
        if p.poll() is None:
            p.terminate()
        br.terminate()
