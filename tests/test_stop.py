"""SK-06 contract validator: Stop button + step-timeline plumbing.

Bridge-side pins (the timeline/Stop UI itself is verified live on the firefox-158 bench):
  * {v:1,type:"stop",task?} from the extension is CONSUMED: raw line -> events.jsonl,
    hub/stop touched (idempotent), audit {ev:"stop", task?}; never inbox.jsonl;
  * task/step push frames written to outbox.jsonl are broadcast to connected extension
    clients verbatim (the bridge never interprets them);
  * contract v1 holds: a stop frame without v closes the socket 4002.
"""
import asyncio
import json
import time

import pytest
import websockets

from conftest import kill_bridge, start_bridge, token_of

URI = "ws://127.0.0.1:8799"


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


def run(coro):
    return asyncio.run(coro)


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def mock_client(home, send_frames, collect=None, wait_s=4.0):
    """Handshaked mock extension: sends send_frames after hello, optionally collects
    broadcast frames until collect(frame) returns True or the window closes."""
    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            assert json.loads(await asyncio.wait_for(c.recv(), 3)).get("ok") is True
            for f in send_frames:
                await c.send(json.dumps(f, ensure_ascii=False))
            end = time.time() + wait_s
            got = []
            while time.time() < end:
                try:
                    msg = json.loads(await asyncio.wait_for(
                        c.recv(), max(0.1, end - time.time())))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    break
                got.append(msg)
                if collect and collect(msg):
                    break
            return got
    return run(_m())


def wait_for(path, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_stop_creates_flag_event_audit(home):
    mock_client(home, [{"v": 1, "type": "stop", "task": "t-42"}], wait_s=0.5)
    assert wait_for(home / "stop"), "hub/stop flag was not created"
    evs = read_jsonl(home / "events.jsonl")
    assert [e for e in evs if e.get("type") == "stop" and e.get("task") == "t-42"]
    aud = [a for a in read_jsonl(home / "audit.jsonl") if a.get("ev") == "stop"]
    assert aud and aud[-1].get("task") == "t-42"
    # consumed: a stop frame must never leak into the agent's inbox
    assert not (home / "inbox.jsonl").exists() or not (home / "inbox.jsonl").read_text().strip()


def test_stop_is_idempotent(home):
    mock_client(home, [{"v": 1, "type": "stop"}, {"v": 1, "type": "stop"}], wait_s=0.5)
    assert wait_for(home / "stop")
    time.sleep(0.3)
    assert (home / "stop").exists()  # second touch must not delete or corrupt
    evs = [e for e in read_jsonl(home / "events.jsonl") if e.get("type") == "stop"]
    assert len(evs) == 2  # every press is an event the agent can count


def test_stop_requires_version(home):
    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            assert json.loads(await asyncio.wait_for(c.recv(), 3)).get("ok") is True
            await c.send(json.dumps({"type": "stop"}))  # no v -> contract v1: error frame + close 4002
            code = None
            try:
                while True:  # first the no_version error frame, then the close
                    await asyncio.wait_for(c.recv(), 3)
            except websockets.ConnectionClosed as e:
                code = e.code
            return code
    assert run(_m()) == 4002
    time.sleep(0.3)
    assert not (home / "stop").exists()  # rejected frame must not touch the pause flag


def test_step_and_task_frames_broadcast(home):
    frames = [{"v": 1, "type": "task", "id": "t1", "title": "Заполнить форму", "state": "running"},
              {"v": 1, "type": "step", "task": "t1", "n": 1, "text": "кликнуть «Войти»", "state": "active"}]

    # the mock must be connected BEFORE the outbox pump (0.4 s) drains our lines
    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            assert json.loads(await asyncio.wait_for(c.recv(), 3)).get("ok") is True
            with open(home / "outbox.jsonl", "a") as f:
                for fr in frames:
                    f.write(json.dumps(fr, ensure_ascii=False) + "\n")
            got = []
            end = time.time() + 6
            while time.time() < end:
                msg = json.loads(await asyncio.wait_for(c.recv(), max(0.1, end - time.time())))
                got.append(msg)
                if len([g for g in got if g.get("type") in ("task", "step")]) == 2:
                    break
            return got
    got = run(_m())
    tl = [g for g in got if g.get("type") in ("task", "step")]
    assert tl == frames  # verbatim pass-through — the bridge never reinterprets the timeline


def test_outbox_without_clients_is_not_lost(home):
    # no extension connected: draining the outbox is the bridge's business, nothing crashes
    with open(home / "outbox.jsonl", "a") as f:
        f.write(json.dumps({"v": 1, "type": "step", "task": "t9", "n": 1, "text": "x"}) + "\n")
    time.sleep(1.2)
    assert not (home / "inbox.jsonl").exists() or not (home / "inbox.jsonl").read_text().strip()
    assert not (home / "stop").exists()
