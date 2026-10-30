"""SK-11 contract tests — the audit feed's bridge side.

Two read legs over one shared tail (audit_tail):
  * rpc audit.tail {limit, domain} — bridge-local, agent-side (rpc_in/rpc_out);
  * WS audit_get {id, limit, domain} -> audit_data — the sidebar's pull, consumed
    like stop/resume (never inbox) and NOT audited (the panel polls; auditing the
    poll would feed the audit with itself).
Pinned contracts: tail semantics (limit clamp, whole-file domain filter, total =
file lines), read-time masking incl. the card's «поле value -> ***» and the literal
session token (runbook check: token grep in the tail output = 0), the SK-08 gate
(audit.tail refused during a hand-off, audit_get answered — the human at the
keyboard may read the trail), and the domain column on page.* audit lines.
"""
import asyncio
import json
import os
import time

import pytest
import websockets

from conftest import start_bridge, kill_bridge, token_of
from test_rpc_e2e import read_rpc_out, rpc_call

URI = "ws://127.0.0.1:8799"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


def audit_lines(home):
    p = home / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def append_audit(home, *objs_or_lines):
    """Append raw lines (strings) or dicts (json) straight to audit.jsonl — the
    bridge reads the file on demand, no restart needed."""
    with open(home / "audit.jsonl", "a") as f:
        for o in objs_or_lines:
            f.write((o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)) + "\n")


def tail(home, params=None, timeout=5.0):
    rid = rpc_call(home, "audit.tail", params or {})
    out = read_rpc_out(home, rid, timeout=timeout)
    assert out is not None, "no rpc_out for audit.tail"
    return out


# ---- rpc audit.tail: bridge-local tail ------------------------------------------

def test_audit_tail_returns_last_n_in_order(home):
    base = len(audit_lines(home))               # the bridge audits its startup state
    append_audit(home, *[{"ts": 1792000000 + i, "ev": "state", "state": "idle", "n": i} for i in range(60)])
    out = tail(home, {"limit": 10})
    assert out["ok"] is True, out
    lines = out["result"]["lines"]
    assert len(lines) == 10
    ns = [json.loads(ln)["n"] for ln in lines]
    assert ns == list(range(50, 60))          # the TAIL, chronological
    assert out["result"]["total"] == base + 60
    assert out["result"]["truncated"] is True   # 61 lines in file, 10 shown — honest flag

def test_audit_tail_limit_default_and_clamps(home):
    append_audit(home, *[{"ts": 1, "ev": "state", "state": "idle", "n": i} for i in range(60)])
    assert len(tail(home)["result"]["lines"]) == 50          # default 50
    assert len(tail(home, {"limit": "abc"})["result"]["lines"]) == 50  # garbage -> default
    assert len(tail(home, {"limit": 0})["result"]["lines"]) == 1       # floor 1
    # audit.tail is itself audited (every known rpc is) — count what the file holds NOW
    expect = len(audit_lines(home))
    big = tail(home, {"limit": 999999})
    assert len(big["result"]["lines"]) == expect and big["result"]["truncated"] is False

def test_audit_tail_missing_file_is_empty_not_error(home):
    (home / "audit.jsonl").unlink(missing_ok=True)  # fresh hub before the first event
    out = tail(home)
    assert out["ok"] is True and out["result"] == {"lines": [], "total": 0, "truncated": False}

def test_audit_tail_domain_filter_runs_over_whole_file(home):
    base = len(audit_lines(home))
    # a rare domain buried under 60 later lines must survive the tail window
    append_audit(home, {"ts": 1, "ev": "perm", "domain": "rare.example", "action": "click",
                        "decision": "allow", "via": "grant"})
    append_audit(home, *[{"ts": 2, "method": "page.text", "ok": True, "domain": f"busy{i}.test"}
                         for i in range(60)])
    out = tail(home, {"domain": "rare.example"})
    assert out["ok"] and len(out["result"]["lines"]) == 1
    assert json.loads(out["result"]["lines"][0])["domain"] == "rare.example"
    assert out["result"]["total"] == base + 61                  # completeness counter = file
    # truncated fires when MATCHES exceed the limit
    out2 = tail(home, {"domain": ".test", "limit": 5})
    assert out2["result"]["truncated"] is True and len(out2["result"]["lines"]) == 5
    # case-insensitive substring
    assert len(tail(home, {"domain": "RARE.EX"})["result"]["lines"]) == 1

def test_audit_tail_masks_on_read_including_value_and_token(home):
    tok = token_of(home)
    poison = {"ts": 1, "ev": "perm", "domain": "poison.test", "action": "sensitive_input",
              "decision": "allow", "via": "human_once",
              "value": "hunter2-SUPERSECRET", "password": "p@ssw0rd", "cvv": "123",
              "note": "literal token " + tok + " leaked here"}
    append_audit(home, json.dumps(poison))
    out = tail(home, {"domain": "poison.test"})
    line = out["result"]["lines"][0]
    for secret in ("hunter2-SUPERSECRET", "p@ssw0rd", "123", tok):
        assert secret not in line, f"secret {secret[:6]}... leaked into the feed"
    obj = json.loads(line)
    assert obj["value"] == "***" and obj["password"] == "***" and obj["cvv"] == "***"
    assert tok not in obj["note"]                              # literal token scrubbed
    # the runbook check: grep -c "$(cat hub/token)" over the tail output == 0
    assert tok not in "\n".join(out["result"]["lines"])
    # non-secret fields survive masking (the feed stays useful)
    assert obj["domain"] == "poison.test" and obj["decision"] == "allow"


# ---- SK-08 gate -------------------------------------------------------------------

def test_audit_tail_refused_during_handoff_pause(home):
    (home / "paused").touch()  # the human holds the browser — the trail names their pages
    out = tail(home)
    assert out["ok"] is False and out["err"] == "paused_for_human"
    (home / "paused").unlink()
    assert tail(home)["ok"] is True


# ---- WS audit_get: the sidebar's pull leg ------------------------------------------

async def ws_client(home):
    c = await websockets.connect(URI, open_timeout=3)
    await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
    ack = json.loads(await asyncio.wait_for(c.recv(), 3))
    assert ack.get("ok") is True, ack
    return c


async def recv_until(c, type_, timeout=3.0, except_types=("state",)):
    """Skip the connect-replay frames (state etc.) — answer only the frame we asked for."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(await asyncio.wait_for(c.recv(), max(0.1, deadline - time.time())))
        if msg.get("type") in except_types:
            continue
        assert msg.get("type") == type_, msg
        return msg
    raise AssertionError(f"no {type_} frame in {timeout}s")


def test_ws_audit_get_answers_and_is_consumed(home):
    append_audit(home, {"ts": 5, "ev": "perm", "domain": "ws.example", "action": "click",
                        "decision": "block", "via": "grant_block"})
    async def t():
        c = await ws_client(home)
        async with c:
            await c.send(json.dumps({"v": 1, "type": "audit_get", "id": "q1", "limit": 7,
                                     "domain": "ws.example"}))
            f = await recv_until(c, "audit_data")
            assert f["id"] == "q1" and f["v"] == 1
            assert len(f["lines"]) == 1 and f["total"] >= 2 and f["truncated"] is False
            assert json.loads(f["lines"][0])["domain"] == "ws.example"
            await c.send(json.dumps({"v": 1, "type": "audit_get", "id": "q2", "domain": "WS.EXAMPLE"}))
            f2 = await recv_until(c, "audit_data")
            assert f2["id"] == "q2" and len(f2["lines"]) == 1     # domain filter over WS too
    run(t())
    # consumed like stop/resume: never inbox...
    inbox = home / "inbox.jsonl"
    assert not inbox.exists() or "audit_get" not in inbox.read_text()
    # ...and NOT audited — the panel polls, auditing the poll feeds the audit itself
    time.sleep(0.3)
    assert not any("audit_get" in json.dumps(a) for a in audit_lines(home))

def test_ws_audit_get_works_during_handoff_pause(home):
    # the human at the keyboard is exactly who may want to read the trail —
    # the WS leg answers while the rpc leg (agent-facing) is gated
    (home / "paused").touch()
    append_audit(home, {"ts": 6, "method": "page.text", "ok": True})
    async def t():
        c = await ws_client(home)
        async with c:
            await c.send(json.dumps({"v": 1, "type": "audit_get", "id": "p1",
                                     "domain": "page.text"}))
            f = await recv_until(c, "audit_data")
            assert f["id"] == "p1" and len(f["lines"]) == 1
    run(t())


# ---- domain column on page.* audit lines (the feed's «домен») -----------------------

def test_page_rpc_audit_line_carries_domain_from_result(home):
    async def t():
        rid = rpc_call(home, "page.text")
        async with await ws_client(home) as c:
            deadline = time.time() + 5
            while True:
                msg = json.loads(await asyncio.wait_for(c.recv(), max(0.1, deadline - time.time())))
                if msg.get("type") == "rpc" and msg.get("method") == "page.text":
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"], "ok": True,
                                             "result": {"url": "https://WWW.Example.COM:8899/login",
                                                        "text": "hi"}}, ensure_ascii=False))
                    return
        raise AssertionError("no page.text rpc frame")
    run(t())
    hit = []
    deadline = time.time() + 5
    while time.time() < deadline:
        hit = [a for a in audit_lines(home) if a.get("method") == "page.text"]
        if hit:
            break
        time.sleep(0.05)
    assert hit and hit[-1]["ok"] is True
    assert hit[-1]["domain"] == "www.example.com"   # norm_host: lowercase, no port
    # and the domain filter finds it
    assert len(tail(home, {"domain": "example.com"})["result"]["lines"]) >= 1
