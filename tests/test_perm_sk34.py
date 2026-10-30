"""SK-34: трение человека — меньше вопросов, «всегда» в один тап, честный таймаут.

Проверяет ровно то, что болит (аудит 21.10 п.3, п.8):
  * perm-гейт ждёт 60 с, не 300;
  * «Всегда для сайта» — ответ always_site: вердикт + grant одним кадром,
    без второго вопроса (регресс: раньше человек жмал «всегда» дважды);
  * чувствительные классы (payment/oauth/2fa/sensitive_input/send_message/
    send_email, банк, auth-путь) «всегда» не получают никогда: always/always_site
    понижаются до разово, grant не пишется;
  * ask_timeout = отказ: два молчаливых таймаута одного пермиса — третий запрос
    того же типа блокируется без вопроса человеку (ask_timeout_streak); живой
    ответ человека стрик обнуляет;
  * текст наружу (send_message/send_email) — всегда-человеческий класс;
  * причина отказа (perm_reason) — только аудит, вердикт не меняет;
  * превью вне потока задачи (standalone action.preview) аудируется via=standalone;
  * агентд: perm_timeout со streak>=2 паузирует задачу (атрибуция по task),
    perm_pause_text — явный вопрос в чат.
"""
import asyncio
import json
import time

import websockets

from conftest import kill_bridge, start_bridge, token_of
from test_perm import (MockExt, answered, grants_of, home,  # noqa: F401
                       perm_check, read_jsonl, rpc_ok, run)
from test_preview import mock_preview
from test_rpc_e2e import rpc_call

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agentd"))
import sidekick_agentd as agd  # noqa: E402


def audits(home):
    return read_jsonl(home / "audit.jsonl")


# ---- таймаут гейта: 60 с, не 300 -----------------------------------------------------

def test_perm_gate_timeout_is_60s():
    import re
    src = (ROOT / "bridge" / "sidekick_bridge.py").read_text(encoding="utf-8")
    m = re.search(r'PERM_TIMEOUT_S = float\(os\.environ\.get\("SIDEKICK_PERM_TIMEOUT", "(\d+)"\)\)', src)
    assert m and m.group(1) == "60"


# ---- «всегда» в один тап + show_always только после «разово» --------------------------

def test_always_single_frame_grant(home):
    async def main():
        async with MockExt(home) as ext:
            # первый вопрос того же типа — кнопки «всегда» ещё нет (разово не было)
            rid1 = rpc_call(home, "perm.check", {"domain": "shop.example", "action": "submit"})
            ask1 = await ext.wait_ask()
            assert ask1["id"] == rid1 and "show_always" not in ask1
            await ext.answer(rid1, "once")
            assert answered(home, rid1)["result"] == {"decision": "allow", "via": "human_once"}
            # второй вопрос того же типа — «Разрешить для сайта всегда» в один тап:
            # ОДИН кадр даёт и вердикт, и постоянный грант
            rid2 = rpc_call(home, "perm.check", {"domain": "shop.example", "action": "submit"})
            ask2 = await ext.wait_ask()
            assert ask2.get("show_always") is True
            await ext.answer(rid2, "always")
            assert answered(home, rid2)["result"] == {"decision": "allow", "via": "human_always"}
            assert grants_of(home)["shop.example"]["level"] == "allow"
            assert len(ext.asks()) == 2
            # повтор того же класса — молча по гранту
            _, o = perm_check(home, "shop.example", "submit")
            assert o["result"] == {"decision": "allow", "via": "grant_hit"}
            assert len(ext.asks()) == 2
    run(main())


def test_always_sensitive_never_grants_no_button(home):
    async def main():
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check",
                           {"domain": "mail.example", "action": "send_message"})
            ask = await ext.wait_ask()
            assert "show_always" not in ask  # текст наружу — кнопки «всегда» не бывает
            await ext.answer(rid, "always")   # даже кривой клиент не пройдёт
            out = answered(home, rid)
            assert out["result"] == {"decision": "allow", "via": "human_once"}
            assert grants_of(home) == {}      # grant не написан
            assert len(ext.asks()) == 1
    run(main())


def test_send_message_and_email_always_human(home):
    async def main():
        async with MockExt(home) as ext:
            for act in ("send_message", "send_email"):
                rid = rpc_call(home, "perm.check", {"domain": "ok.example", "action": act})
                await ext.wait_ask()
                await ext.answer(rid, "once")
                assert answered(home, rid)["result"]["decision"] == "allow"
            assert grants_of(home) == {}
    run(main())


# ---- ask_timeout = отказ: стрик двух молчаливых таймаутов ----------------------------

def test_timeout_streak_blocks_third_ask_without_human(tmp_path):
    p = start_bridge(tmp_path, env_extra={"SIDEKICK_PERM_TIMEOUT": 1})
    try:
        async def main():
            async with MockExt(tmp_path) as ext:
                for i in (1, 2):
                    rid = rpc_call(tmp_path, "perm.check",
                                   {"domain": "x.example", "action": "click", "task": "t1"})
                    await ext.wait_ask()                       # молчим
                    out = answered(tmp_path, rid, timeout=4.0)
                    assert out["result"] == {"decision": "block", "via": "ask_timeout"}
                assert len(ext.asks()) == 2
                # третий запрос того же типа — block БЕЗ нового perm_ask
                rid3 = rpc_call(tmp_path, "perm.check",
                                {"domain": "x.example", "action": "click", "task": "t1"})
                out3 = answered(tmp_path, rid3, timeout=4.0)
                assert out3["result"] == {"decision": "block", "via": "ask_timeout_streak"}
                assert len(ext.asks()) == 2                    # человека не дёргали
                assert any(a.get("ev") == "perm" and a.get("via") == "ask_timeout_streak"
                           for a in audits(tmp_path))
                # событие для агентда со стриком и задачей
                evs = [e for e in read_jsonl(tmp_path / "events.jsonl")
                       if e.get("type") == "perm_timeout"]
                assert evs and evs[-1]["streak"] == 2 and evs[-1]["task"] == "t1"
                # другой тип пермиса не задет стриком: спросили — есть perm_ask №3
                rid4 = rpc_call(tmp_path, "perm.check",
                                {"domain": "x.example", "action": "submit", "task": "t1"})
                await ext.wait_ask(timeout=2.0)
                assert len(ext.asks()) == 3
                await ext.answer(rid4, "deny")
                assert answered(tmp_path, rid4)["result"]["decision"] == "block"
        run(main())
    finally:
        kill_bridge(p)


def test_human_answer_resets_streak(tmp_path):
    p = start_bridge(tmp_path, env_extra={"SIDEKICK_PERM_TIMEOUT": 1})
    try:
        async def main():
            async with MockExt(tmp_path) as ext:
                rid = rpc_call(tmp_path, "perm.check",
                               {"domain": "y.example", "action": "click", "task": "tz"})
                await ext.wait_ask()
                answered(tmp_path, rid, timeout=4.0)           # таймаут №1
                rid2 = rpc_call(tmp_path, "perm.check",
                                {"domain": "y.example", "action": "click", "task": "tz"})
                await ext.wait_ask()
                await ext.answer(rid2, "once")                 # живой ответ — стрик сброшен
                assert answered(tmp_path, rid2)["result"]["decision"] == "allow"
                rid3 = rpc_call(tmp_path, "perm.check",
                                {"domain": "y.example", "action": "click", "task": "tz"})
                await ext.wait_ask()
                out3 = answered(tmp_path, rid3, timeout=4.0)
                # после живого ответа стрик кочится заново: это ask_timeout, не стрик-блок
                assert out3["result"] == {"decision": "block", "via": "ask_timeout"}
        run(main())
    finally:
        kill_bridge(p)


# ---- причина отказа: только аудит -----------------------------------------------------

def test_deny_reason_is_audit_only(home):
    async def main():
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check", {"domain": "r.example", "action": "click"})
            await ext.wait_ask()
            await ext.answer(rid, "deny")
            assert answered(home, rid)["result"]["decision"] == "block"
            await ext.c.send(json.dumps({"v": 1, "type": "perm_reason", "id": rid,
                                         "reason": "не туда жмёшь"}, ensure_ascii=False))
            await asyncio.sleep(0.3)
            reasons = [a for a in audits(home) if a.get("ev") == "deny_reason"]
            assert reasons and reasons[-1]["reason"] == "не туда жмёшь"
            # вердикт не поменялся: rpc_out для rid ровно один
            outs = [o for o in read_jsonl(home / "rpc_out.jsonl") if o["id"] == rid]
            assert len(outs) == 1 and outs[0]["result"]["decision"] == "block"
            # чужой/несуществующий id — в аудит не пишется
            await ext.c.send(json.dumps({"v": 1, "type": "perm_reason", "id": "nope",
                                         "reason": "мусор"}, ensure_ascii=False))
            await asyncio.sleep(0.3)
            assert not [a for a in audits(home) if a.get("reason") == "мусор"]
    run(main())


# ---- превью вне потока задачи ----------------------------------------------------------

def test_standalone_preview_audited(home):
    rid = rpc_call(home, "action.preview",
                   {"sid": "s", "selector": "b", "action": "click", "domain": "pv.example"})
    mock_preview(home, {"shown": True, "cancelled": False})
    out = answered(home, rid)
    assert out["result"]["shown"] is True
    prev = [a for a in audits(home) if a.get("ev") == "preview"]
    assert prev and prev[-1]["via"] == "standalone"


# ---- агентд: атрибуция паузы по perm_timeout ------------------------------------------

def test_perm_timeout_target_needs_streak_two():
    act = {"t1": object()}
    assert agd.perm_timeout_target({"streak": 1, "task": "t1"}, act) is None
    assert agd.perm_timeout_target({"streak": 2, "task": "t1"}, act) == "t1"
    assert agd.perm_timeout_target({"streak": 2, "task": "ghost"}, act) is None
    # без task — единственная активная задача; при двух угадывать нельзя
    assert agd.perm_timeout_target({"streak": 2}, act) == "t1"
    assert agd.perm_timeout_target({"streak": 2}, {"t1": 0, "t2": 0}) is None


def test_perm_pause_text_is_explicit_question():
    txt = agd.perm_pause_text({"domain": "x.example", "action": "click"})
    assert "x.example" in txt and "click" in txt and "паузу" in txt
    assert "Напиши в чат" in txt  # явный вопрос, а не восьмой карточный
