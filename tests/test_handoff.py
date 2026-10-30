"""SK-08 contract validator: hand-off to the human (hub/paused + «Готово, продолжай»).

Bridge-side pins (the «нужен ты» banner UI itself is verified live on the firefox-158 bench):
  * handoff.request {what,task?,url?} touches hub/paused (the HAND-OFF flag — never
    hub/stop!), pushes a {type:"handoff"} banner frame, and the rpc BLOCKS until resume;
  * while paused every other rpc (page.*, action.preview, perm.check, grants.*) answers
    err paused_for_human + audit {ev:"handoff_block"} — NOTHING is forwarded to the
    extension (privacy: no screenshots/previews while the human types secrets);
  * {type:"resume",id,task?} is consumed: raw line -> events.jsonl (never inbox),
    hub/paused removed, the waiting rpc resolves {resumed:true}, audit {ev:"resume"};
    resume must NOT touch hub/stop (SK-06 is a different pause);
  * no answer within SIDEKICK_HANDOFF_TIMEOUT -> err handoff_timeout, flag removed,
    banner greyed via handoff_resolved{timeout};
  * no extension connected -> err no_extension and the flag is NOT touched;
  * reconnect re-pushes the open hand-off; a second request supersedes the first
    (err superseded + handoff_resolved{superseded}) — SK-05 vocabulary;
  * a stale hub/paused left by a crash is removed at bridge startup; a resume with no
    pending hand-off still clears the flag (idempotent).
"""
import asyncio
import json
import time
import uuid

import pytest
import websockets

from conftest import kill_bridge, refire_noext, start_bridge, token_of
from test_perm import MockExt, read_jsonl, run
from test_rpc_e2e import read_rpc_out, rpc_call

URI = "ws://127.0.0.1:8799"


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


class HoExt(MockExt):
    """Mock extension that also speaks the SK-08 hand-off protocol."""

    async def wait_handoff(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in self.frames:
                if f.get("type") == "handoff":
                    return f
            await asyncio.sleep(0.05)
            refire_noext(self.home, "handoff.request")  # гонка no_extension (см. conftest)
        raise AssertionError(f"no handoff frame in {timeout}s "
                             f"(frames: {[f.get('type') for f in self.frames]})")

    def handoff_frames(self):
        return [f for f in self.frames if f.get("type") == "handoff"]

    def rpcs(self):
        return [f for f in self.frames if f.get("type") == "rpc"]

    async def resume(self, hid, task=None):
        msg = {"v": 1, "type": "resume", "id": hid}
        if task:
            msg["task"] = task
        await self.c.send(json.dumps(msg))

    async def wait_ho_resolved(self, hid, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in self.frames:
                if f.get("type") == "handoff_resolved" and f.get("id") == hid:
                    return f
            await asyncio.sleep(0.05)
        raise AssertionError(f"no handoff_resolved for {hid} in {timeout}s "
                             f"(frames: {[f.get('type') for f in self.frames]})")


def start_handoff(home, what="введи пароль на странице логина", task="t-8", url=None):
    rid = rpc_call(home, "handoff.request",
                   {"what": what, "task": task, **({"url": url} if url else {})})
    return rid


# ---- the pause itself -------------------------------------------------------------

def test_handoff_request_pauses_shows_banner_and_audits(home):
    async def main():
        async with HoExt(home) as ext:
            rid = start_handoff(home, url="http://127.0.0.1:8899/login.html")
            ho = await ext.wait_handoff()
            assert ho["v"] == 1 and ho["id"] == rid
            assert ho["what"] == "введи пароль на странице логина"
            assert ho["task"] == "t-8" and ho["url"].endswith("login.html")
            assert (home / "paused").exists(), "hub/paused (hand-off flag) not created"
            assert not (home / "stop").exists(), "hand-off must NOT touch hub/stop (SK-06)"
            # the rpc BLOCKS — no answer until the human resumes
            assert read_rpc_out(home, rid, timeout=0.6) is None
            aud = read_jsonl(home / "audit.jsonl")
            assert any(a.get("ev") == "handoff" and a.get("decision") == "pause"
                       and a.get("task") == "t-8" for a in aud)
    run(main())


def test_paused_refuses_everything_visual(home):
    async def main():
        async with HoExt(home) as ext:
            rid = start_handoff(home)
            await ext.wait_handoff()
            rids = {m: rpc_call(home, m, {"selector": "#login-btn"})
                    for m in ("page.screenshot", "page.text", "page.dom", "action.preview")}
            rids["perm.check"] = rpc_call(home, "perm.check", {"domain": "x.example", "action": "click"})
            rids["grants.set"] = rpc_call(home, "grants.set", {"domain": "x.example", "level": "allow"})
            for m, r in rids.items():
                out = read_rpc_out(home, r, timeout=3.0)
                assert out is not None and out["ok"] is False, m
                assert out["err"] == "paused_for_human", (m, out)
            # the privacy guarantee: not a single frame was forwarded to the extension
            assert ext.rpcs() == [], f"frames leaked during pause: {ext.rpcs()}"
            aud = read_jsonl(home / "audit.jsonl")
            blocked = {a.get("method") for a in aud if a.get("ev") == "handoff_block"}
            assert {"page.screenshot", "action.preview", "perm.check"} <= blocked, blocked
            # handoff.status is the one thing allowed while paused
            rs = rpc_call(home, "handoff.status")
            out = read_rpc_out(home, rs, timeout=3.0)
            assert out is not None and out["ok"] is True and out["result"]["paused"] is True
            assert out["result"]["id"] == rid and "пароль" in out["result"]["what"]
    run(main())


def test_resume_resolves_unblocks_and_is_consumed(home):
    async def main():
        async with HoExt(home) as ext:
            rid = start_handoff(home)
            ho = await ext.wait_handoff()
            (home / "stop").write_text("")  # SK-06 flag must survive an SK-08 resume
            await ext.resume(ho["id"], task="t-8")
            out = read_rpc_out(home, rid, timeout=4.0)
            assert out is not None and out["ok"] is True, out
            assert out["result"] == {"resumed": True}
            deadline = time.time() + 4
            while time.time() < deadline and (home / "paused").exists():
                await asyncio.sleep(0.05)
            assert not (home / "paused").exists()
            assert (home / "stop").exists(), "resume cleared hub/stop — flags must stay separate"
            (home / "stop").unlink()
            ev = read_jsonl(home / "events.jsonl")
            assert any(e.get("type") == "resume" and e.get("id") == rid
                       and e.get("task") == "t-8" and e.get("v") == 1 for e in ev)
            if (home / "inbox.jsonl").exists():
                assert "resume" not in (home / "inbox.jsonl").read_text()
            aud = read_jsonl(home / "audit.jsonl")
            assert any(a.get("ev") == "resume" and a.get("id") == rid for a in aud)
            # traffic flows again: page.text is forwarded and answered
            rt = rpc_call(home, "page.text", {"maxChars": 50})
            deadline = time.time() + 4
            while time.time() < deadline and not ext.rpcs():
                await asyncio.sleep(0.05)
            assert ext.rpcs(), "page.text was not forwarded after resume"
            await ext.c.send(json.dumps({"v": 1, "type": "rpc_result", "id": rt,
                                         "ok": True, "result": {"text": "SUBMITTED"}}))
            out = read_rpc_out(home, rt, timeout=4.0)
            assert out is not None and out["ok"] is True and out["result"]["text"] == "SUBMITTED"
            # and the hand-off is over: status says not paused
            rs = rpc_call(home, "handoff.status")
            out = read_rpc_out(home, rs, timeout=3.0)
            assert out is not None and out["ok"] is True and out["result"]["paused"] is False
    run(main())


# ---- timeout / no extension ---------------------------------------------------------

def test_handoff_timeout_unblocks_and_greys_banner(tmp_path):
    p = start_bridge(tmp_path, env_extra={"SIDEKICK_HANDOFF_TIMEOUT": 1})
    try:
        async def main():
            async with HoExt(tmp_path) as ext:
                rid = start_handoff(tmp_path)
                ho = await ext.wait_handoff()
                out = read_rpc_out(tmp_path, rid, timeout=4.0)
                assert out is not None and out["ok"] is False, out
                assert out["err"] == "handoff_timeout"
                assert not (tmp_path / "paused").exists(), "timeout must clear the flag"
                res = await ext.wait_ho_resolved(ho["id"], timeout=4.0)
                assert res["choice"] == "timeout"
                aud = read_jsonl(tmp_path / "audit.jsonl")
                assert any(a.get("ev") == "handoff" and a.get("decision") == "timeout"
                           for a in aud)
        run(main())
    finally:
        kill_bridge(p)


def test_no_extension_rejects_without_flag(home):
    rid = start_handoff(home)
    out = read_rpc_out(home, rid, timeout=3.0)
    assert out is not None and out["ok"] is False and out["err"] == "no_extension"
    assert not (home / "paused").exists(), "flag must stay untouched without a banner path"
    aud = read_jsonl(home / "audit.jsonl")
    assert any(a.get("ev") == "handoff" and a.get("decision") == "err"
               and a.get("via") == "no_extension" for a in aud)


# ---- reconnect / supersede / contract v1 ----------------------------------------------

def test_reconnect_replays_open_handoff(home):
    async def main():
        async with HoExt(home) as ext1:
            rid = start_handoff(home)
            ho = await ext1.wait_handoff()
        # WS dropped (extension reinstalled / network blip): a fresh client must re-see the banner
        async with HoExt(home) as ext2:
            ho2 = await ext2.wait_handoff(timeout=6.0)
            assert ho2["id"] == rid == ho["id"]
            assert (home / "paused").exists()
            await ext2.resume(rid)
            out = read_rpc_out(home, rid, timeout=4.0)
            assert out is not None and out.get("result") == {"resumed": True}
    run(main())


def test_second_request_supersedes_first(home):
    async def main():
        async with HoExt(home) as ext:
            rid1 = start_handoff(home, what="первый")
            ho1 = await ext.wait_handoff()
            rid2 = start_handoff(home, what="второй")
            out1 = read_rpc_out(home, rid1, timeout=4.0)
            assert out1 is not None and out1["ok"] is False and out1["err"] == "superseded"
            res = await ext.wait_ho_resolved(rid1, timeout=4.0)
            assert res["choice"] == "superseded"
            deadline = time.time() + 4
            while time.time() < deadline and len(ext.handoff_frames()) < 2:
                await asyncio.sleep(0.05)
            assert ext.handoff_frames()[-1]["id"] == rid2
            assert ext.handoff_frames()[-1]["what"] == "второй"
            assert (home / "paused").exists()  # the pause continues under the new banner
            await ext.resume(rid2)
            out2 = read_rpc_out(home, rid2, timeout=4.0)
            assert out2 is not None and out2.get("result") == {"resumed": True}
    run(main())


def test_resume_requires_version(home):
    async def main():
        async with HoExt(home) as ext:
            rid = start_handoff(home)
            await ext.wait_handoff()
            async with websockets.connect(URI, open_timeout=3) as c:
                await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
                assert json.loads(await asyncio.wait_for(c.recv(), 3)).get("ok") is True
                await c.send(json.dumps({"type": "resume", "id": rid}))  # no v -> 4002
                code = None
                try:
                    while True:
                        await asyncio.wait_for(c.recv(), 3)
                except websockets.ConnectionClosed as e:
                    code = e.code
                assert code == 4002
            await asyncio.sleep(0.3)
            assert (home / "paused").exists(), "rejected resume must not clear the pause"
            rs = rpc_call(home, "handoff.status")
            out = read_rpc_out(home, rs, timeout=3.0)
            assert out is not None and out["result"]["paused"] is True and out["result"]["id"] == rid
    run(main())


# ---- flag hygiene ---------------------------------------------------------------------

def test_stale_paused_removed_on_startup(tmp_path):
    (tmp_path / "paused").write_text("")  # left by a crashed bridge: nobody can resolve it
    p = start_bridge(tmp_path)
    try:
        deadline = time.time() + 4
        while time.time() < deadline and (tmp_path / "paused").exists():
            time.sleep(0.05)
        assert not (tmp_path / "paused").exists()
    finally:
        kill_bridge(p)


def test_resume_without_pending_still_clears_flag(home):
    async def main():
        async with HoExt(home) as ext:
            (home / "paused").write_text("")  # e.g. the waiter timed out mid-click
            await ext.resume(str(uuid.uuid4()))
            deadline = time.time() + 4
            while time.time() < deadline and (home / "paused").exists():
                await asyncio.sleep(0.05)
            assert not (home / "paused").exists()
            await asyncio.sleep(0.3)
            assert not (home / "rpc_out.jsonl").exists() or \
                not (home / "rpc_out.jsonl").read_text().strip(), "orphan resume invented an rpc answer"
            ev = read_jsonl(home / "events.jsonl")
            assert len([e for e in ev if e.get("type") == "resume"]) == 1
    run(main())
