"""SK-03 contract validator: rpc_in.jsonl -> WS "rpc" frame -> mock extension -> rpc_out.jsonl.

No browser here: the mock extension answers with a canned snapshot (the real snapshot
logic lives in content.js and is verified live on the firefox-158 bench). What this
pins down is the bridge-side contract: routing, no_extension, timeout, versioning,
rpc_result consumption (never inbox), audit + secret masking.
"""
import asyncio
import json
import time
import uuid

import pytest
import websockets

from conftest import kill_bridge, refire_noext, start_bridge, token_of

URI = "ws://127.0.0.1:8799"

CANNED_DOM = {
    "url": "http://127.0.0.1:8899/form.html",
    "title": "Тест-форма",
    "elements": [
        {"role": "textbox", "name": "Email", "selector": "#email", "frame": "",
         "visible": True, "rect": {"x": 10, "y": 60, "w": 200, "h": 22}, "sensitive": False},
        {"role": "textbox", "name": "Пароль", "selector": "#pass", "frame": "",
         "visible": True, "rect": {"x": 10, "y": 100, "w": 200, "h": 22}, "sensitive": True},
    ],
    "truncated": False,
}


def rpc_call(home, method, params=None, rid=None, v: int | None = 1):
    rid = rid or str(uuid.uuid4())
    req = {"id": rid, "method": method, "params": params or {}}
    if v is not None:
        req = {"v": v, **req}
    # conftest.refire_noext перезаряжает кадр при гонке no_extension — копия нужна живее,
    # чем 0.1-секундный drain rpc_in мостом
    with open(home / ".rpc_frames.jsonl", "a") as f:
        f.write(json.dumps(req, ensure_ascii=False) + "\n")
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


async def mock_extension(home, reply=None, respond=True, wait_s=5.0, expect_methods=None):
    """Handshake, wait for one rpc frame; optionally answer it. Returns the frame seen."""
    async with websockets.connect(URI, open_timeout=3) as c:
        await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
        ack = json.loads(await asyncio.wait_for(c.recv(), 3))
        assert ack.get("ok") is True, ack
        deadline = time.time() + wait_s
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(c.recv(), min(0.5, max(0.05, deadline - time.time())))
            except asyncio.TimeoutError:
                refire_noext(home)  # гонка no_extension (см. conftest) — не ломать ожидание
                continue
            msg = json.loads(raw)
            if msg.get("type") != "rpc":
                continue
            if expect_methods and msg.get("method") not in expect_methods:
                continue
            if respond and reply is not None:
                await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                         "ok": True, "result": reply}, ensure_ascii=False))
            return msg
    raise AssertionError("no rpc frame arrived in time")


# pytest-asyncio is not a project dep — drive the loop manually, same style as test_bridge.py.
def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


def test_page_dom_roundtrip(home):
    async def t():
        rid = rpc_call(home, "page.dom")
        frame = await mock_extension(home, reply=CANNED_DOM, expect_methods=["page.dom"])
        assert frame["method"] == "page.dom" and frame["id"] == rid and frame["v"] == 1
        return rid
    rid = run(t())
    out = read_rpc_out(home, rid)
    assert out is not None, "no rpc_out line for the request id"
    assert out["v"] == 1 and out["ok"] is True
    assert out["result"]["elements"][1]["sensitive"] is True
    assert out["result"]["elements"][0]["selector"] == "#email"
    # rpc_result is consumed by the bridge — must NOT leak into inbox.jsonl
    inbox = (home / "inbox.jsonl")
    assert not inbox.exists() or "rpc_result" not in inbox.read_text()
    # every completed rpc is audited
    audit_lines = [json.loads(ln) for ln in (home / "audit.jsonl").read_text().splitlines()]
    assert any(a.get("method") == "page.dom" and a["ok"] is True for a in audit_lines)


def test_no_extension_immediate_error(home):
    rid = rpc_call(home, "page.text")
    out = read_rpc_out(home, rid, timeout=3.0)
    assert out is not None and out["ok"] is False and out["err"] == "no_extension"


def test_rpc_in_requires_version(home):
    rid = rpc_call(home, "page.dom", v=None)
    out = read_rpc_out(home, rid, timeout=3.0)
    assert out is not None and out["ok"] is False and out["err"] == "no_version"
    rid2 = rpc_call(home, "page.dom", v=99)
    out2 = read_rpc_out(home, rid2, timeout=3.0)
    assert out2 is not None and out2["ok"] is False and out2["err"] == "bad_version"


def test_timeout(tmp_path):
    p = start_bridge(tmp_path, env_extra={"SIDEKICK_RPC_TIMEOUT": 1})
    try:
        async def t():
            rid = rpc_call(tmp_path, "page.screenshot")
            await mock_extension(tmp_path, respond=False, wait_s=2.0)  # connects, answers nothing
            return rid
        rid = run(t())
        out = read_rpc_out(tmp_path, rid, timeout=4.0)
        assert out is not None and out["ok"] is False and out["err"] == "timeout"
    finally:
        kill_bridge(p)


def test_audit_masks_secrets(tmp_path):
    p = start_bridge(tmp_path, env_extra={"SIDEKICK_RPC_TIMEOUT": 1})
    try:
        async def t():
            rpc_call(tmp_path, "page.text", params={"token": "supersecret-token"})
            await mock_extension(tmp_path, reply={"text": 'leaked: {"password": "hunter2"}'},
                                 expect_methods=["page.text"], wait_s=2.0)
            return True
        assert run(t())
        deadline = time.time() + 4
        while time.time() < deadline and not (tmp_path / "audit.jsonl").exists():
            time.sleep(0.05)
        audit = (tmp_path / "audit.jsonl").read_text()
        assert "supersecret-token" not in audit and "hunter2" not in audit
        # rpc_out carries the page result verbatim (masking is audit-only)
        rid_line = json.loads((tmp_path / "rpc_out.jsonl").read_text().splitlines()[-1])
        assert rid_line["ok"] is True and "hunter2" in rid_line["result"]["text"]
    finally:
        kill_bridge(p)
