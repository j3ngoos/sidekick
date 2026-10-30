"""SK-49 contract: политика одного соединения расширения к мосту.

Инцидент 23.10 (приёмка SK-44): Firefox пользователя и тест-Firefox висели на
:8791 одновременно, next(iter(CLIENTS)) маршрутизировал RPC произвольно (порядок
set): page.dom без task ушёл в браузер пользователя, агентские вкладки открылись
в тест-Firefox. Здесь зафиксировано:

  * второй hello с ВАЛИДНЫМ токеном вытесняет первый сокет — close 4009
    "replaced by newer connection"; молчаливый рандом недопустим;
  * RPC уходят только последнему подтверждённому hello (инвариант ws_count ≤ 1);
  * health.json несёт peer активного сокета, audit.jsonl — строки ev "ext"
    (connect / replaced / disconnect): «агент бьёт не туда» читается по факту;
  * hello с ПЛОХИМ токеном никого не вытесняет (4003 как раньше);
  * конкурентные handshake не оставляют в мосту два сокета.

Живой Firefox здесь не нужен: websockets-клиенты и есть «два браузера» —
политика живёт целиком на стороне моста.
"""
import asyncio
import json
import re
import time
import uuid

import pytest
import websockets

from conftest import kill_bridge, start_bridge, token_of

URI = f"ws://127.0.0.1:8799"
PEER_RE = re.compile(r"^127\.0\.0\.1:\d+$")


async def ws():
    return await websockets.connect(URI, open_timeout=3)


async def hello(c, token):
    await c.send(json.dumps({"v": 1, "type": "hello", "token": token}))
    return json.loads(await asyncio.wait_for(c.recv(), 3))


async def drain_until_closed(c, timeout=6.0):
    """Дождаться close, проглатывая попутные кадры (state-replay нового сокета)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            await asyncio.wait_for(c.recv(), min(0.5, max(0.05, deadline - time.time())))
        except asyncio.TimeoutError:
            continue
        except websockets.ConnectionClosed as e:
            rcvd = e.rcvd
            assert rcvd is not None, e
            return rcvd
    raise AssertionError("socket stayed open")


async def recv_rpc(c, timeout=6.0):
    """Первый rpc-кадр, пропуская state/ask-replay."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = await asyncio.wait_for(c.recv(), min(1.0, max(0.05, deadline - time.time())))
        msg = json.loads(raw)
        if msg.get("type") == "rpc":
            return msg
    raise AssertionError("no rpc frame")


def rpc_call(home, method, params=None):
    rid = str(uuid.uuid4())
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


def health(home):
    return json.loads((home / "health.json").read_text())


def wait_health(home, pred, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            h = health(home)
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        if pred(h):
            return h
        time.sleep(0.1)
    raise AssertionError(f"health never satisfied the predicate: {health(home)}")


def ext_events(home):
    p = home / "audit.jsonl"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if o.get("ev") == "ext":
            out.append(o)
    return out


def wait_ext(home, n, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        evs = ext_events(home)
        if len(evs) >= n:
            return evs
        time.sleep(0.1)
    raise AssertionError(f"audit ext lines: {ext_events(home)}")


# ---- вытеснение ------------------------------------------------------------

def test_second_valid_hello_evicts_first_with_4009(bridge):
    """Проверка из карточки: второй hello вытесняет первый, виден close 4009."""
    async def t():
        tok = token_of(bridge)
        async with await ws() as a:
            assert (await hello(a, tok)).get("ok") is True
            async with await ws() as b:
                assert (await hello(b, tok)).get("ok") is True
                closed = await drain_until_closed(a)
                assert closed.code == 4009
                assert closed.reason == "replaced by newer connection"
                # RPC уходят активному (последнему подтверждённому hello) — только B
                rid = rpc_call(bridge, "page.text")
                frame = await recv_rpc(b)
                assert frame["method"] == "page.text" and frame["id"] == rid
                await b.send(json.dumps({"v": 1, "type": "rpc_result", "id": rid,
                                         "ok": True, "result": {"text": "bench"}}))
                out = read_rpc_out(bridge, rid)
                assert out and out["ok"] is True and out["result"]["text"] == "bench"
    asyncio.run(t())


def test_replaced_socket_never_gets_rpc(bridge):
    """Инцидент в миниатюре: вытесненный сокет мёртв, а не «иногда живой»."""
    async def t():
        tok = token_of(bridge)
        a = await ws()
        await hello(a, tok)
        b = await ws()
        await hello(b, tok)
        await drain_until_closed(a)
        # A закрыт НАСТОЯТЕЛЬНО: любой следующий кадр — ConnectionClosed
        with pytest.raises((websockets.ConnectionClosed, RuntimeError)):
            await a.send(json.dumps({"v": 1, "type": "hb"}))
            await asyncio.sleep(0.2)
            await a.send(json.dumps({"v": 1, "type": "hb"}))
        await b.close()
    asyncio.run(t())


def test_concurrent_handshakes_leave_exactly_one_socket(bridge):
    """Гонка двух hello не оставляет в мосту два сокета (мутация CLIENTS синхронна)."""
    async def t():
        tok = token_of(bridge)
        c1, c2 = await asyncio.gather(ws(), ws())
        r1, r2 = await asyncio.gather(hello(c1, tok), hello(c2, tok))
        assert r1.get("ok") is True and r2.get("ok") is True
        h = wait_health(bridge, lambda h: h.get("ws_count") == 1)
        assert h["extension"] == "connected"
        # ровно один сокет живой: rpc доходит до одного, второй вытеснен 4009
        rid = rpc_call(bridge, "page.text")
        live, dead = [], []
        for c in (c1, c2):
            try:
                frame = await recv_rpc(c, timeout=4.0)
                if frame["id"] == rid:
                    live.append(c)
            except Exception:
                dead.append(c)
        assert len(live) == 1 and len(dead) == 1
        await live[0].send(json.dumps({"v": 1, "type": "rpc_result", "id": rid,
                                       "ok": True, "result": {"text": "race"}}))
        assert (read_rpc_out(bridge, rid) or {}).get("ok") is True
        closed = await drain_until_closed(dead[0], timeout=4.0)
        assert closed.code == 4009
        await live[0].close()
    asyncio.run(t())


def test_bad_token_hello_does_not_evict(bridge):
    """4003 — не вытеснение: активный сокет и его peer остаются на месте."""
    async def t():
        tok = token_of(bridge)
        async with await ws() as a:
            assert (await hello(a, tok)).get("ok") is True
            peer_a = wait_health(bridge, lambda h: h.get("peer"))["peer"]
            async with await ws() as b:
                await hello(b, "deadbeef" * 8)
                closed = await drain_until_closed(b, timeout=4.0)
                assert closed.code == 4003
            assert wait_health(bridge, lambda h: h.get("peer") == peer_a)["peer"] == peer_a
            rid = rpc_call(bridge, "page.text")
            frame = await recv_rpc(a)
            assert frame["id"] == rid
            await a.send(json.dumps({"v": 1, "type": "rpc_result", "id": rid,
                                     "ok": True, "result": {"text": "user"}}))
            assert (read_rpc_out(bridge, rid) or {}).get("ok") is True
    asyncio.run(t())


# ---- наблюдаемость: health.json / audit.jsonl ------------------------------

def test_health_peer_and_audit_ext_trail(bridge):
    """Проверка из карточки: peer активного расширения виден в health.json,
    вытеснение — в audit.jsonl (ev ext), дубля disconnect для вытесненного нет."""
    async def t():
        tok = token_of(bridge)
        a = await ws()
        await hello(a, tok)
        h = wait_health(bridge, lambda h: h.get("extension") == "connected")
        assert PEER_RE.match(h["peer"]), h
        peer_a = h["peer"]

        b = await ws()
        await hello(b, tok)
        await drain_until_closed(a)
        h = wait_health(bridge, lambda h: h.get("peer") and h["peer"] != peer_a)
        assert h["ws_count"] == 1 and PEER_RE.match(h["peer"]), h
        peer_b = h["peer"]

        await b.close()
        h = wait_health(bridge, lambda h: h.get("extension") == "offline")
        assert h["peer"] is None and h["ws_count"] == 0

        evs = wait_ext(bridge, 4)
        kinds = [e["decision"] for e in evs]
        # connect(A), connect(B), replaced(A→B), disconnect(B) — без disconnect(A)
        assert kinds == ["connect", "connect", "replaced", "disconnect"], evs
        assert evs[0]["peer"] == peer_a and evs[1]["peer"] == peer_b
        assert evs[2]["peer"] == peer_a and evs[2]["by"] == peer_b
        assert evs[3]["peer"] == peer_b
    asyncio.run(t())


def test_reconnect_after_eviction_becomes_active(bridge):
    """Браузер пользователя после 4009 переподключается и становится активным:
    RPC снова уходят ему, а не закрывшемуся тест-Firefox."""
    async def t():
        tok = token_of(bridge)
        user = await ws()
        await hello(user, tok)
        bench = await ws()
        await hello(bench, tok)
        await drain_until_closed(user)          # стенд вытеснил пользователя
        user2 = await ws()                       # «расширение» переподключилось
        await hello(user2, tok)
        await drain_until_closed(bench)
        rid = rpc_call(bridge, "page.text")
        frame = await recv_rpc(user2)
        assert frame["id"] == rid
        await user2.send(json.dumps({"v": 1, "type": "rpc_result", "id": rid,
                                     "ok": True, "result": {"text": "back"}}))
        assert (read_rpc_out(bridge, rid) or {}).get("ok") is True
        await user2.close()
    asyncio.run(t())
