"""SK-30 контракты: UI v2 — меню «⋯» вместо шести кнопок, inline-SVG иконки,
liquid glass / стиль Firefox Proton.

Спека карточки t_2b7edeb8:
  1. Нижняя панель = композер + ОДНА кнопка «⋯»; меню-портал, Esc/клик вне,
     клавиатурная навигация.
  2. Иконки — inline-SVG спрайт (16px, stroke 2, currentColor); эмодзи в chrome
     UI запрещены, статус-глифы мигрируют в SVG-кандидатов (текстовая метка
     остаётся честным fallback-ом для DOM-заглушки node-контрактов).
  3. Glass-токены в theme.css (--glass-bg/--glass-border/--glass-shadow),
     обе темы + ручной оверрайд data-theme.
     SK-53: glass-режим убит принятым вариантом «Steel Terminal» (SK-52,
     выбор пользователя 26.10): --glass-blur: 0px, --glass-* непрозрачные,
     --glass-shadow: none, скругления 6/8/99 (лестница Binance), тень только
     на всплывающих (--pop-shadow). Пины ниже — Steel-контракт, имена
     функций сохранены (преемственность истории приёмок).
  4. Кнопки: hover/active/focus-visible/disabled, размеры 26/30 (SK-53
     компактность Binance; было 28/32).
  5. Options — steel-карточка (surface-2 + hairline без тени); превью-пузырёк
     content.js SK-53 не тронут (свой единственный blur-слой живёт в чужом
     документе).
  6. backdrop-filter в сайдбаре — НОЛЬ слоёв (SK-53; было не более 3).

Живое (скрины, vision, fps, клавиатура в реальном Firefox) — харнесс
sk30_acc.py на стенде firefox-158, не здесь.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
HTML = (EXT / "sidebar.html").read_text(encoding="utf-8")
SB = (EXT / "sidebar.js").read_text(encoding="utf-8")
FEED = (EXT / "feed.js").read_text(encoding="utf-8")
AUD = (EXT / "audit.js").read_text(encoding="utf-8")
CSS = (EXT / "sidebar.css").read_text(encoding="utf-8")
THEME = (EXT / "theme.css").read_text(encoding="utf-8")
OPT_HTML = (EXT / "options.html").read_text(encoding="utf-8")
OPT_CSS = (EXT / "options.css").read_text(encoding="utf-8")
CONTENT = (EXT / "content.js").read_text(encoding="utf-8")

# Диапазоны «настоящих» эмодзи/пиктограмм. Монохромные типографские метки
# (▸ ⚙ ✓ ✗ ○ ▶ ⏸ — статус-язык контрактов SK-22/11) сюда НЕ входят: они
# в живом UI подменяются SVG-кандидатами, а в текстах fallback-ов живут.
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # пиктограммы: 💭🖐🕘💾📜🗑🌐📷🔐👁🗝⛔ …
    "\u2600-\u27BF"          # misc symbols + dingbats: ⚠ ➤ ✓(на всякий) …
    "\u27F0-\u27FF"          # supplemental arrows: ⟳ …
    "\u2B00-\u2BFF"          # arrows/squares: ⬆⬇ …
    "\uFF0B"                 # fullwidth plus ＋
    "\uFE0F"                 # emoji-variant
    "]")


def _no_comments(s):
    return re.sub(r"<!--.*?-->", "", s, flags=re.S)


# ---- п.1: композер + одна кнопка «⋯» ------------------------------------------------
# SK-38 редизайн: «⋯» переехала из #bar в шапку, в баре осталась ОДНА морф-кнопка.
# Контракт «плоских кнопок в баре нет» жив и в новой геометрии — просто степень 2→1.

def test_bar_has_composer_and_single_morph_button():
    # внутри #bar вложенный #inwrap — режем блок по началу меню-портала
    bar = HTML.split('id="bar"')[1].split('<div id="menu"')[0]
    assert bar.count("<button") == 1, "в #bar осталась лишняя кнопка (SK-38: композер + 1 кнопка)"
    assert 'id="go"' in bar and 'id="menuBtn"' not in bar
    # «⋯» — в шапке, правый край, со всей меню-семантикой
    hd = HTML.split('id="hdrow"')[1].split("</div>\n</div>")[0]
    assert 'id="menuBtn"' in hd
    assert 'aria-haspopup="menu"' in hd and 'aria-expanded="false"' in hd


def test_menu_portal_holds_the_old_flat_buttons():
    # портал — прямой ребёнок body после #bar; старые id переехали в него
    assert HTML.index('id="menu"') > HTML.index('id="bar"')
    menu = HTML.split('id="menu"')[1].split("</div>\n<script")[0]
    for bid in ("newBtn", "histBtn", "audBtn", "sksave", "skioBtn"):
        assert f'id="{bid}"' in menu, f"{bid} должен жить в меню-портале"
    assert 'role="menu"' in HTML and HTML.count('role="menuitem"') >= 7
    assert 'id="optBtn"' in menu and 'id="stopTask"' in menu
    assert "disabled" in menu.split('id="stopTask"')[1][:120], \
        "«Стоп задача» стартует disabled — включается только на running (п.4)"


def test_menu_keyboard_contract_in_sidebar_js():
    assert "function menuOpen" in SB
    for key in ('"Escape"', '"ArrowDown"', '"ArrowUp"', '"Home"', '"End"', '"Tab"'):
        assert key in SB, f"меню обязано знать {key}"
    assert "menuBtn.setAttribute(\"aria-expanded\"" in SB
    assert "browser.runtime.openOptionsPage()" in SB
    # закрытие кликом вне — document mousedown, а не onclick (не ловит т.ср.)
    assert 'document.addEventListener("mousedown"' in SB
    # «Стоп задача» из меню шлёт тот же stop-кадр, что и ■ в шапке
    assert 'stopTaskBtn.onclick = () => port.postMessage({ type: "stop", task: TL.task });' in SB
    # disabled-логика: живёт только при running (включая параллельные SK-25)
    assert 'stopTaskBtn.disabled = !anyRun' in SB
    # открытие второго вида (аудит/история) закрывает меню
    assert SB.count("if (on) menuOpen(false);") == 2


# ---- п.2: inline-SVG спрайт, эмодзи в chrome UI нет ---------------------------------

SPRITE_ICONS = ["chat", "history", "audit", "settings", "skills", "stop", "send",
                "plus", "menu", "theme", "refresh", "close", "save", "download",
                "upload", "transfer", "hand", "think", "memory", "globe", "camera",
                "chevron", "ring", "play", "check", "cross", "pause", "dot",
                "alert", "clock", "replace", "lock", "eye", "key", "ban", "state"]


def test_sprite_has_the_required_icon_set():
    for name in SPRITE_ICONS:
        assert f'<symbol id="ic-{name}"' in HTML, f"спрайт обязан содержать ic-{name}"
    assert 'viewBox="0 0 24 24"' in HTML
    # единый язык: stroke 2, currentColor, fill none — классом, не инлайн
    assert re.search(r"\.icn\s*\{[^}]*stroke-width:\s*2[^}]*\}", CSS)
    assert "stroke: currentColor" in CSS and "fill: none" in CSS


def test_no_emoji_in_chrome_markup():
    for name, src in (("sidebar.html", HTML), ("options.html", OPT_HTML)):
        body = _no_comments(src)
        bad = EMOJI_RE.findall(body)
        assert not bad, f"{name}: эмодзи в chrome UI запрещены: {bad}"


def test_no_emoji_in_the_preview_overlay():
    # тень-оверлей content.js: разметка пузыря между `const TEMPLATE = \`` и `;`
    # (SK-41: шаблон вынесен в константу — рендерится лениво в ensure(), но
    # разметка та же; пины ниже проверяют именно её)
    m = re.search(r"const TEMPLATE = `\n(.*?)`;", CONTENT, re.S)
    assert m, "шаблон оверлея content.js не найден"
    bad = EMOJI_RE.findall(m.group(1))
    assert not bad, f"content.js overlay: {bad}"
    # SK-37: кнопки «Стоп» на превью-оверлее нет (иконка ic-stop убрана вместе с
    # ней) — на курсоре только кольцо + подпись. Проверяем, что шаблон пузыря
    # не содержит ни <button>, ни ic-stop (вердикты/стоп — в окне чата).
    assert "<button" not in m.group(1), "SK-37: на превью-оверлее не должно быть кнопок"
    assert "ic-stop" not in m.group(1), "SK-37: иконка Стоп на оверлее больше не нужна"


def test_status_glyphs_migrate_to_svg_candidates():
    # текстовые карты остаются (fallback + контракты SK-28/31), но живой DOM
    # обязан подменять глиф иконкой: GLYPH_ICON + setGlyphText в сайдбаре,
    # setIcoText + createElementNS-guard в feed.js (stub без SVG — текст).
    assert "const GLYPH_ICON" in SB and "function setGlyphText" in SB
    for g in ("💭", "🖐", "⏸", "✓", "✗", "○", "⚠", "⏱", "🗑", "⚙", "🌐"):
        assert f'"{g}"' in SB or f'"{g}"' in FEED or f'"{g}":' in SB, g
    assert "function setIcoText" in FEED and "createElementNS" in FEED
    assert re.search(r"if \(!doc \|\| typeof doc\.createElementNS !== \"function\"\) return null", FEED)
    # audit.js: row model больше не несёт эмодзи — только стабильные ключи
    assert not EMOJI_RE.findall(AUD), "audit.js обязан отдавать ключи иконок"
    assert 'row("perm", o, "lock"' in AUD and 'row("preview", o, "eye"' in AUD


# ---- п.3: liquid glass / Proton ------------------------------------------------------

def test_glass_tokens_defined_in_both_themes():
    # SK-53 Steel Terminal: per-theme токены (базовый light + media light +
    # media dark + 2 оверрайда). Glass мёртв: blur 0, --glass-bg непрозрачные
    # (hex, не rgba-стекло), --glass-shadow none; radii 6/8; dark-границы —
    # стальные hex-ступени (Binance «borders = surface steps»).
    for tok in ("--glass-bg", "--glass-border", "--glass-shadow", "--bg-grad"):
        assert THEME.count(f"{tok}:") >= 3, f"{tok} обязан быть в light, dark и оверрайдах"
    assert re.search(r"--glass-blur:\s*0px", THEME), "SK-53: glassmorphism мёртв — blur 0px"
    assert re.search(r"--glass-bg:\s*#[0-9a-fA-F]{6}", THEME), "--glass-bg непрозрачный steel"
    assert re.search(r"--r1:\s*6px", THEME) and re.search(r"--r2:\s*8px", THEME)
    dark = THEME.split('prefers-color-scheme: dark')[1]
    assert "--glass-border: #333236" in dark and "--glass-shadow: none" in dark
    assert "--pop-shadow:" in THEME, "SK-53: единственная тень — всплывающие слои"
    # анти-slop: linear-gradient — только плоские одноцветные подмены (2 одинаковые
    # стопы): --grad-hot/--grad-hot-hi/--bg-grad (см. шапку theme.css)
    for g in re.findall(r"linear-gradient\(([^)]*)\)", THEME):
        stops = [s.strip() for s in g.split(",")]
        assert len(stops) == 2 and stops[0] == stops[1], f"градиент не плоский: {g!r}"
    assert not re.search(r"(radial|conic)-gradient", THEME)


def test_glass_surfaces_use_the_tokens():
    # SK-53: шапка/композер/меню красятся теми же семантическими токенами, но
    # непрозрачной сталью и БЕЗ blur; тень — только на всплывающих (#menu,
    # #bellpanel) через --pop-shadow
    for hook in ("#hd", "#bar", "#menu"):
        block = CSS.split(f"{hook} {{")[1].split("}")[0]
        assert "var(--glass-bg" in block, hook
        assert "backdrop-filter" not in block, f"{hook}: SK-53 — blur мёртв"
    assert CSS.count("var(--pop-shadow)") >= 2, "pop-shadow на меню и bellpanel"


def test_backdrop_filter_budget_three_layers():
    # SK-53: бюджет стекла обнулён (Binance «no glassmorphism — depth comes from
    # color-block contrast»): в сайдбаре 0 слоёв blur. Превью-пузырёк content.js
    # живёт в чужом документе и SK-53 не тронут — остаётся 1 слой.
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    n = len(re.findall(r"(?<![-\w])backdrop-filter", css))
    assert n == 0, f"SK-53: сайдбар обязан жить без blur-слоёв (найдено {n})"
    content = re.sub(r"/\*.*?\*/", "", CONTENT, flags=re.S)
    assert len(re.findall(r"(?<![-\w])backdrop-filter", content)) == 1
    # превью-пузырёк — свой единственный слой в чужом документе
    content = re.sub(r"/\*.*?\*/", "", CONTENT, flags=re.S)
    assert len(re.findall(r"(?<![-\w])backdrop-filter", content)) == 1


def test_theme_override_mechanism():
    assert ':root[data-theme="light"]' in THEME and ':root[data-theme="dark"]' in THEME
    assert ':root:not([data-theme="dark"])' in THEME and ':root:not([data-theme="light"])' in THEME
    assert 'const THEME_KEY = "sk30Theme"' in SB
    assert "document.documentElement.dataset.theme" in SB
    assert "browser.storage.local.get(THEME_KEY)" in SB  # выбор переживает перезагрузку


# ---- п.4: кнопки ----------------------------------------------------------------------

def test_button_states_and_sizes():
    assert "--ctl-h: 30px" in THEME and "--ctl-h-sm: 26px" in THEME  # SK-53 Binance-плотность
    # SK-38: ■ Stop в шапке умер — его роль у morph-состояния #go.running
    for sel in ("#go:hover", "#go:active", "#menuBtn:hover", ".mi:hover", ".mi:active",
                ".mi:disabled", "#go.running:hover"):
        assert sel in CSS, f"нет состояния {sel}"
    assert ":focus-visible" in CSS
    # disabled без opacity (урок SK-13 про контраст)
    dis = CSS.split(".mi:disabled")[1].split("}")[0]
    assert "opacity" not in dis
    # «никаких border:none без фона»: у accent-кнопок фона нет только вместе с disabled-стилем
    go = CSS.split("#go {")[1].split("}")[0]
    assert "background" in go


# ---- п.5: options и превью-пузырёк в том же стиле -------------------------------------

def test_options_page_glass():
    # SK-53 (спека §7д): карточка = surface-2 + hairline --line, elevation = шаг
    # тона, box-shadow: none (raycast feature-card-dark, стекло мертво)
    card = re.search(r"\.card\s*\{[^}]+\}", OPT_CSS).group(0)
    assert "var(--surface-2)" in card and "var(--line)" in card
    assert "box-shadow: none" in card
    assert "button:focus-visible" in OPT_CSS and "button:active" in OPT_CSS
    assert "min-height: var(--ctl-h)" in OPT_CSS
    # SK-53: primary — плоский solid, без градиента и цветной тени
    btn = OPT_CSS.split("button {")[1].split("}")[0]
    assert "var(--btn-accent)" in btn and "var(--grad-hot)" not in btn
    assert 'id="spr"' in OPT_HTML and "ic-alert" in OPT_HTML  # ⚠ → SVG


def test_preview_bubble_glass():
    pv = CONTENT.split(".pv{")[1].split("}")[0]
    assert "backdrop-filter" in pv and "color-mix(in srgb, var(--accent) 88%" in pv
    assert "var(--glass-border)" in pv
    # 88% акцента: подложка почти сплошная — читаемость (vision-приёмка) не зависит от страницы
    assert CONTENT.count("color-mix(in srgb, var(--accent) 88%, transparent)") >= 3


# ---- п.6: без npm/картинок; hygiene-контракты SK-13/22 живые --------------------------

def test_no_external_assets_or_npm():
    for src, name in ((HTML, "sidebar.html"), (OPT_HTML, "options.html"),
                      (CSS, "sidebar.css"), (OPT_CSS, "options.css"),
                      (THEME, "theme.css")):
        assert not re.search(r"url\((?!#)", src), f"{name}: внешние картинки-файлы запрещены"
        assert "node_modules" not in src
    assert not (EXT.parent / "package.json").exists()


def test_sidebar_css_still_token_only():
    # SK-13 hygiene переживает редизайн: hex-литералов в компонентах нет
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", CSS)
    for ms in re.findall(r"transition:[^;]*?(\d+)\s*ms", CSS):
        assert int(ms) <= 150
    for ms in re.findall(r"transition:[^;]*?(\d+(?:\.\d+)?)s(?!\w)", CSS):
        assert float(ms) * 1000 <= 150


def test_no_inline_styles_in_sidebar_js():
    assert ".style." not in SB
    assert ".style." not in FEED
