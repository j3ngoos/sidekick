"""SK-16 contract tests — preview + ghost cursor: единый оверлей, ring до исполнения.

Живая проверка (стенд firefox-158: флип вверх у нижнего края, wrap/clamp по фактической
ширине, курсор+пузырёк+ring одновременно, ■ Стоп → user_stop, палитра в обеих темах) —
скрипт sk16_r1_panel.py, не pytest. Здесь — статические контракты: семантика вердиктов
SK-05 (contract v1) не поехала; ring больше не гаснет по фиксированному 1.2 с; оверлей
красится в единую accent-схему SK-13; позиционирование без nowrap и хардкода 230px.
"""
import pathlib
import re
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "extension" / "content.js").read_text()

# блоки токенов SK-13 (единственное место, где hex разрешён) и toast (вне скоупа SK-16)
TOKENS = re.search(r":host,:root\{.*?\}\s*@media \(prefers-color-scheme: dark\)\s*\{.*?\}\}", JS, re.S)


def rule(sel):
    """Тело одного CSS-правила оверлея (первое вхождение селектора)."""
    m = re.search(re.escape(sel) + r"\{(.*?)\}", JS, re.S)
    assert m, f"правило {sel} пропало из оверлея"
    return m.group(1)


# ---- контракт вердиктов SK-05 (v1) — свяшен, едет только визуал ----------------

def test_verdict_shapes_unchanged():
    assert '{ shown: false, cancelled: false, reason: "bad_selector" }' in JS
    assert '{ shown: false, cancelled: false, reason: "no_element" }' in JS
    assert '{ shown: true, cancelled: true, reason: "superseded" }' in JS
    assert 'held_ms: wait' in JS  # положительный вердикт: {shown:true, cancelled:false, held_ms, label}


def test_no_stop_button_on_overlay():
    # SK-37: кнопок «Разрешить/Отклонить/Стоп» на курсорном превью-оверлее нет —
    # на курсоре только кольцо + человекочитаемая подпись. Вердикты и ■ Stop
    # переехали в окно чата. Пины: шаблон оверлея без <button>, обработчика ps нет,
    # legacy-вердикт user_stop остаётся в словаре (агент его обрабатывает), но
    # оверлей его больше не выдаёт.
    assert 'id="ps"' not in JS, "SK-37: на превью-оверлее снова завелась кнопка Стоп"
    assert "ps.onclick" not in JS, "SK-37: обработчик кнопки Стоп должен быть удалён"
    assert 'class="pv" id="p"' in JS and '<span id="pt">' in JS, "превью-пузырёк с подписью должен остаться"
    # legacy-вердикт user_stop живёт в finish() (cancelled:true) — контракт v1 цел
    assert 'reason: "user_stop"' in JS or "user_stop" in JS
    # CSS оверлея: правила .pv button больше нет
    assert ".pv button" not in JS, "SK-37: мёртвый CSS кнопки Стоп на оверлее"


# ---- SK-16.1: ring держится до исполнения --------------------------------------

def test_ring_held_until_execution_not_fixed_fade():
    assert "armRelease(" in JS and "PREVIEW_HOLD_MAX_MS" in JS
    assert not re.search(r"\}, 1200\)", JS), "старый фиксированный фейд ring на 1.2 с вернулся"
    # исполнение = реальные события на цели (клик/ввод/submit), без новых rpc моста
    assert re.search(r'for \(const ev of \["click", "input", "keydown", "submit"\]\) doc\.addEventListener', JS)
    assert "visibilitychange" in JS  # и fallback-таймер, и скрытая вкладка


def test_no_new_bridge_methods():
    # UI-only: content.js не добавляет исходящих rpc/WS-методов — только onMessage-обработчики
    assert "sendMessage" not in JS.replace('browser.runtime.sendMessage({ type: "sidekick-ready" })', "")


# ---- SK-16.2: позиционирование ---------------------------------------------------

def test_no_nowrap_no_hardcoded_230():
    assert "innerWidth - 230" not in JS
    assert not re.search(r"\.pv\{[^}]*white-space:nowrap", JS), "пузырёк снова nowrap"
    assert "overflow-wrap:anywhere" in JS and "min(320px,85vw)" in JS


def test_flip_and_clamp_by_measured_size():
    assert "pv.offsetWidth" in JS and "pv.offsetHeight" in JS
    assert 'side = "below"' in JS and 'side = "above"' in JS  # флип по свободному месту
    assert re.search(r"Math\.max\(4, Math\.min\(Math\.round\(cx - bw / 2\), innerWidth - bw - 4\)\)", JS)
    assert '"--ax"' in JS  # стрелка едет по цели, когда пузырёк прижат к краю


def test_bubble_rides_the_cursor():
    # курсор и пузырёк едут вместе (.slide) и стрелка смотрит на цель (::after below/above)
    assert ".cur.slide" in JS and ".pv.slide" in JS
    assert ".pv.below::after" in JS and ".pv.above::after" in JS
    assert "c.classList.add(\"on\")" in JS  # превью само показывает призрачный курсор


# ---- SK-16.3: единая accent-палитра ----------------------------------------------

def test_accent_scheme_everywhere():
    assert TOKENS, "блок токенов SK-13 пропал"
    assert "--accent:#7c3aed" in TOKENS.group(0) and "--accent:#a78bfa" in TOKENS.group(0)
    for sel in (".cur", ".ring", ".pv"):
        body = rule(sel)
        assert "var(--accent" in body, f"{sel}: нет var(--accent)"
        assert not re.search(r"#[0-9a-fA-F]{3,8}", body), f"{sel}: hex в компоненте"
    # три старые несовместимые расцветки выключены
    for gone in ("#f59e0b", "rgba(245,158,11", "#7f1d1d", "#111827f2"):
        assert gone not in JS, f"старая расцветка вернулась: {gone}"


def test_dark_theme_ink_is_dark_on_light_accent():
    # --on-accent: белый в light, тёмный в dark (контраст на светлом accent-пузырьке)
    assert "--on-accent:#fff" in JS and "--on-accent:#1b1b20" in JS
    assert "color:var(--on-accent)" in rule(".pv")


# ---- кольцевые конфликты ----------------------------------------------------------

def test_cursor_and_highlight_yield_to_preview():
    guard = "if (previewPending || previewRelease) return;"
    assert JS.count(guard) >= 2  # cursor-толчок и highlight не воюют с активным превью
    # seq highlight снимается ДО scroll-wait (превью, стартовавшее в середине, побеждает)
    assert re.search(r"const seq = previewSeq;.*?scrollIntoView", JS[JS.index('m.type === "highlight"'):], re.S)


def test_content_js_parses():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node нет — синтаксис проверим на стенде")
    r = subprocess.run([node, "--check", str(ROOT / "extension" / "content.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:300]
