"""SK-26 контракт: SoM-скриншоты — page.screenshot {marks:true} + action.do {mark:N}.

Пины здесь:
  * page.screenshot {marks:true} форвардится насквозь (флаг доходит до расширения),
    ответ {dataUrl, marks:[...]} проходит вербатим; marks не-bool -> bad_request ЛОКАЛЬНО;
    во время handoff-паузы — paused_for_human (общий гейт SK-08);
  * mark — целевой режим action.do: гейт-превью уходит с mark (без selector), исполняющий
    лег — с mark насквозь, ответ проходит вербатим; name рядом — человеческая подпись;
  * отказ превью mark_stale — УСПЕШНЫЙ SK-05-вердикт {shown:false, executed:false,
    reason:"mark_stale"} и НИКАКОГО исполняющего лега (агент обязан переснять);
  * гонка между легами: исполнение отдаёт err mark_stale -> rpc err, не ложный успех;
  * bad_request валидируется локально: mark+selector/x/y/frame, mark у key/move,
    mark 0/-1/1.5/"7"/True/65, input без value;
  * один preview-кадр аудита на action.do с mark (SK-05 форма + via);
  * статика проводки: background пробрасывает mark в оба лега, content содержит
    som_marks/резолвер/словарь mark_stale, мост — словарь глаголов ACTION_DO_MARK.

Плюс node-юнит резолвера sk26ResolveMark и mark-лега исполнителя (тот же блок
SK-17-BEGIN/END): url/rect/селектор-инвалидация карты, полный путь по центру,
input/submit/hover/rightclick, stale -> ни одного события на элементе.
Живая часть (бейджи на кадре, ozon-стенд, скролл-инвалидация в живом Firefox) —
приёмка панели, не pytest.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from conftest import kill_bridge, start_bridge
from test_action_do import CLEAN, mock_chain, read_jsonl
from test_rpc_e2e import mock_extension, read_rpc_out, rpc_call
from test_rpc_e2e import run as run_async

ROOT = pathlib.Path(__file__).resolve().parent.parent

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

JS = (ROOT / "extension" / "content.js").read_text()

MARK_EXEC = {"done": True, "executed": True, "action": "click", "synthetic": True,
             "url": "http://127.0.0.1:8895/som.html",
             "hit": {"tag": "button", "text": "Кнопка 7"}}
MARKS_REPLY = {"dataUrl": "data:image/png;base64,AAAA", "url": "http://127.0.0.1:8895/som.html",
               "marks": [{"n": 1, "selector": "#b1", "tag": "button", "name": "Кнопка 1",
                          "rect": [10, 10, 80, 24]}]}


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


# ---- page.screenshot {marks:true}: флаг насквозь, карта насквозь ------------------------

def test_screenshot_marks_flag_and_map_passthrough(home):
    async def t():
        rid = rpc_call(home, "page.screenshot", {"marks": True})
        frame = await mock_extension(home, reply=MARKS_REPLY, expect_methods=["page.screenshot"])
        return rid, frame
    rid, frame = run_async(t())
    assert frame["params"] == {"marks": True}
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"] == MARKS_REPLY  # карта номер→селектор — вербатим


def test_screenshot_marks_with_tab(home):
    async def t():
        rid = rpc_call(home, "page.screenshot", {"marks": True, "tab": 5})
        frame = await mock_extension(home, reply=MARKS_REPLY, expect_methods=["page.screenshot"])
        return rid, frame
    rid, frame = run_async(t())
    assert frame["params"] == {"marks": True, "tab": 5}  # SK-25 вкладка + SK-26 флаг рядом
    assert read_rpc_out(home, rid)["ok"] is True


def test_screenshot_marks_non_bool_bad_request(home):
    for bad in ("yes", 1, {}, [True]):
        rid = rpc_call(home, "page.screenshot", {"marks": bad})
        out = read_rpc_out(home, rid, timeout=2.0)
        assert out is not None and out["ok"] is False and out["err"] == "bad_request", bad


def test_screenshot_plain_stays_clean_no_marks_key(home):
    """Аддитивность: скрин без marks — тот же round-trip, что был (никакого флага)."""
    async def t():
        rid = rpc_call(home, "page.screenshot")
        frame = await mock_extension(home, reply={"dataUrl": "data:image/png;base64,AAAA"},
                                     expect_methods=["page.screenshot"])
        return rid, frame
    rid, frame = run_async(t())
    assert "marks" not in frame["params"]
    assert read_rpc_out(home, rid)["ok"] is True


def test_screenshot_marks_paused_for_human(home):
    (home / "paused").touch()
    rid = rpc_call(home, "page.screenshot", {"marks": True})
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "paused_for_human"


# ---- mark — целевой режим action.do ------------------------------------------------------

def test_mark_click_clean_gate_executes_mark(home):
    rid = rpc_call(home, "action.do", {"mark": 7, "action": "click"})
    seen = mock_chain(home, CLEAN, exec_reply=MARK_EXEC)
    pv, ex = seen
    assert pv["method"] == "action.preview" and ex["method"] == "action.do"
    assert pv["params"] == {"action": "click", "mark": 7}  # кольцо — на элементе метки
    assert "selector" not in pv["params"] and "x" not in pv["params"]
    assert ex["id"] == rid  # исполнение — на id агента
    assert ex["params"] == {"mark": 7, "action": "click"}
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"]["executed"] is True
    assert out["result"]["hit"] == {"tag": "button", "text": "Кнопка 7"}


def test_mark_name_is_label_only(home):
    rid = rpc_call(home, "action.do", {"mark": 3, "action": "click", "name": "Удалить"})
    seen = mock_chain(home, CLEAN, exec_reply=MARK_EXEC)
    assert seen[0]["params"] == {"action": "click", "mark": 3, "name": "Удалить"}
    assert read_rpc_out(home, rid)["ok"] is True


def test_mark_input_flows_both_legs(home):
    rid = rpc_call(home, "action.do", {"mark": 2, "action": "input", "value": "раз"})
    seen = mock_chain(home, CLEAN,
                      exec_reply={"done": True, "executed": True, "action": "input",
                                  "synthetic": True, "url": "http://x/", "hit": {"tag": "input", "text": ""}})
    assert seen[0]["params"] == {"action": "input", "mark": 2}
    assert seen[1]["params"] == {"mark": 2, "action": "input", "value": "раз"}
    assert read_rpc_out(home, rid)["ok"] is True


def test_mark_gate_refusal_mark_stale_no_execution(home):
    rid = rpc_call(home, "action.do", {"mark": 7, "action": "click"})
    seen = mock_chain(home, {"shown": False, "cancelled": False, "reason": "mark_stale"},
                      expect_exec=False)
    assert [f["method"] for f in seen] == ["action.preview"]
    out = read_rpc_out(home, rid)
    assert out["ok"] is True  # отказ-вердикт — успешный ответ (SK-05), агент переснимает
    r = out["result"]
    assert r.pop("hint", "") .startswith("карта меток протухла")  # SK-42 п.4: отказ самодостаточен
    assert r == {"shown": False, "cancelled": False, "executed": False,
                 "reason": "mark_stale", "action": "click"}


def test_mark_exec_race_mark_stale_is_rpc_error(home):
    """Гонка: превью пропустил, карта протухла между легами -> err, не ложный успех."""
    rid = rpc_call(home, "action.do", {"mark": 7, "action": "click"})
    mock_chain(home, CLEAN, exec_err="mark_stale")
    out = read_rpc_out(home, rid)
    assert out is not None and out["ok"] is False and out["err"] == "mark_stale"


def test_mark_user_stop_is_successful_veto(home):
    rid = rpc_call(home, "action.do", {"mark": 7, "action": "click"})
    seen = mock_chain(home, {"shown": True, "cancelled": True, "reason": "user_stop"},
                      expect_exec=False)
    assert [f["method"] for f in seen] == ["action.preview"]
    out = read_rpc_out(home, rid)
    assert out["ok"] is True and out["result"]["executed"] is False \
        and out["result"]["cancelled"] is True


def test_mark_one_preview_audit_frame(home):
    rid = rpc_call(home, "action.do", {"mark": 7, "action": "click"})
    mock_chain(home, CLEAN, exec_reply=MARK_EXEC)
    read_rpc_out(home, rid)
    audit = read_jsonl(home / "audit.jsonl")
    pv = [a for a in audit if a.get("ev") == "preview" and a.get("via") == "action.do"]
    assert len(pv) == 1, pv


# ---- локальные bad_request (форма цели) ---------------------------------------------------

BAD = [
    {"mark": 7, "action": "click", "selector": "#b"},     # две цели: mark и селектор
    {"mark": 7, "action": "click", "x": 5, "y": 5},       # две цели: mark и координаты
    {"mark": 7, "action": "click", "frame": "iframe#f"},  # кадры — selector/координаты
    {"mark": 7, "action": "key", "key": "Enter"},         # у key цель — фокус
    {"mark": 7, "action": "move", "x": 1, "y": 2},        # move — координатный глагол
    {"mark": 7, "action": "drag"},                        # вне словаря
    {"mark": 0, "action": "click"},
    {"mark": -1, "action": "click"},
    {"mark": 1.5, "action": "click"},
    {"mark": "7", "action": "click"},
    {"mark": True, "action": "click"},                    # bool — не номер
    {"mark": 65, "action": "click"},                      # за потолком карты
    {"mark": 7, "action": "input"},                       # input без value
    {"mark": 7, "action": "input", "value": 5},           # value не строка
    {"action": "click"},                                  # ни цели, ни имени — цели нет (легаси-пин)
]


@pytest.mark.parametrize("params", BAD, ids=[str(i) for i in range(len(BAD))])
def test_bad_request_local(home, params):
    rid = rpc_call(home, "action.do", params)
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "bad_request", params


def test_mark_no_extension_and_paused(home):
    rid = rpc_call(home, "action.do", {"mark": 7, "action": "click"})
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "no_extension"
    (home / "paused").touch()
    rid = rpc_call(home, "action.do", {"mark": 7, "action": "click"})
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "paused_for_human"


def test_mark_valid_verbs_accepted(home):
    """click/input(с value)/submit/rightclick/dblclick/hover — все глаголы с элементом-целью."""
    ok = [{"mark": 1, "action": "click"}, {"mark": 1, "action": "rightclick"},
          {"mark": 1, "action": "dblclick"}, {"mark": 1, "action": "hover"},
          {"mark": 1, "action": "input", "value": ""}, {"mark": 1, "action": "submit"}]
    for p in ok:
        rid = rpc_call(home, "action.do", p)
        seen = mock_chain(home, CLEAN)  # bad_request был бы без кадра вообще
        assert seen and seen[0]["method"] == "action.preview" and seen[0]["params"]["mark"] == 1, p
        assert [f["method"] for f in seen] == ["action.preview", "action.do"], p
        out = read_rpc_out(home, rid)
        assert out["ok"] is True and out["result"]["executed"] is True, p


# ---- статика: проводка полей -------------------------------------------------------------

def test_background_forwards_mark_both_legs():
    bg = (ROOT / "extension" / "background.js").read_text()
    leg = bg[bg.index("actionDoRpc"):bg.index("async function handleRpc")]
    assert "mark: params.mark" in leg, "actionDoRpc не пробрасывает mark"
    pv = bg[bg.index('method === "action.preview"'):]
    pv = pv[:pv.index("\n")]
    assert "mark: params.mark" in pv, "action.preview не пробрасывает mark"


def test_background_screenshot_marks_flow():
    bg = (ROOT / "extension" / "background.js").read_text()
    fn = bg[bg.index("async function pageScreenshot"):bg.index("async function pageActive")]
    assert 'params.marks === true' in fn
    assert '"som_marks"' in fn and '"som_marks_clear"' in fn
    assert "marks_unavailable" in fn  # честный отказ без content.js (лечится SK-18)
    assert "out.marks" in fn and "noteUrl" in fn  # карта + свидетель SK-25


def test_content_has_marks_layer_resolver_and_stale():
    assert "som_marks" in JS and "som_marks_clear" in JS
    assert "sk26ResolveMark" in JS
    assert "mark_stale" in JS
    assert "somDraw" in JS and "somClearVisual" in JS
    assert "sk17ActionDo(m, document, somMap, (s) => sk27ResolveSid(s, document))" in JS  # карта + реестр sid
    # метки — изолированный слой без pointer-events (не воруют клики)
    assert ".som{position:fixed;inset:0;pointer-events:none" in JS


def test_bridge_mark_vocabulary():
    src = (ROOT / "bridge" / "sidekick_bridge.py").read_text()
    assert "ACTION_DO_MARK" in src and "ACTION_DO_MARK_MAX" in src
    assert "mark_stale" in src


# ---- node-юнит: резолвер карты + mark-лег исполнителя -------------------------------------

NODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
eval(src); // sloppy-mode eval: определяет sk17*/sk26* хелперы и sk17ActionDo

class Ev { constructor(type, opts = {}) { this.type = type; Object.assign(this, opts); } }
class PointerEvent extends Ev {}
class MouseEvent extends Ev {}
class KeyboardEvent extends Ev {}
class InputEvent extends Ev {}
const win = { Event: Ev, PointerEvent, MouseEvent, KeyboardEvent, InputEvent };
win.innerWidth = 1280; win.innerHeight = 800;
win.getComputedStyle = (el) => ({ cursor: el.cursorStyle || "auto" });

const URL = "http://127.0.0.1:8895/som.html";
function makeEl(tag, extra) {
  const el = { tagName: tag, events: [], clicks: 0, style: {},
               rect: { left: 100, top: 50, width: 40, height: 20, right: 140, bottom: 70 },
               textContent: "" };
  el.click = () => { el.clicks++; el.events.push({ type: "__click" }); };
  el.dispatchEvent = (e) => el.events.push(e);
  el.getBoundingClientRect = () => el.rect;
  return Object.assign(el, extra || {});
}
function markDoc(bySel, opts) {
  opts = opts || {};
  return { defaultView: win, documentElement: { nodeType: 1 },
    location: { href: opts.href || URL },
    querySelector: (sel) => {
      if (opts.throwSel === sel) throw new Error("bad selector");
      return bySel && bySel[sel] !== undefined ? bySel[sel] : null;
    } };
}
const types = (el) => el.events.map((e) => e.type);
const MAP = { url: URL, ts: 1, marks: [
  { n: 7, selector: "#b7", tag: "button", name: "Кнопка 7", rect: [100, 50, 40, 20] },
  { n: 3, selector: "#q", tag: "input", name: "q", rect: [10, 10, 200, 22] },
] };

const out = {};
(async () => {
  // ---- резолвер: живой путь и все способы протухания ----
  let el = makeEl("BUTTON"); el.textContent = "Кнопка 7";
  let doc = markDoc({ "#b7": el });
  let r = sk26ResolveMark(7, doc, MAP);
  out.r_ok = [r.el === el, r.entry.n, r.entry.selector];
  out.r_nomap = sk26ResolveMark(7, doc, null).err;
  out.r_empty = sk26ResolveMark(7, doc, { url: URL, marks: [] }).err;
  out.r_nav = sk26ResolveMark(7, markDoc({ "#b7": el }, { href: URL + "#spa" }), MAP).err;
  out.r_nonav = sk26ResolveMark(7, markDoc({ "#b7": el }, { href: "http://127.0.0.1:8895/other.html" }), MAP).err;
  out.r_noN = sk26ResolveMark(9, doc, MAP).err;
  out.r_gone = sk26ResolveMark(7, markDoc({}), MAP).err;
  out.r_badsel = sk26ResolveMark(3, markDoc({ "#q": null }, { throwSel: "#q" }), MAP).err;
  // скролл: rect уехал на 300px по вертикали — карта снимка больше не действительна
  let scrolled = makeEl("BUTTON"); scrolled.rect = { left: 100, top: -250, width: 40, height: 20, right: 140, bottom: -230 };
  out.r_scroll = sk26ResolveMark(7, markDoc({ "#b7": scrolled }), MAP).err;
  // субпиксельный шум (1px) — толерантность резолвера, не протухание
  let jitter = makeEl("BUTTON"); jitter.rect = { left: 101, top: 51, width: 40, height: 20, right: 141, bottom: 71 };
  out.r_jitter = !!sk26ResolveMark(7, markDoc({ "#b7": jitter }), MAP).el;
  let hidden = makeEl("BUTTON"); hidden.rect = { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
  out.r_hidden = sk26ResolveMark(7, markDoc({ "#b7": hidden }), MAP).err;
  out.r_zero = sk26ResolveMark(0, doc, MAP).err;

  // ---- исполнитель: mark-клик = полный путь по ЦЕНТРУ элемента метки ----
  el = makeEl("BUTTON"); el.textContent = "Кнопка 7";
  doc = markDoc({ "#b7": el });
  out.m_click = await sk17ActionDo({ action: "click", mark: 7 }, doc, MAP);
  out.m_types = types(el);
  out.m_clicks = el.clicks;
  const pd = el.events.find((e) => e.type === "pointerdown");
  out.m_center = [pd.clientX, pd.clientY]; // rect 100,50,40x20 -> центр 120,60
  out.m_hit = out.m_click.hit;

  // stale-карта: ОТКАЗ и ни одного события на элементе
  el = makeEl("BUTTON");
  doc = markDoc({ "#b7": el });
  out.m_stale = await sk17ActionDo({ action: "click", mark: 7 }, doc, null);
  out.m_stale_events = el.events.length;
  out.m_no_map_arg = await sk17ActionDo({ action: "click", mark: 7 }, doc); // 2-аргументный вызов (легаси)

  // defensive: чужие цели рядом с mark — bad_request (мост такие не пропускает, дубль)
  out.m_plus_sel = await sk17ActionDo({ action: "click", mark: 7, selector: "#x" }, doc, MAP);
  out.m_move = await sk17ActionDo({ action: "move", mark: 7, x: 1, y: 2 }, doc, MAP);

  // mark-input: value + input/change (React-путь), mark-submit: requestSubmit
  let inp = makeEl("INPUT"); inp.rect = { left: 10, top: 10, width: 200, height: 22, right: 210, bottom: 32 };
  doc = markDoc({ "#q": inp });
  out.m_input = await sk17ActionDo({ action: "input", mark: 3, value: "привет" }, doc, MAP);
  out.m_input_value = inp.value;
  out.m_input_types = types(inp);
  let btn = makeEl("BUTTON");
  let submitted = 0;
  btn.form = { requestSubmit: () => { submitted++; } };
  doc = markDoc({ "#b7": btn });
  out.m_submit = await sk17ActionDo({ action: "submit", mark: 7 }, doc, MAP);
  out.m_submit_called = submitted;
  let noform = makeEl("BUTTON"); noform.form = null;
  doc = markDoc({ "#b7": noform });
  out.m_noform = await sk17ActionDo({ action: "submit", mark: 7 }, doc, MAP);
  let div = makeEl("DIV");
  doc = markDoc({ "#b7": div });
  out.m_not_editable = await sk17ActionDo({ action: "input", mark: 7, value: "x" }, doc, MAP);

  // mark-hover: только наведение, без нажатия и клика; mark-rightclick: button:2 + contextmenu
  el = makeEl("BUTTON"); doc = markDoc({ "#b7": el });
  out.m_hover = await sk17ActionDo({ action: "hover", mark: 7 }, doc, MAP);
  out.m_hover_types = types(el); out.m_hover_clicks = el.clicks;
  el = makeEl("BUTTON"); doc = markDoc({ "#b7": el });
  out.m_rc = await sk17ActionDo({ action: "rightclick", mark: 7 }, doc, MAP);
  out.m_rc_buttons = el.events.filter((e) => ["pointerdown", "mousedown"].includes(e.type)).map((e) => e.button);
  out.m_rc_has_ctx = types(el).includes("contextmenu");
  out.m_rc_clicks = el.clicks;

  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String((e && e.stack) || e) })); process.exit(1); });
"""


def test_marks_node_unit(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    m = re.search(r"// SK-17-BEGIN executor[^\n]*\n(.*?)// SK-17-END executor", JS, re.S)
    assert m, "исполняющий блок с маркерами SK-17 исчез из content.js"
    block = tmp_path / "sk26_executor.js"
    block.write_text(m.group(1), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(SK71_SHIM + NODE_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(harness), str(block)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    out = json.loads(r.stdout)
    assert "fatal" not in out, out.get("fatal")

    FULL = ["pointerover", "mouseover", "pointerenter", "mouseenter", "pointermove", "mousemove",
            "pointerdown", "mousedown", "pointerup", "mouseup", "__click"]
    HOVER = FULL[:6]

    # резолвер: живой путь и протухание
    assert out["r_ok"] == [True, 7, "#b7"]
    assert out["r_nomap"] == "mark_stale" and out["r_empty"] == "mark_stale"
    assert out["r_nav"] == "mark_stale" and out["r_nonav"] == "mark_stale"  # SPA/навигация
    assert out["r_noN"] == "mark_stale" and out["r_gone"] == "mark_stale" and out["r_badsel"] == "mark_stale"
    assert out["r_scroll"] == "mark_stale"   # скролл — карта снимка мертва
    assert out["r_jitter"] is True           # 1px — шум, не протухание
    assert out["r_hidden"] == "mark_stale" and out["r_zero"] == "bad_request"

    # исполнитель: полный путь по центру + hit-репорт
    assert out["m_click"]["executed"] is True and out["m_click"]["synthetic"] is True
    assert out["m_types"] == FULL
    assert out["m_clicks"] == 1
    assert out["m_center"] == [120, 60]
    assert out["m_hit"] == {"tag": "button", "text": "Кнопка 7"}

    # stale — отказ и ноль событий
    assert out["m_stale"] == {"err": "mark_stale"} and out["m_stale_events"] == 0
    assert out["m_no_map_arg"] == {"err": "mark_stale"}  # легаси-вызов без карты не кликает вслепую
    assert out["m_plus_sel"] == {"err": "bad_request"} and out["m_move"] == {"err": "bad_request"}

    # input/submit/hover/rightclick
    assert out["m_input"]["executed"] is True and out["m_input_value"] == "привет"
    assert out["m_input_types"] == ["input", "change"]
    assert out["m_submit"]["executed"] is True and out["m_submit_called"] == 1
    assert out["m_noform"] == {"err": "no_form"}
    assert out["m_not_editable"]["err"] == "not_editable"  # SK-42: + hint
    assert out["m_hover"]["executed"] is True and out["m_hover_types"] == HOVER and out["m_hover_clicks"] == 0
    assert out["m_rc_buttons"] == [2, 2] and out["m_rc_has_ctx"] is True and out["m_rc_clicks"] == 0
