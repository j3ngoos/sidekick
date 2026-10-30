"""SK-47 контракт: vision-скорость — даунскейл/кроп кадра, кэш 2 с, метка scale.

Аудит №2 22.10, проблема 7: vision_analyze 33–120 с на PNG 2578×1898 DPR2.

Пины здесь:
  * мост валидирует локально (прецедент marks/near): max_width не-число/bool/отрицательное/
    >8192 -> bad_request; clip не-объект/пропущенный ключ/не-число/w<=0/h<=0 -> bad_request;
    валидные max_width/clip форвардятся расширению вербатим (вместе с marks);
  * node-юнит sk47Plan (чистая математика кадра): DPR2-даунскейл 2578×1898→1280 с scale≈1280/
    CSS-ширины; «чистый» путь max_width:0 — без ресайза, jpeg:false, scale=DPR; кроп в
    device-пиксели по DPR с клинчем границами; кроп без метрик -> no_metrics; битый clip ->
    bad_clip; без метрик scale:null, даунскейл всё равно работает; rect-карта не трогается —
    план ничего про marks не знает (пересчёт обратно в CSS делает агент по scale, п.10 SK-47);
  * статика проводки: background — OffscreenCanvas/JPEG q80/SHOT_CACHE/sk47_vp/инвалидация в
    actionDoRpc+onUpdated+webNavigation; content — sk47_vp-хендлер и vp в ответе somDraw;
    мост — словарь валидации; AGENTS.md — контракт SK-47.

Живая часть (реальный даунскейл канвасом, hit:cache, кроп диалога, DPR2 через devPixelsPerPx,
замер vision_analyze до/после) — tests/sk47_acc.py, приёмка панели.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from conftest import kill_bridge, start_bridge
from test_rpc_e2e import mock_extension, read_rpc_out, rpc_call
from test_rpc_e2e import run as run_async

ROOT = pathlib.Path(__file__).resolve().parent.parent
BG = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
JS = (ROOT / "extension" / "content.js").read_text(encoding="utf-8")
BR = (ROOT / "bridge" / "sidekick_bridge.py").read_text(encoding="utf-8")


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


# ---- мост: локальная валидация max_width/clip (раунд-трипа не бывает) --------------------

BAD_MAX_WIDTH = ["1280", True, False, -1, -0.5, 8193, 10 ** 9, [1280], {"w": 1280}]
BAD_CLIP = [[], [1, 2, 3, 4], {"x": 0, "y": 0, "w": 0, "h": 5}, {"x": 0, "y": 0, "w": 5},
            {"x": 0, "y": 0, "w": "10", "h": 5},
            {"x": 0, "y": 0, "w": 10, "h": -5}, {"x": 0, "y": 0, "w": 10, "h": 10 ** 7},
            "clip", {"x": 0, "y": 0, "w": 10, "h": None}]
# x/y отрицательны — ЛЕГИТИМНО (rect элемента, уехавшего за край вьюпорта), клинчится
# на стороне расширения; мост режет только форму/размер.


@pytest.mark.parametrize("mw", BAD_MAX_WIDTH)
def test_bridge_rejects_bad_max_width_locally(home, mw):
    # локальный bad_request: вердикт без единого rpc-раунд-трипа (mock-расширение не нужно —
    # прецедент marks: если кадр всё же ушёл, вердикта нет и read_rpc_out вернёт None)
    rid = rpc_call(home, "page.screenshot", {"max_width": mw})
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "bad_request", (mw, out)


@pytest.mark.parametrize("clip", BAD_CLIP)
def test_bridge_rejects_bad_clip_locally(home, clip):
    rid = rpc_call(home, "page.screenshot", {"clip": clip})
    out = read_rpc_out(home, rid, timeout=2.0)
    assert out is not None and out["ok"] is False and out["err"] == "bad_request", (clip, out)


def test_bridge_max_width_zero_and_valid_pass_through(home):
    """0 («чистый» PNG) и валидные число/объект clip — насквозь, вердикт вербатим."""
    async def t1():
        rid = rpc_call(home, "page.screenshot", {"max_width": 0})
        frame = await mock_extension(home, reply={"dataUrl": "data:image/png;base64,AA",
                                                  "scale": 2.0, "capture": {"w": 100, "h": 80}},
                                     expect_methods=["page.screenshot"])
        return rid, frame
    rid, frame = run_async(t1())
    assert frame["params"] == {"max_width": 0}, frame
    r = read_rpc_out(home, rid)
    assert r is not None and r["ok"] is True and r["result"]["scale"] == 2.0, r

    async def t2():
        rid2 = rpc_call(home, "page.screenshot",
                        {"marks": True, "max_width": 1280,
                         "clip": {"x": 10, "y": 20.5, "w": 300, "h": 200}})
        f2 = await mock_extension(home, reply={"dataUrl": "data:image/jpeg;base64,/9j/AA",
                                               "scale": 0.993},
                                  expect_methods=["page.screenshot"])
        return rid2, f2
    rid2, f2 = run_async(t2())
    assert f2["params"] == {"marks": True, "max_width": 1280,
                            "clip": {"x": 10, "y": 20.5, "w": 300, "h": 200}}, f2
    r2 = read_rpc_out(home, rid2)
    assert r2 is not None and r2["ok"] is True and r2["result"]["scale"] == 0.993, r2


# ---- node-юнит: sk47Plan — чистая математика кадра ----------------------------------------

NODE_HARNESS = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8")); // определяет sk47Plan

const out = {};
const near = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps;

// DPR2 инцидента: кадр 2578×1898 при вьюпорте 1289×949, vision-дефолт 1280
const p = sk47Plan({ w: 2578, h: 1898 }, { w: 1289, h: 949 }, null, 1280);
out.dpr2_out = p.outW === 1280 && p.outH === Math.round(1898 * 1280 / 2578);
out.dpr2_scale = near(p.scale, 1280 / 1289); // кадровых пикселей на CSS-пиксель
out.dpr2_full = p.cx === 0 && p.cy === 0 && p.cw === 2578 && p.ch === 1898;
out.dpr2_jpeg = p.jpeg === true;

// «чистый» путь max_width:0 — без ресайза, PNG, scale = DPR
const c = sk47Plan({ w: 2578, h: 1898 }, { w: 1289, h: 949 }, null, 0);
out.clean = c.outW === 2578 && c.outH === 1898 && c.jpeg === false && near(c.scale, 2);

// DPR1, кадр уже уже max_width — ресайза нет, но jpeg есть (vision-путь)
const d1 = sk47Plan({ w: 1200, h: 800 }, { w: 1200, h: 800 }, null, 1280);
out.dpr1_noresize = d1.outW === 1200 && d1.outH === 800 && d1.jpeg === true && near(d1.scale, 1);

// кроп диалога при DPR2: CSS-координаты × DPR в device-пиксели, ресайза нет (600 ≤ 1280)
const k = sk47Plan({ w: 2578, h: 1898 }, { w: 1289, h: 949 }, { x: 100, y: 50, w: 300, h: 200 }, 1280);
out.clip_dev = k.cx === 200 && k.cy === 100 && k.cw === 600 && k.ch === 400;
out.clip_out = k.outW === 600 && k.outH === 400 && near(k.scale, 2);

// кроп у правого/нижнего края — клинчится границами кадра, scale остаётся честным (device/CSS)
const ke = sk47Plan({ w: 2578, h: 1898 }, { w: 1289, h: 949 }, { x: 1200, y: 900, w: 300, h: 300 }, 1280);
out.clip_edge = ke.cx === 2400 && ke.cw === 178 && ke.cy === 1800 && ke.ch === 98 && near(ke.scale, 2);

// кроп + даунскейл: широкий регион 2000 CSS px при DPR1 режется до 1280, scale пересчитан
const kr = sk47Plan({ w: 2000, h: 1000 }, { w: 2000, h: 1000 }, { x: 0, y: 0, w: 2000, h: 1000 }, 1280);
out.clip_resize = kr.outW === 1280 && kr.outH === 640 && near(kr.scale, 1280 / 2000);

// кроп без метрик вьюпорта — CSS-пиксели неразрешимы: честный отказ, не угадывание DPR
out.clip_no_vp = !!sk47Plan({ w: 2578, h: 1898 }, null, { x: 1, y: 1, w: 2, h: 2 }, 1280).err;

// битый clip
out.bad_clip_w0 = !!sk47Plan({ w: 100, h: 100 }, { w: 100, h: 100 }, { x: 0, y: 0, w: 0, h: 5 }, 1280).err;
out.bad_clip_str = !!sk47Plan({ w: 100, h: 100 }, { w: 100, h: 100 }, { x: 0, y: 0, w: "10", h: 5 }, 1280).err;
// отрицательный origin — легитимен (rect за край вьюпорта): не err, клинчится нулём
const kn = sk47Plan({ w: 100, h: 100 }, { w: 100, h: 100 }, { x: -5, y: -1, w: 10, h: 5 }, 1280);
out.clip_neg_ok = !kn.err && kn.cx === 0 && kn.cy === 0 && kn.cw === 10 && kn.ch === 5;

// без метрик (внутренняя страница): scale=null, даунскейл по пикселям кадра работает
const nm = sk47Plan({ w: 2578, h: 1898 }, null, null, 1280);
out.no_metrics = nm.scale === null && nm.outW === 1280 && nm.jpeg === true;

// битый захват
out.bad_cap = !!sk47Plan({ w: 0, h: 100 }, { w: 100, h: 100 }, null, 1280).err;

// outH округляется, а не дробится (canvas целочисленный)
const r = sk47Plan({ w: 3000, h: 2000 }, { w: 3000, h: 2000 }, null, 1280);
out.round_h = r.outH === Math.round(2000 * 1280 / 3000) && Number.isInteger(r.outH);

console.log(JSON.stringify(out));
"""


def test_sk47_plan_node_unit(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    m = re.search(r"// SK-47-BEGIN transform[^\n]*\n(.*?)// SK-47-END transform", BG, re.S)
    assert m, "блок sk47Plan с маркерами SK-47 исчез из background.js"
    block = tmp_path / "sk47_plan.js"
    block.write_text(m.group(1), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(harness), str(block)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    out = json.loads(r.stdout)
    bad = {k: v for k, v in out.items() if v is not True}
    assert not bad, f"sk47Plan: {bad}"


# ---- статика проводки ----------------------------------------------------------------------

def test_background_wires_downscale_clip_cache():
    fn = BG[BG.index("async function pageScreenshot"):BG.index("async function pageActive")]
    assert "max_width" in fn and "clip" in fn
    assert "sk47Xform" in fn and "sk47Plan" in BG and "SHOT_CACHE" in BG
    assert "image/jpeg" in BG and "quality" in BG and "OffscreenCanvas" in BG
    assert 'hit: "cache"' in fn  # п.3: хит помечен в ответе
    assert "scale" in fn and "capture" in fn
    # дефолт vision-пути — 1280, «чистый» кадр — явный 0
    assert "1280" in fn and "params.max_width === 0" in fn


def test_background_invalidates_cache_on_action_and_navigation():
    fn = BG[BG.index("async function actionDoRpc"):BG.index("// SK-25: снимок вкладки")]
    assert "sk47Inval(tab.id)" in fn  # любое агентское действие снимает кэш
    upd = BG[BG.index("browser.tabs.onUpdated.addListener"):BG.index("browser.tabs.onRemoved")]
    assert "sk47Inval(id)" in upd and "sk47Inval(d.tabId)" in upd  # навигация/SPA-роутер


def test_content_serves_viewport_metrics():
    assert 'm.type === "sk47_vp"' in JS
    assert "dpr: devicePixelRatio" in JS
    # somDraw несёт vp с момента отрисовки (даунскейл SoM-кадра без второго раунд-трипа)
    som = JS[JS.index("async function somDraw"):JS.index("function somClearVisual")]
    assert "vp: { w: innerWidth" in som


def test_bridge_validates_sk47_params():
    assert 'params.get("max_width")' in BR and 'params.get("clip")' in BR
    assert "8192" in BR  # потолок max_width


def test_agents_md_documents_sk47():
    doc = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "SK-47" in doc
    for needle in ("max_width", "clip:{x,y,w,h}", "scale", "hit:\"cache\"", "max_width:0"):
        assert needle in doc, needle
    # координатная формула обратно в CSS — обязательная часть контракта
    assert "x_frame/scale" in doc


def test_background_webnavigation_events_are_feature_guarded():
    """Пин стендовой находки SK-47: в сборке firefox-158 (20261007) НЕТ
    webNavigation.onLocationChange — прямой addListener по undefined-событию ронял
    весь startup background (молча, только в консоль браузера; коннект к мосту
    пропал целиком). Любое событие webNavigation вне заведомо имеющихся обязано
    стоять под feature-guard'ом."""
    for m in re.finditer(r"browser\.webNavigation\.(\w+)\.addListener", BG):
        ev = m.group(1)
        if ev == "onHistoryStateUpdated":
            continue  # есть во всех сборках (пин SK-44)
        window = BG[max(0, m.start() - 400):m.start()]
        assert f"if (browser.webNavigation.{ev})" in window, \
            f"webNavigation.{ev}.addListener без feature-guard — уронит background на сборках без события"
