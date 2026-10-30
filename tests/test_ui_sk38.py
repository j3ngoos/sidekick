"""SK-38 контракты: шапка/композер — меню наверху, иконка расширения,
стоп-в-кнопке, живой плейсхолдер.

Спека карточки t_7cf9bfad:
  1. Шапка: без надписи «Hermes» и без статуса «Думает/выполняет» (статус
     живёт внизу — карточка задачи/think-блок). В шапке: иконка-молния
     (inline-SVG) + заголовок чата (первый запрос с truncation и «…», либо
     «Новый чат»).
  2. Иконки расширения 16/32/48/96 (PNG из вектора, scripts/gen-icons.py),
     в manifest icons + action.default_icon — тулбар не дефолтный пазл,
     цветной тайл читаем в обеих темах Firefox.
  3. «⋯» в шапке (правый край), портал раскрывается вниз от --hd-h;
     нижняя панель = композер + ОДНА кнопка.
  4. Кнопка морфится: покой = самолётик, running = квадрат на красном
     (fade+scale 150 мс — потолок hygiene SK-13, нижняя граница спеки
     150–200 покрыта); Esc = тот же стоп; без running-задачи кнопка — отправка.
  5. Плейсхолдер ротируется с fade; пауза на фокусе/вводе;
     prefers-reduced-motion → статичная первая фраза.

Живое (скрины обеих тем, DOM-замеры, морф на реальной задаче, кадры ротации,
тулбар-иконка) — харнесс sk38_acc.py на стенде firefox-158, не здесь.
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
HTML = (EXT / "sidebar.html").read_text(encoding="utf-8")
SB = (EXT / "sidebar.js").read_text(encoding="utf-8")
CSS = (EXT / "sidebar.css").read_text(encoding="utf-8")
THEME = (EXT / "theme.css").read_text(encoding="utf-8")
MAN = json.loads((EXT / "manifest.json").read_text())


def _no_comments(s):
    return re.sub(r"<!--.*?-->", "", s, flags=re.S)


def _hd_html():
    return _no_comments(HTML).split('<div id="hd">')[1].split("</div>\n")[0] \
        if '<div id="hd">' in _no_comments(HTML) else ""


# ---- п.1: шапка — иконка + заголовок, никакого статуса и «Hermes» ---------------------

def test_header_holds_logo_and_title_only():
    hd = HTML.split('<div id="hd">')[1].split("\n</div>")[0]
    assert 'id="hdic"' in hd and '#ic-bolt' in hd, "в шапке обязана быть иконка-молния (inline-SVG)"
    assert 'id="ttl"' in hd and "Новый чат" in hd, "дефолт заголовка — «Новый чат»"
    assert 'id="menuBtn"' in hd, "«⋯» обязана жить в шапке (п.3)"
    assert ">Hermes<" not in hd, "текстовая надпись «Hermes» из шапки убрана"
    for gone in ('id="stop"', 'id="chips"', 'id="achip"', 'id="chip"'):
        assert gone not in hd, f"{gone} больше не должен жить в шапке (статус — внизу, в ленте)"


def test_no_status_chip_rendering_left_in_sidebar_js():
    # чипы умерли вместе с AG_RU/STATE_RU; шапка не рисует статус вообще
    for dead in ("renderAG", "AG_RU", "STATE_RU", 'getElementById("achip")',
                 'getElementById("chip")', 'getElementById("stop")'):
        assert dead not in SB, f"мёртвый код шапочного статуса: {dead}"
    # но агентский кадр не теряется — AST живёт для bell (SK-37)
    assert 'AST = m.state || "unknown"' in SB


def test_chat_title_from_first_query_with_truncation():
    assert "function setChatTitle" in SB
    assert '"Новый чат"' in SB, "пустой чат — «Новый чат»"
    assert re.search(r'chatTitle\.length > \d+ .*slice\(0, \d+\).*"…"', SB), \
        "первый запрос режется с «…»"
    # источник — первый пользовательский запрос: и submit, и загрузка ленты чата
    assert "if (!chatTitle) setChatTitle(t);" in SB
    assert 'firstUser' in SB and 'x.role === "user"' in SB
    # смена чата сбрасывает заголовок (newBtn идёт через switchChat)
    assert 'setChatTitle("");' in SB


# ---- п.2: иконки расширения ------------------------------------------------------------

def test_manifest_icons_complete_and_files_exist():
    want = {"16", "32", "48", "96"}
    assert want <= set(MAN["icons"]), "manifest icons: нужны 16/32/48/96"
    assert want <= set(MAN["action"]["default_icon"]), "action.default_icon — тот же комплект"
    for size in sorted(want, key=int):
        rel = MAN["icons"][size]
        assert MAN["action"]["default_icon"][size] == rel
        p = EXT / rel
        assert p.exists(), f"иконка {rel} не найдена"
        blob = p.read_bytes()
        assert blob[:8] == b"\x89PNG\r\n\x1a\n", f"{rel} — не PNG"
        w, h = int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")
        assert (w, h) == (int(size), int(size)), f"{rel}: размер {w}x{h} не {size}"


def test_icons_are_reproducible_from_script():
    # генератор в репо, дизайн-сетка и палитра --accent зафиксированы в нём
    gen = (ROOT / "scripts" / "gen-icons.py").read_text(encoding="utf-8")
    assert "SIZES = (16, 32, 48, 96)" in gen
    assert "LANCZOS" in gen, "суперсэмплинг+даунскейл — источник гладких краёв"
    assert "BOLT24" in gen, "силуэт молнии = спрайтовый ic-skills/ic-bolt"
    assert 'id="ic-bolt"' in HTML, "шапочный логотип = тот же силуэт"


# ---- п.3: меню сверху, бар = композер + 1 кнопка ---------------------------------------

def test_menu_opens_below_header():
    assert "--hd-h:" in THEME, "постоянная высота шапки — токен (позиция меню от неё)"
    m = re.search(r"#menu \{[^}]*top: calc\(var\(--hd-h\)[^)]*\)", CSS)
    assert m, "меню раскрывается вниз от шапки (top от --hd-h), не от композера"
    assert "bottom: 57px" not in CSS


def test_composer_bar_is_input_plus_one_button():
    bar = HTML.split('id="bar"')[1].split('<div id="menu"')[0]
    assert bar.count("<button") == 1 and 'id="go"' in bar
    assert 'id="inwrap"' in bar and 'id="phin"' in bar, "оверлей плейсхолдера живёт в обёртке поля"


# ---- п.4: морф send↔stop ----------------------------------------------------------------

def test_morph_button_markup_and_css():
    go = HTML.split('id="go"')[1].split("</button>")[0]
    assert '#ic-send' in go and '#ic-stop' in go, "в кнопке оба слоя: самолётик и квадрат"
    assert ".ic-send" in CSS and ".ic-stop" in CSS
    assert re.search(r"#go\.running \{[^}]*var\(--btn-stop\)", CSS), "running — красный стоп-акцент"
    # морф 150–200 мс по спеке; hygiene SK-13 (transition <= 150) задаёт потолок
    for ms in re.findall(r"#go[^{]*\{[^}]*?transition:[^;]*?(\d+)ms", CSS):
        assert 150 <= int(ms) <= 200, f"морф {ms}ms вне спеки 150–200 (и вне hygiene <=150)"
    assert re.search(r"#go\.running \.ic-stop \{[^}]*opacity: 1", CSS)
    assert re.search(r"#go\.running \.ic-send \{[^}]*opacity: 0", CSS)


def test_morph_state_and_stop_paths_in_js():
    assert "function setGoRun" in SB and 'go.classList.toggle("running", on)' in SB
    assert "setGoRun(anyRun);" in SB, "морф кормится тем же anyRun, что и пункт меню"
    # клик по running-кнопке = stop-кадр; покой = submit
    assert re.search(r"go\.onclick = \(\) => \{\s*if \(GO_RUN\).*type: \"stop\"", SB, re.S)
    # Esc = тот же стоп (после закрытия «⋯»), и не канibalизирует скилл-меню/поле имени
    esc = SB.split('document.addEventListener("keydown"')[1].split("menu.addEventListener")[0]
    assert "if (GO_RUN)" in esc and 'type: "stop"' in esc
    assert SB.count("e.stopPropagation()") >= 2, "Esc в skmenu/skname гасится до document-стопa"
    # a11y: подпись кнопки меняется вместе с состоянием
    assert 'aria-label' in SB and "Стоп задача" in SB


# ---- п.5: живой плейсхолдер --------------------------------------------------------------

def test_placeholder_rotation():
    for phrase in ("спроси что угодно", "могу закрыть вкладки", "найду и очищу корзину",
                   "открою YouTube"):
        assert f'"{phrase}"' in SB, f"фраза «{phrase}» из спеки обязана ротироваться"
    assert "PH_RU" in SB and "setInterval(phTick" in SB
    assert re.search(r'fade|transition: opacity 150ms', CSS), "fade — CSS-переход 150 мс"
    # пауза на фокусе и на вводе
    assert "document.activeElement === inp" in SB
    assert 'inp.addEventListener("input", phSync)' in SB
    # prefers-reduced-motion → статичная первая фраза, оверлей выключен
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in SB
    rm = SB.split("function phStart")[1].split("inp.addEventListener")[0]
    assert "RMOTION.matches" in rm and "clearInterval(PHT)" in rm and "PH_RU[0]" in rm


# ---- hygiene: редизайн не ломает старые контракты ---------------------------------------

def test_no_inline_styles_and_hex_in_components():
    assert ".style." not in SB
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", CSS)
    for ms in re.findall(r"transition:[^;]*?(\d+)\s*ms", CSS):
        assert int(ms) <= 150
