"""SK-07 contract validator: agent tab groups (tabs.open_grouped / use_tab /
highlight_group / park_group) on the bridge side + manifest pins.

The bridge FORWARDS these like page.* (the extension executes browser.tabs/tabGroups);
what this pins down:
  * manifest: "tabGroups" permission present, strict_min_version 139.0 (tabGroups API
    is FF 139; tabs.group is 138 — research 2026-10-17-firefox-tabgroups-sidebar-action);
  * every tabs.* method reaches the extension as an rpc frame with its params, the
    extension's verdict (ok or err) is relayed verbatim, and each call is audited;
  * no extension -> err no_extension (nothing is queued);
  * while hub/paused (SK-08) tabs.* are refused with paused_for_human and NOT forwarded
    — the human has the browser, the agent must not shuffle tabs;
The group semantics themselves (real group, title/color, collapse, highlight) are
verified live on the firefox-158 bench (sk07_bench.py).
"""
import asyncio
import json
import os
import time

import pytest

from conftest import ROOT, kill_bridge, start_bridge
from test_handoff import HoExt
from test_perm import MockExt, read_jsonl, run
from test_rpc_e2e import read_rpc_out, rpc_call

TAB_METHODS = ["tabs.open_grouped", "tabs.use_tab", "tabs.highlight_group", "tabs.park_group"]


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


async def wait_rpc(ext, method, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for f in ext.frames:
            if f.get("type") == "rpc" and f.get("method") == method:
                return f
        await asyncio.sleep(0.05)
    raise AssertionError(f"no rpc frame for {method} in {timeout}s "
                         f"(frames: {[f.get('method') or f.get('type') for f in ext.frames]})")


async def answer(ext, frame, ok=True, result=None, err=None):
    msg = {"v": 1, "type": "rpc_result", "id": frame["id"], "ok": ok}
    if ok:
        msg["result"] = result
    else:
        msg["err"] = err
    await ext.c.send(json.dumps(msg))


# ---- manifest pins -------------------------------------------------------------------

def test_manifest_declares_tabgroups_api():
    m = json.load(open(os.path.join(ROOT, "extension", "manifest.json")))
    assert "tabGroups" in m["permissions"], "browser.tabGroups.* needs the tabGroups permission"
    assert "tabs" in m["permissions"]
    # tabs.group/groupId = FF 138, the whole tabGroups namespace = FF 139 (BCD)
    assert m["browser_specific_settings"]["gecko"]["strict_min_version"] == "139.0"


# ---- forwarding / relay / audit --------------------------------------------------------

def test_open_grouped_forwarded_relayed_and_audited(home):
    canned = {"tabId": 42, "windowId": 1, "url": "http://example.test/a", "grouped": True,
              "groupId": 7, "groupCreated": True, "title": "Sidekick", "color": "blue"}

    async def main():
        async with MockExt(home) as ext:
            rid = rpc_call(home, "tabs.open_grouped",
                           {"url": "http://example.test/a", "focus": False})
            f = await wait_rpc(ext, "tabs.open_grouped")
            assert f["v"] == 1 and f["id"] == rid
            assert f["params"] == {"url": "http://example.test/a", "focus": False}
            await answer(ext, f, result=canned)
            out = read_rpc_out(home, rid, timeout=4.0)
            assert out is not None and out["ok"] is True and out["result"] == canned, out
            aud = read_jsonl(home / "audit.jsonl")
            assert any(a.get("method") == "tabs.open_grouped" and a.get("ok") is True for a in aud)
    run(main())


def test_all_four_methods_forwarded(home):
    async def main():
        async with MockExt(home) as ext:
            for i, m in enumerate(TAB_METHODS):
                rid = rpc_call(home, m, {"url": "http://x.test", "mode": "collapse"})
                f = await wait_rpc(ext, m)
                assert f["id"] == rid and f["params"]["mode"] == "collapse", (m, f)
                await answer(ext, f, result={"n": i})
                out = read_rpc_out(home, rid, timeout=4.0)
                assert out is not None and out["ok"] is True and out["result"] == {"n": i}, (m, out)
    run(main())


def test_extension_error_relays_verbatim(home):
    async def main():
        async with MockExt(home) as ext:
            rid = rpc_call(home, "tabs.open_grouped", {})
            f = await wait_rpc(ext, "tabs.open_grouped")
            await answer(ext, f, ok=False, err="url_required")
            out = read_rpc_out(home, rid, timeout=4.0)
            assert out is not None and out["ok"] is False and out["err"] == "url_required", out
            aud = read_jsonl(home / "audit.jsonl")
            assert any(a.get("method") == "tabs.open_grouped" and a.get("ok") is False
                       and a.get("err") == "url_required" for a in aud)
    run(main())


def test_no_extension_fails_loudly(home):
    for m in TAB_METHODS:
        rid = rpc_call(home, m, {"url": "http://x.test"})
        out = read_rpc_out(home, rid, timeout=3.0)
        assert out is not None and out["ok"] is False and out["err"] == "no_extension", (m, out)


# ---- SK-08 interaction: the human has the browser --------------------------------------

def test_paused_refuses_tab_ops(home):
    async def main():
        async with HoExt(home) as ext:
            rh = rpc_call(home, "handoff.request", {"what": "логин"})
            await ext.wait_handoff()
            rids = {m: rpc_call(home, m, {"url": "http://x.test"}) for m in TAB_METHODS}
            for m, r in rids.items():
                out = read_rpc_out(home, r, timeout=3.0)
                assert out is not None and out["ok"] is False, (m, out)
                assert out["err"] == "paused_for_human", (m, out)
            leaked = [f for f in ext.frames if f.get("type") == "rpc"]
            assert leaked == [], f"tab ops leaked to the browser during hand-off: {leaked}"
            aud = read_jsonl(home / "audit.jsonl")
            blocked = {a.get("method") for a in aud if a.get("ev") == "handoff_block"}
            assert set(TAB_METHODS) <= blocked, blocked
            await ext.resume(rh)
            out = read_rpc_out(home, rh, timeout=4.0)
            assert out is not None and out.get("result") == {"resumed": True}
            # unblocked: tabs.open_grouped flows again
            rid = rpc_call(home, "tabs.open_grouped", {"url": "http://x.test"})
            f = await wait_rpc(ext, "tabs.open_grouped")
            await answer(ext, f, result={"tabId": 1, "grouped": True})
            out = read_rpc_out(home, rid, timeout=4.0)
            assert out is not None and out["ok"] is True, out
    run(main())
