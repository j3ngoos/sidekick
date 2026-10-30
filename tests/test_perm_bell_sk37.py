# -*- coding: utf-8 -*-
"""SK-37 — пермиссы и стоп в окно чата; bell/badge при свёрнутом сайдбаре.

ТЗ (карточка kanban t_e32947a4, запрос пользователя 22.10):
1. На курсорном превью-оверлее кнопок нет — кольцо + подпись (пины удалённой
   кнопки — test_content_sk16.test_no_stop_button_on_overlay, test_ui_sk30).
2. perm.check принимает `name` (имя цели, ≤120); perm_ask аддитивно несёт
   name/tab/shot; миниатюра вкладки — отдельной асинхронной ногой
   page.screenshot {tab, max_width:320}, доносится кадром perm_shot; карточку
   не блокирует; ошибка/размер/таймаут — карточка без картинки.
3. Внутренняя лег миниатюры — не агенту: её rpc_result не попадает в rpc_out.
4. bell/badge — общий модуль extension/bell.js (чистое ядро в node-юните);
   сайдбар не виден → browser.notifications + бейдж-колокольчик; вердикт/
   resolve — звонок снят; клик по уведомлению → bell_focus в сайдбар.
5. Дрейф: ключи ACTION_RU (сайдбар) ⊆ BELL_ACTION_RU (копия в background).
"""
import json
import pathlib
import re
import shutil
import subprocess
import time

import pytest

from conftest import kill_bridge, start_bridge
from test_perm import MockExt, answered, read_jsonl  # noqa: F401
from test_rpc_e2e import read_rpc_out, rpc_call

ROOT = pathlib.Path(__file__).resolve().parents[1]
BG_SRC = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
SIDEBAR_SRC = (ROOT / "extension/sidebar.js").read_text(encoding="utf-8")
BELL_SRC = (ROOT / "extension/bell.js").read_text(encoding="utf-8")
BRIDGE_SRC = (ROOT / "bridge" / "sidekick_bridge.py").read_text(encoding="utf-8")
MANIFEST = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))

import asyncio  # noqa: E402  (после conftest-зависимых импортов — как в test_perm)
from test_perm import run  # noqa: E402


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


# --------------------------------------------------------------- mock-расширение
class ThumbExt(MockExt):
    """MockExt + авто-ответы на вкладочный лег и лег миниатюры (SK-37)."""

    def __init__(self, home, shot="data:image/jpeg;base64,AAAA", respond_shot=True):
        super().__init__(home)
        self.shot = shot
        self.respond_shot = respond_shot
        self.shotreqs = []

    async def _reply(self, rid, result):
        await self.c.send(json.dumps({"v": 1, "type": "rpc_result", "id": rid,
                                      "ok": True, "result": result}))

    async def _pump(self):
        async for raw in self.c:
            try:
                f = json.loads(raw)
            except ValueError:
                continue
            self.frames.append(f)
            if f.get("type") != "rpc":
                continue
            if f.get("method") == "tabs.open_grouped":
                await self._reply(f["id"], {"tabId": 42, "windowId": 7,
                                            "url": "https://ok.ru/"})
            elif f.get("method") == "page.screenshot":
                self.shotreqs.append(f)
                if self.respond_shot:
                    await self._reply(f["id"], {"dataUrl": self.shot})

    async def wait_pred(self, pred, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in self.frames:
                if pred(f):
                    return f
            await asyncio.sleep(0.05)
        raise AssertionError(f"no frame in {timeout}s "
                             f"({[f.get('type') or f.get('method') for f in self.frames]})")

    async def wait_shotreq(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.shotreqs:
                return self.shotreqs[0]
            await asyncio.sleep(0.05)
        raise AssertionError("no page.screenshot rpc leg for the thumbnail")

    async def sleep_for(self, secs):
        await asyncio.sleep(secs)


async def _bind_task(ext, home, task="sk37t"):
    rid = rpc_call(home, "tabs.open_grouped", {"task": task, "url": "https://ok.ru/"})
    # сначала дождаться доставки расширению (pump сам ответит), потом читать
    # rpc_out: синхронный poll блокирует ивент-луп, а pump живёт в этом же лупе
    await ext.wait_pred(lambda f: f.get("type") == "rpc"
                        and f.get("method") == "tabs.open_grouped")
    out = read_rpc_out(home, rid, timeout=3)
    assert out is not None and out.get("ok") is True, out


# --------------------------------------------------------------------- e2e моста

def test_perm_ask_carries_name_and_thumb(home):
    """perm_ask: name+tab аддитивны; миниатюра — отдельной ногой и perm_shot;
    лег не светит агенту в rpc_out; вердикт — обычный human_once."""
    async def _t():
        async with ThumbExt(home) as ext:
            await _bind_task(ext, home)
            rid = rpc_call(home, "perm.check", {
                "domain": "ok.ru", "action": "submit", "perm": "send_message",
                "task": "sk37t", "name": "Ответ в личке «Привет, как дела?»"})
            ask = await ext.wait_ask()
            assert ask["name"] == "Ответ в личке «Привет, как дела?»"
            assert ask.get("tab") == 42, "perm_ask обязан назвать вкладку задачи"
            assert "shot" not in ask  # карточка уходит ДО готовности кадра
            leg = await ext.wait_shotreq()
            assert leg["params"] == {"tab": 42, "max_width": 320}
            ps = await ext.wait_pred(lambda f: f.get("type") == "perm_shot"
                                     and f.get("id") == ask["id"])
            assert ps["shot"] == "data:image/jpeg;base64,AAAA"
            await ext.answer(ask["id"], "once")
            out = answered(home, rid)
            assert out["result"] == {"decision": "allow", "via": "human_once"}
            await ext.sleep_for(0.4)
            for ln in read_jsonl(home / "rpc_out.jsonl"):
                assert "~thumb" not in str(ln.get("id", "")), \
                    "внутренняя лег миниатюры протекла в rpc_out агента"
    run(_t())


def test_perm_ask_without_task_tab_no_thumb_leg(home):
    """Нет вкладки у задачи — нечего снимать: ни лега, ни tab/shot в кадре."""
    async def _t():
        async with ThumbExt(home) as ext:
            rid = rpc_call(home, "perm.check", {
                "domain": "ok.ru", "action": "submit", "perm": "send_message",
                "task": "sk37-без-вкладки", "name": "Что-то"})
            ask = await ext.wait_ask()
            assert ask["name"] == "Что-то"
            assert "tab" not in ask and "shot" not in ask
            await ext.sleep_for(0.8)
            assert not ext.shotreqs, "лег миниатюры без вкладки задачи запрещён"
            await ext.answer(ask["id"], "deny")
            assert answered(home, rid)["result"]["decision"] == "block"
    run(_t())


def test_thumb_after_answer_is_dropped(home):
    """Вердикт раньше кадра: perm_thumb_cancel снимает лег — perm_shot не придёт."""
    async def _t():
        async with ThumbExt(home, respond_shot=False) as ext:
            await _bind_task(ext, home)
            rid = rpc_call(home, "perm.check", {
                "domain": "ok.ru", "action": "submit", "perm": "send_message",
                "task": "sk37t", "name": "Спешка"})
            ask = await ext.wait_ask()
            await ext.answer(ask["id"], "once")
            assert answered(home, rid)["result"]["decision"] == "allow"
            leg = await ext.wait_shotreq()  # лег мог уйти до вердикта — ответим теперь
            await ext._reply(leg["id"], {"dataUrl": "data:image/jpeg;base64,ZZZZ"})
            await ext.sleep_for(0.8)
            assert not [f for f in ext.frames if f.get("type") == "perm_shot"], \
                "закрытый ask не должен догонять миниатюрой"
    run(_t())


def test_thumb_oversize_is_dropped(home):
    """Мусорный/гигантский dataUrl — карточка без картинки, вердикт жив."""
    huge = "data:image/jpeg;base64," + "A" * 400_001  # > PERM_THUMB_MAX
    async def _t():
        async with ThumbExt(home, shot=huge) as ext:
            await _bind_task(ext, home)
            rid = rpc_call(home, "perm.check", {
                "domain": "ok.ru", "action": "submit", "perm": "send_message",
                "task": "sk37t", "name": "Огромный кадр"})
            ask = await ext.wait_ask()
            await ext.wait_shotreq()
            await ext.sleep_for(0.8)
            assert not [f for f in ext.frames if f.get("type") == "perm_shot"], \
                "перебор по размеру обязан отсекаться"
            await ext.answer(ask["id"], "once")
            assert answered(home, rid)["result"]["decision"] == "allow"
    run(_t())


def test_name_truncated_to_120(home):
    async def _t():
        async with ThumbExt(home) as ext:
            rpc_call(home, "perm.check", {
                "domain": "ok.ru", "action": "submit", "perm": "send_message",
                "task": "sk37t", "name": "Д" * 300})
            ask = await ext.wait_ask()
            assert len(ask["name"]) == 120
            await ext.answer(ask["id"], "deny")
    run(_t())


# ------------------------------------------------------------------ node-юниты

def _run_node(script, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    f = tmp_path / "unit.js"
    f.write_text(script, encoding="utf-8")
    r = subprocess.run([node, str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    return r.stdout


def test_bell_core_node_unit(tmp_path):
    """Чистое ядро bell.js (SK-37-BEGIN/END): текст бейджа и решение звонить.
    SK-50 (v0.7): правило показа — всплывашка задачи при НЕ-фокусе окна;
    perm зовёт всегда (SK-37 семантика), фокус на неё не влияет."""
    m = re.search(r"// SK-37-BEGIN bell-core.*?\n(.*?)// SK-37-END bell-core",
                  BELL_SRC, re.S)
    assert m, "ядро bell-core с маркерами не найдено"
    out = _run_node(m.group(1) + """
const eq = (a, b, w) => { if (a !== b) { console.error(w, JSON.stringify(a), JSON.stringify(b)); process.exit(1); } };
eq(bellBadgeText(0), "", "пусто при нуле");
eq(bellBadgeText(1), "🔔", "один звонок — колокольчик");
eq(bellBadgeText(3), "🔔3", "пачка — счётчик");
eq(bellBadgeText(120), "🔔12", "клиппинг до 4 символов (Firefox-лимит бейджа)");
eq(bellShouldNotify("perm", true, 1), true, "пермис зовёт даже при фокусе окна (SK-50: всегда)");
eq(bellShouldNotify("perm", false, 1), true, "пермис зовёт при не-фокусе");
eq(bellShouldNotify("perm", true, 0), false, "звонить нечего — тишина и у пермиса");
eq(bellShouldNotify("task_done", true, 3), false, "окно в фокусе — всплывашка задачи подавлена (бейдж+журнал)");
eq(bellShouldNotify("task_done", false, 3), true, "окно не в фокусе — всплывашка задачи выходит");
eq(bellShouldNotify("task_failed", false, 1), true, "не-фокус — «не сделана» тоже звонит");
eq(bellShouldNotify("task_failed", true, 0), false, "пустой pending — тишина");
console.log("BELL-OK");
""", tmp_path)
    assert "BELL-OK" in out


def test_bell_action_ru_superset_of_sidebar():
    """Дрейф подписей: каждая подпись сайдбар-карточки обязана быть в bell-копии."""
    def keys(src, name):
        m = re.search(rf"const {name} = \{{(.*?)\}};", src, re.S)
        assert m, f"{name} не найден"
        return {k for k in re.findall(r'"?([a-z0-9_]+)"?\s*:', m.group(1))}
    side = keys(SIDEBAR_SRC, "ACTION_RU")
    bell = keys(BELL_SRC, "BELL_ACTION_RU")
    missing = side - bell
    assert not missing, f"BELL_ACTION_RU в background.js отстаёт от ACTION_RU: {sorted(missing)}"


# -------------------------------------------------------------------- исходники

def test_manifest_bell_wiring():
    assert "notifications" in MANIFEST["permissions"], "browser.notifications без разрешения"
    scripts = MANIFEST["background"]["scripts"]
    assert scripts[:2] == ["bell.js", "background.js"], \
        "bell.js обязан грузиться до background.js (чистое ядро + обёртки)"


def test_background_bell_wiring_pins():
    # пермис пришёл — звени; вердикт человека и resolve моста — звонок снят
    assert 'bellRing(msg.id, { kind: "perm"' in BG_SRC
    assert BG_SRC.count("bellClear(") >= 2, "bellClear обязан вызываться и на perm_answer, и на perm_resolved"
    # миниатюра: парковка в PENDING_PERM + broadcast в карточки
    assert 'msg.type === "perm_shot"' in BG_SRC and "PENDING_PERM.get(msg.id)" in BG_SRC
    # бейдж: колокольчик приоритетнее состояния агента
    assert "BELL.pending.size" in BG_SRC and "bellBadgeText(" in BG_SRC
    # приход/уход сайдбара пересобирает звонок
    assert BG_SRC.count("bellRefresh()") >= 2, "bellRefresh на connect и на disconnect порта"
    # клик по уведомлению — фокус на карточке (шлёт bell.js через background-глобалы)
    assert 'type: "bell_focus"' in BELL_SRC and "sendToUi(" in BELL_SRC


def test_sidebar_perm_card_pins():
    assert "renderPermShot(d, m.shot)" in SIDEBAR_SRC, "карточка не рисует миниатюру"
    assert 'm.name || ""' in SIDEBAR_SRC, "имя цели не попало в подзаголовок карточки"
    assert 'm.type === "perm_shot"' in SIDEBAR_SRC and 'm.type === "bell_focus"' in SIDEBAR_SRC
    assert "function bellFocus(" in SIDEBAR_SRC
    css = (ROOT / "extension/sidebar.css").read_text(encoding="utf-8")
    assert "img.pshot" in css and "max-height" in css, "миниатюра без компактного потолка высоты"
    assert "flash" in css, "bell_focus без подсветки карточки"


def test_bridge_thumb_contract_pins():
    assert "PERM_THUMB_W = 320" in BRIDGE_SRC
    assert '"max_width": PERM_THUMB_W' in BRIDGE_SRC
    # name — из params, ≤120
    assert 'params.get("name")' in BRIDGE_SRC and "[:120]" in BRIDGE_SRC
    # perm_frame аддитивно несёт name/tab/shot
    for field in ('"name"', '"tab"', '"shot"'):
        assert field in BRIDGE_SRC.split("def perm_frame")[1].split("def audit_perm")[0], \
            f"perm_frame не несёт {field}"
    # лег перехватывается ДО обычной обработки rpc_result (и до rpc_out)
    i_thumb = BRIDGE_SRC.index("rid in PERM_THUMB")
    i_pending = BRIDGE_SRC.index("entry = PENDING.pop(rid, None) if isinstance(rid, str) else None")
    assert i_thumb < i_pending, "перехват миниатюры обязан предшествовать агентскому разбору"
    # вердикт закрывает лег
    assert "perm_thumb_cancel(rid)" in BRIDGE_SRC.split("def resolve_perm")[1].split("\ndef ")[0]
    # capabilities: perm.check обязан учить агента класть name
    assert "name — человекочитаемое имя цели" in BRIDGE_SRC


def test_bell_module_is_generic():
    """Общий модуль: kind параметризован (perm + handoff/SK-35), не зашит в perm."""
    assert 'kind = "perm"' in BELL_SRC and "kind:" in BELL_SRC or "kind," in BELL_SRC
    assert "bellRing(" in BELL_SRC and "bellClear(" in BELL_SRC
    # идемпотентность: повторный ring по тому же id не плодит второе уведомление
    assert "prev ? prev.notified" in BELL_SRC
