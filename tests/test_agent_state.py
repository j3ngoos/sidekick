"""SK-09 contract validator: derived agent state (the bridge is the single source of truth).

Bridge-side pins (the chip/badge rendering is verified live on the firefox-158 bench):
  * after every handshake the bridge replays {v:1,type:"state",state,ts,...} — the fresh
    client starts on the truth, not on "unknown";
  * every CHANGE broadcasts a state frame: needs_you (handoff/perm ask) > paused (hub/stop
    or task frame "paused") > acting (browser rpc in flight) > thinking (task running /
    other rpc pending) > idle; on-change only — no duplicate consecutive frames;
  * hub/stop removal (the agent rm's the flag itself) is caught by the 0.25 s tick;
  * agent-authored {type:"state"} outbox frames are DROPPED — the bridge owns the
    vocabulary; task/step frames still broadcast verbatim;
  * every change is audited {ev:"state", state, reason?, task?}; state frames never
    enter inbox.jsonl;
  * latency pins: event -> state frame < 0.5 s (the card's acceptance budget).
"""
import asyncio
import json
import time
import uuid

import pytest
import websockets

from conftest import kill_bridge, start_bridge, token_of

URI = "ws://127.0.0.1:8799"


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


async def aw(got, pred, timeout=4.0, lo=0):
    """await the first frame in `got[lo:]` matching pred -> (t, frame, idx) or (None,)*3"""
    end = time.monotonic() + timeout
    while True:
        for i in range(lo, len(got)):
            if pred(got[i][1]):
                return got[i][0], got[i][1], i
        if time.monotonic() > end:
            return None, None, lo
        await asyncio.sleep(0.02)


def states(got, lo=0):
    return [f["state"] for _, f in got[lo:] if f.get("type") == "state"]


def session(home, actions, settle=0.35):
    """Handshaked mock extension; a reader timestamps every frame into `got`."""
    async def _m():
        got = []
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            assert json.loads(await asyncio.wait_for(c.recv(), 3)).get("ok") is True
            stop = asyncio.Event()

            async def reader():
                while not stop.is_set():
                    try:
                        got.append((time.monotonic(), json.loads(await asyncio.wait_for(c.recv(), 0.05))))
                    except asyncio.TimeoutError:
                        continue
                    except websockets.ConnectionClosed:
                        break

            r = asyncio.create_task(reader())
            try:
                await actions(c, got)
                await asyncio.sleep(settle)  # let any extra frame land before we judge "none arrived"
            finally:
                stop.set()
                r.cancel()
        return got
    return asyncio.run(_m())


def push(home, obj):
    with open(home / "outbox.jsonl", "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def rpc_in(home, method, params=None):
    rid = str(uuid.uuid4())
    with open(home / "rpc_in.jsonl", "a") as f:
        f.write(json.dumps({"v": 1, "id": rid, "method": method, "params": params or {}}) + "\n")
    return rid


def rpc_out(home, rid, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        for m in read_jsonl(home / "rpc_out.jsonl"):
            if m.get("id") == rid:
                return m
        time.sleep(0.05)
    return None


# ---- replay on connect --------------------------------------------------------

def test_state_replayed_on_connect(home):
    async def acts(c, got):
        t, f, _ = await aw(got, lambda m: m.get("type") == "state")
        assert f and f["state"] == "idle" and f["v"] == 1 and "ts" in f, f
        assert t is not None
    session(home, acts)


# ---- task frames drive thinking/idle (verbatim broadcast preserved) -----------

def test_task_frames_drive_thinking_then_idle(home):
    async def acts(c, got):
        t0 = time.monotonic()
        push(home, {"v": 1, "type": "task", "id": "t-sk09", "title": "собрать", "state": "running"})
        t, f, i = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "thinking")
        assert f, "no thinking frame"
        assert t - t0 < 0.5, f"thinking took {t - t0:.2f}s (outbox pump budget)"
        assert f.get("reason") == "task_running" and f.get("task") == "t-sk09", f
        _, tf, _ = await aw(got, lambda m: m.get("type") == "task" and m.get("id") == "t-sk09")
        assert tf == {"v": 1, "type": "task", "id": "t-sk09", "title": "собрать", "state": "running"}
        push(home, {"v": 1, "type": "task", "id": "t-sk09", "state": "done"})
        t2, f2, _ = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "idle", lo=i + 1)
        assert f2, "no idle frame after task done"
    session(home, acts)


def test_task_paused_frame_is_paused(home):
    async def acts(c, got):
        push(home, {"v": 1, "type": "task", "id": "tp", "state": "paused"})
        t, f, _ = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "paused")
        assert f and f.get("reason") == "task_paused", f
        assert not (home / "stop").exists()  # derived from the frame, no flag involved
    session(home, acts)


# ---- acting during a browser rpc, lift on the result ---------------------------

def _rpc_roundtrip(home, got, c, method="page.text", budget=0.5):
    """write rpc_in -> expect acting frame -> answer ok -> expect the lift back.
    A closure with a cursor: consecutive round-trips must not re-match old frames."""
    cur = [0]

    async def _one():
        rid = rpc_in(home, method)
        t0 = time.monotonic()
        t, f, i = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "acting",
                           lo=cur[0])
        assert f, "no acting frame while a browser rpc is in flight"
        assert t - t0 < budget, f"acting took {t - t0:.2f}s"
        assert f.get("reason") == "browser_rpc", f
        _, rpcf, _ = await aw(got, lambda m: m.get("type") == "rpc" and m.get("id") == rid)
        assert rpcf, "rpc frame never arrived"
        await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": rid, "ok": True,
                                 "result": {"text": "x"}}))
        t2, f2, i2 = await aw(got, lambda m: m.get("type") == "state" and m.get("state") != "acting",
                              lo=i + 1)
        assert f2, "state never lifted after rpc_result"
        cur[0] = i2 + 1
        return i2
    return _one


def test_acting_during_browser_rpc(home):
    async def acts(c, got):
        await _rpc_roundtrip(home, got, c)()
    session(home, acts)


def test_no_duplicate_state_frames(home):
    """two round-trips -> exactly idle,acting,idle,acting,idle — on-change only."""
    async def acts(c, got):
        one = _rpc_roundtrip(home, got, c)
        await one()
        await one()
        _, base, i = await aw(got, lambda m: m.get("type") == "state")
        assert states(got, i) == ["idle", "acting", "idle", "acting", "idle"], states(got, i)
    session(home, acts)


# ---- Stop -> paused; external hub/stop removal recovers via the tick -----------

def test_stop_pauses_and_removal_recovers(home):
    async def acts(c, got):
        push(home, {"v": 1, "type": "task", "id": "ts", "state": "running"})
        _, _, i_th = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "thinking")
        t0 = time.monotonic()
        await c.send(json.dumps({"v": 1, "type": "stop", "task": "ts"}))
        t, f, i_pa = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "paused")
        assert f, "no paused frame after Stop"
        assert t - t0 < 0.5, f"paused took {t - t0:.2f}s"
        assert f.get("reason") == "stop" and f.get("task") == "ts", f
        (home / "stop").unlink()  # the agent rm's the flag itself when the human says continue
        t2, f2, _ = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "thinking",
                             timeout=1.5, lo=i_pa + 1)
        assert f2, "tick never noticed hub/stop removal"
        assert t2 - t0 < 1.5
    session(home, acts)


# ---- needs_you: hand-off and perm ask ------------------------------------------

def test_handoff_needs_you_and_resume_back(home):
    async def acts(c, got):
        push(home, {"v": 1, "type": "task", "id": "th", "state": "running"})
        await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "thinking")
        rid = rpc_in(home, "handoff.request", {"what": "введи пароль", "task": "th"})
        t0 = time.monotonic()
        t, f, i_ho = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "needs_you")
        assert f and f.get("reason") == "handoff", f
        assert t - t0 < 0.5, f"needs_you took {t - t0:.2f}s"
        assert rpc_out(home, rid, timeout=0.5) is None  # the agent is still blocked
        await c.send(json.dumps({"v": 1, "type": "resume", "id": rid, "task": "th"}))
        t2, f2, _ = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "thinking",
                             lo=i_ho + 1)
        assert f2, "state never returned after resume"
        assert (rpc_out(home, rid) or {}).get("result") == {"resumed": True}
    session(home, acts)


def test_perm_ask_needs_you(home):
    async def acts(c, got):
        rid = rpc_in(home, "perm.check", {"domain": "ask.test", "action": "click"})
        t0 = time.monotonic()
        t, f, i_pa = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "needs_you")
        assert f and f.get("reason") == "perm_ask", f
        assert t - t0 < 0.5, f"needs_you took {t - t0:.2f}s"
        await c.send(json.dumps({"v": 1, "type": "perm_answer", "id": rid, "choice": "once"}))
        t2, f2, _ = await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "idle",
                             lo=i_pa + 1)
        assert f2, "state never lifted after perm_answer"
        assert (rpc_out(home, rid) or {}).get("result", {}).get("decision") == "allow"
    session(home, acts)


# ---- vocabulary ownership, audit, hygiene ---------------------------------------

def test_agent_authored_state_frame_dropped(home):
    async def acts(c, got):
        push(home, {"v": 1, "type": "state", "state": "zzz-authored"})
        push(home, {"v": 1, "type": "task", "id": "td", "state": "running"})
        await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "thinking")
        assert not any(f.get("state") == "zzz-authored" for _, f in got if f.get("type") == "state")
        _, tf, _ = await aw(got, lambda m: m.get("type") == "task" and m.get("id") == "td")
        assert tf, "task frame must still broadcast verbatim"
    session(home, acts)


def test_state_changes_audited_and_never_in_inbox(home):
    async def acts(c, got):
        push(home, {"v": 1, "type": "task", "id": "ta", "state": "running"})
        await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "thinking")
        await c.send(json.dumps({"v": 1, "type": "stop"}))
        await aw(got, lambda m: m.get("type") == "state" and m.get("state") == "paused")
    session(home, acts)
    aud = [a for a in read_jsonl(home / "audit.jsonl") if a.get("ev") == "state"]
    seq = [(a["state"], a.get("reason")) for a in aud]
    assert ("thinking", "task_running") in seq and ("paused", "stop") in seq, seq
    inbox = (home / "inbox.jsonl").read_text() if (home / "inbox.jsonl").exists() else ""
    assert '"type": "state"' not in inbox and '"type":"state"' not in inbox
