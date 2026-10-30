"""SK-39 contract: выразительное движение — пружина, ripple, позы, reduced-motion.

Живая часть (fps на стенде, скриншот-серии «до/во время/после», эмуляция
prefers-reduced-motion, latency before/after) — tests/sk39_acc.py, не pytest.
Здесь — статические контракты и node-юнит:

  * движение только на transform/opacity (композитинг); left/top-переходов нет;
  * пружина подлёта ease-out-back (.34,1.56,.64,1) 300 мс;
  * prefers-reduced-motion: CSS transition:none + sk39RM() для WAAPI-веток;
  * ripple = WAAPI scale(.28→1)+opacity(.95→0), 620–900 мс, 2–3 кольца
    (dblclick — 4), акцент из темы страницы, защита от шквала кликов;
  * «отдача» scale(.98) через composite:add, inline-элементы и BODY/HTML пропускаем;
  * позы: клик = указатель, ввод = I-beam (s-text), спиннер на грейс-период;
  * хуки в SK-17-исполнителе обёрнуты в typeof-guard + try/catch: node-юнит
    без sk39Fx жив, с recorder-ом — видит вызовы ПОСЛЕ fire, submit/key/scroll
    визуал не дёргают;
  * sk39Fx определён ВНЕ SK-17-блока (блок извлекается verbatim и не имеет права
    на неё ссылаться без guard).
"""
import pathlib
import re
import shutil
import subprocess

import pytest

from test_action_do import NODE_HARNESS

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "extension" / "content.js").read_text()
BLOCK = re.search(r"// SK-17-BEGIN executor[^\n]*\n(.*?)// SK-17-END executor", JS, re.S)
assert BLOCK, "маркеры SK-17 исчезли из content.js"


def rule(sel):
    """Тело одного CSS-правила оверлея (первое вхождение селектора)."""
    m = re.search(re.escape(sel) + r"\{(.*?)\}", JS, re.S)
    assert m, f"правило {sel} пропало из оверлея"
    return m.group(1)


# ---- движение: только transform/opacity ----------------------------------------

def test_motion_is_transform_opacity_only():
    # ни одного CSS-перехода по left/top/width/height — только transform/opacity
    assert not re.search(r"transition:[^;}]*\b(left|top|width|height)\b", JS), \
        "анимируется layout-свойство — просадка fps; позиция ездит на translate3d"
    cur = rule(".cur")
    assert "transition:transform" in cur and "opacity" in cur
    assert "translate3d" in cur  # стартовая позиция — тоже transform
    assert 'el.style.transform = "translate3d("' in JS  # place() — единая точка записи


def test_spring_arrival_curve():
    # ease-out-back эквивалент: cubic-bezier(.34,1.56,.64,1) — overshoot на посадке
    assert "cubic-bezier(.34,1.56,.64,1)" in JS
    assert re.search(r"\.cur\{[^}]*transition:transform \.3s cubic-bezier\(\.34,1\.56,\.64,1\)", JS, re.S)
    assert ".cur.slide" in JS and ".pv.slide" in JS  # цепочка превью — мягче, без overshoot-войн


def test_reduced_motion_instant_states():
    assert "@media (prefers-reduced-motion: reduce)" in JS
    assert ".cur,.lbl,.pv,.ring{transition:none}" in JS
    assert ".cur.on .curi,.spin{animation:none}" in JS
    # WAAPI-ветки (ripple/glow/recoil) тоже сверяются с prefers-reduced-motion
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in JS
    assert "sk39RM()" in JS


def test_breathing_and_spinner():
    assert ".cur.on .curi{animation:sk39breath" in JS  # дыхание только когда курсор виден
    assert "@keyframes sk39breath{50%{transform:scale(1.07)}}" in JS
    assert "@keyframes sk39spin{to{transform:rotate(360deg)}}" in JS
    assert ".cur.waiting .spin{display:block}" in JS


# ---- ripple / glow / recoil ------------------------------------------------------

def test_ripple_waapi_contract():
    assert 'ring.className = "rp"' in JS
    assert 'transform: base + " scale(.28)", opacity: 0.95' in JS
    assert 'transform: base + " scale(1)", opacity: 0' in JS
    assert "duration: 620 + i * 130" in JS  # 620–900 мс
    assert "delay: i * 90" in JS
    assert "dbl ? 4 : 3" in JS  # 2–3 кольца, dblclick — 4
    assert "childElementCount > 24" in JS  # шквал кликов: визуал сыпется первым
    assert "a.finished.then(() => ring.remove()" in JS  # кольца не текут
    # reduced-motion: мгновенное состояние без движения
    assert 'ring.style.transform = base + " scale(.7)"' in JS


def test_ripple_accent_from_page_theme():
    assert "function sk39AccentOf" in JS
    assert "sk39Ripple(x, y, el ? sk39AccentOf(el) : \"\"" in JS
    # хроматичность: серый/полупрозрачный не годится
    assert "mx - mn >=" in JS and "a > 0.6" in JS


def test_recoil_composite_add():
    assert 'Object.assign({ composite: "add" }' in JS  # не воюем со своими transform страницы
    assert '"scale(0.98)"' in JS.replace("'", '"')
    assert 'display === "inline"' in JS  # non-replaced inline не трансформируется
    assert 'tag === "BODY" || tag === "HTML"' in JS
    assert "catch (e2)" in JS  # фолбэк без composite для старых движков


def test_hover_glow():
    assert 'g.className = "glw"' in JS
    assert "sk39Glow(el)" in JS
    assert '{ opacity: 0.9, offset: 0.3 }' in JS


# ---- позы курсора -----------------------------------------------------------------

def test_pose_api():
    assert 'function sk39Shape(kind) { $("c").classList.toggle("s-text", kind === "text")' in JS
    # превью выбирает позу ДО подлёта: ввод/клавиша — I-beam, остальное — указатель
    assert 'sk39Shape(act === "input" || isKey ? "text" : "arrow")' in JS
    # спиннер: горит на грейс-периоде, гаснет на вердикте
    assert "sk39Wait(true);" in JS and "sk39Wait(false);" in JS
    assert JS.index("sk39Wait(false)") < JS.index("armRelease(seq, el, c);")
    # I-beam нарисован CSS-ом (без emoji/картинок)
    assert ".sh-text::before,.sh-text::after" in JS
    assert ".cur.s-text .sh-arrow{display:none}" in JS


def test_cursor_push_uses_pointer_pose():
    # push-режим (фоновый курсор агента) — всегда указатель
    m = re.search(r'if \(m\.type === "cursor"\) \{.*?sk39Shape\("arrow"\)', JS, re.S)
    assert m, "cursor-хендлер не сбрасывает позу в указатель"


# ---- неисполняющий характер визуала ------------------------------------------------

def test_sk39fx_defined_outside_executor_block():
    assert BLOCK, "маркеры SK-17 исчезли"
    assert JS.index("function sk39Fx") < JS.index("SK-17-BEGIN"), \
        "sk39Fx должен жить ВНЕ извлекаемого блока (node-юнит его не знает)"


def test_executor_hooks_are_guarded():
    block = BLOCK.group(1)
    for i, ln in enumerate(block.splitlines()):
        if re.search(r"\bsk39(Fx|Recoil)\(", ln):
            assert "typeof sk39" in ln, f"строка {i}: вызов без typeof-guard разорвёт node-юнит"
    # каждый клик-хук стоит ПОСЛЕ чехла fire (визуал — отчёт об исполнении, не гейт)
    assert len(re.findall(r"sk48ClickWrap[\s\S]{0,900}?sk39Fx", block)) >= 4


def test_no_fx_for_submit_key_scroll():
    # хуки стоят только на клик- и input-путях; submit/key/scroll-возвраты чисты
    block = BLOCK.group(1)
    for m in re.finditer(r'return \{ done: true, executed: true, action, synthetic: true,[^\n]*', block):
        ctx = block[max(0, m.start() - 260):m.start()]
        if "requestSubmit" in ctx or "sk17Fire(win, el, \"keydown\"" in ctx or "scrollBy" in ctx:
            assert "sk39Fx" not in ctx, "визуал прицепился к submit/key/scroll"


# ---- node-юнит: guard жив, recorder видит вызовы -----------------------------------

def _run_harness(tmp_path, prelude=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    block = tmp_path / "sk17_executor.js"
    block.write_text(BLOCK.group(1), encoding="utf-8")
    harness = NODE_HARNESS.replace("eval(src);", prelude + "eval(src);")
    harness = harness.replace("console.log(JSON.stringify(out));",
                              "out.fx = globalThis.__fx || [];\n  console.log(JSON.stringify(out));")
    h = tmp_path / "harness.js"
    h.write_text(harness, encoding="utf-8")
    r = subprocess.run([node, str(h), str(block)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    import json
    out = json.loads(r.stdout)
    assert "fatal" not in out, out.get("fatal")
    return out


def test_executor_alive_without_sk39fx(tmp_path):
    # без sk39Fx (как в node-юните SK-17) — исполнитель жив и слеп: ни одного вызова
    out = _run_harness(tmp_path)
    assert out["click"]["executed"] is True
    assert out["fx"] == []


def test_executor_reports_fx_after_fire(tmp_path):
    prelude = ('globalThis.sk39Fx = (kind, x, y, el) => '
               '{ (globalThis.__fx = globalThis.__fx || []).push([kind, x, y, !!el]); };\n'
               'globalThis.sk39Recoil = () => { (globalThis.__fx = globalThis.__fx || []).push(["recoil"]); };\n')
    out = _run_harness(tmp_path, prelude)
    assert out["click"]["executed"] is True and out["input"]["executed"] is True
    kinds = [e[0] for e in out["fx"]]
    assert "click" in kinds, "клик не породил визуальный отчёт"
    assert "input" in kinds, "ввод не породил I-beam-позу"
    assert "submit" not in kinds and "key" not in kinds and "scroll" not in kinds
    assert all(e[3] is True for e in out["fx"]), "хук обязан передавать цель (el)"
