"""SK-31 контракты: общая память чатов сайдбара через knet.

Три механизма:
  * agentd TaskFeed — флаг «из памяти»: непустой успешный результат knet_search /
    knet_fact lookup подмешивает в финальный chat-кадр аддитивное поле memory:true
    (мост ретранслирует as-is, background пробрасывает, сайдбар рисует глиф);
  * knet_core.redact_secrets — секреты не попадают ни в chunks (через sanitize),
    ни в facts (через fact_put); идемпотентен и не трогает обычный текст;
  * knet_ingest.PROFILES — профиль sidekick инжестится наравне с остальными.

Живая приёмка (сценарий A/B через сайдбар, скрин глифа, курсор knet_stats,
grep токена по индексу) — харнесс стенда, не pytest (прецедент SK-21/26/29).
"""
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agentd"))
import sidekick_agentd as A  # noqa: E402

KNET = pathlib.Path.home() / "knet"
sys.path.insert(0, str(KNET))
import knet_core as K  # noqa: E402
import knet_ingest as I  # noqa: E402

BG = (ROOT / "extension" / "background.js").read_text()
SB = (ROOT / "extension" / "sidebar.js").read_text()
CSS = (ROOT / "extension" / "sidebar.css").read_text()
AD = (ROOT / "agentd" / "sidekick_agentd.py").read_text()


# ------------------------------------------------------- memory_hit (юнит) ---

def test_memory_hit_search_nonempty_vs_empty():
    assert A.memory_hit("knet_search", json.dumps({"results": [{"text": "x"}]}))
    assert not A.memory_hit("knet_search", json.dumps({"results": []}))
    assert not A.memory_hit("knet_search", "")
    assert not A.memory_hit("knet_search", "не json")
    assert not A.memory_hit("knet_search", json.dumps({"error": "knet.db недоступен"}))
    assert not A.memory_hit("knet_search", json.dumps([1, 2]))  # не dict — нет


def test_memory_hit_fact_lookup_vs_put():
    assert A.memory_hit("knet_fact", json.dumps({"facts": [{"predicate": "p", "value": "v"}]}))
    assert not A.memory_hit("knet_fact", json.dumps({"facts": []}))
    # put — запись, а не вспоминание: у ответа нет ключа facts
    assert not A.memory_hit("knet_fact", json.dumps({"result": "added"}))


# ------------------------------------------------- TaskFeed: флаг памяти -----

def _run_with_tool(tool, args, output, is_error=False):
    f = A.TaskFeed("t1", "вопрос")
    frames = f.start() + f.on_init("sid")
    frames += f.on_tool_use(tool, args)
    frames += f.on_tool_result(is_error, tool, output)
    fin = f.finish(rc=0, text="ответ", stopped=False)
    return frames + fin, fin


def _chat_of(fin):
    return [x for x in fin if x["type"] == "chat"][-1]


def test_feed_memory_flag_on_nonempty_knet_search():
    frames, fin = _run_with_tool("knet_search", {"query": "корзина ozon"},
                                 json.dumps({"results": [{"source": "session:sidekick", "text": "полигон"}]}))
    assert _chat_of(fin).get("memory") is True
    # аддитивность: терминальный task-кадр флага не несёт, форма chat-кадра прежняя
    assert "memory" not in fin[-1]
    chat = _chat_of(fin)
    assert chat["role"] == "agent" and chat["task"] == "t1" and chat["text"] == "ответ"
    # данные — в шаге ленты: knet-шаг подписан 🗑
    steps = [x for x in frames if x["type"] == "step"]
    assert any(s["text"].startswith("🗑 вспоминаю") for s in steps)  # SK-36 перепин: фраза, глиф тот же


def test_feed_no_memory_flag_on_empty_error_or_nonmemory():
    for tool, args, out, err in [
        ("knet_search", {"query": "q"}, json.dumps({"results": []}), False),
        ("knet_search", {"query": "q"}, json.dumps({"results": [{"t": 1}]}), True),
        ("knet_fact", {"action": "put", "entity": "e"}, json.dumps({"result": "added"}), False),
        ("terminal", {"command": "echo"}, json.dumps({"results": [{"x": 1}]}), False),
        ("knet_search", {"query": "q"}, "не json", False),
    ]:
        _, fin = _run_with_tool(tool, args, out, err)
        assert "memory" not in _chat_of(fin), f"ложный флаг: {tool} {out[:30]} err={err}"


def test_feed_memory_flag_on_fact_lookup():
    _, fin = _run_with_tool("knet_fact", {"action": "lookup", "entity": "корзина ozon"},
                            json.dumps({"facts": [{"predicate": "статус", "value": "полигон"}]}))
    assert _chat_of(fin).get("memory") is True


def test_feed_memory_survives_later_steps_and_second_tool():
    f = A.TaskFeed("t2", "задача")
    f.start(); f.on_init("s")
    f.on_tool_use("knet_search", {"query": "q"})
    f.on_tool_result(False, "knet_search", json.dumps({"results": [{"t": 1}]}))
    f.on_tool_use("terminal", {"command": "ls"})
    f.on_tool_result(False, "terminal", "files")
    fin = f.finish(rc=0, text="да", stopped=False)
    assert _chat_of(fin).get("memory") is True


def test_feed_pending_memory_cleared_by_nonmemory_tool():
    # knet_use без результата, затем обычный тул с результатом — вердикт не по чужому выводу
    f = A.TaskFeed("t3", "задача")
    f.start(); f.on_init("s")
    f.on_tool_use("knet_search", {"query": "q"})
    f.on_tool_use("terminal", {"command": "ls"})
    f.on_tool_result(False, "terminal", json.dumps({"results": [{"x": 1}]}))
    fin = f.finish(rc=0, text="нет", stopped=False)
    assert "memory" not in _chat_of(fin)


def test_feed_backward_compat_on_tool_result_defaults():
    # старые вызовы on_tool_result(is_error) — подпись аддитивна, шаг закрыт, флага нет
    f = A.TaskFeed("t4", "задача")
    f.start(); f.on_init("s")
    f.on_tool_use("terminal", {"command": "ls"})
    frames = f.on_tool_result(False)
    # SK-36 перепин: кадр шага стал аддитивно шире — raw (сырая команда под кат
    # «подробнее») обязателен, остальное — прежняя форма
    assert {k: v for k, v in frames[0].items() if k != "raw"} == \
        {"type": "step", "task": "t4", "n": frames[0]["n"],
         "text": frames[0]["text"], "state": "done"}
    assert frames[0]["raw"] == "ls"
    assert "memory" not in _chat_of(f.finish(rc=0, text="ок", stopped=False))


def test_pause_and_abandon_never_carry_memory():
    f = A.TaskFeed("t5", "задача")
    f.start(); f.on_init("s")
    f.on_tool_use("knet_search", {"query": "q"})
    f.on_tool_result(False, "knet_search", json.dumps({"results": [{"t": 1}]}))
    for frames in (f.pause("канал мёртв"), f.abandon("просрочено")):
        for x in frames:
            assert "memory" not in x  # служебные chat-кадры — не ответ из памяти


def test_humanize_knet_icon():
    # SK-36 (перепин): глиф 🗑 сохранён, текст — человекочитая русская фраза
    assert A.humanize_tool("knet_search", {"query": "корзина ozon"}) == "🗑 вспоминаю: корзина ozon"
    assert A.humanize_tool("knet_fact", {"entity": "пылесос"}).startswith("🗑 сверяюсь с памятью")


def test_agentd_call_site_passes_name_and_output():
    # без name/output вердикт memory_hit слепой — контракт на месте проброса
    assert 'a.feed.on_tool_result(bool(ev.get("is_error")),\n' in AD
    assert 'str(ev.get("output") or "")' in AD
    assert '_MEM_TOOLS = ("knet_search", "knet_fact")' in AD


# --------------------------------------------------------- редактор секретов -

# форма «похожий на токен»; собирается конкатенацией — скраббер инструментов
# режет sk-литералы даже в исходниках тестов (побочное подтверждение, что форма верная)
TOKEN = "sk-TE" + "ST-31-9f3a7c2b8e5d4106af59b3c7"
PLAIN = [
    "завтра в 15:00 встретимся у метро, захвати зонт",
    "модель qwen3.8-flash-next-strata-ud-iq4-xs пойдёт на inference",
    "токен моста — только в hub/token (0600), не светить в логах",
    "пароль от вайфая — бананхлеб, запомни для тещи",
    "git commit b67ae0f починил race в rename",
]


@pytest.mark.parametrize("secret", [
    TOKEN,
    "deadbeefcafebabe0123456789abcdef01234567",  # hex 40
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "Authorization: Bearer abcdefghijklmnop0123456789",
    "token=SuperSecret1234567890",
    "api_key: 1234567890abcdef",
    "-----BEGIN RSA PRIVATE KEY-----",
    "ghp_16CharSample0123456789abcdefABCD",
    "aXdfT3V0RG9UaGlzVG9rZW4xMjM0NTY3ODlBY0VmR2hJa0xtTm8=",  # base64 40+ mix
])
def test_redactor_eats_secret_shapes(secret):
    out = K.redact_secrets(f"вот значение: {secret} — аккуратно")
    assert secret not in out
    assert "[секрет скрыт]" in out


@pytest.mark.parametrize("text", PLAIN)
def test_redactor_leaves_plain_text(text):
    assert K.redact_secrets(text) == text


def test_redactor_short_things_untouched():
    assert K.redact_secrets("sk-12345 и ключ=5") == "sk-12345 и ключ=5"


def test_redactor_idempotent():
    once = K.redact_secrets("ключ " + TOKEN + " и ещё " + TOKEN)
    assert K.redact_secrets(once) == once


def test_sanitize_applies_redactor_and_spectoken():
    _P = chr(124)
    out = K.sanitize(f"a <{_P}endoftext{_P}> b {TOKEN}")
    assert TOKEN not in out and _P not in out and "[секрет скрыт]" in out


def test_fact_put_sanitizes_value(tmp_path):
    con = K.init_db(str(tmp_path / "k.db"))
    K.fact_put(con, "профиль", "api", f"токен {TOKEN}")
    row = K.facts_lookup(con, "профиль")[0]
    assert TOKEN not in row[2] and "[секрет скрыт]" in row[2]


# ------------------------------------------------------------- инжест -------

def test_sidekick_profile_in_ingest():
    assert "sidekick" in I.PROFILES
    assert I.PROFILES["sidekick"].endswith(".hermes/profiles/sidekick/state.db")
    assert set(I.PROFILES) == {"default", "jev", "monitor", "sidekick"}


# --------------------------------------------------- статические JS-контракты -

def test_background_passes_memory():
    line = [l for l in BG.splitlines() if 'msg.type === "chat"' in l and "sendToUi" in l]
    assert len(line) == 1
    assert "msg.memory" in line[0] and "memory: true" in line[0]


def test_sidebar_glyph_and_passthrough():
    assert "function add(cls, text, memory)" in SB
    assert 'g.className = "mem"' in SB and "🗑" in SB
    assert "add(m.role === \"me\" ? \"me\" : \"hm\", m.text, m.memory === true)" in SB
    # history-replay (chats_data) глифа не имеет — флаг живёт только в live-кадре
    assert 'add(msg.role === "user" ? "me" : "hm", msg.text);' in SB


def test_sidebar_css_glyph_rule():
    rule = [l for l in CSS.splitlines() if l.startswith(".m .mem")]
    assert len(rule) == 1
    assert "#" not in rule[0]  # SK-13: токены, без hex-цветов
