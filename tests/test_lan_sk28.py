"""SK-28 контракты: мост в LAN (SIDEKICK_BIND) + rate-limit отказов токена +
поле «Сервер» с fallback на localhost + пауза агентда при мёртвом браузерном канале.

Живой LAN с второй машины (панели) — приёмочный харнесс, не pytest. Здесь —
настоящий мост-процесс (SIDEKICK_PORT=8799, conftest) и настоящие websockets-клиенты:
bind, отказы, окно, изоляция по IP проверяются поведенчески, а не грепом.
"""
import asyncio
import json
import pathlib
import re
import socket
import subprocess
import sys

import pytest

from conftest import PORT, ROOT, start_bridge, kill_bridge, token_of

websockets = pytest.importorskip("websockets")

EXT = pathlib.Path(ROOT) / "extension"
BG = (EXT / "background.js").read_text(encoding="utf-8")
OPT_HTML = (EXT / "options.html").read_text(encoding="utf-8")
OPT_JS = (EXT / "options.js").read_text(encoding="utf-8")
SIDE = (EXT / "sidebar.js").read_text(encoding="utf-8")
BRIDGE = (pathlib.Path(ROOT) / "bridge" / "sidekick_bridge.py").read_text(encoding="utf-8")
AGENTD = (pathlib.Path(ROOT) / "agentd" / "sidekick_agentd.py").read_text(encoding="utf-8")


def lan_ip():
    """Не-loopback адрес этой машины (UDP-trick, пакет не уходит). None — стенда нет."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


LAN = lan_ip()
need_lan = pytest.mark.skipif(LAN is None, reason="нет не-loopback интерфейса — LAN-стенд недоступен")


def hello_result(host, token, timeout=6):
    """Одно подключение: hello -> вердикт.
    ("ack", msg) — пришел hello_ack; ("closed", code) — закрыто БЕЗ ack
    (rate-limit/обрыв); ("refused", None) — порт не слушается; ("timeout", None)."""
    async def t():
        try:
            async with websockets.connect(f"ws://{host}:{PORT}", open_timeout=3) as c:
                try:
                    await c.send(json.dumps({"v": 1, "type": "hello", "token": token}))
                except Exception:
                    pass  # мост закрыл до чтения hello — вердикт ниже по закрытию
                try:
                    raw = await asyncio.wait_for(c.recv(), timeout)
                    return ("ack", json.loads(raw))
                except asyncio.TimeoutError:
                    return ("timeout", None)
                except websockets.ConnectionClosed as e:
                    return ("closed", e.code)
        except OSError:
            return ("refused", None)
    return asyncio.run(t())


# ================================================================ bind =======

def test_default_bind_is_loopback_only(bridge):
    """Чистый запуск = туннельный режим как до SK-28: loopback жив, LAN — refused."""
    kind, msg = hello_result("127.0.0.1", token_of(bridge))
    assert kind == "ack" and msg["ok"] is True
    if LAN:
        assert hello_result(LAN, token_of(bridge)) == ("refused", None)


@need_lan
def test_bind_all_interfaces_accepts_lan_handshake(tmp_path):
    """SIDEKICK_BIND=0.0.0.0 — расширение из LAN проходит полный handshake (v1)."""
    p = start_bridge(tmp_path, {"SIDEKICK_BIND": "0.0.0.0"})
    try:
        kind, msg = hello_result(LAN, token_of(tmp_path))
        assert kind == "ack" and msg["ok"] is True and msg["v"] == 1
    finally:
        kill_bridge(p)


@need_lan
def test_lan_bad_token_gets_clean_rejection(tmp_path):
    """Из LAN плохой токен — штатный hello_ack bad_token + 4003 (не обрыв)."""
    p = start_bridge(tmp_path, {"SIDEKICK_BIND": "0.0.0.0"})
    try:
        kind, msg = hello_result(LAN, "dead" * 16)
        assert kind == "ack" and msg["ok"] is False and msg["err"] == "bad_token"
    finally:
        kill_bridge(p)


# ========================================================= rate limit =======

def test_token_failures_rate_limited_per_ip(tmp_path):
    """> LIMIT плохих токенов за окно: дальше — закрытие БЕЗ handshake, даже с
    правильным токеном; окно истекло — правильный токен проходит."""
    p = start_bridge(tmp_path, {"SIDEKICK_TOKEN_FAIL_LIMIT": "2",
                                "SIDEKICK_TOKEN_FAIL_WINDOW": "2"})
    try:
        for _ in range(2):  # лимит отрабатывает штатно: ack bad_token
            kind, msg = hello_result("127.0.0.1", "bad" * 32)
            assert kind == "ack" and msg["err"] == "bad_token"
        kind, code = hello_result("127.0.0.1", "bad" * 32)
        assert kind == "closed" and code == 4003  # шум отрезан до hello_ack
        kind, code = hello_result("127.0.0.1", token_of(tmp_path))
        assert kind == "closed" and code == 4003  # и верный токен в окне не спасает
        import time
        time.sleep(2.3)  # окно истекло
        kind, msg = hello_result("127.0.0.1", token_of(tmp_path))
        assert kind == "ack" and msg["ok"] is True
    finally:
        kill_bridge(p)


def test_successful_handshake_clears_fail_counter(tmp_path):
    """Доказанный токен = не брутфорсер: успех снимает счётчик (шум — не криминал)."""
    p = start_bridge(tmp_path, {"SIDEKICK_TOKEN_FAIL_LIMIT": "2",
                                "SIDEKICK_TOKEN_FAIL_WINDOW": "60"})
    try:
        assert hello_result("127.0.0.1", "bad" * 32)[0] == "ack"
        assert hello_result("127.0.0.1", token_of(tmp_path))[1]["ok"] is True  # сброс
        for _ in range(2):  # снова два свежих — всё ещё штатные ack
            assert hello_result("127.0.0.1", "bad" * 32)[0] == "ack"
        kind, code = hello_result("127.0.0.1", "bad" * 32)
        assert kind == "closed" and code == 4003  # третий свежий — уже шум
    finally:
        kill_bridge(p)


@need_lan
def test_rate_limit_isolated_per_source_ip(tmp_path):
    """Отрезанный loopback не трогает LAN-источник: лимит — на IP, не на мост."""
    p = start_bridge(tmp_path, {"SIDEKICK_BIND": "0.0.0.0",
                                "SIDEKICK_TOKEN_FAIL_LIMIT": "1",
                                "SIDEKICK_TOKEN_FAIL_WINDOW": "60"})
    try:
        assert hello_result("127.0.0.1", "bad" * 32)[0] == "ack"
        kind, code = hello_result("127.0.0.1", "bad" * 32)
        assert kind == "closed" and code == 4003  # loopback отрезан
        kind, msg = hello_result(LAN, "bad" * 32)
        assert kind == "ack" and msg["err"] == "bad_token"  # LAN-источник — при своём лимите
    finally:
        kill_bridge(p)


# ==================================================== bridge static pins ====

def test_bridge_serves_on_bind_env():
    assert 'BIND = os.environ.get("SIDEKICK_BIND", "127.0.0.1")' in BRIDGE
    assert "websockets.serve(handler, BIND, PORT" in BRIDGE
    assert 'log(f"listening on ws://{BIND}:{PORT}' in BRIDGE


def test_bridge_rate_gate_before_hello_read():
    """Gate стоит ДО чтения hello: шумному IP не достаётся ни ack, ни парсинга."""
    hs = BRIDGE.split("async def handshake(ws):")[1].split("async def pump_outbox")[0]
    assert hs.index("token_rate_limited(ip)") < hs.index("HELLO_TIMEOUT_S")
    assert 'await ws.close(4003, "token rate limit")' in hs
    assert "note_token_fail(ip)" in hs
    assert 'TOKEN_FAILS.pop(ip, None)' in hs  # успех снимает счётчик


# ==================================================== extension static ======

def test_background_no_hardcoded_bridge_constant():
    assert 'const BRIDGE = "ws://' not in BG
    assert 'const FALLBACK_SERVER = "ws://127.0.0.1:8791"' in BG


def test_background_server_candidates_and_backoff():
    assert 'browser.storage.local.get("bridgeServer")' in BG
    assert "for (const u of [saved, FALLBACK_SERVER])" in BG  # fallback-порядок спеки п.3
    assert "BACKOFF_MIN = 2000, BACKOFF_MAX = 60000" in BG
    assert "Math.min(backoff * 2, BACKOFF_MAX)" in BG


def test_background_open_only_on_hello_ack():
    """Сокет без нашего hello_ack — не подключение (иначе «висячий» коннект к
    чужому ws-серверу выглядел бы успехом)."""
    ws = BG.split("function wsAttempt(url, token) {")[1].split("// Акк получен")[0]
    assert 'if (msg.ok) { finish("open"); promote(sock, url); }' in ws
    assert "OPEN_TIMEOUT_MS" in ws and "ACK_TIMEOUT_MS" in ws


def test_background_badge_retry_and_offline():
    assert 'BRIDGE_ST === "retry") [t, c] = ["◌"' in BG
    assert 'BRIDGE_ST === "offline") [t, c] = ["✗"' in BG


def test_background_server_updated_reconnects_immediately():
    assert 'm.type === "token-updated" || m.type === "server-updated"' in BG


def test_server_list_fallback_order_under_node():
    """serverList — чистая логика порядка кандидатов; гоняем её под node со стабом
    storage (тот же паттерн, что pageClass в test_selfheal_sk18)."""
    m = re.search(r"async function serverList\(\) \{.*?\n\}", BG, re.S)
    assert m, "serverList исчез из background.js"
    fb = re.search(r'const FALLBACK_SERVER = "ws://127\.0\.0\.1:8791";', BG)
    assert fb, "FALLBACK_SERVER (localhost-туннель) исчез из background.js"
    node = _node_bin()
    script = (
        "const cases = [[" + json.dumps("ws://host-a.local:8791") + ", "
        + json.dumps(["ws://host-a.local:8791", "ws://127.0.0.1:8791"]) + "], ["
        + json.dumps("") + ", " + json.dumps(["ws://127.0.0.1:8791"]) + "], ["
        + json.dumps("ws://127.0.0.1:8791/") + ", " + json.dumps(["ws://127.0.0.1:8791"]) + "], ["
        + json.dumps("  ws://host:1/ws/  ") + ", " + json.dumps(["ws://host:1/ws", "ws://127.0.0.1:8791"]) + "]];\n"
        + "const browser = { storage: { local: { get: async () => serverObj } } };\n"
        + "let serverObj;\n"
        + fb.group(0) + "\n"
        + m.group(0) + "\n"
        + "for (const [saved, want] of cases) { serverObj = { bridgeServer: saved };\n"
        + "  const got = await serverList();\n"
        + "  if (JSON.stringify(got) !== JSON.stringify(want)) { console.log('FAIL', JSON.stringify(got), want); process.exit(1); } }\n"
        + "console.log('OK');\n"
    )
    r = subprocess.run([node, "--input-type=module", "-e", script],
                       capture_output=True, text=True, timeout=30)
    assert "OK" in r.stdout, f"node: {r.stdout}{r.stderr}"


def _node_bin():
    import glob, shutil
    cands = glob.glob(str(pathlib.Path.home() / ".hermes/tools/node-*/bin/node"))
    return cands[0] if cands else (shutil.which("node") or "node")


def test_options_server_card_and_warnings():
    assert ">Сервер<" in OPT_HTML
    assert "ws://host-a.local:8791" in OPT_HTML          # плейсхолдер-пример
    assert "SIDEKICK_BIND=0.0.0.0" in OPT_HTML            # шаг на машине Hermes
    assert "открытым текстом" in OPT_HTML                  # предупреждение о ws://
    assert 'id="srv"' in OPT_HTML and 'id="srvSave"' in OPT_HTML


def test_options_js_validates_and_persists_server():
    assert "SRV_RE" in OPT_JS and "bridgeServer" in OPT_JS
    assert 'type: "server-updated"' in OPT_JS
    assert 'browser.storage.local.remove("bridgeServer")' in OPT_JS  # «на localhost»


def test_sidebar_human_hints_with_address():
    assert "retry:" in SIDE and "переподключаюсь" in SIDE
    assert "проверь мост" in SIDE and "«Сервер»" in SIDE
    assert "function setBridge(state, url)" in SIDE
    assert "setBridge(m.state, m.url)" in SIDE


# ==================================================== agentd static/pure ====

def _agentd_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sidekick_agentd_sk28", str(pathlib.Path(ROOT) / "agentd" / "sidekick_agentd.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_channel_state_matrix():
    m = _agentd_mod()
    now = 1_000_000.0
    assert m.channel_state(None, now) == "bridge_down"
    assert m.channel_state({"bridge": "active"}, now) == "bridge_down"          # нет ts
    assert m.channel_state({"ts": "123", "extension": "connected"}, now) == "bridge_down"
    assert m.channel_state({"ts": True, "extension": "connected"}, now) == "bridge_down"
    assert m.channel_state({"ts": now - 31, "extension": "connected"}, now) == "bridge_down"  # протух (SK-33: >30)
    assert m.channel_state({"ts": now - 29, "extension": "connected"}, now) == "ext_online"
    assert m.channel_state({"ts": now, "extension": "offline"}, now) == "ext_offline"
    assert m.channel_state({"ts": now}, now) == "ext_offline"  # нет поля extension
    # ws_count — НЕ сигнал: offline при свежем ws_count=1 остаётся ext_offline
    assert m.channel_state({"ts": now, "extension": "offline", "ws_count": 1}, now) == "ext_offline"


def test_read_health_never_raises(tmp_path):
    m = _agentd_mod()
    assert m.read_health(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    assert m.read_health(str(bad)) is None
    good = tmp_path / "ok.json"
    good.write_text('{"bridge":"active","extension":"connected","ts":1}', encoding="utf-8")
    assert m.read_health(str(good))["extension"] == "connected"


def test_task_feed_pause_resume_cycle():
    m = _agentd_mod()
    f = m.TaskFeed("t1", "задача")
    f.start()
    fr = f.pause("⏸ Браузер офлайн")
    kinds = [(x["type"], x.get("state")) for x in fr]
    assert ("step", "failed") in kinds            # открытый шаг закрыт
    assert ("chat", None) in kinds                # подсказка человеку
    assert kinds[-1] == ("task", "paused")
    assert f.closed is False                       # НЕ финал: resume продолжит
    fr = f.resume()
    assert fr[0] == {"type": "task", "id": "t1", "title": "задача", "state": "running"}
    assert fr[-1]["state"] == "active" and "канал восстановлен" in fr[-1]["text"]
    fr = f.finish(rc=0, text="готово", stopped=False)
    assert fr[-1]["state"] == "done" and f.closed is True
    assert f.pause("posthumous") == []  # закрытая задача не паузится — финал не перетирается


def test_agentd_channel_supervision_wired():
    assert 'HEALTH_F = f"{HUB}/health.json"' in AGENTD
    assert "channel_state(read_health()" in AGENTD
    assert "открой Firefox" in AGENTD and "задача продолжится автоматически" in AGENTD
    assert "pause_task(a, CHAN_HINT" in AGENTD
    assert "not chan_down" in AGENTD                       # очередь не стартует в мёртвый канал
    assert "list(actives.values()) + list(paused.values())" in AGENTD  # курсор не «съедает» паузу
    assert "EXT_PAUSE_AFTER_S" in AGENTD and "chan_bad_since" in AGENTD


def test_agentd_envelope_teaches_no_extension_discipline():
    assert "no_extension" in AGENTD and "НЕ долби rpc_in" in AGENTD


def test_sidebar_paused_state_not_stop_specific():
    # SK-28: paused — не только Stop; причина — в chat-кадре, метка нейтральна.
    # SK-38 убрал чипы из шапки — носителем той же семантики стал TASK_MARK
    # карточки задачи (feed.js): «⏸» без упоминания Stop.
    feed = (EXT / "feed.js").read_text(encoding="utf-8")
    assert 'paused: "⏸"' in feed, "пауза в карточке — нейтральный глиф, не «по Stop»"
    assert "Stop" not in feed.split('TASK_MARK')[1].split("};")[0]


def test_manifest_csp_allows_any_ws_host():
    # Живая приёмка SK-28: connect-src ws://127.0.0.1:* ws://localhost:* резал
    # LAN-подключение ещё до TCP (WebSocket ctor бросал NS_ERROR_CONTENT_BLOCKED).
    # LAN-режим живёт только при схемном allow-list'е ws:/wss:.
    import json as _json
    man = _json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    csp = man["content_security_policy"]["extension_pages"]
    connect = [d for d in csp.split(";") if d.strip().startswith("connect-src")]
    assert connect, "connect-src в CSP есть"
    srcs = connect[0].split()[1:]
    assert "ws:" in srcs and "wss:" in srcs, f"схемный allow-list, а не loopback-only: {srcs}"
    assert not any("127.0.0.1" in s or "localhost" in s for s in srcs), \
        "loopback-only connect-src возвращать запрещено (убивает SK-28 LAN)"
