"""SK-24 contract: action.do координаты, клавиши, полный указательный путь.

Гейт SK-17 не меняется по форме — меняется охват. Пины здесь:
  * {x,y,action:"click"|"rightclick"|"hover"|"dblclick"|"move"} без selector:
    гейт-превью уходит с x/y (без selector), исполняющий лег — с x/y насквозь,
    ответ проходит с hit:{tag,text} (п.5 — «куда реально попал»);
  * {action:"key", key, modifiers?} — превью с key/name=клавиша, исполнение насквозь;
  * frame (цепочка page.dom) проходит в оба лега;
  * отказ гейта на координатах — исполнения нет, вето = успешный ответ (SK-05);
  * bad_request валидируется локально: hover без координат, click с selector+x,
    key без key, key с чужим модификатором, key с selector, frame не-строка;
  * валидатор принимает «ctrlKey»-стиль модификаторов и click по координатам;
  * paused_for_human / no_extension / один preview-кадр аудита — как в SK-17;
  * аддитивность: action.preview с x/y остаётся отдельным round-trip.

Плюс node-юнит расширенного исполняющего блока (те же маркеры SK-17-BEGIN/END):
полный указательный путь с координатами, hover без нажатия, rightclick+contextmenu,
dblclick, keydown/keyup на активном элементе, промах -> no_element, разрешение
фреймов (цепочка, авто-спуск, cross-origin), полный путь для клика по селектору.
Живая часть (canvas/hover-меню/ozon-клоун на стенде) — приёмка панели, не pytest.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from conftest import kill_bridge, start_bridge
from test_action_do import CLEAN, EXEC_OK, mock_chain, read_jsonl, run
from test_rpc_e2e import mock_extension, read_rpc_out, rpc_call

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "extension" / "content.js").read_text()

COORD_CLEAN = {"shown": True, "cancelled": False, "held_ms": 450, "label": "кликну"}
COORD_EXEC = {"done": True, "executed": True, "action": "click", "synthetic": True,
              "url": "http://127.0.0.1:8899/canvas.html",
              "hit": {"tag": "canvas", "text": "draw-area"}}


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


# ---- гейт на координатах: чистый вердикт -> исполнение с x/y насквозь ---------------

def test_coord_click_clean_gate(home):
    rid = rpc_call(home, "action.do", {"x": 320, "y": 210, "action": "click", "name": "кнопка на canvas"})
    seen = mock_chain(home, COORD_CLEAN, exec_reply=COORD_EXEC)
    out = read_rpc_out(home, rid)
    pv, ex = seen
    assert pv["method"] == "action.preview" and ex["method"] == "action.do"
    assert pv["params"] == {"action": "click", "x": 320, "y": 210, "name": "кнопка на canvas"}
    assert "selector" not in pv["params"]
    assert ex["id"] == rid  # исполнение — на id агента
    assert ex["params"] == {"x": 320, "y": 210, "action": "click", "name": "кнопка на canvas"}
    assert out["ok"] is True and out["result"]["executed"] is True
    assert out["result"]["hit"] == {"tag": "canvas", "text": "draw-area"}  # п.5


def test_coord_action_frame_passthrough(home):
    rid = rpc_call(home, "action.do", {"x": 40, "y": 90, "action": "hover", "frame": "iframe#f > #wrap"})
    seen = mock_chain(home, COORD_CLEAN,
                      exec_reply={"done": True, "executed": True, "action": "hover",
                                  "synthetic": True, "url": "http://x/inner.html",
                                  "hit": {"tag": "div", "text": "меню"}})
    assert seen[0]["params"]["frame"] == "iframe#f > #wrap"
    assert seen[1]["params"]["frame"] == "iframe#f > #wrap"
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"]["hit"]["tag"] == "div"


def test_selector_with_frame_passes_to_both_legs(home):
    rid = rpc_call(home, "action.do", {"selector": "#q", "action": "input", "value": "раз",
                                       "frame": "iframe#f"})
    seen = mock_chain(home, CLEAN,
                      exec_reply={"done": True, "executed": True, "action": "input",
                                  "synthetic": True, "url": "http://x/"})
    assert seen[0]["params"] == {"action": "input", "selector": "#q", "frame": "iframe#f"}
    assert seen[1]["params"]["frame"] == "iframe#f"
    assert read_rpc_out(home, rid)["ok"] is True


def test_key_action_flow(home):
    rid = rpc_call(home, "action.do", {"action": "key", "key": "Enter", "modifiers": ["ctrl"]})
    seen = mock_chain(home, CLEAN,
                      exec_reply={"done": True, "executed": True, "action": "key",
                                  "synthetic": True, "url": "http://x/",
                                  "hit": {"tag": "input", "text": ""}})
    assert seen[0]["params"] == {"action": "key", "key": "Enter", "name": "Enter",
                                 "modifiers": ["ctrl"]}
    assert seen[1]["params"] == {"action": "key", "key": "Enter", "modifiers": ["ctrl"]}
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"]["hit"] == {"tag": "input", "text": ""}


def test_key_explicit_name_kept(home):
    rid = rpc_call(home, "action.do", {"action": "key", "key": "Escape", "name": "закрыть меню"})
    seen = mock_chain(home, CLEAN)
    assert seen[0]["params"]["name"] == "закрыть меню"
    assert seen[0]["params"]["key"] == "Escape"


# ---- отказ гейта на координатах: исполнения НЕТ --------------------------------------

def test_coord_cancelled_refuses_execution(home):
    rid = rpc_call(home, "action.do", {"x": 5, "y": 5, "action": "rightclick"})
    seen = mock_chain(home, {"shown": True, "cancelled": True, "reason": "user_stop"},
                      expect_exec=False)
    assert [f["method"] for f in seen] == ["action.preview"]
    out = read_rpc_out(home, rid)
    assert out["ok"] is True  # вето — успешный ответ (SK-05)
    r = out["result"]
    assert r["executed"] is False and r["cancelled"] is True and r["action"] == "rightclick"


def test_coord_gate_single_preview_audit(home):
    rid = rpc_call(home, "action.do", {"x": 1, "y": 2, "action": "move"})
    mock_chain(home, COORD_CLEAN, exec_reply={"done": True, "executed": True, "action": "move",
                                              "synthetic": True, "url": "http://x/",
                                              "hit": {"tag": "html", "text": ""}})
    assert read_rpc_out(home, rid) is not None
    evs = [e for e in read_jsonl(home / "audit.jsonl") if e.get("ev") == "preview"]
    assert len(evs) == 1 and evs[0]["via"] == "action.do"


# ---- валидация: bad_request без раунд-трипа к расширению ------------------------------

BAD = [
    {"action": "hover"},                                   # координат нет
    {"action": "hover", "x": "a", "y": 2},                 # x не число
    {"action": "hover", "x": 1},                           # y нет
    {"action": "click", "x": 1, "y": 2, "selector": "#a"}, # оба цели — неоднозначно
    {"action": "rightclick", "x": 1, "y": 2, "selector": " "},
    {"action": "click", "x": True, "y": 2},                # bool — не число
    {"action": "click", "x": float("nan"), "y": 2},
    {"action": "key"},                                     # key обязателен
    {"action": "key", "key": "   "},
    {"action": "key", "key": "Enter", "modifiers": "ctrl"},# список, не строка
    {"action": "key", "key": "Enter", "modifiers": ["hyper"]},
    {"action": "key", "key": "Enter", "selector": "#a"},   # у key цель — фокус
    {"action": "key", "key": "Enter", "x": 1, "y": 1},
    {"action": "hover", "x": 1, "y": 1, "frame": 5},       # frame — строка
    {"action": "hover", "x": 1, "y": 1, "frame": "x" * 501},
    {"action": "input", "selector": "#a", "value": "v", "x": 1, "y": 2},
    {"action": "drag"},                                    # вне словаря
    # SK-24 аддендум (в): name/role — только свои глагола и только как цель
    {"action": "click", "role": "button"},                 # роль без имени — нечего резолвить
    {"action": "click", "name": "x", "role": ""},
    {"action": "click", "name": "x", "role": 5},
    {"action": "click", "name": "x" * 201, "role": "button"},
    {"action": "click", "name": "  "},                     # имя из пробелов — цели нет
    {"action": "click", "name": "x", "frame": "iframe#f"}, # резолвер топ-документный
    {"action": "input", "name": "x"},                      # input — не name-глагол
    {"action": "move", "name": "x"},                       # move — не name-глагол
]


@pytest.mark.parametrize("params", BAD, ids=[str(i) for i in range(len(BAD))])
def test_bad_request_local(home, params):
    rid = rpc_call(home, "action.do", params)
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "bad_request", params


def test_modifier_key_suffix_style_ok(home):
    # «ctrlKey»-стиль (DOM-имена) принимается: суффикс Key снимается
    rid = rpc_call(home, "action.do", {"action": "key", "key": "A",
                                       "modifiers": ["ctrlKey", "Shift", "metaKey"]})
    seen = mock_chain(home, CLEAN)
    assert seen[0]["method"] == "action.preview"


def test_coord_no_extension_and_paused(home):
    rid = rpc_call(home, "action.do", {"x": 1, "y": 1, "action": "hover"})
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "no_extension"
    (home / "paused").touch()
    rid = rpc_call(home, "action.do", {"x": 1, "y": 1, "action": "click"})
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "paused_for_human"


def test_standalone_preview_with_coords_passthrough(home):
    """Аддитивность: отдельный round-trip action.preview принимает x/y как есть."""
    rid = rpc_call(home, "action.preview", {"x": 12, "y": 34, "action": "hover"})
    frame = run(mock_extension(home, reply=COORD_CLEAN, expect_methods=["action.preview"]))
    assert frame["params"] == {"x": 12, "y": 34, "action": "hover"}
    out = read_rpc_out(home, rid)
    assert out is not None and out["ok"] is True and out["result"] == COORD_CLEAN


# ---- статика: проводка новых полей -----------------------------------------------------

def test_background_forwards_coord_fields():
    bg = (ROOT / "extension" / "background.js").read_text()
    leg = bg[bg.index("actionDoRpc"):bg.index("async function handleRpc")]
    for f in ("x: params.x", "y: params.y", "frame: params.frame",
              "key: params.key", "modifiers: params.modifiers"):
        assert f in leg, f"actionDoRpc не пробрасывает {f}"
    pv = bg[bg.index('method === "action.preview"'):]
    pv = pv[:pv.index("\n")]
    assert "x: params.x" in pv and "y: params.y" in pv and "frame: params.frame" in pv


def test_content_uses_element_from_point_and_point_ring():
    assert "elementFromPoint" in JS
    assert ".ring.pt" in JS
    assert 'r.classList.toggle("pt", isPoint)' in JS


def test_bridge_coord_vocabulary():
    src = (ROOT / "bridge" / "sidekick_bridge.py").read_text()
    assert "ACTION_DO_COORDS" in src and "cross_origin_frame" in src


# ---- node-юнит расширенного исполняющего блока ------------------------------------------

NODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
eval(src); // sloppy-mode eval: определяет sk17* хелперы и sk17ActionDo

class Ev { constructor(type, opts = {}) { this.type = type; Object.assign(this, opts); } }
class PointerEvent extends Ev {}
class MouseEvent extends Ev {}
class KeyboardEvent extends Ev {}
class InputEvent extends Ev {}
const win = { Event: Ev, PointerEvent, MouseEvent, KeyboardEvent, InputEvent };
// аддендум (б): корневой контейнер опознаётся по вьюпорту и курсору — мутируем win
// ОДИН раз до всех тестов: у фейковых rect 40x20 он и так false, поведение прежнее
win.innerWidth = 1280; win.innerHeight = 800;
win.getComputedStyle = (el) => ({ cursor: el.cursorStyle || "auto" });

function makeEl(tag, extra) {
  const el = { tagName: tag, events: [], clicks: 0, style: {},
               rect: { left: 100, top: 50, width: 40, height: 20, right: 140, bottom: 70 },
               textContent: "" };
  el.click = () => { el.clicks++; el.events.push({ type: "__click" }); };
  el.dispatchEvent = (e) => el.events.push(e);
  el.getBoundingClientRect = () => el.rect;
  return Object.assign(el, extra || {});
}
function makeDoc(el, opts) {
  opts = opts || {};
  const doc = { defaultView: win, documentElement: { nodeType: 1 }, // contentDocument «живой» признак
    location: { href: opts.href || "http://127.0.0.1:8899/canvas.html" },
    querySelector: (sel) => {
      if (sel === "!!bad") throw new Error("bad selector");
      if (sel === "#none") return null;
      return (opts.querySelector ? opts.querySelector(sel) : undefined) !== undefined
        ? opts.querySelector(sel) : el;
    },
    elementFromPoint: (x, y) => (opts.hit ? opts.hit(x, y) : el) };
  if (opts.activeElement !== undefined) doc.activeElement = opts.activeElement;
  if (opts.body !== undefined) doc.body = opts.body;
  return doc;
}
const types = (el) => el.events.map((e) => e.type);

const out = {};
(async () => {
  // 1) клик по координатам: полный путь + el.click() для default-действия; координаты,
  //    button:0, isPrimary, pointerId:1 — на pointerdown/mousedown.
  let el = makeEl("CANVAS"); el.textContent = "draw-area";
  let doc = makeDoc(el);
  out.coord_click = await sk17ActionDo({ action: "click", x: 120, y: 60 }, doc);
  out.cc_types = types(el);
  const pd = el.events.find((e) => e.type === "pointerdown");
  const md = el.events.find((e) => e.type === "mousedown");
  out.cc_coords = [pd.clientX, pd.clientY, md.clientX, md.clientY];
  out.cc_flags = [pd.button, pd.isPrimary, pd.pointerId, pd.pointerType, pd.bubbles, pd.composed];

  // 2) hover: только over/enter/move, без нажатия и клика (дроп-меню!)
  el = makeEl("DIV"); doc = makeDoc(el);
  out.hover = await sk17ActionDo({ action: "hover", x: 10, y: 10 }, doc);
  out.hover_types = types(el);

  // 3) move — тот же hover-путь
  el = makeEl("DIV"); doc = makeDoc(el);
  out.move = await sk17ActionDo({ action: "move", x: 3, y: 4 }, doc);
  out.move_types = types(el);

  // 4) rightclick: button:2 на down/up, contextmenu, НИКАКОГО el.click()
  el = makeEl("DIV"); doc = makeDoc(el);
  out.rc = await sk17ActionDo({ action: "rightclick", x: 7, y: 8 }, doc);
  out.rc_types = types(el);
  out.rc_buttons = el.events.filter((e) => ["pointerdown", "mousedown", "pointerup", "mouseup"].includes(e.type))
    .map((e) => e.button);
  out.rc_clicks = el.clicks;

  // 5) dblclick: два полных пути, два клика, dblclick(detail:2) в конце
  el = makeEl("DIV"); doc = makeDoc(el);
  out.dbl = await sk17ActionDo({ action: "dblclick", x: 1, y: 1 }, doc);
  out.dbl_clicks = el.clicks;
  const last = el.events[el.events.length - 1];
  out.dbl_last = [last.type, last.detail, last.clientX];

  // 6) key: keydown+keyup на активном элементе, модификаторы, code
  let input = makeEl("INPUT");
  doc = makeDoc(null, { activeElement: input });
  out.key = await sk17ActionDo({ action: "key", key: "Enter", modifiers: ["ctrl", "shiftKey"] }, doc);
  out.key_types = types(input);
  const kd = input.events[0];
  out.key_fields = [kd.key, kd.code, kd.ctrlKey, kd.shiftKey, kd.altKey, kd.metaKey];
  out.key_hit = out.key.hit;

  // 6b) key без активного элемента — на body
  let body = makeEl("BODY");
  doc = makeDoc(null, { activeElement: null, body });
  out.key_body = await sk17ActionDo({ action: "key", key: "Escape" }, doc);
  out.key_body_types = types(body);
  out.key_body_hit_tag = out.key_body.hit.tag;

  // 7) клик по селектору — полный путь в ЦЕНТРЕ элемента + ровно один el.click()
  el = makeEl("BUTTON"); el.textContent = "  Выбрать   все  ";
  doc = makeDoc(el);
  out.sel_click = await sk17ActionDo({ action: "click", selector: "#b" }, doc);
  out.sel_types = types(el);
  out.sel_clicks = el.clicks;
  const spd = el.events.find((e) => e.type === "pointerdown");
  out.sel_center = [spd.clientX, spd.clientY]; // rect 100,50,40x20 -> центр 120,60
  out.sel_hit = out.sel_click.hit; // текст нормализован и обрезан

  // 8) промах: точка вне вьюпорта (elementFromPoint -> null) = no_element
  doc = makeDoc(null, { hit: () => null });
  out.miss = await sk17ActionDo({ action: "click", x: 9999, y: 9999 }, doc);

  // 8b) hit = то, что реально под точкой (не «то, что хотели»)
  let overlay = makeEl("DIV"); overlay.textContent = "оверлей ozon";
  doc = makeDoc(null, { hit: () => overlay });
  out.hit_report = await sk17ActionDo({ action: "click", x: 50, y: 50 }, doc);
  out.hit_report_hit = out.hit_report.hit;

  // 9) формы ошибок координатного лега
  doc = makeDoc(makeEl("DIV"));
  out.coord_no_xy = await sk17ActionDo({ action: "hover", selector: "#a" }, doc);
  out.coord_with_selector = await sk17ActionDo({ action: "click", x: 1, y: 1, selector: "#a" }, doc);
  out.key_no_key = await sk17ActionDo({ action: "key" }, doc);
  out.bad_action = await sk17ActionDo({ action: "drag", x: 1, y: 1 }, doc);

  // 10) фрейм: явная цепочка page.dom + смещение координат
  let innerBtn = makeEl("BUTTON"); innerBtn.textContent = "Найти";
  const innerDoc = makeDoc(innerBtn, { href: "http://127.0.0.1:8899/inner.html" });
  let frameEl = makeEl("IFRAME", { contentDocument: innerDoc,
    rect: { left: 20, top: 30, width: 300, height: 150, right: 320, bottom: 180 } });
  let topBody = makeEl("BODY");
  doc = makeDoc(null, { hit: (x, y) => (x >= 20 && x < 320 && y >= 30 && y < 180 ? frameEl : topBody),
                        querySelector: (sel) => (sel === "iframe#f" ? frameEl : null) });
  out.frame_click = await sk17ActionDo({ action: "click", x: 120, y: 130, frame: "iframe#f" }, doc);
  out.frame_types = types(innerBtn); // события сели ВНУТРЬ фрейма, не на рамку
  const fpd = innerBtn.events.find((e) => e.type === "pointerdown");
  out.frame_local = [fpd.clientX, fpd.clientY]; // 120-20, 130-30
  out.frame_url = out.frame_click.url;

  // 10b) авто-спуск без frame-параметра: elementFromPoint отдаёт IFRAME — кликаем внутрь
  innerBtn = makeEl("BUTTON");
  const innerDoc2 = makeDoc(innerBtn, { href: "http://127.0.0.1:8899/inner.html" });
  frameEl = makeEl("IFRAME", { contentDocument: innerDoc2,
    rect: { left: 20, top: 30, width: 300, height: 150, right: 320, bottom: 180 } });
  doc = makeDoc(null, { hit: (x, y) => (x >= 20 && x < 320 && y >= 30 && y < 180 ? frameEl : topBody) });
  out.auto_descend = await sk17ActionDo({ action: "click", x: 120, y: 130 }, doc);
  out.auto_types = types(innerBtn);

  // 10c) cross-origin фрейм — честный отказ, не вслепую по рамке
  frameEl = makeEl("IFRAME", { rect: { left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100 } });
  Object.defineProperty(frameEl, "contentDocument", { get() { throw new Error("cross-origin"); } });
  doc = makeDoc(null, { hit: () => frameEl });
  out.xo = await sk17ActionDo({ action: "click", x: 50, y: 50 }, doc);
  out.xo_clicks = frameEl.clicks; // на рамке НЕ кликали

  // 10d) селектор + frame: querySelector уже внутри фрейма
  innerBtn = makeEl("INPUT");
  const innerDoc3 = makeDoc(innerBtn, { href: "http://x/inner.html" });
  frameEl = makeEl("IFRAME", { contentDocument: innerDoc3,
    rect: { left: 0, top: 0, width: 10, height: 10, right: 10, bottom: 10 } });
  doc = makeDoc(null, { querySelector: (sel) => (sel === "iframe#f" ? frameEl : null) });
  // верхний doc: любой селекровый запрос цепочки идёт через querySelector выше;
  // сам #q ищем уже в innerDoc3 (у него querySelector возвращает innerBtn всегда)
  out.frame_input = await sk17ActionDo({ action: "input", selector: "#q", value: "ищи", frame: "iframe#f" }, doc);
  out.frame_input_value = innerBtn.value;

  // 11) input/submit — старые пути без изменений (без hit, точная форма ответа)
  el = makeEl("INPUT"); doc = makeDoc(el);
  out.input = await sk17ActionDo({ action: "input", selector: "#e", value: "v" }, doc);
  let via = "";
  el = makeEl("BUTTON", { form: { requestSubmit: () => { via = "rs"; } } }); doc = makeDoc(el);
  out.submit = await sk17ActionDo({ action: "submit", selector: "#go" }, doc);
  out.submit_via = via;

  // 12) аддендум (а): hit-test пропускает СОБСТВЕННЫЙ оверлей — тень прячется на кадр
  let under = makeEl("CANVAS"); under.textContent = "под баблом";
  let host = makeEl("DIV"); host.id = "hermes-sidekick-host";
  const docH = makeDoc(null, { hit: () => (host.style.display === "none" ? under : host) });
  docH.getElementById = (id) => (id === "hermes-sidekick-host" ? host : null);
  out.host_click = await sk17ActionDo({ action: "click", x: 50, y: 50 }, docH);
  out.host_hit = out.host_click.hit; // попал В ПОДЛОЖКУ, а не в свой бабл
  out.host_clicks = host.clicks; // по своему оверлею НЕ кликали
  out.host_restored = host.style.display !== "none"; // и себя снова показали

  // 13) аддендум (б): body/html/корневой контейнер — промах, а не ложный успех
  let bodyEl = makeEl("BODY");
  doc = makeDoc(null, { hit: () => bodyEl });
  out.body_click = await sk17ActionDo({ action: "click", x: 5, y: 5 }, doc);
  out.body_hover = await sk17ActionDo({ action: "hover", x: 5, y: 5 }, doc); // hover фона легален
  let rootDiv = makeEl("DIV");
  rootDiv.rect = { left: 0, top: 0, width: 1280, height: 800, right: 1280, bottom: 800 };
  doc = makeDoc(null, { hit: () => rootDiv });
  out.root_click = await sk17ActionDo({ action: "click", x: 600, y: 400 }, doc);
  let ptrDiv = makeEl("DIV", { cursorStyle: "pointer" }); ptrDiv.rect = rootDiv.rect;
  doc = makeDoc(null, { hit: () => ptrDiv });
  out.ptr_click = await sk17ActionDo({ action: "click", x: 600, y: 400 }, doc); // авторский кликабельный
  let wideCanvas = makeEl("CANVAS"); wideCanvas.rect = rootDiv.rect;
  doc = makeDoc(null, { hit: () => wideCanvas });
  out.wide_canvas = await sk17ActionDo({ action: "click", x: 600, y: 400 }, doc); // canvas на весь экран — можно
  doc = makeDoc(bodyEl); // и по селектору на body — тот же вердикт
  out.body_sel = await sk17ActionDo({ action: "click", selector: "body" }, doc);

  // 14) аддендум (в): резолвер name/role
  function nameDoc(list) {
    const d = makeDoc(null, { hit: () => null });
    d.querySelectorAll = (sel) =>
      (sel.includes("button") ? list.filter((e) => e.tagName === "BUTTON") : list);
    return d;
  }
  let b1 = makeEl("BUTTON"); b1.textContent = "  Удалить  ";
  doc = nameDoc([b1]);
  out.name_click = await sk17ActionDo({ action: "click", name: "Удалить", role: "button" }, doc);
  out.name_types = types(b1); out.name_clicks = b1.clicks;
  let b2 = makeEl("BUTTON", { getAttribute: (a) => (a === "aria-label" ? "Закрыть профиль" : "") });
  b2.textContent = "×";
  doc = nameDoc([b2]);
  out.name_aria = await sk17ActionDo({ action: "click", name: "Закрыть профиль" }, doc);
  out.name_aria_clicks = b2.clicks;
  let c1 = makeEl("BUTTON"); c1.textContent = "Открыть";
  let c2 = makeEl("BUTTON"); c2.textContent = "Открыть";
  c2.rect = { left: 300, top: 300, width: 50, height: 20, right: 350, bottom: 320 };
  doc = nameDoc([c1, c2]);
  out.name_amb = await sk17ActionDo({ action: "click", name: "Открыть", role: "button" }, doc);
  out.name_amb_clicks = c1.clicks + c2.clicks;
  let lnk = makeEl("A"); lnk.textContent = "Удалить";
  doc = nameDoc([lnk]);
  out.name_role_miss = await sk17ActionDo({ action: "click", name: "Удалить", role: "button" }, doc);
  out.name_none = await sk17ActionDo({ action: "click", name: "Нытик", role: "button" }, doc);
  el = makeEl("BUTTON"); doc = makeDoc(el);
  out.name_sel = await sk17ActionDo({ action: "click", selector: "#b", name: "Удалить" }, doc);
  out.name_sel_clicks = el.clicks; // имя — подпись, селектор главный (SK-17)
  let menu = makeEl("BUTTON"); menu.textContent = "Фильтры";
  doc = nameDoc([menu]);
  out.name_hover = await sk17ActionDo({ action: "hover", name: "Фильтры", role: "button" }, doc);
  out.name_hover_types = types(menu); out.name_hover_clicks = menu.clicks;
  out.name_bare_hover = await sk17ActionDo({ action: "hover" }, doc); // ни цели, ни имени

  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String((e && e.stack) || e) })); process.exit(1); });
"""


def test_executor_coords_node_unit(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    m = re.search(r"// SK-17-BEGIN executor[^\n]*\n(.*?)// SK-17-END executor", JS, re.S)
    assert m, "исполняющий блок с маркерами SK-17 исчез из content.js"
    block = tmp_path / "sk24_executor.js"
    block.write_text(m.group(1), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(harness), str(block)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    out = json.loads(r.stdout)
    assert "fatal" not in out, out.get("fatal")

    FULL = ["pointerover", "mouseover", "pointerenter", "mouseenter", "pointermove", "mousemove",
            "pointerdown", "mousedown", "pointerup", "mouseup", "__click"]
    HOVER = FULL[:6]

    # 1) клик по координатам: полный путь, координаты, флаги указателя
    assert out["coord_click"]["executed"] is True and out["coord_click"]["synthetic"] is True
    assert out["cc_types"] == FULL
    assert out["cc_coords"] == [120, 60, 120, 60]
    assert out["cc_flags"] == [0, True, 1, "mouse", True, True]
    assert out["coord_click"]["hit"] == {"tag": "canvas", "text": "draw-area"}

    # 2/3) hover и move — только over/enter/move, без нажатий и кликов
    assert out["hover_types"] == HOVER and out["hover"]["executed"] is True
    assert out["move_types"] == HOVER

    # 4) rightclick: button:2, contextmenu, ноль el.click()
    assert out["rc_types"] == FULL[:6] + ["pointerdown", "mousedown", "pointerup", "mouseup",
                                          "contextmenu"]
    assert out["rc_buttons"] == [2, 2, 2, 2] and out["rc_clicks"] == 0

    # 5) dblclick
    assert out["dbl_clicks"] == 2 and out["dbl_last"] == ["dblclick", 2, 1]

    # 6) key на активном элементе
    assert out["key_types"] == ["keydown", "keyup"]
    assert out["key_fields"] == ["Enter", "Enter", True, True, False, False]
    assert out["key"]["hit"] == {"tag": "input", "text": ""}
    assert out["key_body_types"] == ["keydown", "keyup"] and out["key_body_hit_tag"] == "body"

    # 7) клик по селектору — полный путь в центре + ровно один click + нормализованный hit
    assert out["sel_types"] == FULL and out["sel_clicks"] == 1
    assert out["sel_center"] == [120, 60]
    assert out["sel_hit"] == {"tag": "button", "text": "Выбрать все"}

    # 8) промах и hit-репорт
    assert out["miss"] == {"err": "no_element"}
    assert out["hit_report_hit"] == {"tag": "div", "text": "оверлей ozon"}

    # 9) ошибки координатного лега
    assert out["coord_no_xy"] == {"err": "bad_request"}
    assert out["coord_with_selector"] == {"err": "bad_request"}
    assert out["key_no_key"] == {"err": "bad_request"}
    assert out["bad_action"] == {"err": "bad_request"}

    # 10) фреймы: цепочка со смещением, авто-спуск, cross-origin, селектор+frame
    assert out["frame_types"] == FULL and out["frame_local"] == [100, 100]
    assert out["frame_url"] == "http://127.0.0.1:8899/inner.html"
    assert out["auto_types"] == FULL
    assert out["xo"] == {"err": "cross_origin_frame"} and out["xo_clicks"] == 0
    assert out["frame_input"]["executed"] is True and out["frame_input_value"] == "ищи"

    # 11) старые пути без изменений в форме ответа (hit — только у click/hover/key)
    assert out["input"] == {"done": True, "executed": True, "action": "input",
                            "synthetic": True, "url": "http://127.0.0.1:8899/canvas.html"}
    assert out["submit_via"] == "rs" and out["submit"]["executed"] is True

    # 12) аддендум (а): hit-test в обход собственного оверлея
    assert out["host_click"]["executed"] is True
    assert out["host_hit"] == {"tag": "canvas", "text": "под баблом"}
    assert out["host_clicks"] == 0 and out["host_restored"] is True

    # 13) аддендум (б): промах по фону — не ложный успех; hover легален
    assert out["body_click"] == {"err": "not_clickable"}
    assert out["body_sel"] == {"err": "not_clickable"}
    assert out["root_click"] == {"err": "not_clickable"}
    assert out["body_hover"]["executed"] is True  # hover по фону — можно
    assert out["ptr_click"]["executed"] is True   # cursor:pointer — авторский кликабельный
    assert out["wide_canvas"]["executed"] is True  # canvas на весь вьюпорт — виджет, не шелл

    # 14) аддендум (в): резолвер name/role
    assert out["name_click"]["executed"] is True and out["name_clicks"] == 1
    assert out["name_types"] == FULL  # тот же полный путь по центру
    assert out["name_aria"]["executed"] is True and out["name_aria_clicks"] == 1  # aria-label
    assert out["name_amb"]["ambiguous"] is True and out["name_amb"]["executed"] is False
    assert out["name_amb_clicks"] == 0  # по неоднозначному НЕ кликаем
    assert [c["rect"] for c in out["name_amb"]["candidates"]] == [[100, 50, 40, 20], [300, 300, 50, 20]]
    assert out["name_role_miss"] == {"err": "no_element"}  # роль не совпала — не угадываем
    assert out["name_none"] == {"err": "no_element"}
    assert out["name_sel_clicks"] == 1  # имя+селектор — SK-17 подпись, селектор главный
    assert out["name_hover_types"] == HOVER and out["name_hover_clicks"] == 0
    assert out["name_bare_hover"] == {"err": "bad_request"}  # ни цели, ни имени


# ---- SK-24 аддендум аудита: name/role через гейт, кандидаты, not_clickable ------------

def test_name_mode_flow(home):
    rid = rpc_call(home, "action.do", {"action": "click", "name": "Удалить", "role": "button"})
    seen = mock_chain(home, COORD_CLEAN,
                      exec_reply={"done": True, "executed": True, "action": "click",
                                  "synthetic": True, "url": "http://x/",
                                  "hit": {"tag": "button", "text": "Удалить"}})
    pv, ex = seen
    assert pv["method"] == "action.preview"
    # имя — одновременно цель резолвера и человеческая подпись; selector/x/y — НИКАКИХ
    assert pv["params"] == {"action": "click", "name": "Удалить", "role": "button"}
    assert ex["id"] == rid and ex["params"]["role"] == "button"
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"]["executed"] is True
    assert out["result"]["hit"] == {"tag": "button", "text": "Удалить"}


def test_name_mode_without_role_and_hover(home):
    rid = rpc_call(home, "action.do", {"action": "hover", "name": "Фильтры"})
    seen = mock_chain(home, COORD_CLEAN,
                      exec_reply={"done": True, "executed": True, "action": "hover",
                                  "synthetic": True, "url": "http://x/",
                                  "hit": {"tag": "button", "text": "Фильтры"}})
    assert seen[0]["params"] == {"action": "hover", "name": "Фильтры"}
    assert "role" not in seen[0]["params"]
    assert read_rpc_out(home, rid)["ok"] is True


def test_name_label_reading_wins_with_selector_or_xy(home):
    """name+selector / name+x,y — это SK-17-подпись, целевой режим не включается."""
    rid = rpc_call(home, "action.do", {"action": "click", "selector": "#b", "name": "Удалить"})
    seen = mock_chain(home, CLEAN, exec_reply=EXEC_OK)
    assert seen[0]["params"]["selector"] == "#b" and "role" not in seen[0]["params"]
    assert read_rpc_out(home, rid)["ok"] is True
    rid = rpc_call(home, "action.do", {"action": "click", "x": 9, "y": 8, "name": "канвас"})
    seen = mock_chain(home, COORD_CLEAN, exec_reply=COORD_EXEC)
    assert seen[0]["params"] == {"action": "click", "x": 9, "y": 8, "name": "канвас"}
    assert read_rpc_out(home, rid)["ok"] is True


def test_name_ambiguous_candidates_reach_the_agent(home):
    """Неоднозначное имя — успешный ответ со списком кандидатов (не «угадай сам»)."""
    cands = [{"tag": "button", "text": "Открыть", "rect": [10, 20, 30, 40]},
             {"tag": "button", "text": "Открыть", "rect": [300, 400, 30, 40]}]
    rid = rpc_call(home, "action.do", {"action": "click", "name": "Открыть", "role": "button"})
    seen = mock_chain(home, {"shown": False, "cancelled": False, "reason": "ambiguous",
                             "candidates": cands}, expect_exec=False)
    assert [f["method"] for f in seen] == ["action.preview"]  # исполнения НЕТ
    out = read_rpc_out(home, rid)
    assert out["ok"] is True  # отказ гейта — успешный ответ (SK-05)
    r = out["result"]
    assert r["executed"] is False and r["reason"] == "ambiguous"
    assert r["candidates"] == cands  # rect'ы дошли до агента — выберет координатой


def test_not_clickable_verdict_refuses_without_execution(home):
    """Аддендум (б): превью отказало на промахе — кольцо не показано, исполнения нет."""
    rid = rpc_call(home, "action.do", {"action": "click", "x": 3, "y": 3})
    seen = mock_chain(home, {"shown": False, "cancelled": False, "reason": "not_clickable"},
                      expect_exec=False)
    assert [f["method"] for f in seen] == ["action.preview"]
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"]["executed"] is False
    assert out["result"]["shown"] is False and out["result"]["reason"] == "not_clickable"


def test_background_forwards_role():
    bg = (ROOT / "extension" / "background.js").read_text()
    assert "role: params.role" in bg  # action_do
    assert bg.count("role: params.role") >= 2  # и action.preview


def test_content_addendum_helpers_present():
    for needle in ("sk17HitPoint", "sk17NotClickable", "sk17ByName", "sk17AccNames",
                   'SK17_HOST_ID = "hermes-sidekick-host"', "not_clickable", "ambiguous"):
        assert needle in JS, needle
