# -*- coding: utf-8 -*-
"""SK-50 — уведомления: всплывающий «островок» и явное прочтение журнала.

ТЗ (карточка kanban t_014ef5d8, v0.7 пакет пользователя 25.10, скриншоты с Mac):
1. notifications.create — валидный iconUrl (РЕАЛЬНЫЙ PNG из icons/ через
   runtime.getURL; data: схема отклоняется, без иконки Firefox молча не
   показывает тост). create() НЕ падает тихо: причина уходит в events.jsonl
   ногой bell_event (notify / notify_fail) — catch не глотает.
2. Правило показа: всплывашка task_* — только когда окно Firefox НЕ в фокусе
   (windows.getAll + focused); при фокусе — бейдж-колокольчик + журнал.
   perm-звонок зовёт всегда (SK-37 семантика). onFocusChanged пересобирает.
3. options «Проверить звонок»: живой create + getPermissionLevel + чек-лист
   подсказок (Firefox-разрешение, macOS «Позволить уведомления»/«Звуки» —
   звук даёт ОС, у WebExtensions API звука нет).
4. Журнал: открытие НЕ помечает всё прочитанным; клик по событию — точечное
   прочтение; «Прочитать всё» — явная кнопка (bell_read); пермис-звонки
   «прочитать всё» не снимает (bellAckIds трогает только task_*).
5. Таймеров автозакрытия журнала нет (живое утверждение «10 с» — сворачивание
   ПАНЕЛИ эвристикой LONG_MS; SK-51 её убрал — теперь только явный long:true).

Контракт моста аддитивен: кадр bell_event → events.jsonl, consumed (never inbox).
"""
import asyncio  # noqa: E402
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from conftest import kill_bridge, start_bridge
from test_perm import MockExt, read_jsonl, run  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
BG_SRC = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
BELL_SRC = (ROOT / "extension" / "bell.js").read_text(encoding="utf-8")
SIDEBAR_SRC = (ROOT / "extension/sidebar.js").read_text(encoding="utf-8")
SIDEBAR_HTML = (ROOT / "extension/sidebar.html").read_text(encoding="utf-8")
SIDEBAR_CSS = (ROOT / "extension/sidebar.css").read_text(encoding="utf-8")
OPTIONS_HTML = (ROOT / "extension/options.html").read_text(encoding="utf-8")
OPTIONS_JS = (ROOT / "extension/options.js").read_text(encoding="utf-8")
BRIDGE_SRC = (ROOT / "bridge" / "sidekick_bridge.py").read_text(encoding="utf-8")


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


# ---------------------------------------------------------------- п.1: иконка
def test_icon_is_real_png_and_geturl():
    """Без валидной иконки Firefox молча не показывает тост (диагностика 25.10):
    iconUrl — реальный PNG из icons/ относительным путём от manifest."""
    raw = (ROOT / "extension" / "icons" / "icon-96.png").read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "icon-96.png — не PNG"
    assert 'browser.runtime.getURL("icons/icon-96.png")' in BELL_SRC
    code = re.sub(r"//[^\n]*", "", BELL_SRC)  # комментарии разрешены, код — нет
    urls = re.findall(r"iconUrl:\s*([^,\n]+)", code)
    assert urls and all("runtime.getURL" in u for u in urls), \
        "iconUrl обязан быть ОДНИМ путём — runtime.getURL(PNG из icons/)"
    # data: схема валидатором отклоняется — её быть не должно
    assert "data:image" not in code
    # регресс SK-35: Firefox 158 синхронно отклоняет requireInteraction — не возвращать
    assert "requireInteraction" not in code, "requireInteraction вернулся в КОД (схема 158 его отклоняет)"


def test_create_failure_reason_not_swallowed():
    """catch не глотает причину: bellEventLog notify_fail с текстом ошибки."""
    assert "bellEventLog(\"notify\", id, e)" in BELL_SRC
    assert 'bellEventLog("notify_fail"' in BELL_SRC
    assert "String((err && err.message) || err)" in BELL_SRC
    # сам звонок от отказа не роняется: notified сбрасывается (повтор на refresh)
    assert re.search(r"\.catch\(\(er\) => \{ e\.notified = false;", BELL_SRC)


# ------------------------------------------------- п.2: правило фокуса + legs
def test_focus_rule_replaces_sidebar_visibility():
    assert "function bellWindowFocused" in BELL_SRC
    assert 'browser.windows.getAll({ windowTypes: ["normal"] })' in BELL_SRC
    assert "w.focused" in BELL_SRC
    assert "bellShouldNotify(e.kind, focused" in BELL_SRC
    # старое правило «сайдбар закрыт → звонок» заменено (windows.get focused)
    assert "bellSidebarVisible" not in BELL_SRC and "sidebarAction.isOpen" not in BELL_SRC
    # вернулись в окно — тост задачи снят, пермис live
    assert re.search(r'e\.kind !== "perm" && e\.notified', BELL_SRC)
    # смена фокуса пересобирает звонок (слушатель в background.js дёргает bellRefresh)
    assert "windows.onFocusChanged" in BG_SRC and \
        re.search(r"onFocusChanged\.addListener\(\(\) => \{ bellRefresh\(\)", BG_SRC)


def test_bell_event_leg_contract():
    # расширение: кадр bell_event через WS (send добавляет v:1)
    assert 'send({ type: "bell_event"' in BELL_SRC
    assert '"bell_event"' in BELL_SRC
    # «проверить звонок»: свой ev, чтобы не мешался с боевыми звонками
    assert 'ev: "notify_test"' in BG_SRC and 'ev: "notify_test_fail"' in BG_SRC
    # мост: consumed (events.jsonl, never inbox) — аддитивная ветка
    branch = BRIDGE_SRC.split('msg.get("type") == "bell_event"')[1].split("continue")[0]
    assert "event_log(raw)" in branch, "bell_event обязан писаться в events.jsonl"


def test_bridge_bell_event_e2e(home):
    """Живой мост: bell_event кадр приземляется в events.jsonl с ts и НЕ
    попадает в агентский outbox (consumed)."""
    async def _t():
        async with MockExt(home) as ext:
            await ext.c.send(json.dumps({"v": 1, "type": "bell_event", "ev": "notify",
                                         "id": "sk50-x", "kind": "task_done", "title": "Задача сделана"}))
            rows = []
            t0 = __import__("time").time()
            while __import__("time").time() - t0 < 4:
                rows = [r for r in read_jsonl(home / "events.jsonl")
                        if isinstance(r, dict) and r.get("type") == "bell_event"]
                if rows:
                    break
                await asyncio.sleep(0.1)
            assert rows, "bell_event не попал в events.jsonl"
            r = rows[-1]
            assert r.get("ev") == "notify" and r.get("id") == "sk50-x" and "ts" in r, r
            # consumed (never inbox): агентский outbox не получает кадр
            outbox = home / "outbox.jsonl"
            if outbox.exists():
                assert "bell_event" not in outbox.read_text(encoding="utf-8"), \
                    "bell_event просочился агенту в outbox (контракт: consumed)"
            # мост жив: обычный stop работает
            await ext.c.send(json.dumps({"v": 1, "type": "stop"}))
    run(_t())


# ------------------------------------------------------ п.3: options-кнопка
def test_options_bell_test_card():
    assert 'id="bellTest"' in OPTIONS_HTML and "Проверить звонок" in OPTIONS_HTML
    assert 'id="bellSt"' in OPTIONS_HTML and 'id="bellHint"' in OPTIONS_HTML
    h = " ".join(OPTIONS_HTML.split())  # нормализация переносов чек-листа
    assert "about:preferences#privacy" in h, "нет подсказки про разрешение Firefox"
    assert "Позволить уведомления" in h and "Звуки" in h, "нет macOS-чек-листа"
    assert "звук даёт" in h.lower() or "звук даёт" in h, "не сказано, что звук даёт ОС"
    assert 'type: "bell-test"' in OPTIONS_JS
    assert "getPermissionLevel" in BG_SRC, "проверка обязана честно докладывать уровень разрешения"
    assert "bellHint" in OPTIONS_JS, "чек-лист обязан разворачиваться, если человек ничего не увидел"


# ------------------------------------------------------- п.4/5: журнал
def test_journal_explicit_read_pins():
    assert 'id="bellReadAll"' in SIDEBAR_HTML
    assert re.search(r"bellReadAll\.onclick = \(\) =>", SIDEBAR_SRC)
    assert 'port.postMessage({ type: "bell_read" })' in SIDEBAR_SRC
    open_block = re.search(r"function bellPanelOpen\(on\)[\s\S]*?\n}", SIDEBAR_SRC)
    assert open_block and "postMessage" not in open_block.group(0), \
        "открытие журнала не имеет права слать прочтение (SK-50)"
    assert 'port.postMessage({ type: "bell_read_id"' in SIDEBAR_SRC, \
        "клик по событию — точечное прочтение"
    # «прочитать всё» снимает звонки задач, но НЕ пермисы: bellAckIds — только task_*
    ack = re.search(r"function bellAckIds\(log\)[\s\S]*?\n}", BELL_SRC)
    assert ack and 'kind === "task_done"' in ack.group(0) and 'kind === "task_failed"' in ack.group(0)
    ra = re.search(r"function bellLogReadAll\(\)[\s\S]*?\n}", BELL_SRC)
    assert ra and "bellAckIds" in ra.group(0), "read-all обязан снимать звонки по task-списку, а не все"
    # подвал журнала стилизован и живёт в CSS
    assert "#bellfoot" in SIDEBAR_CSS and "#bellReadAll" in SIDEBAR_CSS


def test_journal_has_no_autoclose_timer():
    """Живое утверждение пользователя «журнал закрывается через 10 с»: таймеров
    автозакрытия в коде журнала нет (закрытие — dropdown-паттерн: клик-вне/Esc).
    Источник эффекта 10 с был в сворачивании ПАНЕЛИ эвристикой LONG_MS (SK-51 её
    убрал). Пин-гаранизм:
    таймер, закрывающий bellpanel, сюда не вернётся."""
    assert not re.search(r"set(Timeout|Interval)\([^;]*bellPanelOpen", SIDEBAR_SRC), \
        "таймер автозакрытия журнала вернулся — SK-50 п.5"
    # клик-вне и Esc остаются единственными путями закрыть дропдаун
    assert "bellPanelOpen(false)" in SIDEBAR_SRC


def test_journal_repush_dedup_source_pin():
    """SK-50: повтор того же события (мост заново шлёт висящий пермис) не плодит
    новую строку журнала. Стенд SK-50 (26.10): один пермис дал 9 одинаковых строк —
    bellLogPush без дедупа. Пин: guard + чистая bellLogHas."""
    assert "function bellLogHas" in BELL_SRC
    push = BELL_SRC[BELL_SRC.index("function bellLogPush"):]
    push = push[:push.index("\n}")]
    assert "if (bellLogHas(BELL_LOG, ev.id)) return;" in push, \
        "дедуп ре-пуша в bellLogPush потерян"


def test_journal_dedup_core_node_unit(tmp_path):
    """Чистая bellLogHas (блок SK-35 bell-log): id совпадает строково, miss — False."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    m = re.search(r"// SK-35-BEGIN bell-log.*?\n(.*?)// SK-35-END bell-log",
                  BELL_SRC, re.S)
    assert m, "ядро bell-log с маркерами SK-35-BEGIN/END не найдено"
    f = tmp_path / "unit.mjs"
    f.write_text(m.group(1) + """
const eq = (a, b, w) => { if (a !== b) { console.error(w, JSON.stringify(a), JSON.stringify(b)); process.exit(1); } };
const log = [{ id: 7, kind: "perm", read: true }, { id: "p2", kind: "task_done", read: false }];
eq(bellLogHas(log, 7), true, "числовой id совпадает строково");
eq(bellLogHas(log, "7"), true, "строковый id совпадает");
eq(bellLogHas(log, "p2"), true, "присутствует");
eq(bellLogHas(log, "нет"), false, "miss — False");
eq(bellLogHas([], "x"), false, "пустой журнал");
eq(bellLogHas(null, "x"), false, "null-журнал без падения");
console.log("DEDUP_OK");
""", encoding="utf-8")
    r = subprocess.run([node, str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    assert "DEDUP_OK" in r.stdout
