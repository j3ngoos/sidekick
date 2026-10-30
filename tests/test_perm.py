"""SK-04 contract validator: permission model on the BRIDGE (grants.json + perm.check).

Mock extension like SK-03, but it also speaks perm_ask/perm_answer. What this pins down:
  * grants.get/grants.set — bridge-local, file 0600, audited;
  * perm.check decision table: allow-grant passes normal actions WITHOUT an extension
    (bridge-local), block blocks, no entry = ask;
  * «always human» classes even at Allow: action sensitive_input/payment/oauth/2fa,
    bank domains (subdomain match), auth/payment URL paths -> perm_ask pushed;
  * human answers: once -> allow (no write), always -> grants.json[level=allow] +
    repeat passes without asking, deny -> block;
  * no answer within SIDEKICK_PERM_TIMEOUT -> block via ask_timeout;
  * ask with no extension connected -> err no_extension;
  * perm_answer lands in events.jsonl, never in inbox.jsonl; every decision in audit.jsonl.
"""
import asyncio
import json
import os
import time
import uuid

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
    if not os.path.exists(path):
        return []
    return [json.loads(ln) for ln in open(path).read().splitlines() if ln.strip()]


def grants_of(home):
    return json.loads((home / "grants.json").read_text()) if (home / "grants.json").exists() else {}


def rpc_ok(home, method, params=None, timeout=5.0):
    rid = rpc_call(home, method, params)
    out = read_rpc_out(home, rid, timeout=timeout)
    assert out is not None, f"no rpc_out for {method}"
    return rid, out


def perm_check(home, domain, action="click", url=None, hint=None, timeout=5.0):
    params = {"domain": domain, "action": action}
    if url:
        params["url"] = url
    if hint:
        params["hint"] = hint
    return rpc_ok(home, "perm.check", params, timeout=timeout)


def answered(home, rid, timeout=5.0):
    out = read_rpc_out(home, rid, timeout=timeout)
    assert out is not None, f"no rpc_out answer for {rid}"
    return out


class MockExt:
    """Handshake once, keep the socket open; collect frames, answer perm_asks on demand."""

    def __init__(self, home):
        self.home = home
        self.frames = []
        self._answered = set()

    async def __aenter__(self):
        self.c = await websockets.connect(URI, open_timeout=3)
        await self.c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(self.home)}))
        ack = json.loads(await asyncio.wait_for(self.c.recv(), 3))
        assert ack.get("ok") is True, ack
        self._pump_task = asyncio.ensure_future(self._pump())
        return self

    async def __aexit__(self, *exc):
        self._pump_task.cancel()
        await self.c.close()

    async def _pump(self):
        async for raw in self.c:
            try:
                self.frames.append(json.loads(raw))
            except ValueError:
                pass

    def asks(self):
        return [f for f in self.frames if f.get("type") == "perm_ask"]

    async def wait_ask(self, timeout=5.0):
        """First UNANSWERED perm_ask frame — never re-serves one already answered."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in self.asks():
                if f["id"] not in self._answered:
                    self._answered.add(f["id"])
                    return f
            await asyncio.sleep(0.05)
            refire_noext(self.home, "perm.check")  # гонка no_extension (см. conftest)
        raise AssertionError(f"no perm_ask frame in {timeout}s "
                             f"(frames: {[f.get('type') for f in self.frames]})")

    async def answer(self, ask_id, choice):
        self._answered.add(ask_id)
        await self.c.send(json.dumps({"v": 1, "type": "perm_answer", "id": ask_id, "choice": choice}))

    async def wait_resolved(self, ask_id, timeout=5.0):
        """Bridge-initiated perm_resolved for ask_id (e.g. ask_timeout grey-out)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in self.frames:
                if f.get("type") == "perm_resolved" and f.get("id") == ask_id:
                    return f
            await asyncio.sleep(0.05)
        raise AssertionError(f"no perm_resolved for {ask_id} in {timeout}s "
                             f"(frames: {[f.get('type') for f in self.frames]})")


# ---- grants.get / grants.set ---------------------------------------------------

def test_grants_set_get_and_file_0600(home):
    _, out = rpc_ok(home, "grants.set", {"domain": "https://Example.com/x", "level": "allow"})
    assert out["ok"] is True and out["result"]["domain"] == "example.com"  # нормализован
    _, out = rpc_ok(home, "grants.get")
    assert out["ok"] is True and out["result"]["grants"]["example.com"]["level"] == "allow"
    assert oct((home / "grants.json").stat().st_mode & 0o777) == "0o600"
    assert any(a.get("ev") == "grants.set" and a["domain"] == "example.com"
               for a in read_jsonl(home / "audit.jsonl"))
    _, out = rpc_ok(home, "grants.set", {"domain": "example.com", "level": "nope"})
    assert out["ok"] is False and out["err"] == "bad_request"


def test_allow_domain_normal_action_bridge_local_no_extension(home):
    # НИ ОДНОГО расширения — allow/block решаются на мосту сами
    rpc_ok(home, "grants.set", {"domain": "example.com", "level": "allow"})
    _, out = perm_check(home, "www.example.com", "click")
    assert out["ok"] is True and out["result"] == {"decision": "allow", "via": "grant_hit"}


def test_block_domain(home):
    rpc_ok(home, "grants.set", {"domain": "example.com", "level": "block"})
    _, out = perm_check(home, "example.com", "click")
    assert out["result"] == {"decision": "block", "via": "grant_block"}
    # block сильнее always-human ask-пути тоже
    _, out = perm_check(home, "example.com", "sensitive_input")
    assert out["result"]["decision"] == "block"


# ---- always-human classes even at Allow -----------------------------------------

def test_no_grant_defaults_to_ask(home):
    async def main():
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check", {"domain": "novice.org", "action": "click"})
            ask = await ext.wait_ask()
            assert ask["id"] == rid and ask["domain"] == "novice.org" and ask["reason"] == "no_grant"
            await ext.answer(rid, "once")
            out = answered(home, rid)
            assert out["ok"] is True and out["result"] == {"decision": "allow", "via": "human_once"}
    run(main())


def test_sensitive_input_asks_once_at_allow(home):
    async def main():
        rpc_ok(home, "grants.set", {"domain": "example.com", "level": "allow"})
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check",
                           {"domain": "www.example.com", "action": "sensitive_input",
                            "hint": "поле пароля #login-pass"})
            ask = await ext.wait_ask()
            assert ask["id"] == rid and ask["action"] == "sensitive_input"
            assert "action:sensitive_input" in ask["reason"]
            assert ask["hint"] == "поле пароля #login-pass"
            await ext.answer(rid, "once")
            out = answered(home, rid)
            assert out["ok"] is True and out["result"] == {"decision": "allow", "via": "human_once"}
            # «разово» — уровень не повышаем (granт уже allow из setup'а, level не менялся на always-запись)
            assert grants_of(home)["example.com"]["level"] == "allow"
            # perm_answer — в events.jsonl, и никогда в inbox.jsonl
            ev = read_jsonl(home / "events.jsonl")
            assert any(e.get("id") == rid and e.get("choice") == "once" for e in ev)
            if (home / "inbox.jsonl").exists():
                assert "perm_answer" not in (home / "inbox.jsonl").read_text()
            # аудит решения есть
            assert any(a.get("ev") == "perm" and a["decision"] == "allow"
                       and a["via"] == "human_once" for a in read_jsonl(home / "audit.jsonl"))
    run(main())


def test_always_writes_grant_and_repeat_is_silent(home):
    async def main():
        rpc_ok(home, "grants.set", {"domain": "example.com", "level": "allow"})
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check", {"domain": "example.com", "action": "payment"})
            await ext.wait_ask()
            await ext.answer(rid, "always")
            # SK-34: payment — чувствительный класс, «всегда» понижается до разово
            assert answered(home, rid)["result"] == {"decision": "allow", "via": "human_once"}
            # повтор — тот же класс, но уже без вопроса (allow по гранту не спрашивает обычные действия)
            _, out = perm_check(home, "example.com", "click")
            assert out["result"] == {"decision": "allow", "via": "grant_hit"}
            assert len(ext.asks()) == 1
            # «всегда» на НЕчувствительном домене БЕЗ гранта -> level allow в grants.json
            rid3 = rpc_call(home, "perm.check", {"domain": "shop.example", "action": "submit"})
            await ext.wait_ask()
            await ext.answer(rid3, "always")
            assert answered(home, rid3)["result"]["decision"] == "allow"
            assert grants_of(home)["shop.example"]["level"] == "allow"
    run(main())


def test_deny_blocks(home):
    async def main():
        rpc_ok(home, "grants.set", {"domain": "evil.example", "level": "allow"})
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check", {"domain": "evil.example", "action": "oauth"})
            await ext.wait_ask()
            await ext.answer(rid, "deny")
            out = answered(home, rid)
            assert out["ok"] is True and out["result"] == {"decision": "block", "via": "human_deny"}
            assert any(a.get("ev") == "perm" and a["decision"] == "block"
                       and a["via"] == "human_deny" for a in read_jsonl(home / "audit.jsonl"))
    run(main())


def test_bank_domain_always_human_even_at_allow(home):
    async def main():
        rpc_ok(home, "grants.set", {"domain": "sberbank.ru", "level": "allow"})
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check",
                           {"domain": "online.sberbank.ru", "action": "click",
                            "url": "https://online.sberbank.ru/dashboard"})
            ask = await ext.wait_ask()
            assert ask["reason"] == "bank_domain"
            await ext.answer(rid, "once")
            assert answered(home, rid)["result"]["decision"] == "allow"
    run(main())


def test_oauth_payment_path_always_human(home):
    async def main():
        rpc_ok(home, "grants.set", {"domain": "shop.example", "level": "allow"})
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check",
                           {"domain": "shop.example", "action": "click",
                            "url": "https://shop.example/login/oauth2/code/google"})
            ask = await ext.wait_ask()
            assert ask["reason"] == "auth_or_payment_path"
            await ext.answer(rid, "deny")
            assert answered(home, rid)["result"]["decision"] == "block"
        # обычная страница того же домена — allow без вопроса (новая сессия не нужна: мост-локально)
        _, out = perm_check(home, "shop.example", "click", url="https://shop.example/catalog")
        assert out["result"] == {"decision": "allow", "via": "grant_hit"}
    run(main())


def test_ask_timeout_denies(tmp_path):
    p = start_bridge(tmp_path, env_extra={"SIDEKICK_PERM_TIMEOUT": 1})
    try:
        async def main():
            rpc_ok(tmp_path, "grants.set", {"domain": "example.com", "level": "allow"})
            async with MockExt(tmp_path) as ext:
                rid = rpc_call(tmp_path, "perm.check",
                               {"domain": "example.com", "action": "sensitive_input"})
                ask = await ext.wait_ask()  # молчим — человек не ответил
                out = answered(tmp_path, rid, timeout=4.0)
                assert out["ok"] is True
                assert out["result"] == {"decision": "block", "via": "ask_timeout"}
                assert any(a.get("ev") == "perm" and a["via"] == "ask_timeout"
                           for a in read_jsonl(tmp_path / "audit.jsonl"))
                # мёртвый промпт гасится в UI: мост сам шлёт perm_resolved(timeout)
                res = await ext.wait_resolved(ask["id"], timeout=4.0)
                assert res["choice"] == "timeout"
        run(main())
    finally:
        kill_bridge(p)


def test_ask_without_extension_is_no_extension(home):
    _, out = perm_check(home, "example.com", "sensitive_input", timeout=3.0)
    assert out["ok"] is False and out["err"] == "no_extension"
    assert any(a.get("ev") == "perm" and a["via"] == "no_extension"
               for a in read_jsonl(home / "audit.jsonl"))


def test_orphan_perm_answer_ignored(home):
    async def main():
        async with MockExt(home) as ext:
            await ext.c.send(json.dumps({"v": 1, "type": "perm_answer",
                                         "id": str(uuid.uuid4()), "choice": "always"}))
            await asyncio.sleep(0.4)
            # orphan записан в events (агент видит), но grants не трогает и rpc_out не плодит
            assert grants_of(home) == {}
            assert not (home / "rpc_out.jsonl").exists()
            ev = read_jsonl(home / "events.jsonl")
            assert len(ev) == 1
    run(main())
