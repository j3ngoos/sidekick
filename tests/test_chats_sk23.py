"""SK-23 контракты: история чатов (chat→session --resume), мостовая нога.

Мост: chats.jsonl — append-only зеркало беседы (обе стороны), RPC
chats.list/chats.get (bridge-local, за PAUSED-гейтом как audit.tail) и WS-лег
chats_get -> chats_data (consumed, не аудится, живёт во время hand-off).
Agentd: каждый кадр несёт chat (SessionsMap chat→sid уже был из SK-21).
Сайдбар/фон: статические контракты (＋/🕘, sk23Chat, проброс chat, фильтр ленты).

Живая приёмка (реальный Hermes, изоляция чатов, resume «42», BiDi-переустановка)
— харнесс tests/sk23_acc.py, не pytest: здесь детерминированные части.
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agentd"))
import sidekick_agentd as A  # noqa: E402

from conftest import kill_bridge, start_bridge, token_of  # noqa: E402
from test_rpc_e2e import read_rpc_out, rpc_call  # noqa: E402

URI = "ws://127.0.0.1:8799"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


def chats_lines(home):
    p = home / "chats.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def append_chats(home, *recs):
    """Write raw message lines straight into chats.jsonl — the bridge aggregates
    on read, no restart needed (same pattern as append_audit)."""
    with open(home / "chats.jsonl", "a") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def rpc(home, method, params=None, timeout=5.0):
    rid = rpc_call(home, method, params or {})
    out = read_rpc_out(home, rid, timeout=timeout)
    assert out is not None, f"no rpc_out for {method}"
    return out


async def ws_client(home):
    c = await websockets.connect(URI, open_timeout=3)
    await c.send(json.dumps({"v": 1, "type": "hello", "token": token_of(home)}))
    ack = json.loads(await asyncio.wait_for(c.recv(), 3))
    assert ack.get("ok") is True, ack
    return c


async def recv_until(c, type_, timeout=3.0, except_types=("state",)):
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(await asyncio.wait_for(c.recv(), max(0.1, deadline - time.time())))
        if msg.get("type") in except_types:
            continue
        assert msg.get("type") == type_, msg
        return msg
    raise AssertionError(f"no {type_} frame in {timeout}s")


# ---- зеркало беседы: обе стороны ложатся в chats.jsonl --------------------------

def test_ws_user_chat_mirrors_and_still_reaches_inbox(home):
    async def t():
        c = await ws_client(home)
        async with c:
            await c.send(json.dumps({"v": 1, "type": "chat", "role": "user",
                                     "text": "запомни число 42", "chat": "cA1"}))
            await asyncio.sleep(0.3)
    run(t())
    lines = chats_lines(home)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["chat_id"] == "cA1" and rec["role"] == "user"
    assert rec["text"] == "запомни число 42" and isinstance(rec["ts"], int)
    # аддитивность: кадр по-прежнему уходит агентду в inbox (raw-строка клиента)
    inbox = home / "inbox.jsonl"
    assert inbox.exists()
    rec_in = json.loads(inbox.read_text().splitlines()[-1])
    assert rec_in["text"] == "запомни число 42" and rec_in["chat"] == "cA1"


def test_chat_frame_without_chat_is_default(home):
    async def t():
        c = await ws_client(home)
        async with c:
            await c.send(json.dumps({"v": 1, "type": "chat", "role": "user", "text": "старое"}))
            await asyncio.sleep(0.3)
    run(t())
    lines = chats_lines(home)
    assert len(lines) == 1 and lines[0]["chat_id"] == "default"  # п.4 совместимость


def test_outbox_agent_chat_mirrors_and_broadcasts_verbatim(home):
    async def t():
        c = await ws_client(home)
        async with c:
            with open(home / "outbox.jsonl", "a") as f:
                f.write(json.dumps({"v": 1, "type": "chat", "role": "agent",
                                    "task": "t1", "chat": "cA1", "text": "42"},
                                   ensure_ascii=False) + "\n")
            f = await recv_until(c, "chat")
            assert f["text"] == "42" and f["chat"] == "cA1"  # broadcast as-is
    run(t())
    deadline = time.time() + 3
    while time.time() < deadline and not chats_lines(home):
        time.sleep(0.1)
    lines = chats_lines(home)
    assert len(lines) == 1 and lines[0]["role"] == "agent" and lines[0]["chat_id"] == "cA1"


def test_agent_chat_without_chat_lands_in_default(home):
    with open(home / "outbox.jsonl", "a") as f:
        f.write(json.dumps({"v": 1, "type": "chat", "role": "agent", "text": "эхо"}) + "\n")
    deadline = time.time() + 3
    while time.time() < deadline and not chats_lines(home):
        time.sleep(0.1)
    lines = chats_lines(home)
    assert len(lines) == 1 and lines[0]["chat_id"] == "default"


def test_empty_text_never_logs_and_text_is_capped(home):
    async def t():
        c = await ws_client(home)
        async with c:
            await c.send(json.dumps({"v": 1, "type": "chat", "role": "user",
                                     "text": "   ", "chat": "cE"}))
            await c.send(json.dumps({"v": 1, "type": "chat", "role": "user",
                                     "text": "д" * 5000, "chat": "cE"}))
            await asyncio.sleep(0.3)
    run(t())
    lines = chats_lines(home)
    assert len(lines) == 1 and len(lines[0]["text"]) == 4000


# ---- RPC chats.list / chats.get ---------------------------------------------------

def seed(home, cid, first_ts, pairs):
    """pairs = [(role, text), ...] with distinct increasing ts."""
    append_chats(home, *[{"chat_id": cid, "role": r, "text": t, "ts": first_ts + i}
                         for i, (r, t) in enumerate(pairs)])


def test_chats_list_empty_file_is_empty_list(home):
    out = rpc(home, "chats.list")
    assert out["ok"] is True and out["result"] == {"chats": []}


def test_chats_list_shape_order_title_last(home):
    seed(home, "cA", 1792000100, [("user", "запомни число 42"), ("agent", "запомнил")])
    seed(home, "cB", 1792000200, [("user", "новое дело"), ("agent", "слушаю")])
    res = rpc(home, "chats.list")["result"]["chats"]
    assert [c["id"] for c in res] == ["cB", "cA"]          # ts desc — последние сверху
    a = [c for c in res if c["id"] == "cA"][0]
    assert a["title"] == "запомни число 42" and a["ts"] == 1792000100
    assert a["last"] == {"role": "agent", "text": "запомнил", "ts": 1792000101}


def test_chats_list_title_is_first_user_utterance_clipped(home):
    long = "число " * 30  # 180 символов
    seed(home, "cT", 1792000300, [("agent", "агент первым"), ("user", long), ("user", "ещё")])
    res = rpc(home, "chats.list")["result"]["chats"]
    assert res[0]["title"] == ("число " * 30)[:59] + "…"   # ≤60, с многоточием
    assert len(res[0]["title"]) == 60
    # чата без user-реплики title честно берётся из первой вообще реплики
    seed(home, "cOnly", 1792000400, [("agent", "только агент")])
    res = rpc(home, "chats.list")["result"]["chats"]
    assert [c for c in res if c["id"] == "cOnly"][0]["title"] == "только агент"


def test_chats_list_cuts_to_30_newest_by_ts(home):
    # 35 чатов: самый «свежий» по ts — не последний по записи (проверка сортировки)
    for i in range(35):
        seed(home, f"c{i:02d}", 1792000000 + i * 10, [("user", f"чат {i}")])
    res = rpc(home, "chats.list")["result"]["chats"]
    assert len(res) == 30
    ts = [c["ts"] for c in res]
    assert ts == sorted(ts, reverse=True)                   # порядок по ts
    assert res[0]["id"] == "c34" and res[-1]["id"] == "c05"  # срезаны 5 самых старых
    assert not any(c["id"] in {"c00", "c01", "c02", "c03", "c04"} for c in res)


def test_chats_get_returns_record_and_unknown_is_empty(home):
    seed(home, "cA", 1792000100, [("user", "раз"), ("agent", "два"), ("user", "три")])
    res = rpc(home, "chats.get", {"chat_id": "cA"})["result"]["chat"]
    assert res["chat_id"] == "cA" and res["title"] == "раз" and res["ts"] == 1792000100
    assert [(m["role"], m["text"]) for m in res["msgs"]] == [("user", "раз"), ("agent", "два"), ("user", "три")]
    assert [m["ts"] for m in res["msgs"]] == [1792000100, 1792000101, 1792000102]
    unk = rpc(home, "chats.get", {"chat_id": "нет-такого"})["result"]["chat"]
    assert unk == {"chat_id": "нет-такого", "title": "", "ts": None, "msgs": []}


def test_chats_rpc_behind_paused_gate(home):
    seed(home, "cA", 1792000100, [("user", "приват")])
    (home / "paused").touch()  # человек держит браузер — история его, не агента
    for m in ("chats.list", "chats.get"):
        out = rpc(home, m, {"chat_id": "cA"})
        assert out["ok"] is False and out["err"] == "paused_for_human", (m, out)
    (home / "paused").unlink()
    assert rpc(home, "chats.list")["ok"] is True
    assert rpc(home, "chats.get", {"chat_id": "cA"})["ok"] is True


def test_chats_rpc_lines_are_audited_without_text(home):
    seed(home, "cA", 1792000100, [("user", "секретная реплика")])
    rpc(home, "chats.list")
    audit = [json.loads(ln) for ln in (home / "audit.jsonl").read_text().splitlines()]
    hits = [a for a in audit if a.get("method") == "chats.list"]
    assert hits and hits[-1]["ok"] is True
    assert "секретная реплика" not in json.dumps(hits[-1])  # аудит — про метод, не про текст


# ---- WS chats_get: лег сайдбара -----------------------------------------------------

def test_ws_chats_get_list_and_chat_legs(home):
    seed(home, "cA", 1792000100, [("user", "привет"), ("agent", "ага")])
    async def t():
        c = await ws_client(home)
        async with c:
            await c.send(json.dumps({"v": 1, "type": "chats_get", "id": "h1"}))
            f = await recv_until(c, "chats_data")
            assert f["id"] == "h1" and f["chats"][0]["id"] == "cA" and "chat" not in f
            await c.send(json.dumps({"v": 1, "type": "chats_get", "id": "h2", "chat_id": "cA"}))
            f2 = await recv_until(c, "chats_data")
            assert f2["id"] == "h2" and f2["chat"]["chat_id"] == "cA"
            assert [m["text"] for m in f2["chat"]["msgs"]] == ["привет", "ага"]
            assert "chats" not in f2
    run(t())


def test_ws_chats_get_consumed_not_audited_works_during_pause(home):
    seed(home, "cA", 1792000100, [("user", "история человека")])
    (home / "paused").touch()  # человек у клавиатуры — СВОЮ историю он читать не перестал
    async def t():
        c = await ws_client(home)
        async with c:
            await c.send(json.dumps({"v": 1, "type": "chats_get", "id": "p1"}))
            f = await recv_until(c, "chats_data")
            assert f["id"] == "p1" and len(f["chats"]) == 1
    run(t())
    (home / "paused").unlink()
    inbox = home / "inbox.jsonl"
    assert not inbox.exists() or "chats_get" not in inbox.read_text()
    time.sleep(0.3)
    audit = (home / "audit.jsonl").read_text() if (home / "audit.jsonl").exists() else ""
    assert "chats_get" not in audit  # не аудитим пулл панели (тот же резон, что audit_get)


def test_malformed_chats_lines_are_skipped_not_fatal(home):
    with open(home / "chats.jsonl", "a") as f:
        f.write("не json\n")
        f.write("[список, не объект]\n")
        f.write('{"chat_id": "cA", "role": "user", "text": "живая", "ts": 5}\n')
        f.write('{"без": "полей"}\n')  # dict без chat_id → default, text "" → msg пустой
    res = rpc(home, "chats.list")["result"]["chats"]
    ids = {c["id"] for c in res}
    assert "cA" in ids and "default" in ids
    a = [c for c in res if c["id"] == "cA"][0]
    assert a["title"] == "живая"


# ---- agentd: каждый кадр несёт chat; sessions — по чатам ---------------------------

def test_emit_stamps_chat_on_every_frame(tmp_path, monkeypatch):
    out = tmp_path / "outbox.jsonl"
    monkeypatch.setattr(A, "OUTBOX", str(out))

    class FakeA:
        chat = "cX9"

    f = A.TaskFeed("t9", "вопрос")
    frames = f.start() + f.on_think("думаю") + f.finish(rc=0, text="ответ", stopped=False)
    A.emit(FakeA(), frames)
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert lines and all(l["chat"] == "cX9" for l in lines)
    assert lines[0]["type"] == "task" and lines[-1]["type"] == "task"
    chat = [l for l in lines if l["type"] == "chat"][0]
    assert chat["role"] == "agent" and chat["chat"] == "cX9"


def test_sessions_map_isolates_chats(tmp_path):
    m = A.SessionsMap(str(tmp_path / "sessions.json"))
    m.set("cA", "sidA")
    m.set("cB", "sidB")
    assert m.get("cA") == "sidA" and m.get("cB") == "sidB"
    m.set("cA", None)  # битый resume сбрасывает СВОЮ сессию, чат B не тронут
    assert m.get("cA") is None and m.get("cB") == "sidB"
    assert m.get("cНовый") is None  # новый чат — нет записей → старт без --resume


# ---- статические контракты UI --------------------------------------------------------

HTML = (ROOT / "extension" / "sidebar.html").read_text()
SB = (ROOT / "extension" / "sidebar.js").read_text()
BG = (ROOT / "extension" / "background.js").read_text()
CSS = (ROOT / "extension" / "sidebar.css").read_text()
AGENTD = (ROOT / "agentd" / "sidekick_agentd.py").read_text()
BRIDGE = (ROOT / "bridge" / "sidekick_bridge.py").read_text()


def test_html_has_chat_buttons_and_history_panel():
    # SK-30 п.1/п.2: плоские кнопки убраны из #bar в меню-портал «⋯» (id те же),
    # иконки — inline-SVG из спрайта вместо эмодзи ＋/🕘 (контракт синхронизирован
    # карточкой SK-30; прецедент: SK-22/test_audit_js).
    assert 'id="menuBtn"' in HTML and 'id="menu"' in HTML
    assert HTML.index('id="menu"') > HTML.index('id="bar"')  # портал — после композера
    assert 'id="newBtn"' in HTML and "ic-plus" in HTML.split('id="newBtn"')[1][:200]
    assert 'id="histBtn"' in HTML and "ic-history" in HTML.split('id="histBtn"')[1][:200]
    for el in ('id="hist"', 'id="histlist"', 'id="histref"', 'id="histclose"', 'id="histn"'):
        assert el in HTML
    # порядок скриптов SK-22 не сломан: feed раньше сайдбара
    assert HTML.index("feed.js") < HTML.index("sidebar.js")


def test_sidebar_chat_model_contracts():
    assert 'const CHAT_KEY = "sk23Chat"' in SB          # активный чат — storage.local
    assert "browser.storage.local.get(CHAT_KEY)" in SB  # реставрируется после перезагрузки
    assert "browser.storage.local.set({ [CHAT_KEY]: cid })" in SB
    assert "port.postMessage({ type: \"chat\", text: t, chat: activeChat })" in SB
    assert "type: \"chats_get\"" in SB                  # обе ноги: список и загрузка чата
    assert "chats_data" in SB and "chats_err" in SB
    assert "function clearFeed" in SB and "function switchChat" in SB
    # фильтр ленты: чужие chat/think-кадры не рисуются поверх открытого чата
    assert "const inChat = (m) => ((m && m.chat) || \"default\") === activeChat" in SB
    assert "if (!inChat(m)) return; add(" in SB
    assert "if (inChat(m)) addThink(m)" in SB
    # карточка задачи — только своего чата (SK-25: фильтр стал per-task —
    # параллельные задачи рисуются карточками только активного чата)
    assert '|| "default") !== activeChat' in SB
    # после загрузки ленты глобальные блоки возвращаются replay-ом
    assert "port.postMessage({ type: \"replay\" })" in SB
    # второй вид взаимоисключающ (аудит ↔ история)
    assert "if (on && HIST.open) histOpen(false)" in SB
    assert "if (on && AUD.open) audOpen(false)" in SB


def test_background_chat_passthrough_contracts():
    # исходящий кадр человека несёт chat (аддитивно: пусто — не добавляем поле)
    assert "const chat = String(m.chat || \"\").slice(0, 80);" in BG
    assert "send({ type: \"chat\", role: \"user\", text: m.text, ...(chat ? { chat } : {}) });" in BG
    # входящие кадры пробрасывают chat (hermes-чат, think, TL)
    assert "role: \"hermes\", text: msg.text, task: msg.task, chat: msg.chat" in BG
    assert "sendToUi({ type: \"think\", task: msg.task, text: msg.text, chat: msg.chat })" in BG
    # SK-25: TL стал per-task (TASKS) — chat оседает на записи задачи; интент SK-23
    # (задача знает свой чат, срез 80) сохранён
    assert "e.chat = String(msg.chat).slice(0, 80)" in BG
    # SK-25: tlPush резолвит активного свидетеля (TLK) — handoff/chat с активным
    # снапшотом; интент SK-23 (TL-кадр несёт handoff и chat) сохранён
    assert "handoff: TLW.handoff" in BG and 'chat: cur ? cur.chat : ""' in BG
    # лег сайдбара: chats_get -> chats_data / chats_err + replay после смены чата
    assert 'm.type === "chats_get"' in BG and 'type: "chats_err"' in BG
    assert 'if (msg.type === "chats_data") { sendToUi(msg); return; }' in BG
    assert 'm.type === "replay"' in BG


def test_css_history_uses_theme_tokens_only():
    assert "#hist" in CSS and ".hrow" in CSS and ".hrow.cur" in CSS
    new_block = CSS.split("SK-23: chat history")[1]
    assert "#hist" in new_block
    import re as _re
    assert not _re.search(r"#[0-9a-fA-F]{3,6}\b", new_block), "hex-цвета в SK-23 блоке запрещены (SK-13 токены)"


def test_agentd_stamps_chat_and_bridge_constants():
    assert "out_frame(chat=a.chat, **f)" in AGENTD
    assert "def emit(a: \"Active\", frames)" in AGENTD
    assert "CHATS_LIST_MAX = 30" in BRIDGE
    assert "chats.jsonl" in BRIDGE
    # мост не изобретает chat_id: attribution только из кадра (default — п.4)
    assert 'str(chat_id or "default")' in BRIDGE


@pytest.mark.parametrize("f", ["background.js", "sidebar.js"])
def test_js_syntax(f):
    r = subprocess.run(["node", "--check", str(ROOT / "extension" / f)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
