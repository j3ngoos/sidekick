"""SK-48 contract validator: пост-чек без зрения (checked/post_state/page.media) +
верификация handoff-«Готово» ожидаемым состоянием (expect).

Мост (pin'ы на живом мосте с mock-расширением):
  * page.media {} форвардится расширению как rpc method page.media (TAB_METHODS:
    task/tab проходят резолвер SK-25); во время handoff-паузы — paused_for_human;
  * handoff.request принимает expect {kind:"video_playing"} | {kind:"dom",name,role?,
    checked?,near?}; кривая форма — bad_request + hint ЛОКАЛЬНО (hub/paused не тронут,
    баннера нет);
  * resume руки с expect НЕ снимает паузу: мост шлёт внутренний лег page.media/page.dom
    (id "<rid>~vfN" — в rpc_out агента этих строк нет), при достижении вердикт
    {resumed:true,verified:true}, hub/paused снят, аудит {ev:"handoff_verify"};
  * недостиг — лег page.screenshot ("<rid>~shotN"), затем РЕ-ПУШ баннера с тем же id:
    note (почему зовут снова) + shot (кадр) + attempt; rpc агента всё ещё заблокирован,
    hub/paused на месте; handoff.status отдаёт expect/note/attempt, но НЕ shot;
  * HANDOFF_VERIFY_MAX_RETRY=2: после третьей непроверенной «Готово» —
    {resumed:true,verified:false,verify_err:"not_reached"};
  * отказ лега/таймаут верификации — verified:false + verify_err (пауза снимается:
    человек свою часть сделал, врать про «verified» мост не имеет права);
  * рука БЕЗ expect — прежнее поведение {resumed:true} без полей (не ломать SK-08);
  * post_state из исполнения action.do ложится в audit-строку rpc (silent-noop виден
    grep'ом без разбора скриншотов).

content.js (node-юниты):
  * SK-17 executor: клик по тумблеру несёт post_state {kind,before,after,changed};
    silent-noop (клик дошёл, checked не сдвинулся) — changed:false + waited_ms;
    label→control и aria-pressed читаются; не-тумблер и input — без post_state;
    исчезнувшая из DOM цель — changed:null + note (не «изменилось»);
  * SK-48 snapshot: sk48CheckedOf — нативный checkbox/radio, label→control,
    aria role checkbox/switch (+mixed), aria-pressed/checked/expanded; не-тумблер —
    undefined (ключ выпадает на JSON);
  * SK-48 media: pageMedia — состояние/геометрия/aria-имя, src НЕ отдаётся, count,
    округления, топ-8.
"""
import asyncio
import json
import pathlib

# SK-71 node shim: the spliced blocks call L() for dictionary strings; the unit
# renders from the real EN pack (extension/locales/en.json) — same data as production.
SK71_SHIM = (
    '(() => { const en = JSON.parse(require("fs").readFileSync("%s", "utf8"));'
    'const fill = (s, a) => a ? s.replace(/\{\{(\w+)\}\}/g, (_, k) => (a[k] !== undefined ? String(a[k]) : "")) : s;'
    'global.I18N = { t: (k, a) => fill(en[k] !== undefined ? en[k] : k, a),'
    ' has: (k) => Object.prototype.hasOwnProperty.call(en, k), locale: () => "en-US",'
    ' initFromRuntime: () => Promise.resolve(), watchRuntime: () => {}, load: () => Promise.resolve() };'
    'global.L = (k, a) => global.I18N.t(k, a); })();\n'
) % (pathlib.Path(__file__).resolve().parent.parent / "extension" / "locales" / "en.json")
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import websockets

from conftest import kill_bridge, start_bridge, token_of
from test_action_do import mock_chain
from test_perm import read_jsonl, run
from test_rpc_e2e import read_rpc_out, rpc_call

URI = "ws://127.0.0.1:8799"
JS = (Path(__file__).resolve().parent.parent / "extension" / "content.js").read_text(encoding="utf-8")


async def wait_out(home, rid, timeout=6.0):
    """read_rpc_out, но кооперативный: mock-расширение отвечает на верификационные
    леги своим pump'ом в ЭТОМ же event loop — синхронный sleep его глушит."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        obj = read_rpc_out(home, rid, timeout=0.01)
        if obj is not None:
            return obj
        await asyncio.sleep(0.05)
    return None


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


class Sk48Ext:
    """Mock-расширение: собирает кадры и автоматически отвечает на rpc по responder
    (method, frame) -> (ok, result|err) | None (None — не отвечаем)."""

    def __init__(self, home, responder=None):
        self.home = home
        self.frames = []
        self.responder = responder or (lambda m, f: None)
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
                f = json.loads(raw)
            except ValueError:
                continue
            self.frames.append(f)
            if f.get("type") == "rpc" and f.get("id") not in self._answered:
                try:
                    r = self.responder(f.get("method"), f)
                except Exception as e:  # кривой responder не должен глушить pump молча
                    self.frames.append({"type": "!responder_error", "err": repr(e)})
                    continue
                if r is not None:
                    self._answered.add(f["id"])
                    ok, payload = r
                    await self.c.send(json.dumps(
                        {"v": 1, "type": "rpc_result", "id": f["id"], "ok": ok,
                         **({"result": payload} if ok else {"err": payload})},
                        ensure_ascii=False))

    def rpcs(self, method=None):
        return [f for f in self.frames if f.get("type") == "rpc"
                and (method is None or f.get("method") == method)]

    def handoff_frames(self):
        return [f for f in self.frames if f.get("type") == "handoff"]

    async def wait_handoff(self, timeout=5.0, min_count=1):
        deadline = time.time() + timeout
        while time.time() < deadline:
            fs = self.handoff_frames()
            if len(fs) >= min_count:
                return fs[-1]
            await asyncio.sleep(0.05)
        raise AssertionError(f"no >= {min_count} handoff frames in {timeout}s "
                             f"(got: {self.handoff_frames()})")

    async def wait_rpc(self, method, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in self.rpcs(method):
                return f
            await asyncio.sleep(0.05)
        raise AssertionError(f"no rpc {method} in {timeout}s "
                             f"(rpcs: {[(f.get('method'), f.get('id')) for f in self.rpcs()]})")

    async def resume(self, hid, task=None):
        msg = {"v": 1, "type": "resume", "id": hid}
        if task:
            msg["task"] = task
        await self.c.send(json.dumps(msg))


MEDIA_PLAYING = {"url": "http://127.0.0.1:8899/media.html", "count": 1,
                 "videos": [{"paused": False, "ended": False, "muted": True, "volume": 1,
                             "currentTime": 3.2, "duration": 60.0, "readyState": 3,
                             "rect": {"x": 0, "y": 0, "w": 640, "h": 360}, "name": "player"}]}
MEDIA_PAUSED = {"url": "http://127.0.0.1:8899/media.html", "count": 1,
                "videos": [{"paused": True, "ended": False, "muted": False, "volume": 1,
                            "currentTime": 0, "duration": 60.0, "readyState": 4,
                            "rect": {"x": 0, "y": 0, "w": 640, "h": 360}}]}
# играет, но за вьюпортом (YouTube-карусель превью): «запущено» засчитывать нельзя
MEDIA_PLAYING_OFFSCREEN = {"url": "https://www.youtube.com/watch?v=x", "count": 2,
                           "vp": {"w": 1280, "h": 720},
                           "videos": [
                               {"paused": False, "ended": False, "muted": True, "volume": 1,
                                "currentTime": 5.0, "duration": 600.0, "readyState": 4,
                                "rect": {"x": 1400, "y": 100, "w": 640, "h": 360}},
                               {"paused": True, "ended": False, "muted": False, "volume": 1,
                                "currentTime": 0, "duration": 600.0, "readyState": 4,
                                "rect": {"x": 100, "y": 100, "w": 640, "h": 360}}]}
SHOT = {"url": "http://127.0.0.1:8899/media.html", "w": 1280, "h": 720, "scale": 0.5,
        "dataUrl": "data:image/jpeg;base64,/9j/AAAA"}


def dom_els(checked):
    return {"url": "http://127.0.0.1:8899/noop.html",
            "elements": [{"sid": "ab12", "role": "checkbox", "name": "Выбрать все",
                          "checked": checked, "rect": {"x": 1, "y": 2, "w": 3, "h": 4}}],
            "more": {"count": 0, "elements": []}}


# ---- page.media: форвардинг + пауза + capabilities -----------------------------------

def test_page_media_forwarded(home):
    async def main():
        def resp(m, f):
            if m == "page.media":
                return True, MEDIA_PLAYING
            return None
        async with Sk48Ext(home, resp) as ext:
            rid = rpc_call(home, "page.media", {})
            out = await wait_out(home, rid)
            assert out is not None and out["ok"] is True, out
            assert out["result"]["count"] == 1
            assert out["result"]["videos"][0]["paused"] is False
            leg = ext.rpcs("page.media")
            assert leg and leg[0]["params"] == {}, leg
            # SK-08: во время паузы человека видео-чтение тоже запрещено (приватность)
            rh = rpc_call(home, "handoff.request", {"what": "логин"})
            await ext.wait_handoff()
            rp = rpc_call(home, "page.media", {})
            o2 = await wait_out(home, rp)
            assert o2 is not None and o2["ok"] is False and o2["err"] == "paused_for_human"
            await ext.resume(rh)
            read_rpc_out(home, rh, timeout=3.0)
    run(main())


def test_capabilities_advertise_sk48(home):
    async def main():
        rid = rpc_call(home, "capabilities", {})
        out = read_rpc_out(home, rid, timeout=4.0)
        assert out is not None and out["ok"] is True, out
        m = out["result"]["methods"]
        assert "page.media" in m["forwarded"], m
        ho = out["result"].get("handoff") or {}
        assert "expect" in ho and "verified" in ho.get("verdict", ""), ho
        notes = " ".join(out["result"]["action_do"]["notes"])
        assert "post_state" in notes and "silent-noop" in notes
    run(main())


# ---- expect: валидация формы (локально, без расширения) ------------------------------

@pytest.mark.parametrize("expect", [
    "видео должно играть",                       # не объект
    {"kind": "bogus"},                            # неизвестный kind
    {"kind": "dom"},                              # dom без name
    {"kind": "dom", "name": "   "},               # пустой name
    {"kind": "dom", "name": "x" * 121},           # name длиннее лимита
    {"kind": "dom", "name": "а", "checked": "yes"},  # checked вне три
    {"kind": "dom", "name": "а", "near": [1, 2, 3]},  # near — не 4 числа
    {"kind": "dom", "name": "а", "role": ""},     # пустая роль
])
def test_handoff_expect_bad_form(home, expect):
    rid = rpc_call(home, "handoff.request",
                   {"what": "нажми Play", "expect": expect})
    out = read_rpc_out(home, rid, timeout=4.0)
    assert out is not None and out["ok"] is False, out
    assert out["err"] == "bad_request", out
    assert "expect" in (out.get("hint") or ""), out
    # локальный отказ: ни паузы, ни баннера — рука вообще не начиналась
    assert not (home / "paused").exists()


def test_handoff_expect_valid_forms_pass_validation(home):
    # без расширения валидные формы доходят до no_extension — значит bad_request не они
    for expect in ({"kind": "video_playing"},
                   {"kind": "dom", "name": "Выбрать все", "checked": True},
                   {"kind": "dom", "name": "а", "role": "checkbox", "checked": "mixed",
                    "near": [0, 0, 100, 100]}):
        rid = rpc_call(home, "handoff.request", {"what": "х", "expect": expect})
        out = read_rpc_out(home, rid, timeout=4.0)
        assert out is not None and out["ok"] is False and out["err"] == "no_extension", (expect, out)
        assert not (home / "paused").exists()


# ---- expect video_playing: достиг ------------------------------------------------------

def test_handoff_expect_video_reached(home):
    def resp(m, f):
        if m == "page.media" and str(f["id"]).endswith("~vf1"):
            return True, MEDIA_PLAYING
        return None

    async def main():
        async with Sk48Ext(home, resp) as ext:
            rid = rpc_call(home, "handoff.request",
                           {"what": "нажми Play", "task": "t-48",
                            "expect": {"kind": "video_playing"}})
            ho = await ext.wait_handoff()
            assert ho["id"] == rid
            aud = read_jsonl(home / "audit.jsonl")
            assert any(a.get("ev") == "handoff" and a.get("decision") == "pause"
                       and a.get("expect") == "video_playing" for a in aud)
            await ext.resume(ho["id"], task="t-48")
            out = await wait_out(home, rid)
            assert out is not None and out["ok"] is True, out
            assert out["result"] == {"resumed": True, "verified": True}, out
            leg = ext.rpcs("page.media")
            assert leg and leg[0]["id"] == f"{rid}~vf1", leg
            # верификационный лег — внутренний: в rpc_out агента его строки не выходят
            outs = [json.loads(ln) for ln in (home / "rpc_out.jsonl").read_text().splitlines()]
            assert not any(str(o.get("id", "")).endswith("~vf1") for o in outs), outs
            aud = read_jsonl(home / "audit.jsonl")
            assert any(a.get("ev") == "handoff_verify" and a.get("decision") == "reached"
                       and a.get("kind") == "video_playing" for a in aud)
            deadline = time.time() + 4
            while time.time() < deadline and (home / "paused").exists():
                await asyncio.sleep(0.05)
            assert not (home / "paused").exists()
            # рука закрыта — второго баннера нет
            assert len(ext.handoff_frames()) == 1
    run(main())


# ---- expect: недостиг → ре-пуш баннера с note+кадром, потом достиг --------------------

def test_handoff_expect_not_reached_repush_then_reached(home):
    def resp(m, f):
        if m == "page.media" and str(f["id"]).endswith("~vf1"):
            return True, MEDIA_PAUSED          # «Готово» сказано, видео на паузе
        if m == "page.media" and str(f["id"]).endswith("~vf2"):
            return True, MEDIA_PLAYING          # человек дожал — проверяется на этот раз
        if m == "page.screenshot":
            return True, SHOT
        return None

    async def main():
        async with Sk48Ext(home, resp) as ext:
            rid = rpc_call(home, "handoff.request",
                           {"what": "нажми Play", "task": "t-48",
                            "expect": {"kind": "video_playing"}})
            ho = await ext.wait_handoff()
            await ext.resume(ho["id"], task="t-48")
            ho2 = await ext.wait_handoff(min_count=2)
            assert ho2["id"] == rid, "ре-пуш обязан нести тот же id руки"
            assert ho2.get("attempt") == 1, ho2
            assert "паузе" in (ho2.get("note") or ""), ho2
            assert (ho2.get("shot") or "").startswith("data:image/"), ho2
            # rpc агента всё ещё заблокирован, пауза держится
            assert read_rpc_out(home, rid, timeout=0.6) is None
            assert (home / "paused").exists()
            # кадр лег — внутренний, с тем же префиксом руки
            assert any(f["id"] == f"{rid}~shot1" for f in ext.rpcs("page.screenshot"))
            # handoff.status: expect/note/attempt — да; shot (мегабайты) — нет
            rs = rpc_call(home, "handoff.status")
            st = await wait_out(home, rs)
            assert st is not None and st["ok"] is True, st
            r = st["result"]
            assert r["paused"] is True and r["attempt"] == 1, r
            assert r["expect"] == {"kind": "video_playing"} and "паузе" in r["note"], r
            assert "shot" not in r, r
            aud = read_jsonl(home / "audit.jsonl")
            assert any(a.get("ev") == "handoff_verify" and a.get("decision") == "not_reached"
                       for a in aud)
            assert any(a.get("ev") == "handoff" and a.get("decision") == "reverify"
                       for a in aud)
            # второй «Готово» — на этот раз играет
            await ext.resume(ho["id"], task="t-48")
            out = await wait_out(home, rid)
            assert out is not None and out["ok"] is True, out
            assert not (home / "paused").exists()
    run(main())


# ---- expect: играет ЗА вьюпортом (карусель превью) — не «запущено» ---------------------

def test_handoff_expect_offscreen_play_not_reached(home):
    def resp(m, f):
        if m == "page.media" and str(f["id"]).endswith("~vf1"):
            return True, MEDIA_PLAYING_OFFSCREEN   # autoplay превью за кадром
        if m == "page.media" and str(f["id"]).endswith("~vf2"):
            return True, MEDIA_PLAYING             # человек доскроллил/нажал настоящее
        if m == "page.screenshot":
            return True, SHOT
        return None

    async def main():
        async with Sk48Ext(home, resp) as ext:
            rid = rpc_call(home, "handoff.request",
                           {"what": "нажми Play", "task": "t-48",
                            "expect": {"kind": "video_playing"}})
            ho = await ext.wait_handoff()
            await ext.resume(ho["id"], task="t-48")
            ho2 = await ext.wait_handoff(min_count=2)
            assert ho2.get("attempt") == 1, ho2
            assert read_rpc_out(home, rid, timeout=0.6) is None, \
                "оффскрин-автоплей не вправе засчитывать «Готово»"
            aud = read_jsonl(home / "audit.jsonl")
            assert any(a.get("ev") == "handoff_verify" and a.get("decision") == "not_reached"
                       for a in aud)
            await ext.resume(ho["id"], task="t-48")
            out = await wait_out(home, rid)
            assert out is not None and out["ok"] is True, out
            assert out["result"] == {"resumed": True, "verified": True}, out
    run(main())


# ---- expect: исчерпание повторов → verified:false -------------------------------------

def test_handoff_expect_retry_exhausted(home):
    def resp(m, f):
        if m == "page.dom":
            return True, dom_els(False)          # чекбокс так и не отмечен
        if m == "page.screenshot":
            return True, SHOT
        return None

    async def main():
        async with Sk48Ext(home, resp) as ext:
            rid = rpc_call(home, "handoff.request",
                           {"what": "отметь «Выбрать все»", "task": "t-48",
                            "expect": {"kind": "dom", "name": "Выбрать все",
                                       "role": "checkbox", "checked": True}})
            ho = await ext.wait_handoff()
            # MAX_RETRY=2: «Готово» №1 и №2 получают ре-пуш, №3 — вердикт наружу
            await ext.resume(ho["id"], task="t-48")
            await ext.wait_handoff(min_count=2)
            await ext.resume(ho["id"], task="t-48")
            await ext.wait_handoff(min_count=3)
            assert ext.handoff_frames()[-1].get("attempt") == 2
            await ext.resume(ho["id"], task="t-48")
            out = await wait_out(home, rid)
            assert out is not None and out["ok"] is True, out
            assert out["result"]["resumed"] is True
            assert out["result"]["verified"] is False, out
            assert out["result"]["verify_err"] == "not_reached", out
            assert not (home / "paused").exists()
            # четвёртого баннера не было
            assert len(ext.handoff_frames()) == 3
            # верификация dom-цели шла через page.dom с name-фильтром на стороне моста
            legs = ext.rpcs("page.dom")
            assert [f["id"] for f in legs] == [f"{rid}~vf1", f"{rid}~vf2", f"{rid}~vf3"], legs
    run(main())


def test_handoff_expect_dom_reached(home):
    def resp(m, f):
        if m == "page.dom":
            return True, dom_els(True)
        return None

    async def main():
        async with Sk48Ext(home, resp) as ext:
            rid = rpc_call(home, "handoff.request",
                           {"what": "отметь «Выбрать все»", "task": "t-48",
                            "expect": {"kind": "dom", "name": "выбрать все",  # регистр не важен
                                       "role": "checkbox", "checked": True}})
            ho = await ext.wait_handoff()
            await ext.resume(ho["id"], task="t-48")
            out = await wait_out(home, rid)
            assert out is not None and out["result"] == {"resumed": True, "verified": True}, out
    run(main())


# ---- expect: отказ/таймаут лега верификации -------------------------------------------

def test_handoff_expect_leg_error(home):
    def resp(m, f):
        if m == "page.media":
            return False, "no_content_script"
        return None

    async def main():
        async with Sk48Ext(home, resp) as ext:
            rid = rpc_call(home, "handoff.request",
                           {"what": "нажми Play", "task": "t-48",
                            "expect": {"kind": "video_playing"}})
            ho = await ext.wait_handoff()
            await ext.resume(ho["id"], task="t-48")
            out = await wait_out(home, rid)
            assert out is not None and out["ok"] is True, out
            assert out["result"]["verified"] is False
            assert out["result"]["verify_err"] == "no_content_script", out
            assert not (home / "paused").exists()
            assert len(ext.handoff_frames()) == 1  # ре-пуша нет: читать не удалось
    run(main())


def test_handoff_expect_verify_timeout(tmp_path):
    # SIDEKICK_RPC_TIMEOUT=1 → верификационный лег протухает за ~3 с; свой мост,
    # без fixture-home (порт один на все тесты)
    p = start_bridge(tmp_path, env_extra={"SIDEKICK_RPC_TIMEOUT": 1})
    home = tmp_path
    try:
        async def main():
            async with Sk48Ext(home) as ext:  # responder=None — верификационный лег молчит
                rid = rpc_call(home, "handoff.request",
                               {"what": "нажми Play", "task": "t-48",
                                "expect": {"kind": "video_playing"}})
                ho = await ext.wait_handoff()
                await ext.resume(ho["id"], task="t-48")
                out = await wait_out(home, rid, timeout=9.0)
                assert out is not None and out["ok"] is True, out
                assert out["result"]["verified"] is False
                assert out["result"]["verify_err"] == "verify_timeout", out
                assert not (home / "paused").exists()
        run(main())
    finally:
        kill_bridge(p)


# ---- рука без expect — прежнее поведение ----------------------------------------------

def test_handoff_without_expect_verdict_unchanged(home):
    async def main():
        async with Sk48Ext(home) as ext:
            rid = rpc_call(home, "handoff.request", {"what": "введи пароль", "task": "t-8"})
            ho = await ext.wait_handoff()
            await ext.resume(ho["id"], task="t-8")
            out = await wait_out(home, rid)
            assert out is not None and out["ok"] is True, out
            assert out["result"] == {"resumed": True}, out  # без verified/verify_err
            assert ext.rpcs("page.media") == [] and ext.rpcs("page.dom") == []
    run(main())


# ---- post_state в аудите action.do -----------------------------------------------------

def test_audit_carries_post_state(home):
    exec_res = {"done": True, "executed": True, "action": "click", "synthetic": True,
                "url": "http://127.0.0.1:8899/noop.html",
                "post_state": {"kind": "checkbox", "before": False, "after": False,
                               "changed": False, "waited_ms": 250}}
    rid = rpc_call(home, "action.do", {"selector": "#sel-all", "action": "click",
                                       "name": "Выбрать все"})
    seen = mock_chain(home, {"shown": True, "cancelled": False, "held_ms": 0,
                             "label": "кликну «Выбрать все»"}, exec_reply=exec_res)
    assert [f["method"] for f in seen] == ["action.preview", "action.do"]
    out = read_rpc_out(home, rid, timeout=4.0)
    assert out is not None and out["ok"] is True, out
    assert out["result"]["post_state"] == exec_res["post_state"], out
    aud = read_jsonl(home / "audit.jsonl")
    row = [a for a in aud if a.get("method") == "action.do" and a.get("ok") is True]
    assert row, aud
    assert row[-1]["post_state"] == exec_res["post_state"], row[-1]


def test_audit_no_post_state_key_without_toggle(home):
    exec_res = {"done": True, "executed": True, "action": "click", "synthetic": True,
                "url": "http://127.0.0.1:8899/plain.html"}
    rid = rpc_call(home, "action.do", {"selector": "#ok", "action": "click", "name": "Ок"})
    mock_chain(home, {"shown": True, "cancelled": False, "held_ms": 0,
                      "label": "кликну «Ок»"}, exec_reply=exec_res)
    assert read_rpc_out(home, rid, timeout=4.0) is not None
    aud = read_jsonl(home / "audit.jsonl")
    row = [a for a in aud if a.get("method") == "action.do" and a.get("ok") is True]
    assert row and "post_state" not in row[-1], row


# ---- node-юнит: SK-17 executor + post_state --------------------------------------------

NODE_EXEC_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
eval(src); // sloppy-mode eval: sk48ToggleState/sk48ClickWrap/sk17ActionDo — в глобальной области

class Ev { constructor(type, opts = {}) { this.type = type; Object.assign(this, opts); } }
const win = { Event: Ev, InputEvent: Ev, innerWidth: 1280, innerHeight: 720, scrollY: 0 };

function makeEl(tag, extra) {
  const el = {
    tagName: tag, type: tag === "INPUT" ? "checkbox" : "", checked: false,
    attrs: {}, events: [], clicks: 0,
    getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
    setAttribute(n, v) { this.attrs[n] = String(v); },
    getBoundingClientRect() { return { left: 10, top: 20, width: 100, height: 20, right: 110, bottom: 40 }; },
    dispatchEvent(e) { this.events.push(e.type); return true; },
    click() { if (tag === "INPUT" && this.type === "checkbox") this.checked = !this.checked; this.clicks++; },
    closest(sel) { return this.__closest ? this.__closest(sel) : null; },
  };
  return Object.assign(el, extra || {});
}
function makeDoc(el) {
  return { defaultView: win, location: { href: "http://127.0.0.1:8899/noop.html" },
           querySelector: () => el };
}

const out = {};
(async () => {
  // 1) чекбокс, клик реально тоглит: post_state changed:true
  let el = makeEl("INPUT");
  out.toggle = await sk17ActionDo({ action: "click", selector: "#cb" }, makeDoc(el));

  // 2) silent-noop (Ozon «Выбрать все»): click() ничего не меняет — changed:false + waited_ms
  el = makeEl("INPUT", { click() { this.clicks++; } });
  out.noop = await sk17ActionDo({ action: "click", selector: "#cb" }, makeDoc(el));

  // 3) клик по span внутри label с control-чекбоксом: состояние читаем у control
  const ctl = makeEl("INPUT", { click() { this.checked = !this.checked; } });
  el = makeEl("SPAN", { __closest: (sel) => (sel === "label" ? { control: ctl } : null),
    click() { ctl.checked = !ctl.checked; } });
  out.label = await sk17ActionDo({ action: "click", selector: "#lbl" }, makeDoc(el));
  out.label_checked = ctl.checked;

  // 4) aria-pressed кнопка (Play/Pause): клик переписывает aria-pressed
  el = makeEl("BUTTON", { attrs: { "aria-pressed": "false" },
    click() { this.setAttribute("aria-pressed", this.getAttribute("aria-pressed") === "true" ? "false" : "true"); } });
  out.pressed = await sk17ActionDo({ action: "click", selector: "#play" }, makeDoc(el));

  // 5) aria role=switch mixed: before "mixed", клик ставит true
  el = makeEl("BUTTON", { attrs: { role: "switch", "aria-checked": "mixed" },
    click() { this.setAttribute("aria-checked", "true"); } });
  out.mixed = await sk17ActionDo({ action: "click", selector: "#sw" }, makeDoc(el));

  // 6) не-тумблер: ключа post_state нет вовсе
  el = makeEl("A");
  out.plain = await sk17ActionDo({ action: "click", selector: "#a" }, makeDoc(el));

  // 7) input-действие по чекбоксу: post_state не положен (не клик)
  el = makeEl("INPUT", { type: "checkbox" });
  el.__proto_value = undefined;
  out.input_no_ps = await sk17ActionDo({ action: "input", selector: "#cb", value: "x" }, makeDoc(el));

  // 8) цель исчезла из DOM после клика: changed:null + note (не «изменилось»)
  el = makeEl("INPUT", { click() { this.type = "text"; this.checked = false; } });
  out.gone = await sk17ActionDo({ action: "click", selector: "#cb" }, makeDoc(el));

  // 9) dblclick по тумблеру — тоже в чехле (sid-путь: selector-dblclick не бывает)
  el = makeEl("INPUT", { click() { this.checked = true; } });
  out.dbl = await sk17ActionDo({ action: "dblclick", sid: "abcd1234" }, makeDoc(el),
                               null, () => ({ el }));

  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String((e && e.stack) || e) })); process.exit(1); });
"""


def _run_node(tmp_path, block_re, harness):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    m = re.search(block_re, JS, re.S)
    assert m, "блок с маркерами исчез из content.js"
    block = tmp_path / "block.js"
    block.write_text(m.group(1), encoding="utf-8")
    h = tmp_path / "harness.js"
    h.write_text(SK71_SHIM + harness, encoding="utf-8")
    r = subprocess.run([node, str(h), str(block)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    out = json.loads(r.stdout)
    assert "fatal" not in out, out.get("fatal")
    return out


def test_post_state_node_unit(tmp_path):
    out = _run_node(tmp_path, r"// SK-17-BEGIN executor[^\n]*\n(.*?)// SK-17-END executor",
                    NODE_EXEC_HARNESS)
    # 1) реальный тоггл
    ps = out["toggle"].get("post_state")
    assert out["toggle"]["executed"] is True
    assert ps == {"kind": "checkbox", "before": False, "after": True, "changed": True}, ps
    # 2) silent-noop: executed:true + changed:false + повторная выборка через waited_ms
    ps = out["noop"]["post_state"]
    assert ps["changed"] is False and ps["before"] is False and ps["after"] is False, ps
    assert ps["waited_ms"] >= 250, ps
    # 3) label→control
    ps = out["label"]["post_state"]
    assert ps["kind"] == "checkbox" and ps["changed"] is True, ps
    assert out["label_checked"] is True
    # 4) aria-pressed
    ps = out["pressed"]["post_state"]
    assert ps["kind"] == "pressed" and ps["before"] is False and ps["after"] is True, ps
    # 5) mixed → true
    ps = out["mixed"]["post_state"]
    assert ps["before"] == "mixed" and ps["after"] is True and ps["changed"] is True, ps
    # 6) не-тумблер — ключа нет
    assert "post_state" not in out["plain"], out["plain"]
    # 7) input — ключа нет
    assert "post_state" not in out["input_no_ps"], out["input_no_ps"]
    # 8) исчезнувшая цель — changed:null + note, не «changed:true»
    ps = out["gone"]["post_state"]
    assert ps["changed"] is None and ps["after"] is None and "note" in ps, ps
    # 9) dblclick в чехле
    assert out["dbl"]["post_state"]["changed"] is True, out["dbl"]


# ---- node-юнит: sk48CheckedOf (снапшот) + pageMedia ------------------------------------

NODE_SNAP_HARNESS = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8")); // SK-48 snapshot block
eval(fs.readFileSync(process.argv[3], "utf8")); // SK-48 media block (использует глобальные document/location)

function el(tag, o) {
  const e = Object.assign({ tagName: tag, attrs: {},
    getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
    closest() { return null; } }, o || {});
  return e;
}
const out = { snap: {} };
const s = out.snap;
s.native_on = sk48CheckedOf(el("INPUT", { type: "checkbox", checked: true }));
s.native_off = sk48CheckedOf(el("INPUT", { type: "checkbox", checked: false }));
s.radio = sk48CheckedOf(el("INPUT", { type: "radio", checked: true }));
s.label_ctl = sk48CheckedOf(el("LABEL", { control: { type: "checkbox", checked: true } }));
s.inside_label = sk48CheckedOf(el("SPAN", { closest: () => ({ control: { type: "checkbox", checked: false } }) }));
s.switch_mixed = sk48CheckedOf(el("BUTTON", { attrs: { role: "switch", "aria-checked": "mixed" } }));
s.checkbox_absent = sk48CheckedOf(el("BUTTON", { attrs: { role: "checkbox" } })); // без aria-checked = false
s.pressed = sk48CheckedOf(el("BUTTON", { attrs: { "aria-pressed": "true" } }));
s.expanded_via_closest = sk48CheckedOf(el("SPAN", { closest: () => ({ getAttribute: (n) => (n === "aria-expanded" ? "true" : null), attrs: {} }) }));
s.plain_button = sk48CheckedOf(el("BUTTON")) === undefined ? "undef" : "DEFINED";
s.plain_div = sk48CheckedOf(el("DIV")) === undefined ? "undef" : "DEFINED";
// JSON-сериализация: у не-тумблера ключа нет вовсе
s.json_drop = !("checked" in JSON.parse(JSON.stringify({ role: "button", checked: sk48CheckedOf(el("BUTTON")) })));

// pageMedia: глобальные document/location — мок
const vids = [
  { paused: false, ended: false, muted: true, volume: 0.649, currentTime: 12.349,
    duration: 600, readyState: 3, src: "http://cdn/video?token=SECRET",
    getAttribute: (n) => (n === "aria-label" ? "player" : null),
    getBoundingClientRect: () => ({ left: 1, top: 2, width: 640.4, height: 360.6 }) },
  { paused: true, ended: false, muted: false, volume: 1, currentTime: 0,
    duration: NaN, readyState: 0,
    getAttribute: () => null,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 0, height: 0 }) },
];
global.document = { querySelectorAll: (sel) => (sel === "video" ? vids : []) };
global.location = { href: "http://127.0.0.1:8899/media.html" };
global.window = { innerWidth: 1280, innerHeight: 720 };
out.media = pageMedia();
console.log(JSON.stringify(out));
"""


def test_snapshot_and_media_node_unit(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    ms = re.search(r"// SK-48-BEGIN snapshot[^\n]*\n(.*?)// SK-48-END snapshot", JS, re.S)
    mm = re.search(r"// SK-48-BEGIN media[^\n]*\n(.*?)// SK-48-END media", JS, re.S)
    assert ms and mm, "блоки SK-48 исчезли из content.js"
    b1 = tmp_path / "snap.js"; b1.write_text(ms.group(1), encoding="utf-8")
    b2 = tmp_path / "media.js"; b2.write_text(mm.group(1), encoding="utf-8")
    h = tmp_path / "harness.js"; h.write_text(SK71_SHIM + NODE_SNAP_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(h), str(b1), str(b2)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    out = json.loads(r.stdout)
    s = out["snap"]
    assert s["native_on"] is True and s["native_off"] is False and s["radio"] is True
    assert s["label_ctl"] is True and s["inside_label"] is False
    assert s["switch_mixed"] == "mixed"
    assert s["checkbox_absent"] is False, "role=checkbox без aria-checked — это false"
    assert s["pressed"] is True and s["expanded_via_closest"] is True
    assert s["plain_button"] == "undef" and s["plain_div"] == "undef"  # не-тумблер → undefined
    assert s["json_drop"] is True, "ключ checked у не-тумблера обязан выпадать на сериализации"
    m = out["media"]
    assert m["url"] == "http://127.0.0.1:8899/media.html" and m["count"] == 2
    assert m["vp"] == {"w": 1280, "h": 720}, "vp обязателен: «играет» = видимое в вьюпорте"
    v0 = m["videos"][0]
    assert v0["paused"] is False and v0["muted"] is True and v0["readyState"] == 3
    assert v0["currentTime"] == 12.3 and v0["duration"] == 600.0 and v0["volume"] == 0.65
    assert v0["rect"] == {"x": 1, "y": 2, "w": 640, "h": 361}
    assert v0["name"] == "player"
    assert "src" not in json.dumps(m), "src не отдаём (может нести токенты)"
    v1 = m["videos"][1]
    assert v1["duration"] is None and v1["readyState"] == 0 and "name" not in v1
