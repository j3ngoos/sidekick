"""SK-36: шаги обычным языком — шаблоны + малая CPU-модель, сырое под катом.

Юниты: словарь шаблонов покрывает ВСЕ методы контракта v1 (sk capabilities),
значение value/text селекторов в подпись не попадает (приватность), сырая команда
едет отдельным полем raw (сайдбар прячет под «подробнее»), caption агента —
приоритет (модель его не перетирает), weak-флаг зовёт модель только когда шаблон
слаб, caption_refine — чистый fallback при недоступной модели и честный бюджет.
"""
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agentd"))
import sidekick_agentd as A  # noqa: E402


def sk(method, params, tail=""):
    return f"sk rpc {method} '{json.dumps(params, ensure_ascii=False)}'{tail}"


# ------------------------------------------------------------ словарь -------

def test_all_contract_methods_have_template():
    """П.11 AGENTS.md: новые методы = править и этот словарь. Сверяемся с живым
    capabilities-списком моста (фолбэк — хардкорный список контракта v1)."""
    import subprocess
    try:
        out = subprocess.run([str(ROOT / "bin" / "sk"), "capabilities"],
                             capture_output=True, text=True, timeout=10)
        caps = json.loads(out.stdout)["result"]["methods"]
        methods = sorted(set(caps["bridge_local"]) | set(caps["forwarded"])
                         | set(caps["gated"]))
        assert methods, "capabilities пуст — сверка бессмысленна"
    except Exception:
        methods = ["action.do", "action.preview", "page.screenshot", "page.text",
                   "page.dom", "page.media", "page.active", "tabs.open_grouped",
                   "tabs.close", "tabs.list", "tabs.activate", "tabs.use_tab",
                   "tabs.highlight_group", "tabs.park_group", "perm.check",
                   "handoff.request", "handoff.status", "capabilities", "audit.tail",
                   "chats.get", "chats.list", "grants.get", "grants.set"]
    for m in methods:
        # perm.check без типа пермиса — сознательно weak (уточнит модель, см.
        # test_perm_check_phrase); с валидным типом шаблон обязан быть
        params = {"perm": "payment"} if m == "perm.check" else {}
        phrase, weak = A._phrase_bridge(m, params, "")
        assert phrase and not weak, f"{m}: шаблон не покрыт (weak={weak})"


def test_action_do_phrases_and_privacy():
    p = A.step_parts("terminal", {"command": sk("action.do", {
        "action": "input", "name": "Логин", "value": "секрет@xx", "caption": "подпись агента"})})
    assert p["text"] == "печатаю в «Логин»"
    assert "секрет@xx" not in p["text"] and "секрет@xx" not in p["caption"]
    assert p["caption"] == "подпись агента" and not p["weak"]
    # домен задачи подмешивается к клику с целью
    p = A.step_parts("terminal", {"command": sk("action.do", {"action": "click", "name": "Купить"})},
                     domain="ozon.ru")
    assert p["text"] == "кликаю «Купить» на ozon.ru"
    # неизвестный глагол → weak (уточнит модель), текст — достойная заглушка
    p = A.step_parts("terminal", {"command": sk("action.do", {"action": "drag", "x": 1, "y": 2})})
    assert p["weak"] and p["text"] == "📡 работаю через мост"
    assert "drag" in p["raw"]


def test_domain_tracking_and_open_tab():
    p = A.step_parts("terminal", {"command": sk("tabs.open_grouped", {"url": "https://www.OZON.ru/cart?x=1"})})
    assert p["text"] == "открываю ozon.ru" and p["domain"] == "ozon.ru"
    p = A.step_parts("terminal", {"command": sk("tabs.open_grouped", {})})
    assert p["text"] == "открываю новую вкладку"


def test_legacy_rpc_in_and_plain_terminal():
    cmd = ("printf '%s\\n' '{\"v\":1,\"id\":\"x\",\"method\":\"page.screenshot\","
           "\"params\":{\"marks\":true}}' >> ~/.hermes/sidekick/rpc_in.jsonl")
    p = A.step_parts("terminal", {"command": cmd})
    assert p["text"] == "размечаю кнопки на экране" and not p["weak"]
    p = A.step_parts("terminal", {"command": "ls -la /tmp"})
    assert p["text"] == "🖥 выполняю команду в терминале" and "ls -la" in p["raw"]


def test_nested_json_braces_in_strings():
    # фигурные скобки внутри строки params не ломают разбор
    p = A.step_parts("terminal", {"command": sk("handoff.request",
                                                {"what": "введи {код} из письма"})})
    assert p["text"] == "зову тебя к экрану: введи {код} из письма"


def test_perm_check_phrase():
    p = A.step_parts("terminal", {"command": sk("perm.check",
                                                {"perm": "sensitive_input", "url": "https://bank.example/form"})})
    assert p["text"] == "спрашиваю разрешение: поле с секретом на bank.example"
    p = A.step_parts("terminal", {"command": sk("perm.check", {"perm": "teleport"})})
    assert p["weak"]  # неизвестный пермис → модель


# ------------------------------------------------------------- TaskFeed -----

def test_step_frames_carry_raw_and_caption():
    g = A.TaskFeed("t2", "х")
    frames = g.on_tool_use("terminal", {"command": sk("action.do", {"action": "click",
                                                                    "name": "К", "caption": "жму"})})
    act = frames[-1]
    assert act["state"] == "active" and act["caption"] == "жму" and "action.do" in act["raw"]
    done = g.close_step("done")[0]
    assert done["caption"] == "жму" and done["raw"] == act["raw"]


def test_weak_step_pending_and_refine():
    g = A.TaskFeed("t3", "х")
    g.on_tool_use("terminal", {"command": sk("action.do", {"action": "drag", "x": 1, "y": 2})})
    assert g.pending_weak and g.pending_weak["n"] == g.n and g.pending_weak["method"] == "action.do"
    # refine того же n без state — мост сохранит состояние; caption не перетирается
    fr = g.refine_step(g.n, "тяну мышью")
    assert fr == [{"type": "step", "task": "t3", "n": g.n, "text": "тяну мышью"}]
    assert g.cur_text == "тяну мышью"
    g2 = A.TaskFeed("t4", "х")
    g2.on_tool_use("terminal", {"command": sk("action.do", {"action": "drag", "x": 1, "y": 2,
                                                            "caption": "тащу товар"})})
    assert g2.pending_weak is None  # caption агента — модель не звать
    assert g2.refine_step(g2.n, "модельное") == []  # и не перетирать


def test_refine_after_close_only_when_open():
    g = A.TaskFeed("t5", "х")
    g.on_tool_use("terminal", {"command": sk("action.do", {"action": "drag"})})
    n = g.n
    g.close_step("done")
    g.closed = True
    assert g.refine_step(n, "поздно") == []


# ------------------------------------------------------- caption_refine -----

class _Cap(BaseHTTPRequestHandler):
    mode = "ok"

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.mode == "slow":
            import time
            time.sleep(1.2)
        if self.mode == "down":
            self.send_error(502)
            return
        text = "тяну мышью на странице" if self.mode == "ok" else "«тяну  мышью.»\nхвост"
        payload = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args, **kwargs):
        pass


@pytest.fixture
def cap_server():
    srv = HTTPServer(("127.0.0.1", 0), _Cap)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _point(srv, budget_ms=500):
    A.CAPTION_URL = f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"
    A.CAPTION_BUDGET_MS = float(budget_ms)
    A._CAPTION_CACHE.clear()


def test_caption_ok_strip_and_cache(cap_server):
    _Cap.mode = "ok"
    _point(cap_server)
    assert A.caption_refine("action.do", {"action": "drag"}) == "тяну мышью на странице"
    # кэш: сервер стоит — повтор без сети обязан вернуть то же
    A.CAPTION_URL = "http://127.0.0.1:1/x"
    assert A.caption_refine("action.do", {"action": "drag"}) == "тяну мышью на странице"


def test_caption_budget_is_honest(cap_server):
    _Cap.mode = "slow"
    _point(cap_server, budget_ms=200)
    assert A.caption_refine("action.do", {"action": "drag"}) is None  # не успела — отказ


def test_caption_down_is_silent_fallback(cap_server):
    _Cap.mode = "down"
    _point(cap_server)
    assert A.caption_refine("action.do", {"action": "weird"}) is None


def test_caption_refuses_non_loopback():
    A.CAPTION_URL = "https://cloud.example.com/v1/chat/completions"  # приватность
    A._CAPTION_CACHE.clear()
    assert A.caption_refine("action.do", {"action": "drag"}) is None
    A.CAPTION_URL = ""
    assert A.caption_refine("action.do", {}) is None


def test_caption_sanitizes_multiline(cap_server):
    _Cap.mode = "junk"
    _point(cap_server)
    # многострочный/мусорный ответ модели в ленту не идёт
    assert A.caption_refine("action.do", {"action": "drag", "z": 1}) is None
