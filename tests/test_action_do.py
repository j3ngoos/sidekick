"""SK-17 contract: action.do = preview-гейт на мосту + исполнение в расширении.

Гейт — смысл метода: мост обязан цепочкой action.preview -> action.do и НЕ выпускать
исполняющий лег, пока вердикт не чистый. Пины здесь:
  * чистый вердикт -> расширение сначала видит action.preview, потом action.do с
    АГЕНТНЫМ rpc id; результат исполнения проходит насквозь (executed/synthetic);
  * ■ Stop (cancelled) / superseded -> ok:true {shown:true,cancelled:true,executed:false}
    и исполняющий лег НЕ отправляется никогда;
  * no_element/bad_selector -> executed:false, лока не слан;
  * bad_request валидируется без раунд-трипа к расширению;
  * ровно ОДИН ev:"preview" кадр аудита на action.do (двойного превью нет);
  * paused_for_human при hub/paused; v обязателен; таймаут гейт-лега -> err timeout,
    ничего не исполняется;
  * аддитивность v1: action.preview остаётся отдельным round-trip, hello не тронут.

Плюс node-юнит исполняющего блока content.js (между маркерами SK-17-BEGIN/END):
React-совместимый нативный сеттер (instance-оверрайда React-трекера не должен
перехватывать сеттер прототипа), click/submit-пути, формы ошибок. Живая часть
(реальный клик на стенде, ozon-подобная форма) — приёмка панели, не pytest.
"""
import asyncio
import json
import pathlib
import re
import shutil
import subprocess
import time

import pytest
import websockets

from conftest import kill_bridge, refire_noext, start_bridge, token_of
from test_rpc_e2e import mock_extension, read_rpc_out, rpc_call

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "extension" / "content.js").read_text()
URI = "ws://127.0.0.1:8799"

CLEAN = {"shown": True, "cancelled": False, "held_ms": 450, "label": "кликну «Войти»"}
STOP = {"shown": True, "cancelled": True, "reason": "user_stop"}
SUPERSEDED = {"shown": True, "cancelled": True, "reason": "superseded"}
NOEL = {"shown": False, "cancelled": False, "reason": "no_element"}
BADSEL = {"shown": False, "cancelled": False, "reason": "bad_selector"}

EXEC_OK = {"done": True, "executed": True, "action": "click", "synthetic": True,
           "url": "http://127.0.0.1:8899/form.html"}


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


def mock_chain(home, preview_verdict, exec_reply=None, exec_err=None, expect_exec=True):
    """Одно mock-расширение на всю цепочку action.do.

    На гейт-превью отвечает ВСЕГДА. Если мост после вердикта шлёт исполняющий лег
    (method action.do) — отвечает и на него. Если expect_exec=False и лег всё же
    пришёл в течение 0.7 с — тест падает: гейт отказал, а исполнение ушло.
    Возвращает список rpc-кадров, которые мост прислал расширению (по порядку)."""
    seen = []

    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            ack = json.loads(await asyncio.wait_for(c.recv(), 3))
            assert ack.get("ok") is True, ack
            answered = False
            deadline = time.time() + 6
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(c.recv(), min(0.5, max(0.05, deadline - time.time())))
                except asyncio.TimeoutError:
                    refire_noext(home, "action.do")  # гонка no_extension (см. conftest)
                    continue
                msg = json.loads(raw)
                if msg.get("type") != "rpc":
                    continue
                seen.append(msg)
                if msg.get("method") == "action.preview":
                    answered = True
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                             "ok": True, "result": preview_verdict},
                                            ensure_ascii=False))
                    if expect_exec:
                        continue
                    # отказ гейта: исполняющего лега быть не ни в коем случае
                    stop_at = time.time() + 0.7
                    while time.time() < stop_at:
                        try:
                            m2 = json.loads(await asyncio.wait_for(
                                c.recv(), max(0.05, stop_at - time.time())))
                        except asyncio.TimeoutError:
                            break
                        if m2.get("type") == "rpc" and m2.get("method") == "action.do":
                            raise AssertionError("execution leg sent after a refused gate")
                    break
                elif msg.get("method") == "action.do":
                    if exec_err:
                        await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                                 "ok": False, "err": exec_err}))
                    else:
                        await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                                 "ok": True, "result": exec_reply or EXEC_OK},
                                                ensure_ascii=False))
                    break
            assert answered, "bridge never sent the gate preview"
            return seen
    return run(_m())


# ---- гейт: чистый вердикт -> исполнение тем же агентным id ---------------------

def test_clean_gate_executes_on_agent_id(home):
    rid = rpc_call(home, "action.do",
                   {"selector": "#login-btn", "action": "click", "name": "Войти"})
    seen = mock_chain(home, CLEAN)
    out = read_rpc_out(home, rid)
    assert [f["method"] for f in seen] == ["action.preview", "action.do"]
    pv, ex = seen
    assert pv["params"] == {"selector": "#login-btn", "action": "click", "name": "Войти"}
    assert pv["id"] != rid and pv["id"].startswith(rid)  # внутренний лег, не агентный id
    assert ex["id"] == rid  # исполнение — на id агента
    assert ex["params"] == {"selector": "#login-btn", "action": "click", "name": "Войти"}
    assert out is not None and out["ok"] is True
    assert out["result"]["executed"] is True and out["result"]["synthetic"] is True
    # вердикт внутреннего лега НЕ попадает в rpc_out отдельной строкой
    outs = [json.loads(ln) for ln in (home / "rpc_out.jsonl").read_text().splitlines()]
    assert [o for o in outs if o.get("id") == pv["id"]] == []


def test_preview_params_pass_through(home):
    rid = rpc_call(home, "action.do",
                   {"selector": "#q", "action": "input", "value": "пылесос", "ms": 300})
    seen = mock_chain(home, CLEAN,
                      exec_reply={"done": True, "executed": True, "action": "input",
                                  "synthetic": True, "url": "http://x/"})
    assert seen[0]["params"] == {"selector": "#q", "action": "input", "ms": 300}
    assert seen[1]["params"] == {"selector": "#q", "action": "input", "value": "пылесос", "ms": 300}
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"]["executed"] is True


# ---- гейт: отказ -> исполнения НЕТ ---------------------------------------------

@pytest.mark.parametrize("verdict", [STOP, SUPERSEDED])
def test_cancelled_refuses_execution(home, verdict):
    rid = rpc_call(home, "action.do", {"selector": "#login-btn", "action": "click"})
    seen = mock_chain(home, verdict, expect_exec=False)
    assert [f["method"] for f in seen] == ["action.preview"]  # лег исполнения не послан
    out = read_rpc_out(home, rid)
    assert out is not None and out["ok"] is True  # вето — успешный ответ (SK-05)
    r = out["result"]
    assert r["shown"] is True and r["cancelled"] is True
    assert r["executed"] is False and r["reason"] == verdict["reason"]
    assert r["action"] == "click"


@pytest.mark.parametrize("verdict", [NOEL, BADSEL])
def test_stale_selector_refuses_execution(home, verdict):
    rid = rpc_call(home, "action.do", {"selector": "#gone", "action": "click"})
    seen = mock_chain(home, verdict, expect_exec=False)
    assert [f["method"] for f in seen] == ["action.preview"]
    out = read_rpc_out(home, rid)
    assert out["ok"] is True
    r = out["result"]
    assert r["shown"] is False and r["cancelled"] is False
    assert r["executed"] is False and r["reason"] == verdict["reason"]


def test_gate_leg_failure_is_err_not_execution(home):
    rid = rpc_call(home, "action.do", {"selector": "#a", "action": "click"})
    # gate leg answers ok:false — мост обязан отказать, а не исполнять вслепую
    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            assert json.loads(await asyncio.wait_for(c.recv(), 3)).get("ok") is True
            deadline = time.time() + 4
            while time.time() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(c.recv(), max(0.1, deadline - time.time())))
                except asyncio.TimeoutError:
                    # харнесс-гонка «rpc_in drain раньше подключения мока» (conftest.refire_noext):
                    # под нагрузкой мост успевает ответить no_extension до handshake мока —
                    # перезаряжаем кадр и ждём дальше, иначе тест уедет в TimeoutError/no_extension
                    refire_noext(home, "action.do")
                    continue
                if msg.get("type") == "rpc" and msg.get("method") == "action.preview":
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                             "ok": False, "err": "no_content_script"}))
                    return
    run(_m())
    out = read_rpc_out(home, rid)
    assert out["ok"] is False and out["err"] == "no_content_script"


def test_exec_error_passes_through(home):
    rid = rpc_call(home, "action.do", {"selector": "#a", "action": "click"})
    mock_chain(home, CLEAN, exec_err="no_element")
    out = read_rpc_out(home, rid)
    assert out["ok"] is False and out["err"] == "no_element"


# ---- валидация и общие ворота ---------------------------------------------------

def test_bad_request_without_extension_roundtrip(home):
    for params in ({"selector": "#a"},
                   {"selector": "#a", "action": "hover"},
                   {"action": "click"},
                   {"selector": "   ", "action": "click"},
                   {"selector": "#a", "action": "input"},
                   {"selector": "#a", "action": "input", "value": 5}):
        rid = rpc_call(home, "action.do", params)
        out = read_rpc_out(home, rid, timeout=2.0)
        assert out is not None and out["ok"] is False and out["err"] == "bad_request", params


def test_no_extension_immediate(home):
    rid = rpc_call(home, "action.do", {"selector": "#a", "action": "click"})
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "no_extension"


def test_paused_for_human(home):
    (home / "paused").touch()  # SK-08: человек у клавиатуры — исполнение запрещено
    rid = rpc_call(home, "action.do", {"selector": "#a", "action": "click"})
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "paused_for_human"


def test_requires_version(home):
    rid = rpc_call(home, "action.do", {"selector": "#a", "action": "click"}, v=None)
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "no_version"


def test_gate_leg_timeout_refuses_execution(tmp_path):
    p = start_bridge(tmp_path, {"SIDEKICK_RPC_TIMEOUT": "1"})
    try:
        async def _silent():
            async with websockets.connect(URI, open_timeout=3) as c:
                await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(tmp_path)}))
                assert json.loads(await asyncio.wait_for(c.recv(), 3)).get("ok") is True
                rid = rpc_call(tmp_path, "action.do", {"selector": "#a", "action": "click"})
                got = []
                deadline = time.time() + 3
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(c.recv(), max(0.1, deadline - time.time()))
                    except asyncio.TimeoutError:
                        break
                    m = json.loads(raw)
                    if m.get("type") == "rpc":
                        got.append(m["method"])
                assert got == ["action.preview"], f"молчаливый гейт породил лег: {got}"
                return rid
        rid = run(_silent())
        out = read_rpc_out(tmp_path, rid, timeout=4.0)
        assert out is not None and out["ok"] is False and out["err"] == "timeout"
    finally:
        kill_bridge(p)


# ---- аудит: один action.do = один preview-кадр ----------------------------------

def test_single_preview_audit_frame(home):
    rid = rpc_call(home, "action.do", {"selector": "#a", "action": "click"})
    mock_chain(home, CLEAN)
    assert read_rpc_out(home, rid) is not None
    evs = [e for e in read_jsonl(home / "audit.jsonl") if e.get("ev") == "preview"]
    assert len(evs) == 1, f"двойное превью в аудите: {evs}"
    assert evs[0]["via"] == "action.do" and evs[0]["cancelled"] is False
    assert any(a.get("method") == "action.do" and a.get("ok") is True
               for a in read_jsonl(home / "audit.jsonl"))


def test_refusal_also_single_preview_frame(home):
    rid = rpc_call(home, "action.do", {"selector": "#a", "action": "click"})
    mock_chain(home, STOP, expect_exec=False)
    assert read_rpc_out(home, rid) is not None
    evs = [e for e in read_jsonl(home / "audit.jsonl") if e.get("ev") == "preview"]
    assert len(evs) == 1 and evs[0]["cancelled"] is True and evs[0]["reason"] == "user_stop"


# ---- аддитивность контракта v1 ----------------------------------------------------

def test_action_preview_stays_standalone(home):
    """SK-17 аддитивен: отдельный round-trip action.preview жив, вердикты не поехали."""
    rid = rpc_call(home, "action.preview", {"selector": "#a", "action": "click"})
    frame = run(mock_extension(home, reply=CLEAN, expect_methods=["action.preview"]))
    assert frame["method"] == "action.preview" and frame["id"] == rid
    out = read_rpc_out(home, rid)
    assert out is not None and out["ok"] is True and out["result"] == CLEAN


def test_hello_contract_untouched(home):
    """hello остаётся v:1 — action.do не поднял версию (контракт v1, п.5 карты)."""
    async def _h():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            ack = json.loads(await asyncio.wait_for(c.recv(), 3))
            assert ack == {"v": 1, "type": "hello_ack", "ok": True}, ack
    run(_h())


# ---- статические пины проводки ------------------------------------------------------

def test_content_wires_action_do():
    assert 'm.type === "action_do"' in JS
    # SK-26: третьим аргументом — карта SoM-меток (mark-режим резолвится ею); без неё
    # mark-лег честно отказывает mark_stale, а не кликает вслепую.
    # SK-27: четвёртым — резолвер sid-реестра page.dom; без него sid-лег отказал бы
    # sid_stale вместо слепого клика (та же семантика честного отказа).
    assert "sk17ActionDo(m, document, somMap, (s) => sk27ResolveSid(s, document))" in JS
    assert "synthetic: true" in JS  # п.4 карты: ограничение задокументировано в ответе


def test_background_routes_action_do():
    bg = (ROOT / "extension" / "background.js").read_text()
    assert 'method === "action.do"' in bg
    assert 'type: "action_do"' in bg


def test_bridge_chains_instead_of_forwarding():
    src = (ROOT / "bridge" / "sidekick_bridge.py").read_text()
    assert 'if method == "action.do":' in src
    assert '"method": "action.preview"' in src  # гейт-лег шлёт сам мост


# ---- node-юнит исполняющего блока content.js ---------------------------------------

NODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
eval(src); // определяет sk17NativeSet / sk17ActionDo (sloppy-mode eval)

class Ev { constructor(type, opts = {}) { this.type = type; Object.assign(this, opts); } }
class InputEvent extends Ev {}
function makeProto() {
  const F = function () {};
  Object.defineProperty(F.prototype, "value", {
    configurable: true,
    set(v) { this.__proto_value = v; },   // «нативный» DOM-слот
    get() { return this.__proto_value; },
  });
  return F;
}
const win = { HTMLInputElement: makeProto(), HTMLTextAreaElement: makeProto(),
              HTMLSelectElement: makeProto(), Event: Ev, InputEvent: InputEvent };
function makeEl(tag, extra) {
  const protoCtor = { INPUT: win.HTMLInputElement, TEXTAREA: win.HTMLTextAreaElement,
                      SELECT: win.HTMLSelectElement }[tag] || function () {};
  const el = Object.create(protoCtor.prototype);
  el.tagName = tag; el.events = []; el.clicks = 0;
  el.click = () => { el.clicks++; };
  el.dispatchEvent = (e) => { el.events.push({ type: e.type, bubbles: e.bubbles,
                                               composed: e.composed, data: e.data }); };
  return Object.assign(el, extra || {});
}
function makeDoc(el) {
  return { defaultView: win, location: { href: "http://127.0.0.1:8899/form.html" },
           querySelector: (sel) => {
             if (sel === "!!bad") throw new Error("bad selector");
             if (sel === "#none") return null;
             return el;
           } };
}

const out = {};
(async () => {
  // 1) React-подобный инпут: instance-дескриптор (трекер React) НЕ должен перехватывать
  //    запись — сеттер ПРОТОТИПА пишет DOM-слот, события input+change уходят bubbling.
  let el = makeEl("INPUT");
  let reactTracked = "STALE";
  Object.defineProperty(el, "value", { configurable: true,
    set(v) { reactTracked = "TRACKED:" + v; }, get() { return reactTracked; } });
  let doc = makeDoc(el);
  out.input = await sk17ActionDo({ action: "input", selector: "#email", value: "hi" }, doc);
  out.input_proto_value = el.__proto_value;
  out.input_react_untouched = reactTracked === "STALE";
  out.input_events = el.events;

  // 2) textarea/select — свои прототипы
  el = makeEl("TEXTAREA"); doc = makeDoc(el);
  out.textarea = await sk17ActionDo({ action: "input", selector: "#t", value: "x" }, doc);
  out.textarea_proto_value = el.__proto_value;
  el = makeEl("SELECT"); doc = makeDoc(el);
  out.select = await sk17ActionDo({ action: "input", selector: "#s", value: "opt2" }, doc);
  out.select_proto_value = el.__proto_value;

  // 3) click
  el = makeEl("BUTTON"); doc = makeDoc(el);
  out.click = await sk17ActionDo({ action: "click", selector: "#b" }, doc);
  out.clicks = el.clicks;

  // 4) submit: requestSubmit в приоритете; fallback submit(); без формы — ошибка
  let via = "";
  el = makeEl("BUTTON", { form: { requestSubmit: () => { via = "requestSubmit"; } } });
  doc = makeDoc(el);
  out.submit = await sk17ActionDo({ action: "submit", selector: "#go" }, doc);
  out.submit_via = via;
  via = "";
  el = makeEl("FORM", { submit: () => { via = "submit()"; } }); doc = makeDoc(el);
  out.submit_fallback = await sk17ActionDo({ action: "submit", selector: "form" }, doc);
  out.submit_fallback_via = via;
  el = makeEl("BUTTON"); doc = makeDoc(el);
  out.submit_no_form = await sk17ActionDo({ action: "submit", selector: "#x" }, doc);

  // 5) формы ошибок
  doc = makeDoc(makeEl("INPUT"));
  out.no_element = await sk17ActionDo({ action: "click", selector: "#none" }, doc);
  out.bad_selector = await sk17ActionDo({ action: "click", selector: "!!bad" }, doc);
  out.bad_action = await sk17ActionDo({ action: "hover", selector: "#a" }, doc);
  el = makeEl("DIV"); doc = makeDoc(el);
  out.not_editable = await sk17ActionDo({ action: "input", selector: "#d", value: "v" }, doc);

  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String((e && e.stack) || e) })); process.exit(1); });
"""


def test_executor_node_unit(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    m = re.search(r"// SK-17-BEGIN executor[^\n]*\n(.*?)// SK-17-END executor", JS, re.S)
    assert m, "исполняющий блок с маркерами SK-17 исчез из content.js"
    block = tmp_path / "sk17_executor.js"
    block.write_text(m.group(1), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(harness), str(block)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    out = json.loads(r.stdout)
    assert "fatal" not in out, out.get("fatal")

    # input: значение легло через сеттер ПРОТОТИПА, трекер-оверрайд не перехватил
    assert out["input"] == {"done": True, "executed": True, "action": "input",
                            "synthetic": True, "url": "http://127.0.0.1:8899/form.html"}
    assert out["input_proto_value"] == "hi"
    assert out["input_react_untouched"] is True
    assert [e["type"] for e in out["input_events"]] == ["input", "change"]
    assert all(e["bubbles"] and e["composed"] for e in out["input_events"])
    assert out["input_events"][0]["data"] == "hi"
    assert out["textarea_proto_value"] == "x" and out["textarea"]["executed"] is True
    assert out["select_proto_value"] == "opt2" and out["select"]["executed"] is True

    # click
    assert out["clicks"] == 1 and out["click"]["executed"] is True
    assert out["click"]["synthetic"] is True

    # submit
    assert out["submit_via"] == "requestSubmit"
    assert out["submit_fallback_via"] == "submit()"
    assert out["submit_no_form"] == {"err": "no_form"}

    # ошибки
    assert out["no_element"] == {"err": "no_element"}
    assert out["bad_selector"] == {"err": "bad_selector"}
    assert out["bad_action"] == {"err": "bad_request"}
    assert out["not_editable"]["err"] == "not_editable"  # SK-42: + hint (tag/CE цели)
    assert "tag=div" in out["not_editable"]["hint"]
