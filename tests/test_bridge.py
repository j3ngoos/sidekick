"""SK-02 acceptance tests — bridge contract v1 (runbook «Архитектура: контракт моста v1»).

The five tests from the plan (test_no_hello_closed, test_bad_token, test_good_token,
test_token_file_perm, test_version_required) plus extras pinning the close codes,
token stability across restarts and the heartbeat path the extension relies on.
"""
import asyncio, json

import pytest
import websockets

from conftest import PORT, kill_bridge, start_bridge, token_of

URI = f"ws://127.0.0.1:{PORT}"


async def ws():
    return await websockets.connect(URI, open_timeout=3)


async def hello(c, token, v=1):
    frame = {"type": "hello", "token": token}
    if v is not None:
        frame = {"v": v, **frame}
    await c.send(json.dumps(frame))
    return json.loads(await asyncio.wait_for(c.recv(), 3))


# ---- tests from the plan ----------------------------------------------------

def test_no_hello_closed(bridge):
    async def t():
        async with await ws() as c:
            with pytest.raises(websockets.ConnectionClosed):
                await asyncio.wait_for(c.recv(), 7)  # bridge hello timeout is 5 s
    asyncio.run(t())


def test_bad_token(bridge):
    async def t():
        async with await ws() as c:
            r = await hello(c, "deadbeef")
            assert r == {"v": 1, "type": "hello_ack", "ok": False, "err": "bad_token"}
    asyncio.run(t())


def test_good_token(bridge):
    async def t():
        async with await ws() as c:
            r = await hello(c, token_of(bridge))
            assert r["ok"] is True
    asyncio.run(t())


def test_token_file_perm(bridge):
    assert (bridge / "token").stat().st_mode & 0o077 == 0


def test_version_required(bridge):
    # contract: frame without v -> {"v":1,"type":"error","err":"no_version"} + close 4002.
    # the error frame is sent BEFORE close, so drain it, then expect the close.
    # SK-09: right after hello_ack the bridge replays the current agent state — skip it.
    async def t():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(bridge)}))
            await c.recv()
            await c.send(json.dumps({"type": "hb"}))  # без v
            err = json.loads(await asyncio.wait_for(c.recv(), 3))
            if err.get("type") == "state":  # SK-09 replay frame, not the verdict
                err = json.loads(await asyncio.wait_for(c.recv(), 3))
            assert err == {"v": 1, "type": "error", "err": "no_version"}
            with pytest.raises(websockets.ConnectionClosed) as ei:
                await asyncio.wait_for(c.recv(), 3)
            assert ei.value.rcvd.code == 4002
    asyncio.run(t())


# ---- extras: close codes, compat, restart, heartbeat -------------------------

def test_anonymous_close_code_4001(bridge):
    # SK-41: анонимный сокет (hello не прислан) закрывается МОЛЧА — RST без
    # close-frame; страница-щуп видит 1006 вместо опознавательного 4001+reason.
    async def t():
        async with await ws() as c:
            with pytest.raises(websockets.ConnectionClosed) as ei:
                await asyncio.wait_for(c.recv(), 7)
            assert ei.value.code == 1006 and ei.value.reason == ""
    asyncio.run(t())


def test_bad_token_close_code_4003(bridge):
    async def t():
        async with await ws() as c:
            await hello(c, "deadbeef")
            with pytest.raises(websockets.ConnectionClosed) as ei:
                await asyncio.wait_for(c.recv(), 3)
            assert ei.value.rcvd.code == 4003
    asyncio.run(t())


def test_future_version_rejected(bridge):
    async def t():
        async with await ws() as c:
            r = await hello(c, token_of(bridge), v=99)
            assert r == {"v": 1, "type": "hello_ack", "ok": False, "err": "bad_version"}
            with pytest.raises(websockets.ConnectionClosed) as ei:
                await asyncio.wait_for(c.recv(), 3)
            assert ei.value.rcvd.code == 4002
    asyncio.run(t())


def test_hello_without_v_is_v1(bridge):
    # decision №7: backward compat applies to the hello frame only
    async def t():
        async with await ws() as c:
            r = await hello(c, token_of(bridge), v=None)
            assert r["ok"] is True
    asyncio.run(t())


def test_non_hello_first_frame_4001(bridge):
    # the old Mac extension sends "hb" as its first frame — must be refused
    # SK-41: отказ молчаливый (RST → 1006), опознавательного close-reason нет
    async def t():
        async with await ws() as c:
            await c.send(json.dumps({"type": "hb"}))
            with pytest.raises(websockets.ConnectionClosed) as ei:
                await asyncio.wait_for(c.recv(), 3)
            assert ei.value.code == 1006 and ei.value.reason == ""
    asyncio.run(t())


def test_hb_with_v_keeps_connection_and_chat_reaches_inbox(bridge):
    async def t():
        async with await ws() as c:
            await hello(c, token_of(bridge))
            await c.send(json.dumps({"v": 1, "type": "hb"}))
            await c.send(json.dumps({"v": 1, "type": "chat", "text": "hi"}))
            await asyncio.sleep(0.3)
            # SK-09: the connect-replay state frame is the ONLY extra frame allowed —
            # hb/chat must still produce no reply and no close.
            while True:
                try:
                    f = json.loads(await asyncio.wait_for(c.recv(), 1))
                except asyncio.TimeoutError:
                    break
                assert f.get("type") == "state", f
        lines = (bridge / "inbox.jsonl").read_text().splitlines()
        assert any(json.loads(ln).get("text") == "hi" for ln in lines)
    asyncio.run(t())


def test_token_stable_across_restart(tmp_path):
    p1 = start_bridge(tmp_path)
    tok1 = token_of(tmp_path)
    kill_bridge(p1)
    p2 = start_bridge(tmp_path)
    try:
        assert token_of(tmp_path) == tok1  # rotate only by deleting the file
        assert (tmp_path / "token").stat().st_mode & 0o077 == 0
    finally:
        kill_bridge(p2)
