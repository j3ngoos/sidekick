# -*- coding: utf-8 -*-
"""SK-35 + SK-51 — долгая задача сворачивает UI, по завершении зовёт человека.

ТЗ (карточка kanban t_113c61e8, запрос пользователя 19.10; SK-51 t_56b1dc87,
пользователь 25.10):
1. SK-51: «долгая» — ТОЛЬКО по явному кадру агента long:true (running, аддитивно
   к v1-контракту): сворачивает сайдбар и группу вкладок (⏳ в заголовке).
   Эвристика LONG_MS=10000 УБРАНА: интерактивный диалог без long:true не
   сворачивается НИКОГДА, сколько бы ни шёл; bell такой задаче не звонит.
2. Терминальный кадр долгой задачи: bell (уведомление/бейдж — решает bellRefresh
   из SK-37) + запись в журнал шапки + группа в ✓ (если других долгих нет).
3. Разворачивание группы — только на перерыве на пермисс (perm_ask).
4. Сайдбар закрывает себя сам: window.close() из moz-extension-документа панели
   (пробда на сборке firefox-158 23.10: isOpen true→false, окно живо);
   sidebarAction.close() без пользовательского жеста схема Firefox отклоняет —
   пин ниже сторожит, чтобы никто не вернулся к заведомо мёртвому пути.
5. Колокольчик в шапке — выпадающий журнал событий (task_done/task_failed/perm),
   счётчик непрочитанного; прочтение — клик/«Прочитать всё» (SK-50).
6. Ручное сворачивание — кнопка в шапке (тоже window.close()).
7. AGENTS.md: агенту нельзя принудительно открывать сайдбар.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BG_SRC = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
SIDEBAR_SRC = (ROOT / "extension" / "sidebar.js").read_text(encoding="utf-8")
SIDEBAR_HTML = (ROOT / "extension" / "sidebar.html").read_text(encoding="utf-8")
BELL_SRC = (ROOT / "extension" / "bell.js").read_text(encoding="utf-8")
AGENTS_MD = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ node-юниты
def _run_node(script, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    f = tmp_path / "unit.mjs"
    f.write_text(script, encoding="utf-8")
    r = subprocess.run([node, str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    return r.stdout


def test_bell_log_core_node_unit(tmp_path):
    """Чистое ядро SK-35 (префиксы группы + журнал): чистые функции под node."""
    m = re.search(r"// SK-35-BEGIN bell-log.*?\n(.*?)// SK-35-END bell-log",
                  BELL_SRC, re.S)
    assert m, "ядро bell-log с маркерами SK-35-BEGIN/END не найдено"
    out = _run_node(m.group(1) + """
const eq = (a, b, w) => { if (a !== b) { console.error(w, JSON.stringify(a), JSON.stringify(b)); process.exit(1); } };
const eqj = (a, b, w) => { const x = JSON.stringify(a), y = JSON.stringify(b); if (x !== y) { console.error(w, x, y); process.exit(1); } };

// префиксы группы: повторный префикс исключён (base всегда чистый)
eq(bellStripPrefix("⏳ Sidekick"), "Sidekick", "снять ⏳");
eq(bellStripPrefix("✓ Sidekick"), "Sidekick", "снять ✓");
eq(bellStripPrefix("Sidekick"), "Sidekick", "без префикса — как было");
eq(bellStripPrefix(null), "", "null -> пусто");
eq(bellGroupTitle("⏳ Sidekick", "done"), "✓ Sidekick", "⏳ -> ✓ без двойного префикса");
eq(bellGroupTitle("Sidekick", "long"), "⏳ Sidekick", "long -> ⏳");
eq(bellGroupTitle("✓ Sidekick", ""), "Sidekick", "пустой режим -> чистый");
eq(bellGroupTitle("Sidekick", "long"), "⏳ Sidekick", "идемпотентность long");
eq(bellGroupTitle(bellGroupTitle("Sidekick", "long"), "long"), "⏳ Sidekick", "повторный long не плодит ⏳⏳");

// журнал: cap, непрочитанные, прочтение
const ev = (id, kind, read) => ({ id, kind, read: !!read, ts: 1 });
eqj(bellLogAdd([], ev("a", "task_done"), 3), [{ id: "a", kind: "task_done", read: false, ts: 1 }], "добавление в голову");
let log = [];
for (let i = 0; i < 5; i++) log = bellLogAdd(log, ev("t" + i, "task_done", false), 3);
eq(log.length, 3, "cap держит хвост");
eq(log[0].id, "t4", "свежее — в голове");
eq(bellUnread(log), 3, "все непрочитаны");
eq(bellUnread(bellMarkAllRead(log)), 0, "markAllRead обнуляет счётчик");
eq(bellUnread(log), 3, "чистая: исходный массив не тронут (React-стиль)");
eqj(bellMarkIdRead(log, "t3").filter(e => e.id === "t3").map(e => e.read), [true], "точечное прочтение");
eq(bellMarkIdRead(log, "нет-такого").length, log.length, "miss — массив той же длины без падения");
eqj(bellAckIds([{id:"p",kind:"perm"},{id:"d",kind:"task_done"},{id:"f",kind:"task_failed"},{id:"x",kind:"?"}]), ["d","f"], "ack — только звонки задач");
eq(bellLogAdd(null, ev("z", "perm"), 0).length, 1, "null-журнал живёт, cap<=0 -> дефолт");
console.log("BELLLOG-OK");
""", tmp_path)
    assert "BELLLOG-OK" in out


# ----------------------------------------------------------------- пины фона
def test_background_long_pins():
    # SK-51: эвристики времени больше нет — интерактивный диалог не сворачивается НИКОГДА.
    assert "const LONG_MS" not in BG_SRC, "порог LONG_MS вернулся — SK-51 запрещает эвристику"
    assert "armLong" not in BG_SRC and "clearLongTimer" not in BG_SRC, \
        "таймерный механизм долгой задачи вернулся (SK-51: триггер — только явный long:true)"
    assert "const LONGS = new Set()" in BG_SRC
    # триггер — ЯВНЫЙ кадр агента long:true поверх running, идемпотентный повтор
    assert re.search(r'if \(msg\.state === "running"\) \{[\s\S]*?if \(msg\.long === true'
                     r' && !LONGS\.has\(id\)\) markLong\(id\)\.catch', BG_SRC), \
        "tlTask(running) обязан заводить долгую задачу ЯВНЫМ long:true и только им"
    assert re.search(r'if \(LONGS\.has\(id\)\) bellClear\(id\)', BG_SRC), \
        "новая running-фаза той же id снимает старый звонок «сделана»"
    # сайдбар сворачивается кадром в порт (window.close на стороне сайдбара)
    assert 'sendToUi({ type: "sidebar_collapse" })' in BG_SRC, \
        "долгая задача шлёт сайдбару sidebar_collapse"
    # guard markLong: long:true без running — не триггер
    assert re.search(r'async function markLong\(id\)[\s\S]*?if \(!e \|\| e\.state !== "running"\) return;',
                     BG_SRC), "markLong обязан принимать только running-задачу"
    # терминальный кадр долгой: bell + журнал + ✓ до парковки SK-07
    assert re.search(r'if \(LONGS\.has\(id\)\) finishLong\(id, msg\.state\)', BG_SRC), \
        "terminal-кадр долгой задачи обязан звать finishLong"
    assert 'kind: "task_" + state' in BG_SRC and "bellLogPush" in BG_SRC, \
        "finishLong: bell-звонок задачи и запись журнала"
    # пермисс: журнал + ЕДИНСТВЕННОЕ разворачивание группы
    assert "expandGroupForPerm().catch" in BG_SRC
    assert re.search(r'if \(!LONGS\.size\) return; // не долгая', BG_SRC), \
        "без долгой задачи пермисс группу не трогает (она и так на виду)"
    # ensureGroup: во время долгой — не разворачивать и не переименовывать
    assert "const longNow = LONGS.size > 0;" in BG_SRC
    assert "if (!longNow) upd.collapsed = false;" in BG_SRC, \
        "посреди долгой задачи ensureGroup не имеет права разворачивать группу"
    # журнал: replay позднему сайдбару + команды прочтения + загрузка на старте
    assert 'port.postMessage({ type: "bell_log", events: BELL_LOG })' in BG_SRC
    assert 'm.type === "bell_read"' in BG_SRC and "bellLogReadAll()" in BG_SRC
    assert 'm.type === "bell_read_id"' in BG_SRC and "bellLogReadId(" in BG_SRC
    assert "bellLogLoad()" in BG_SRC, "журнал обязан переживать моргание event-page"


def test_bell_log_persistence_and_kinds():
    assert 'const BELL_LOG_KEY = "sk35BellLog";' in BELL_SRC
    assert 'const BELL_LOG_KINDS = ["task_done", "task_failed", "perm"];' in BELL_SRC, \
        "виды событий журнала фиксированы контрактом"
    assert "bellLogPersist()" in BELL_SRC
    # вердикт/закрытие звонка = запись журнала прочитана (колокольчик не врет)
    assert re.search(r"function bellClear\(id\)[\s\S]*?bellLogReadId\(String\(id\)\)", BELL_SRC), \
        "bellClear обязан помечать журнал прочитанным"


def test_sidebar_close_path_pins():
    # sidebarAction.close() без жеста схема Firefox отклоняет (пробда 23.10:
    # «may only be called from a user input handler») — панель закрывает себя сама.
    assert "sidebar_collapse" in SIDEBAR_SRC and "window.close()" in SIDEBAR_SRC, \
        "сайдбар обязан закрываться window.close() по кадру sidebar_collapse"
    assert re.search(r'else if \(m\.type === "sidebar_collapse"\) \{ window\.close\(\); \}', SIDEBAR_SRC), \
        "кадр sidebar_collapse закрывает панель безусловно"
    assert "collapseBtn.onclick = () => window.close();" in SIDEBAR_SRC, \
        "ручное сворачивание — тот же проверенный путь"
    assert 'browser.sidebarAction.close()' not in SIDEBAR_SRC, \
        "в сайдбар-документе sidebarAction.close() не нужен (жест не гарантирован харнессу)"


def test_sidebar_bell_ui_pins():
    assert 'document.getElementById("bellBtn")' in SIDEBAR_SRC
    assert 'document.getElementById("bellpanel")' in SIDEBAR_SRC
    assert 'm.type === "bell_log"' in SIDEBAR_SRC, "кадр журнала обязан приземляться в UI"
    # SK-50 (v0.7): открытие журнала НЕ читает — bell_read шлёт явная кнопка
    # «Прочитать всё»; прочтение ряда — клик (bell_read_id).
    assert 'port.postMessage({ type: "bell_read" })' in SIDEBAR_SRC, \
        "кнопка «Прочитать всё» шлёт bell_read"
    open_block = re.search(r"function bellPanelOpen\(on\)[\s\S]*?\n}", SIDEBAR_SRC)
    assert open_block and "bell_read" not in open_block.group(0), \
        "SK-50: открытие журнала не имеет права слать bell_read"
    assert 'port.postMessage({ type: "bell_read_id"' in SIDEBAR_SRC
    assert "function flashTaskCard" in SIDEBAR_SRC, "клик по событию подсвечивает карточку задачи"
    assert re.search(r'function bellFocus\(id, kind, chat\)[^{]*\{[^}]*startsWith\("task_"\)', SIDEBAR_SRC, re.S), \
        "bell_focus звонка задачи ведёт в чат задачи"
    assert "switchChat(chat)" in SIDEBAR_SRC


def test_sidebar_html_bell_markup():
    assert 'id="bellBtn"' in SIDEBAR_HTML and 'id="collapseBtn"' in SIDEBAR_HTML
    assert 'id="bellpanel"' in SIDEBAR_HTML and 'id="bellrows"' in SIDEBAR_HTML
    assert 'id="belln"' in SIDEBAR_HTML, "счётчик непрочитанного"
    assert 'id="bellReadAll"' in SIDEBAR_HTML, "SK-50: явная кнопка «Прочитать всё» в подвале журнала"
    assert "Mark all read" in SIDEBAR_HTML and 'data-i18n="sbt.read_all"' in SIDEBAR_HTML  # SK-71
    assert 'href="#ic-bell"' in SIDEBAR_HTML and 'id="ic-bell"' in SIDEBAR_HTML
    assert 'id="ic-collapse"' in SIDEBAR_HTML


def test_agents_md_long_task_rule():
    assert "SK-35" in AGENTS_MD
    assert "НЕЛЬЗЯ открывать/разворачивать сайдбар принудительно" in AGENTS_MD, \
        "доктрина: долгую работу человек не гипнотизирует"


def test_bridge_contract_documents_long():
    """SK-51: кадр task аддитивно несёт long:true (явный «ухожу в долгую»);
    мост — прозрачный ретранслятор, контракт это документирует."""
    bridge_src = (ROOT / "bridge" / "sidekick_bridge.py").read_text(encoding="utf-8")
    assert '"long"?:true' in bridge_src, "v1-контракт моста не документирует long:true"
    assert "SK-51" in bridge_src, "мост не знает про SK-51 (прозрачность поля)"
