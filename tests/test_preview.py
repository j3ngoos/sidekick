"""SK-05 contract validator: pre-action preview over the RPC bus.

Bridge-side pins (the ring/Stop UI itself is verified live on the firefox-158 bench):
  * action.preview is forwarded to the extension like page.* (params pass through);
  * the verdict is a SUCCESSFUL rpc even when cancelled — the agent decides, the
    bridge never swallows or reinterprets it:
      {shown:true, cancelled:false, held_ms}      -> rpc_out ok:true, same shape;
      {shown:true, cancelled:true, reason:...}    -> rpc_out ok:true cancelled:true;
  * every verdict lands in audit.jsonl as {ev:"preview", shown, cancelled, reason?};
  * no_extension immediate when no client; v mandatory; timeout via SIDEKICK_RPC_TIMEOUT.
"""
import asyncio
import json
import time

import pytest
import websockets

from conftest import kill_bridge, refire_noext, start_bridge, token_of
from test_rpc_e2e import read_rpc_out, rpc_call

URI = "ws://127.0.0.1:8799"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def mock_preview(home, verdict, seen=None):
    """One-shot mock extension: answer the next action.preview with the given verdict."""
    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            ack = json.loads(await asyncio.wait_for(c.recv(), 3))
            assert ack.get("ok") is True, ack
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(c.recv(), min(0.5, max(0.05, deadline - time.time())))
                except asyncio.TimeoutError:
                    refire_noext(home, "action.preview")  # гонка no_extension (см. conftest)
                    continue
                msg = json.loads(raw)
                if msg.get("type") == "rpc" and msg.get("method") == "action.preview":
                    if seen is not None:
                        seen.append(msg)
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                             "ok": True, "result": verdict}, ensure_ascii=False))
                    return
            raise AssertionError("no action.preview rpc arrived")
    run(_m())


def test_preview_forwards_params(home):
    seen = []
    verdict = {"shown": True, "cancelled": False, "held_ms": 450, "label": "кликну «Войти»"}
    rid = rpc_call(home, "action.preview",
                   {"selector": "#login-btn", "action": "click", "name": "Войти"})
    mock_preview(home, verdict, seen)
    out = read_rpc_out(home, rid)
    assert seen and seen[0]["params"] == {"selector": "#login-btn", "action": "click", "name": "Войти"}
    assert out == {"v": 1, "id": rid, "ok": True, "result": verdict}


def test_user_stop_verdict_passes_through(home):
    verdict = {"shown": True, "cancelled": True, "reason": "user_stop", "label": "кликну «Войти»"}
    rid = rpc_call(home, "action.preview", {"selector": "#x", "action": "click"})
    mock_preview(home, verdict)
    out = read_rpc_out(home, rid)
    assert out is not None
    assert out["ok"] is True  # a veto is a successful answer, not an error
    assert out["result"]["cancelled"] is True and out["result"]["reason"] == "user_stop"


def test_preview_verdicts_are_audited(home):
    for verdict in ({"shown": True, "cancelled": False, "held_ms": 450, "label": "кликну «Ок»"},
                    {"shown": True, "cancelled": True, "reason": "user_stop"}):
        rid = rpc_call(home, "action.preview", {"selector": "#a"})
        mock_preview(home, verdict)
        assert read_rpc_out(home, rid) is not None
    evs = [e for e in read_jsonl(home / "audit.jsonl") if e.get("ev") == "preview"]
    assert len(evs) == 2
    assert evs[0]["cancelled"] is False and evs[0]["label"] == "кликну «Ок»"
    assert evs[1]["cancelled"] is True and evs[1]["reason"] == "user_stop"


def test_preview_no_element_shape_passes(home):
    verdict = {"shown": False, "cancelled": False, "reason": "no_element"}
    rid = rpc_call(home, "action.preview", {"selector": "#gone"})
    mock_preview(home, verdict)
    out = read_rpc_out(home, rid)
    assert out is not None
    assert out["ok"] is True and out["result"] == verdict


def test_preview_no_extension(home):
    rid = rpc_call(home, "action.preview", {"selector": "#a"})
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "no_extension"


def test_preview_requires_version(home):
    rid = rpc_call(home, "action.preview", {"selector": "#a"}, v=None)
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "no_version"


def test_preview_timeout(tmp_path):
    p = start_bridge(tmp_path, {"SIDEKICK_RPC_TIMEOUT": "1"})
    try:
        async def _silent():  # connected extension that never answers the preview
            async with websockets.connect(URI, open_timeout=3) as c:
                await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(tmp_path)}))
                assert json.loads(await asyncio.wait_for(c.recv(), 3)).get("ok") is True
                rid = rpc_call(tmp_path, "action.preview", {"selector": "#a"})
                out = read_rpc_out(tmp_path, rid, timeout=4.0)
                assert out is not None and out["ok"] is False and out["err"] == "timeout"
        run(_silent())
    finally:
        kill_bridge(p)
