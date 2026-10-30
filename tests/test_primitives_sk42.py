"""SK-42 contract: примитивы моста — contenteditable, scroll, координатный ввод, хинты.

Аудит 22.10 (инциденты A/B): композер web.telegram.org — [contenteditable], мост
умел писать только в нативные поля → пять not_editable и handoff; прокрутки не было
вовсе → спам клавиш через 8 превью-гейтов при scrollY=0; ошибки были без подсказок →
агент грепал исходники моста. Пины здесь:

  * scroll — ЕДИНСТВЕННЫЙ action.do без превью-гейта: мост форвардит его расширению
    напрямую (ни одного action.preview, ни одного ev:"preview" в аудите); форма
    {dy | to:"top"|"bottom" | sid | mark} — ровно один режим, чужие цели bad_request;
  * input по координатам {x,y,value|text} — обычный гейт (точка), оба лега с x/y;
  * input без цели — фокус-режим: превью-лег без selector/x/y, исполнение насквозь;
  * text — алиас value; оба и разные — bad_request (неоднозначность не приоритет);
  * каждый bad_request моста несёт hint с примером допустимой формы; отказ гейта по
    известной причине — hint из REASON_HINTS; hint расширения (exec-лег) доезжает
    аддитивным полем вердикта;
  * node-юнит исполнителя: contenteditable-хост ищется ВВЕРХ (span внутри композера),
    ввод = beforeinput → execCommand('insertText') → change; вето beforeinput ->
    input_prevented; нет execCommand -> ручная запись + InputEvent(insertText);
    not_editable с tag/CE в hint; scroll dy/to со сдвигом и note при промахе;
    scroll sid/mark -> scrollIntoView; координатный и фокусный ввод.

Живая часть (реальный композер web.telegram.org, scroll на стенде) — приёмка панели.
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
from test_action_do import CLEAN, read_jsonl, run
from test_rpc_e2e import read_rpc_out, rpc_call

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "extension" / "content.js").read_text()
URI = "ws://127.0.0.1:8799"

SCROLL_OK = {"done": True, "executed": True, "action": "scroll", "synthetic": True,
             "url": "http://127.0.0.1:8899/form.html", "scrolled": {"dx": 0, "dy": 800, "y": 800}}
INPUT_OK = {"done": True, "executed": True, "action": "input", "synthetic": True,
            "url": "http://127.0.0.1:8899/form.html"}


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


# ---- scroll: гейт не проходит, форвард прямой ----------------------------------------

def mock_scroll(home, exec_reply=SCROLL_OK):
    """Одно mock-расширение: scroll обязан приехать ОДНИМ лег action.do. Любой
    action.preview — падение мока: гейт-фри глагол ушёл в цепочку SK-17."""
    seen = []

    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            ack = json.loads(await asyncio.wait_for(c.recv(), 3))
            assert ack.get("ok") is True, ack
            deadline = time.time() + 5
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
                    raise AssertionError("scroll went through the preview gate")
                if msg.get("method") == "action.do":
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                             "ok": True, "result": exec_reply},
                                            ensure_ascii=False))
                    break
            return seen
    return run(_m())


def test_scroll_dy_gate_free_forward(home):
    rid = rpc_call(home, "action.do", {"action": "scroll", "dy": 800})
    seen = mock_scroll(home)
    out = read_rpc_out(home, rid)
    assert [f["method"] for f in seen] == ["action.do"], seen  # ровно один лег, без превью
    assert seen[0]["id"] == rid  # сразу агентный id — цепи нет
    assert seen[0]["params"] == {"action": "scroll", "dy": 800}
    assert out and out["ok"] is True and out["result"] == SCROLL_OK, out
    # ни одного превью-кадра аудита: инцидент B (8 кадров ради прокрутки) невозможен
    audit = read_jsonl(pathlib.Path(home) / "audit.jsonl")
    assert not [a for a in audit if a.get("ev") == "preview"], audit


def test_scroll_to_bottom_forward(home):
    rid = rpc_call(home, "action.do", {"action": "scroll", "to": "bottom"})
    seen = mock_scroll(home, dict(SCROLL_OK, scrolled={"dx": 0, "dy": 5000, "y": 5000}))
    assert [f["method"] for f in seen] == ["action.do"]
    assert seen[0]["params"]["to"] == "bottom"
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"]["scrolled"]["y"] == 5000


def test_scroll_sid_mark_forward(home):
    rid = rpc_call(home, "action.do", {"action": "scroll", "sid": "a1b2c3"})
    seen = mock_scroll(home, dict(SCROLL_OK, scrolled={"into_view": True, "y": 120}))
    assert [f["method"] for f in seen] == ["action.do"]
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"]["scrolled"]["into_view"] is True


# ---- локальная валидация scroll + хинты ----------------------------------------------

SCROLL_BAD = [
    {"action": "scroll"},                                        # ни одного режима
    {"action": "scroll", "dy": 100, "to": "top"},                # два режима
    {"action": "scroll", "dy": 100, "sid": "a1b2c3"},            # режим + цель
    {"action": "scroll", "dy": "много"},                         # dy не число
    {"action": "scroll", "to": "middle"},                        # to не top/bottom
    {"action": "scroll", "dy": 800, "selector": "#x"},           # чужая цель
    {"action": "scroll", "dy": 800, "x": 10, "y": 20},           # чужие координаты
    {"action": "scroll", "mark": 0},                             # mark вне 1..64
    {"action": "scroll", "sid": "AB!"},                          # sid не [a-z0-9]{4,12}
]


@pytest.mark.parametrize("params", SCROLL_BAD)
def test_scroll_bad_request_local_with_hint(home, params):
    # bad_request решается ЛОКАЛЬНО: ни расширения, ни превью — и hint обязателен
    rid = rpc_call(home, "action.do", params)
    out = read_rpc_out(home, rid)
    assert out is not None and out["ok"] is False and out["err"] == "bad_request", params
    assert out.get("hint") and "scroll" in out["hint"], params  # п.4: самодостаточная форма


BAD_SHAPE = [
    {"action": "input", "selector": "#q"},                        # нет value/text
    {"action": "input", "selector": "#q", "value": "a", "text": "b"},  # алиасы разошлись
    {"action": "input", "x": 5, "y": 5},                          # координаты без значения
    {"action": "input", "selector": "#q", "x": 5, "y": 5, "value": "a"},  # две цели
    {"action": "key"},                                             # key без клавиши
    {"action": "click"},                                           # ни одной цели
    {"action": "teleport", "selector": "#q"},                      # глагола нет
    {"action": "click", "mark": 3, "sid": "a1b2c3"},               # mark+sid
]


@pytest.mark.parametrize("params", BAD_SHAPE)
def test_bad_request_carries_selfcontained_hint(home, params):
    rid = rpc_call(home, "action.do", params)
    out = read_rpc_out(home, rid)
    assert out is not None and out["ok"] is False and out["err"] == "bad_request", params
    hint = out.get("hint") or ""
    assert hint and ("{" in hint or "пример" in hint.lower() or "должен" in hint), \
        f"подсказка обязана показывать форму, не только код: {params} -> {hint!r}"


def test_input_value_text_equal_ok(home):
    # алиасы совпали — не неоднозначность: цепочка стартует (уезжает превью-лег)
    rid = rpc_call(home, "action.do", {"action": "input", "selector": "#q",
                                       "value": "тест", "text": "тест"})
    seen = _capture_preview_only(home, rid)
    assert seen and seen[0]["method"] == "action.preview"


def _capture_preview_only(home, rid, wait_s=1.2):
    """Мокается на превью-лег и НЕ отвечает на него: цепь зависает в гейте — нам важно
    лишь то, КАКОЙ первый лег ушёл расширению (bad_request был бы без кадра вообще)."""
    seen = []

    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            ack = json.loads(await asyncio.wait_for(c.recv(), 3))
            assert ack.get("ok") is True, ack
            deadline = time.time() + wait_s
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(c.recv(), min(0.5, max(0.05, deadline - time.time())))
                except asyncio.TimeoutError:
                    refire_noext(home)  # гонка no_extension (см. conftest)
                    continue
                msg = json.loads(raw)
                if msg.get("type") == "rpc":
                    seen.append(msg)
                    break
            return seen
    return run(_m())


# ---- input: координаты и фокус проходят обычный гейт ----------------------------------

def test_input_xy_clean_gate_both_legs(home):
    rid = rpc_call(home, "action.do", {"action": "input", "x": 400, "y": 520, "text": "тест"})
    seen = _chain_collect(home, rid, {"shown": True, "cancelled": False, "label": "введу"})
    assert [f["method"] for f in seen] == ["action.preview", "action.do"]
    pv, ex = seen
    assert pv["params"]["x"] == 400 and pv["params"]["y"] == 520
    assert "selector" not in pv["params"]
    assert ex["id"] == rid and ex["params"]["text"] == "тест"
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"]["executed"] is True


def test_input_focus_clean_gate(home):
    # input без цели — лег-превью БЕЗ selector/x/y (фокус и есть цель, как у key)
    rid = rpc_call(home, "action.do", {"action": "input", "value": "тест"})
    seen = _chain_collect(home, rid, {"shown": True, "cancelled": False, "label": "введу"})
    assert [f["method"] for f in seen] == ["action.preview", "action.do"]
    pv, ex = seen
    assert pv["params"]["action"] == "input"
    assert "selector" not in pv["params"] and "x" not in pv["params"] and "mark" not in pv["params"]
    assert ex["id"] == rid and ex["params"]["value"] == "тест"


def _chain_collect(home, rid, preview_verdict, exec_reply=None):
    seen = []

    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            ack = json.loads(await asyncio.wait_for(c.recv(), 3))
            assert ack.get("ok") is True, ack
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(c.recv(), max(0.05, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                if msg.get("type") != "rpc":
                    continue
                seen.append(msg)
                if msg.get("method") == "action.preview":
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                             "ok": True, "result": preview_verdict},
                                            ensure_ascii=False))
                elif msg.get("method") == "action.do":
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                             "ok": True, "result": exec_reply or INPUT_OK},
                                            ensure_ascii=False))
                    break
            return seen
    return run(_m())


# ---- хинты: отказ гейта и ошибка исполнения ------------------------------------------

def test_gate_refusal_not_editable_gets_hint(home):
    rid = rpc_call(home, "action.do", {"action": "input", "selector": "#banner", "value": "х"})
    seen = _chain_collect(home, rid, {"shown": False, "cancelled": False, "reason": "not_editable"})
    assert [f["method"] for f in seen] == ["action.preview"]  # отказа достаточно, лега нет
    out = read_rpc_out(home, rid)
    r = out["result"]
    assert r["executed"] is False and r["reason"] == "not_editable"
    assert "ближайший input/textarea" in r.get("hint", ""), r  # п.4: следующий шаг в вердикте


def test_extension_exec_hint_reaches_agent(home):
    # exec-лег отказал с hint (not_editable с tag/CE) — hint аддитивным полем вердикта
    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            ack = json.loads(await asyncio.wait_for(c.recv(), 3))
            assert ack.get("ok") is True, ack
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(c.recv(), max(0.05, deadline - time.time()))
                except asyncio.TimeoutError:
                    # харнесс-гонка «rpc_in drain раньше подключения мока» (conftest.refire_noext):
                    # без перезарядки кадр уже исчерпан no_extension-вердиктом и read_rpc_out
                    # вернёт его вместо not_editable+hint — перезаряжаем и ждём дальше
                    if refire_noext(home, "action.do"):
                        continue
                    break
                msg = json.loads(raw)
                if msg.get("type") != "rpc":
                    continue
                if msg.get("method") == "action.preview":
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                             "ok": True, "result": CLEAN}, ensure_ascii=False))
                elif msg.get("method") == "action.do":
                    await c.send(json.dumps({
                        "v": 1, "type": "rpc_result", "id": msg["id"], "ok": False,
                        "err": "not_editable",
                        "hint": "цель не редактируема (tag=div, contenteditable=null); ..."},
                        ensure_ascii=False))
                    break
    rid = rpc_call(home, "action.do", {"action": "input", "selector": "#cover", "value": "х"})
    run(_m())
    out = read_rpc_out(home, rid)
    assert out and out["ok"] is False and out["err"] == "not_editable", out
    assert "tag=div" in out.get("hint", ""), out


# ---- node-юнит исполнителя: contenteditable/scroll/фокус/координаты --------------------

NODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
eval(src); // sloppy-mode eval: определяет sk17ActionDo + sk42-хелперы

class Ev { constructor(type, opts = {}) { this.type = type; Object.assign(this, opts); }
  preventDefault() { this.defaultPrevented = true; } }
class InputEvent extends Ev {}

function makeWin() {
  const F = function () {};
  Object.defineProperty(F.prototype, "value", { configurable: true,
    set(v) { this.__proto_value = v; }, get() { return this.__proto_value; } });
  return { HTMLInputElement: F, HTMLTextAreaElement: F, HTMLSelectElement: F,
           Event: Ev, InputEvent: InputEvent,
           scrollX: 0, scrollY: 0,
           scrollBy(o) { this.scrollY += (o && o.top) || 0; },
           scrollTo(o) { this.scrollY = (o && o.top) || 0; },
           getSelection() { return { removeAllRanges() {}, addRange() {} }; } };
}
const win = makeWin();

function makeEl(tag, extra) {
  const protoCtor = { INPUT: win.HTMLInputElement, TEXTAREA: win.HTMLTextAreaElement,
                      SELECT: win.HTMLSelectElement }[tag] || function () {};
  const el = Object.create(protoCtor.prototype);
  el.tagName = tag; el.events = []; el.clicks = 0; el.attrs = {};
  el.getAttribute = (a) => (a in el.attrs ? el.attrs[a] : null);
  el.click = () => { el.clicks++; };
  el.focus = () => { el.focused = (el.focused || 0) + 1; };
  el.scrollIntoView = (o) => { el.scrolledInto = o || true; };
  el.dispatchEvent = (e) => {
    el.events.push({ type: e.type, inputType: e.inputType, data: e.data,
                     bubbles: e.bubbles, composed: e.composed });
    if (el.veto && e.type === "beforeinput" && e.cancelable) e.preventDefault();
  };
  return Object.assign(el, extra || {});
}
function makeDoc(el, extra) {
  const doc = Object.assign({
    defaultView: win, location: { href: "http://127.0.0.1:8899/form.html" },
    querySelector: (sel) => {
      if (sel === "!!bad") throw new Error("bad selector");
      if (sel === "#none") return null;
      return el;
    },
    getElementById: () => null,
    elementFromPoint: () => el || null,
    createRange: () => ({ selectNodeContents() {} }),
    execCommand: (cmd, ui, val) => { doc.execCalls.push([cmd, val]); return doc.execOk; },
    execCalls: [], execOk: true,
    activeElement: null, body: null, documentElement: { scrollHeight: 4000 },
  }, extra || {});
  return doc;
}

const out = {};
(async () => {
  // 1) contenteditable-хост по селектору: beforeinput → execCommand('insertText') → change
  let ce = makeEl("DIV", { attrs: { contenteditable: "true" } });
  let doc = makeDoc(ce);
  out.ce_input = await sk17ActionDo({ action: "input", selector: ".composer", value: "тест" }, doc);
  out.ce_exec = doc.execCalls;                 // [insertText, 'тест']
  out.ce_events = ce.events.map((e) => e.type);
  out.ce_focus = ce.focused;

  // 2) span ВНУТРИ композера: хост ищется ВВЕРХ (инцидент A: селектор/клик в span)
  const host2 = makeEl("DIV", { attrs: { contenteditable: "true" } });
  const span = makeEl("SPAN", { parentElement: host2 });
  doc = makeDoc(span);
  out.span_up = await sk17ActionDo({ action: "input", selector: "span", value: "прив" }, doc);
  out.span_host_exec = doc.execCalls;
  out.span_host_focus = host2.focused;

  // 3) beforeinput отменён страницей → input_prevented, execCommand НЕ вызван
  const vetoed = makeEl("DIV", { attrs: { contenteditable: "true" }, veto: true });
  doc = makeDoc(vetoed);
  out.veto = await sk17ActionDo({ action: "input", selector: ".x", value: "а" }, doc);
  out.veto_exec = doc.execCalls.length;

  // 4) execCommand недоступен/отказал → ручная запись + InputEvent(insertText)
  const fb = makeEl("DIV", { attrs: { contenteditable: "" } }); // голый атрибут = true
  doc = makeDoc(fb, { execOk: false });
  delete doc.execCommand;
  out.fallback = await sk17ActionDo({ action: "input", selector: ".fb", value: "рукой" }, doc);
  out.fallback_text = fb.textContent;
  out.fallback_events = fb.events;

  // 5) not_editable: hint с tag и contenteditable цели
  const div = makeEl("DIV");
  doc = makeDoc(div);
  out.ne = await sk17ActionDo({ action: "input", selector: ".banner", value: "v" }, doc);

  // 6) нативное поле — прежний прототипный сеттер (регрессия SK-17)
  const inp = makeEl("INPUT");
  doc = makeDoc(inp);
  out.native = await sk17ActionDo({ action: "input", selector: "#q", value: "hi" }, doc);
  out.native_value = inp.__proto_value;

  // 7) text — алиас value
  const al = makeEl("DIV", { attrs: { contenteditable: "true" } });
  doc = makeDoc(al);
  out.alias = await sk17ActionDo({ action: "input", selector: ".a", text: "алиас" }, doc);
  out.alias_exec = doc.execCalls;

  // 8) scroll dy: окно сдвинулось, note нет
  win.scrollY = 0;
  doc = makeDoc(null);
  out.scroll_dy = await sk17ActionDo({ action: "scroll", dy: 800 }, doc);
  out.scroll_dy_note = out.scroll_dy.note || null;

  // 9) scroll dy, окно не прокручивается → note с маршрутом к sid
  const frozenWin = { ...win, scrollY: 0, scrollBy() {} };
  const fdoc = { defaultView: frozenWin, location: { href: "x" }, documentElement: { scrollHeight: 100 }, body: { scrollHeight: 100 } };
  out.scroll_frozen = await sk17ActionDo({ action: "scroll", dy: 800 }, fdoc);
  out.scroll_frozen_note = !!out.scroll_frozen.note && out.scroll_frozen.note.includes("sid");

  // 10) scroll to:bottom → scrollTo на scrollHeight документа
  win.scrollY = 10;
  doc = makeDoc(null);
  out.scroll_bottom = await sk17ActionDo({ action: "scroll", to: "bottom" }, doc);
  out.scroll_bottom_y = win.scrollY; // 4000 из documentElement.scrollHeight
  out.scroll_top = await sk17ActionDo({ action: "scroll", to: "top" }, doc);
  out.scroll_top_y = win.scrollY;

  // 11) scroll sid/mark: scrollIntoView цели; dy/to рядом с целью — bad_request
  const target = makeEl("DIV");
  doc = makeDoc(target);
  out.scroll_sid = await sk17ActionDo({ action: "scroll", sid: "a1b2c3" }, doc, null,
                                      (s) => ({ el: target }));
  out.scroll_sid_into = target.scrolledInto;
  out.scroll_mark_dy = await sk17ActionDo({ action: "scroll", sid: "a1b2c3", dy: 100 }, doc,
                                          null, (s) => ({ el: target }));

  // 12) input по координатам: хит-тест нашёл CE-поле — ввод в него
  const hit = makeEl("DIV", { attrs: { contenteditable: "true" } });
  doc = makeDoc(hit);
  out.xy_input = await sk17ActionDo({ action: "input", x: 400, y: 520, value: "коорд" }, doc);
  out.xy_exec = doc.execCalls;
  out.xy_hit = out.xy_input.hit;

  // 13) input по координатам в не-поле → not_editable с tag
  const wall = makeEl("P");
  doc = makeDoc(wall);
  out.xy_ne = await sk17ActionDo({ action: "input", x: 1, y: 1, value: "в" }, doc);

  // 14) input без цели: фокус = CE-поле → ввод; фокус = body → not_editable с activeElement
  const focused = makeEl("DIV", { attrs: { contenteditable: "true" } });
  doc = makeDoc(focused, { activeElement: focused, body: makeEl("BODY") });
  out.focus_input = await sk17ActionDo({ action: "input", value: "фокус" }, doc);
  out.focus_exec = doc.execCalls;
  const body = makeEl("BODY");
  doc = makeDoc(null, { activeElement: body, body });
  out.focus_body = await sk17ActionDo({ action: "input", value: "в никуда" }, doc);

  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String((e && e.stack) || e) })); process.exit(1); });
"""


def test_sk42_executor_node_unit(tmp_path):
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

    # contenteditable: beforeinput → execCommand('insertText') → change; фокус на хосте
    assert out["ce_input"]["executed"] is True
    assert out["ce_exec"] == [["insertText", "тест"]]
    assert out["ce_events"] == ["beforeinput", "change"]
    assert out["ce_focus"] == 1

    # span внутри композера: хост найден ВВЕРХ, ввод пошёл в хост
    assert out["span_up"]["executed"] is True
    assert out["span_host_exec"] == [["insertText", "прив"]] and out["span_host_focus"] == 1

    # вето страницы на beforeinput — отказ без мутации
    assert out["veto"]["err"] == "input_prevented" and out["veto"]["hint"]
    assert out["veto_exec"] == 0

    # fallback без execCommand: ручная запись + InputEvent(inputType insertText) + change
    assert out["fallback"]["executed"] is True and out["fallback_text"] == "рукой"
    assert [e["type"] for e in out["fallback_events"]] == ["beforeinput", "input", "change"]
    ins = out["fallback_events"][1]
    assert ins["inputType"] == "insertText" and ins["data"] == "рукой"

    # not_editable — самодостаточный hint
    assert out["ne"]["err"] == "not_editable"
    assert "tag=div" in out["ne"]["hint"] and "contenteditable=null" in out["ne"]["hint"]

    # нативное поле — прежний путь (регрессия)
    assert out["native"]["executed"] is True and out["native_value"] == "hi"

    # text-алиас доехал до вставки
    assert out["alias"]["executed"] is True and out["alias_exec"] == [["insertText", "алиас"]]

    # scroll dy: фактический сдвиг в ответе, note нет
    assert out["scroll_dy"]["executed"] is True
    assert out["scroll_dy"]["scrolled"] == {"dx": 0, "dy": 800, "y": 800}
    assert out["scroll_dy_note"] is None
    # окно стоит → note маршрутизирует к sid-режиму
    assert out["scroll_frozen"]["scrolled"]["dy"] == 0 and out["scroll_frozen_note"] is True
    # to:bottom/top — scrollTo по высоте документа и наверх
    assert out["scroll_bottom_y"] == 4000 and out["scroll_top_y"] == 0

    # scroll цели: scrollIntoView центрирует; dy/to рядом с целью — отказ
    assert out["scroll_sid"]["executed"] is True and out["scroll_sid"]["scrolled"]["into_view"] is True
    assert out["scroll_sid_into"]["block"] == "center"
    assert out["scroll_mark_dy"] == {"err": "bad_request"}

    # координатный ввод: хит-тест → CE-поле → insertText; мимо поля — not_editable с tag
    assert out["xy_input"]["executed"] is True and out["xy_exec"] == [["insertText", "коорд"]]
    assert out["xy_hit"] == {"tag": "div", "text": ""}
    assert out["xy_ne"]["err"] == "not_editable" and "tag=p" in out["xy_ne"]["hint"]

    # фокус-режим: CE в фокусе — ввод; body в фокусе — честный отказ с activeElement
    assert out["focus_input"]["executed"] is True and out["focus_exec"] == [["insertText", "фокус"]]
    assert out["focus_body"]["err"] == "not_editable" and "activeElement=body" in out["focus_body"]["hint"]
