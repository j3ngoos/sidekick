"""SK-27 контракт: скорость и адресация — near-регион, приоритетная отборка, sid, nameSrc.

Аудит живой сессии (21.10): 26 rpc на простую задачу, 40% имён пустые, 41 no_element
против 102 ok (обрезанные css-пути не уникальны), 12 скроллей вслепую. Пины здесь:

  * node-юнит helpers-блока content.js (SK-27-BEGIN/END helpers): цепочка fallback
    elName с nameSrc на каждом звене, hint у пустых имён, полные (без обрыва на 8)
    уникальные css-пути, детерминированность sid;
  * node-юнит SK-17: sid-лег резолвера (sid_stale до превью, клик по резолвнутому,
    взаимоисключение sid+selector локально, mark-клик отдаёт sid из карты);
  * мост: bad_request на кривые sid/near — ЛОКАЛЬНО, без раунд-трипа; чистый sid
    проходит обоими легами цепи (preview-параметры несут sid, name — подпись);
  * вердикт sid_stale — успешный ответ с executed:false (агент переснимает, не гадает);
  * page.dom {near} прокидывается в content (opts.near), без near — прежняя форма +
    новые поля (аддитивность v1);
  * статические пины: MAX_ELS=800, more/nameSrc/hitTarget в снапшоте, проводка sid
    через background обоими глаголами, резолвер реестра в content.

Живая приёмка (диалог раньше невидимок, more-хвост, sid_stale на реальном Ozon-клоне)
— харнесс панели, не pytest.
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

CLEAN = {"shown": True, "cancelled": False, "held_ms": 450, "label": "кликну «Удалить»"}
SID_STALE = {"shown": False, "cancelled": False, "reason": "sid_stale"}
EXEC_OK = {"done": True, "executed": True, "action": "click", "synthetic": True,
           "url": "http://127.0.0.1:8899/cart.html", "hit": {"tag": "button", "text": "Удалить"}}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


# ---- node-юнит helpers-блока (elName / cssPath / sk27Sid) ----------------------------

HELPERS_HARNESS = r"""
const fs = require("fs");
globalThis.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c) };
eval(fs.readFileSync(process.argv[2], "utf8")); // flat / elName / nameOf / cssPath / sk27Sid

const out = {};
function mk(tag, o = {}) {
  const el = {
    tagName: tag, nodeType: 1, attrs: o.attrs || {}, textContent: o.text || "",
    children: o.children || [], parentElement: o.parent || null,
    labels: o.labels || [], ownerDocument: o.doc || null,
    className: o.className || "", name: o.name || "",
    getAttribute(a) { return Object.prototype.hasOwnProperty.call(this.attrs, a) ? this.attrs[a] : null; },
    querySelector: o.querySelector || (() => null),
    closest: typeof o.closest === "function" ? o.closest : (() => null),
    contains: () => false,
  };
  if (o.type) el.type = o.type;
  if (o.value !== undefined) el.value = o.value;
  return el;
}
const T = (text) => ({ nodeType: 1, textContent: text, innerText: text });

// 1) цепочка fallback: приоритет ровно по спеке, nameSrc — источник на каждом звене
out.aria = elName(mk("A", { attrs: { "aria-label": "Закрыть" }, text: "×" }));
out.text = elName(mk("BUTTON", { text: "Корзина" }));
out.title = elName(mk("DIV", { attrs: { title: "Настройки" }, role: "button" }));
out.alt = elName(mk("IMG", { attrs: { alt: "Логотип" } }));
out.ph = elName(mk("INPUT", { attrs: { placeholder: "Искать товары" } }));
out.value = elName(mk("INPUT", { type: "submit", value: "Найти" }));
const lbDoc = { getElementById: (id) => (id === "lb1" ? T("Тема сообщения") : null) };
out.labelll = elName(mk("INPUT", { attrs: { "aria-labelledby": "lb1" }, doc: lbDoc }));
out.labelll_broken = elName(mk("INPUT", { attrs: { "aria-labelledby": "нет" }, doc: lbDoc }));
// битая ссылка — цепочка идёт дальше и честно доходит до none (не падает, не выдумывает)
out.labels = elName(mk("INPUT", { labels: [T("Согласен на обработку")] }));
out.clabel = elName(mk("INPUT", { closest: (s) => (s === "label" ? T("Подписка") : null) }));
out.legend = elName(mk("INPUT", { closest: (s) => (s === "fieldset"
  ? { querySelector: (q) => (q === "legend" ? T("Доставка") : null) } : null) }));
// соседний span в родителе-кнопке: <button><i/><span>Файл</span></button>
const self = mk("I", { attrs: { role: "button" } });
const span = T("Файл"); span.tagName = "SPAN";
const par = { tagName: "BUTTON", children: [self, span], getAttribute: () => null };
self.parentElement = par;
out.sibling = elName(self);
// svg <title> / aria-label внутри svg
const svgTitle = mk("svg", { querySelector: (q) => (q === "title" ? T("Домой") : null) });
out.svg = elName(mk("BUTTON", { querySelector: (q) => (q === "svg" ? svgTitle : null) }));
const svgAria = { getAttribute: (a) => (a === "aria-label" ? "Поиск" : null),
  querySelector: () => null };
out.svg_aria = elName(mk("SPAN", { querySelector: (q) => (q === "svg" ? svgAria : null) }));
// пусто: nameSrc none + hint (svg-иконка / class-подсказка)
out.none_icon = elName(mk("svg", { querySelector: () => null, className: "icon icon-x" }));
out.none_plain = elName(mk("I", { className: "ghost", name: "q" }));

// 2) cssPath: обрыва на 8 частях БОЛЬШЕ НЕТ — путь глубже 8 уровней уникален по построению
const root = { nodeType: 1, tagName: "HTML", children: [], parentElement: null };
let cur = root, chain = [root];
for (let i = 0; i < 12; i++) {
  const kid = { nodeType: 1, tagName: "DIV", children: [], parentElement: cur };
  cur.children.push(kid); cur = kid; chain.push(kid);
}
const docDeep = { documentElement: root, querySelectorAll: () => [] };
const deep = cssPath(cur, docDeep);
out.deep_parts = deep.split(">").length;      // html + 12 дивов
out.deep_head = deep.slice(0, 4);
// два сиблинга-спана различаются nth-of-type
const p2 = { nodeType: 1, tagName: "DIV", parentElement: root, children: [] };
root.children.push(p2);
const spans = [0, 1, 2].map(() => ({ nodeType: 1, tagName: "SPAN", children: [], parentElement: p2 }));
p2.children.push(...spans);
out.sib2 = cssPath(spans[1], docDeep);
out.sib3 = cssPath(spans[2], docDeep);
// id/name — короткие, когда уникальны
const docId = { documentElement: root, querySelectorAll: (s) => (s === "#go" ? [{}] : []) };
out.id_path = cssPath({ nodeType: 1, tagName: "DIV", id: "go" }, docId);
const docName = { documentElement: root, querySelectorAll: (s) => (s === 'input[name="q"]' ? [{}] : []) };
out.name_path = cssPath({ nodeType: 1, tagName: "INPUT", name: "q" }, docName);

// 3) sid: формат, детерминированность, чувствительность к каждому входу, нормализация пробелов
const s1 = sk27Sid("html>body>button", "button", "Удалить");
out.sid_format = /^[a-z0-9]{8}$/.test(s1);
out.sid_same = sk27Sid("html>body>button", "button", "Удалить") === s1;
out.sid_name = sk27Sid("html>body>button", "button", "Очистить") !== s1;
out.sid_path = sk27Sid("html>body>div>button", "button", "Удалить") !== s1;
out.sid_norm = sk27Sid("html>body>button", "button", "  Удалить   x ") ===
               sk27Sid("html>body>button", "button", "Удалить x");
out.nameof_wrapper = nameOf(mk("A", { attrs: { "aria-label": "X" } }));

console.log(JSON.stringify(out));
"""


def _helpers_block(tmp_path):
    m = re.search(r"// SK-27-BEGIN helpers[^\n]*\n(.*?)// SK-27-END helpers", JS, re.S)
    assert m, "helpers-блок с маркерами SK-27 исчез из content.js"
    block = tmp_path / "sk27_helpers.js"
    block.write_text(m.group(1), encoding="utf-8")
    return block


def test_helpers_node_unit(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    block = _helpers_block(tmp_path)
    harness = tmp_path / "harness.js"
    harness.write_text(HELPERS_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(harness), str(block)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    out = json.loads(r.stdout)

    # цепочка fallback — приоритет и nameSrc каждого звена (спека SK-27 п.1)
    assert out["aria"] == {"name": "Закрыть", "nameSrc": "aria-label"}
    assert out["text"] == {"name": "Корзина", "nameSrc": "text"}
    assert out["title"] == {"name": "Настройки", "nameSrc": "title"}
    assert out["alt"] == {"name": "Логотип", "nameSrc": "alt"}
    assert out["ph"] == {"name": "Искать товары", "nameSrc": "placeholder"}
    assert out["value"] == {"name": "Найти", "nameSrc": "value"}
    assert out["labelll"] == {"name": "Тема сообщения", "nameSrc": "aria-labelledby"}
    assert out["labels"] == {"name": "Согласен на обработку", "nameSrc": "label"}
    assert out["clabel"] == {"name": "Подписка", "nameSrc": "label"}
    assert out["legend"] == {"name": "Доставка", "nameSrc": "legend"}
    assert out["sibling"] == {"name": "Файл", "nameSrc": "sibling"}
    assert out["svg"] == {"name": "Домой", "nameSrc": "svg"}
    assert out["svg_aria"] == {"name": "Поиск", "nameSrc": "svg"}
    # пусто — не пустая строка вслепую, а hint: чем пахнет цель
    assert out["none_icon"]["name"] == "" and out["none_icon"]["nameSrc"] == "none"
    assert out["none_icon"]["hint"].startswith("svg-icon") and "icon" in out["none_icon"]["hint"]
    assert out["none_plain"]["hint"] == "no-text name=q class=ghost"
    # битая aria-labelledby не роняет и не съедает цепочку
    assert out["labelll_broken"]["nameSrc"] == "none"

    # css-пути: глубина без обрыва, сиблинги различимы, id/name — короткие
    assert out["deep_parts"] == 13, out  # html + 12 уровней (старый код резал на 8+html)
    assert out["deep_head"] == "html"
    assert out["sib2"].endswith("span:nth-of-type(2)")
    assert out["sib2"] != out["sib3"]
    assert out["id_path"] == "#go"
    assert out["name_path"] == 'input[name="q"]'

    # sid: формат/детерминизм/чувствительность/нормализация
    assert out["sid_format"] is True
    assert out["sid_same"] is True
    assert out["sid_name"] is True and out["sid_path"] is True
    assert out["sid_norm"] is True
    assert out["nameof_wrapper"] == "X"


# ---- node-юнит SK-17: sid-лег резолвера ----------------------------------------------

SID_EXEC_HARNESS = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8")); // SK-17 executor (sk17ActionDo)

class Ev { constructor(type, opts = {}) { this.type = type; Object.assign(this, opts); } }
const win = { Event: Ev };
function makeEl(tag, extra) {
  const el = { tagName: tag, events: [], clicks: 0,
    click: () => { el.clicks++; },
    dispatchEvent: (e) => { el.events.push(e.type); },
    getBoundingClientRect: () => ({ left: 10, top: 10, width: 50, height: 20, right: 60, bottom: 30 }),
    closest: () => null, contains: () => false, textContent: "Удалить",
  };
  return Object.assign(el, extra || {});
}
const doc = { defaultView: win, location: { href: "http://127.0.0.1:8899/cart.html" },
              querySelector: () => null };

const out = {};
(async () => {
  const el = makeEl("BUTTON");
  const okResolve = (sid) => (sid === "ab12cd34" ? { el, name: "Удалить" } : { err: "sid_stale" });

  out.stale = await sk17ActionDo({ action: "click", sid: "zzzzzzzz" }, doc, null, okResolve);
  out.no_resolver = await sk17ActionDo({ action: "click", sid: "ab12cd34" }, doc); // лег без резолвера — честный отказ
  const r2 = await sk17ActionDo({ action: "click", sid: "ab12cd34" }, doc, null, okResolve);
  out.click_executed = r2.executed === true && el.clicks === 1;
  out.click_hit = r2.hit;
  out.input_sid = await sk17ActionDo({ action: "input", sid: "ab12cd34", value: "v" }, doc, null, okResolve); // BUTTON не редактируем
  out.sid_selector = await sk17ActionDo({ action: "click", sid: "ab12cd34", selector: "#a" }, doc, null, okResolve);
  out.sid_key = await sk17ActionDo({ action: "key", key: "Enter", sid: "ab12cd34" }, doc, null, okResolve);
  // mark-клик отдаёт sid из карты (связь метка скрина ↔ снапшот)
  const mel = makeEl("BUTTON");
  const map = { url: "http://127.0.0.1:8899/cart.html", marks: [
    { n: 7, selector: "html>body>button", tag: "button", name: "Удалить",
      rect: [10, 10, 50, 20], sid: "mark7sid" }],
  };
  const docM = { defaultView: win, location: { href: map.url }, querySelector: (s) => (s === "html>body>button" ? mel : null) };
  out.mark_sid = await sk17ActionDo({ action: "click", mark: 7 }, docM, map, null);
  out.mark_sid_bad = await sk17ActionDo({ action: "click", mark: 7, sid: "ab12cd34" }, docM, map, null);
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String((e && e.stack) || e) })); process.exit(1); });
"""


def test_sid_executor_node_unit(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    m = re.search(r"// SK-17-BEGIN executor[^\n]*\n(.*?)// SK-17-END executor", JS, re.S)
    assert m, "исполняющий блок с маркерами SK-17 исчез из content.js"
    block = tmp_path / "sk17_executor.js"
    block.write_text(m.group(1), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(SID_EXEC_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(harness), str(block)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    out = json.loads(r.stdout)
    assert "fatal" not in out, out.get("fatal")

    assert out["stale"] == {"err": "sid_stale"}          # реестр не нашёл/протух — до превью
    assert out["no_resolver"] == {"err": "sid_stale"}    # лег без резолвера не кликает вслепую
    assert out["click_executed"] is True
    assert out["click_hit"] == {"tag": "button", "text": "Удалить"}
    assert out["input_sid"]["err"] == "not_editable"  # SK-42: + hint
    assert out["sid_selector"] == {"err": "bad_request"}  # две цели не приоритизируются
    assert out["sid_key"] == {"err": "bad_request"}       # key — цель фокуса, sid лишняя
    assert out["mark_sid"]["executed"] is True and out["mark_sid"]["sid"] == "mark7sid"
    assert out["mark_sid_bad"] == {"err": "bad_request"}  # mark+sid — неоднозначная цель


# ---- мост: локальные bad_request ------------------------------------------------------

def test_sid_bad_request_local(home):
    # форма/взаимоисключение sid отсекаются ДО раунд-трипа (расширение не нужно)
    for params in (
        {"action": "click", "sid": "ab"},                      # короче 4
        {"action": "click", "sid": "a" * 13},                  # длиннее 12
        {"action": "click", "sid": "не-sid"},                  # не [a-z0-9]
        {"action": "click", "sid": 12345},                     # не строка
        {"action": "click", "sid": True},                      # не строка
        {"action": "key", "key": "Enter", "sid": "ab12cd34"},  # key — цель фокуса
        {"action": "move", "x": 1, "y": 2, "sid": "ab12cd34"}, # move — координатная цель
        {"action": "click", "sid": "ab12cd34", "selector": "#a"},
        {"action": "click", "sid": "ab12cd34", "x": 1, "y": 2},
        {"action": "click", "sid": "ab12cd34", "frame": "iframe"},
        {"action": "click", "sid": "ab12cd34", "mark": 3},     # две цели сразу
        {"action": "input", "sid": "ab12cd34"},                # input без value
    ):
        rid = rpc_call(home, "action.do", params)
        out = read_rpc_out(home, rid, timeout=2.0)
        assert out is not None and out["ok"] is False and out["err"] == "bad_request", params


def test_page_dom_near_bad_request_local(home):
    for params in ({"near": "nope"}, {"near": [1, 2, 3]}, {"near": [1, 2, 3, "x"]},
                   {"near": [1, 2, 3, None]}, {"near": {"x": 1}}, {"near": [1, 2, 3, 4, 5]},
                   {"near": [0, 0, float("inf"), 0]}):
        rid = rpc_call(home, "page.dom", params)
        out = read_rpc_out(home, rid, timeout=2.0)
        assert out is not None and out["ok"] is False and out["err"] == "bad_request", params


# ---- мост: sid проходит цепь обоими легами --------------------------------------------

def mock_chain(home, preview_verdict, exec_reply=None, expect_exec=True):
    """Из test_action_do: превью-вердикт всегда отвечаем; при expect_exec=False падает,
    если исполняющий лег всё же пришёл."""
    seen = []

    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            ack = json.loads(await asyncio.wait_for(c.recv(), 3))
            assert ack.get("ok") is True, ack
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
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                             "ok": True, "result": preview_verdict},
                                            ensure_ascii=False))
                    if expect_exec:
                        continue
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
                    await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"],
                                             "ok": True, "result": exec_reply or EXEC_OK},
                                            ensure_ascii=False))
                    break
        return seen

    return run(_m())


def test_sid_chain_both_legs(home):
    rid = rpc_call(home, "action.do", {"action": "click", "sid": "ab12cd34", "name": "Удалить"})
    seen = mock_chain(home, CLEAN)
    out = read_rpc_out(home, rid)
    assert out and out["ok"] is True and out["result"]["executed"] is True, out
    assert [f["method"] for f in seen] == ["action.preview", "action.do"]
    pv, ex = seen
    assert pv["params"].get("sid") == "ab12cd34"   # кольцо превью — на элементе реестра
    assert pv["params"].get("selector") is None    # sid — ЕДИНСТВЕНная цель
    assert pv["params"].get("name") == "Удалить"   # name рядом — человеческая подпись
    assert ex["params"].get("sid") == "ab12cd34"   # исполнение — тем же резолвером


def test_sid_stale_is_successful_verdict(home):
    rid = rpc_call(home, "action.do", {"action": "click", "sid": "deadbeef"})
    mock_chain(home, SID_STALE, expect_exec=False)  # бросит, если лег уйдёт после отказа
    out = read_rpc_out(home, rid)
    # вердикт превью — успешный SK-05-ответ: агент переснимает page.dom, а не ловит ошибку
    assert out and out["ok"] is True, out
    r = out["result"]
    assert r.pop("hint", "").startswith("элемент из page.dom умер")  # SK-42 п.4
    assert r == {"shown": False, "cancelled": False, "executed": False,
                 "action": "click", "reason": "sid_stale"}, r


def test_page_dom_near_forwarded(home):
    rid = rpc_call(home, "page.dom", {"near": [0, 0, 800, 600]})

    async def _m():
        async with websockets.connect(URI, open_timeout=3) as c:
            await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
            ack = json.loads(await asyncio.wait_for(c.recv(), 3))
            assert ack.get("ok") is True, ack
            while True:  # первый кадр после hello — state-push (SK-09), ждём rpc
                msg = json.loads(await asyncio.wait_for(c.recv(), 5))
                if msg.get("type") == "rpc":
                    break
            await c.send(json.dumps({"v": 1, "type": "rpc_result", "id": msg["id"], "ok": True,
                                     "result": {"url": "http://x/", "title": "", "elements": [],
                                                "truncated": False, "count": 0}}))
            return msg

    seen = run(_m())
    out = read_rpc_out(home, rid)
    assert out and out["ok"] is True, out
    assert seen["method"] == "page.dom"
    assert seen["params"].get("near") == [0, 0, 800, 600]  # мост не съедает регион


# ---- статические пины проводки ---------------------------------------------------------

def test_content_pins_sk27():
    assert "MAX_ELS = 800" in JS  # 400 резал порталы/диалоги (аудит 21.10)
    assert "nameSrc" in JS and "hitTarget" in JS
    assert "sk27ResolveSid" in JS and "sid_stale" in JS
    assert "more:" in JS and "truncated: rest.length > 0" in JS
    assert "pageDom(m.opts)" in JS                      # near прокинут в резолвер снапшота
    assert "sk27InOverlay" in JS                        # приоритет диалога/портала
    assert "parts.length < 8" not in JS                 # обрыва css-пути больше нет
    assert "sk27Sid(cssPath(c.el, document)" in JS      # SoM-карта хранит sid
    assert "role: \"dialog\"" in JS or 'role="dialog"' in JS


def test_background_pins_sk27():
    bg = (ROOT / "extension" / "background.js").read_text()
    assert "sid: params.sid" in bg            # оба глагола: preview и action.do
    assert bg.count("sid: params.sid") >= 2
    assert "opts: params.near" in bg          # page.dom регион — в content opts


def test_bridge_pins_sk27():
    src = (ROOT / "bridge" / "sidekick_bridge.py").read_text()
    assert "ACTION_DO_SID_RE" in src
    assert 'params.get("sid")' in src
    assert "SK-27" in src
    assert 'method == "page.dom" and params.get("near")' in src


def test_agents_md_documents_sk27():
    doc = (ROOT / "AGENTS.md").read_text()
    assert "SK-27" in doc and "sid" in doc and "near" in doc
