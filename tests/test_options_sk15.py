"""SK-15 contract tests — options-страница токена: карточный вид, маскирование,
проверка подключения.

Живой UI (обе темы, реальный «проверить подключение» с остановом/подъёмом
systemd-юнита моста, персистентность через BiDi-перезагрузку расширения) — это
скрипт sk15_r1_panel.py на стенде firefox-158, не pytest. Здесь — статические
контракты, которые дороже скриншотов: значение токена НИКОГДА не рендерится на
странице, проверка идёт runtime-сообщением в background (НОВЫХ rpc-методов моста
не появилось), стили — только на токенах SK-13.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"

HTML = (EXT / "options.html").read_text()
CSS = (EXT / "options.css").read_text()
JS = (EXT / "options.js").read_text()
BG = (EXT / "background.js").read_text()


# ---- карточный layout -------------------------------------------------------

def test_options_links_token_stylesheets():
    assert 'href="theme.css"' in HTML and 'href="options.css"' in HTML


def test_options_is_card_layout_with_two_sections():
    # SK-28: третья карточка — «Сервер» (LAN-коннект без ssh)
    # SK-50: четвёртая — «Звонок» (живая проверка notifications.create + чек-лист)
    # SK-71: пятая — «Язык» (переключатель языка интерфейса)
    assert HTML.count('class="card"') == 5
    assert ">Token<" in HTML and ">Agent tabs<" in HTML and ">Server<" in HTML  # SK-71: EN-разметка, RU из словаря
    assert ">Bell<" in HTML and 'data-i18n="opt.h_lang"' in HTML
    assert "<style" not in HTML  # SK-13: весь стиль в css, ни inline-блоков


def test_options_css_zero_hex_and_tokens_only():
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", CSS), "hex-цвета в компонентах запрещены"
    off = re.findall(r"(?:padding|gap|margin)(?:-[a-z]+)?\s*:[^;]*?(?<![\d.])((?:5|6|7|9|10)px)", CSS)
    assert not off, f"off-scale отступы: {off}"
    rad = re.findall(r"border-radius\s*:\s*(?!\s*var\(|\s*0|\s*50%)[^;}]+", CSS)
    assert not rad, f"радиусы мимо токенов: {rad}"


def test_options_card_and_chip_use_scale():
    card = re.search(r"\.card\s*\{[^}]+\}", CSS).group(0)
    assert "var(--r2)" in card and "var(--surface-2)" in card and "var(--sp3)" in card
    chip = re.search(r"\.chip\s*\{[^}]+\}", CSS).group(0)
    assert "var(--rp)" in chip


# ---- состояние токена: значение не показывается ------------------------------

def test_token_value_never_rendered():
    # поле только для нового значения: секрет не предзаполняется
    assert 'inp.value = "";' in JS
    assert not re.search(r"\.value\s*=\s*(k\.bridgeToken|bridgeToken)\b", JS)
    # значение ни в какой DOM-текст не попадает (bridgeToken — только условие/хранилище)
    for m in re.finditer(r"bridgeToken(?!At)[^;]*", JS):
        frag = m.group(0)
        assert not re.search(r"textContent|innerText|innerHTML|insertAdjacent|\+\s*bridgeToken|bridgeToken\s*\+", frag), frag


def test_token_state_has_time_stamp():
    load_fn = JS.split("async function load")[1].split("async function save")[0]
    assert "bridgeTokenAt" in load_fn          # состояние читает отметку времени
    assert "bridgeTokenAt: Date.now()" in JS   # save пишет её
    assert 'type="password"' in HTML


def test_saved_state_chip_and_meta():
    assert 'L("opt.saved")' in JS and 'L("opt.not_saved")' in JS  # SK-71: тексты — из словаря
    assert 'chip.className = "chip ok"' in JS and 'chip.className = "chip warn"' in JS
    assert 'id="tokMeta"' in HTML and "tokStateTxt" in HTML


# ---- «проверить подключение»: runtime-сообщение, не rpc моста ----------------

def test_check_goes_through_runtime_message():
    assert 'sendMessage({ type: "ws-check" })' in JS
    # состояние на странице: проверка / подключено / не подключено (причина)
    assert 'L("opt.checking")' in JS and 'L("opt.connected")' in JS and 'L("opt.not_connected")' in JS  # SK-71


def test_background_answers_ws_check_without_new_bridge_methods():
    assert 'm.type === "ws-check"' in BG
    assert "return true" in BG.split('m.type === "ws-check"')[1][:200]  # async sendResponse
    ws = BG.split("async function wsCheck")[1].split("function send(")[0]
    assert "sendResponse" in ws and "BRIDGE_ST" in ws
    # никаких новых исходящих rpc-методов моста: wsCheck только состояние сокета
    assert "rpc" not in ws and "send({ type" not in ws
    # адрес без токена: URL режется от query/hash, сам токен в ответ не едет
    # (SK-28: адрес живёт в wsUrl — пин следит за тем же инвариантом, что и раньше)
    assert "wsUrl || FALLBACK_SERVER).split" in BG and "token" not in ws.split("sendResponse({ state: \"connected\"")[1][:80]


def test_token_updated_flow_preserved():
    # save/clear по-прежнему дёргают переподключение background
    assert JS.count('type: "token-updated"') == 2
    assert 'm.type === "token-updated"' in BG


# ---- секция «вкладки агента» (SK-07 select причёсан, поведение то же) --------

def test_after_task_section_intact():
    assert 'id="after"' in HTML and "sk07AfterTask" in JS
    assert 'appearance: none' in CSS  # нормальные стили select
    assert 'class="caret"' in HTML    # стрелка текстом, не hex-data-URI
