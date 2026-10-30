"""SK-33 контракты: CLI sk + ротация jsonl-потоков + hub/health.json от моста.

Аудит 21.10 п.7 чиним тремя механизмами:
  * sk rpc — вердикт по id без ручных хелперов (включая ротацию rpc_out во время
    ожидания: вердикт пишется уже в свежий файл, sk обязан его найти);
  * rotate_if_big на мосту — порог 5 МБ (в тестах мельчим SIDEKICK_ROTATE_MAX),
    хранить 2 бэкапа, переименование при записи;
  * health.json — мост пишет (connect/disconnect + тик), sk status читает без
    systemctl; события получают ts-штамп для sk events --since.

Живой Firefox не нужен: mock extension из test_rpc_e2e-традиции (тред + свой loop).
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid

import pytest
import websockets

from conftest import PORT, kill_bridge, start_bridge, token_of

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SK = os.path.join(ROOT, "bin", "sk")
URI = f"ws://127.0.0.1:{PORT}"


def run_sk(home, *args, timeout=30):
    env = dict(os.environ, SIDEKICK_HOME=str(home))
    return subprocess.run([sys.executable, SK, *[str(a) for a in args]],
                          capture_output=True, text=True, timeout=timeout, env=env)


def json_of(res):
    assert res.stdout.strip(), f"пустой stdout, stderr: {res.stderr}"
    return json.loads(res.stdout.strip().splitlines()[-1])


class MockExt:
    """Тредовая mock-вкладка: hello-handshake, отвечает на rpc-рамки, умеет слать
    произвольные кадры (stop/resume) в мост."""

    def __init__(self, home, reply=None, respond=True):
        self.home, self.reply, self.respond = home, reply, respond
        self.connected = threading.Event()
        self.seen = []
        self.loop = None
        self.conn = None
        self._dead = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        assert self.connected.wait(5), "mock extension не подключился"

    def _run(self):
        async def go():
            async with websockets.connect(URI, open_timeout=3) as c:
                await c.send(json.dumps({"v": 1, "type": "hello",
                                         "token": token_of(self.home)}))
                ack = json.loads(await asyncio.wait_for(c.recv(), 3))
                assert ack.get("ok") is True, ack
                self.loop = asyncio.get_running_loop()
                self.conn = c
                self.connected.set()
                while not self._dead.is_set():
                    try:
                        raw = await asyncio.wait_for(c.recv(), 0.2)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.ConnectionClosed:
                        break
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("type") != "rpc":
                        continue
                    self.seen.append(msg)
                    if self.respond and self.reply is not None:
                        await c.send(json.dumps({"v": 1, "type": "rpc_result",
                                                 "id": msg["id"], "ok": True,
                                                 "result": self.reply}))
        asyncio.run(go())

    def send(self, frame):
        asyncio.run_coroutine_threadsafe(self.conn.send(json.dumps(frame)),
                                         self.loop).result(5)

    def close(self):
        self._dead.set()


def seed_lines(path, approx_bytes, obj=None):
    """Наполнить файл строками ~approx_bytes (валидный jsonl с чужими id —
    ни одна строка не должна совпасть с id живого запроса)."""
    base = obj or {"v": 1, "ok": True, "result": {"pad": "x" * 200}}
    with open(path, "a") as f:
        while f.tell() < approx_bytes:
            f.write(json.dumps({**base, "id": f"seed-{uuid.uuid4()}"}) + "\n")


@pytest.fixture
def ext(bridge):
    """mock extension поверх штатного bridge-фикстуры; закрывается после теста."""
    m = MockExt(bridge, reply={"tabs": [{"tabId": 1, "url": "https://e.test/"}]})
    yield m
    m.close()


# ---- sk rpc: вердикт, ошибки, таймауты ---------------------------------------------

def test_rpc_ok_verdict_json(bridge, ext):
    r = run_sk(bridge, "rpc", "tabs.list", "{}")
    assert r.returncode == 0, r.stderr
    obj = json_of(r)
    assert obj["ok"] is True and len(r.stdout.strip().splitlines()) == 1
    assert obj["result"]["tabs"][0]["url"] == "https://e.test/"
    assert ext.seen and ext.seen[0]["method"] == "tabs.list"
    assert ext.seen[0]["id"] == obj["id"]  # вердикт — ровно по id запроса sk


def test_rpc_no_extension_err(bridge):
    r = run_sk(bridge, "rpc", "page.text", "{}")
    assert r.returncode == 1  # вердикт есть, но ok:false
    obj = json_of(r)
    assert obj["ok"] is False and obj["err"] == "no_extension"


def test_rpc_client_timeout_no_bridge(tmp_path):
    r = run_sk(tmp_path, "rpc", "page.text", "{}", "--timeout", "0.5")
    assert r.returncode == 2
    obj = json_of(r)
    assert obj["ok"] is False and obj["err"] == "client_timeout"
    assert "sk status" in obj["hint"]  # подсказка ведёт к правильному инструменту


def test_rpc_bad_params_json(bridge):
    r = run_sk(bridge, "rpc", "page.text", "не json")
    assert r.returncode == 2
    assert "bad_params_json" in json_of(r)["err"]
    # запрос до мостa не дошёл: rpc_in пуст или отсутствует
    p = bridge / "rpc_in.jsonl"
    assert not p.exists() or not p.read_text().strip()


def test_rpc_params_not_object(bridge):
    r = run_sk(bridge, "rpc", "page.text", "[1,2]")
    assert r.returncode == 2 and "must be a JSON object" in json_of(r)["err"]


def test_rpc_gated_default_timeout_is_long(bridge):
    # SK-34: perm-гейт ждёт человека 60 с на мосту — sk ждёт 90 (вердикт не теряется);
    # handoff остаётся минутным гейтом (615 с)
    import importlib.util
    from importlib.machinery import SourceFileLoader
    # bin/sk — без расширения .py, spec_from_file_location не угадал бы loader
    loader = SourceFileLoader("sk_mod", SK)
    spec = importlib.util.spec_from_loader("sk_mod", loader)
    assert spec is not None
    sk_mod = importlib.util.module_from_spec(spec)
    loader.exec_module(sk_mod)  # __main__-guard не даёт ничего исполнить лишнего
    assert sk_mod.GATED_TIMEOUTS["perm.check"] == 90.0
    assert sk_mod.GATED_TIMEOUTS["tabs.close"] == 90.0
    assert sk_mod.GATED_TIMEOUTS["handoff.request"] > 600
    assert sk_mod.RPC_DEFAULT_TIMEOUT == 15.0


# ---- ротация rpc_out / audit / events -------------------------------------------------

def test_rpc_out_rotates_at_5mb_and_sk_finds_verdict(bridge):
    """Приёмка карты: «записать >5 МБ» — настоящий порог. Вердикт пишется уже в
    свежий файл после листания; sk, начавший чтение в старом inode, обязан найти
    вердикт в новом (иначе каждый агент получил бы client_timeout на ровном месте)."""
    seed_lines(bridge / "rpc_out.jsonl", 5 * 1024 * 1024)
    m = MockExt(bridge, reply={"tabs": []})
    try:
        r = run_sk(bridge, "rpc", "tabs.list", "{}")
        assert r.returncode == 0, f"вердикт через ротацию потерян: {r.stdout}{r.stderr}"
        assert json_of(r)["ok"] is True
    finally:
        m.close()
    assert (bridge / "rpc_out.jsonl.1").stat().st_size >= 5 * 1024 * 1024
    cur = (bridge / "rpc_out.jsonl").read_text()
    assert len(cur) < 1024 * 1024  # свежий файл — с вердикта, а не с мегабайтов
    assert json_of(r)["id"] in cur


def test_rotation_keeps_exactly_two_backups(tmp_path):
    p = start_bridge(tmp_path, {"SIDEKICK_ROTATE_MAX": "2000"})
    try:
        m = MockExt(tmp_path, reply={"ok": 1})
        try:
            for _ in range(3):
                seed_lines(tmp_path / "rpc_out.jsonl", 3000)
                r = run_sk(tmp_path, "rpc", "page.text", "{}")
                assert r.returncode == 0, r.stdout + r.stderr
        finally:
            m.close()
        assert (tmp_path / "rpc_out.jsonl.1").exists()
        assert (tmp_path / "rpc_out.jsonl.2").exists()
        assert not (tmp_path / "rpc_out.jsonl.3").exists()  # «хранить 2 ротации»
    finally:
        kill_bridge(p)


def test_audit_and_events_rotate_too(tmp_path):
    """Аудит-строка пишется на каждый rpc, событие — на stop-кадр; порог общий."""
    p = start_bridge(tmp_path, {"SIDEKICK_ROTATE_MAX": "2000"})
    try:
        seed_lines(tmp_path / "audit.jsonl", 3000)
        seed_lines(tmp_path / "events.jsonl", 3000)
        m = MockExt(tmp_path, reply={"x": 1})
        try:
            assert run_sk(tmp_path, "rpc", "page.text", "{}").returncode == 0
            m.send({"v": 1, "type": "stop"})
            time.sleep(0.5)
        finally:
            m.close()
        assert (tmp_path / "audit.jsonl.1").exists()
        assert (tmp_path / "events.jsonl.1").exists()
    finally:
        kill_bridge(p)


# ---- health.json ----------------------------------------------------------------------

def _health(home):
    return json.loads((home / "health.json").read_text())


def test_health_written_at_startup_offline(bridge):
    h = _health(bridge)
    assert h["bridge"] == "active" and h["extension"] == "offline"
    assert h["ws_count"] == 0 and abs(h["ts"] - time.time()) < 15


def test_health_connect_disconnect_hooks(bridge):
    m = MockExt(bridge, reply={})
    deadline = time.time() + 3
    while time.time() < deadline and _health(bridge)["extension"] != "connected":
        time.sleep(0.1)
    h = _health(bridge)
    assert h["extension"] == "connected" and h["ws_count"] == 1
    m.close()
    deadline = time.time() + 3
    while time.time() < deadline and _health(bridge)["extension"] != "offline":
        time.sleep(0.1)
    assert _health(bridge)["extension"] == "offline"  # disconnect-хук, не ожидая тика


def test_health_tick_refreshes_ts(tmp_path):
    p = start_bridge(tmp_path, {"SIDEKICK_HEALTH_TICK": "0.5"})
    try:
        ts0 = _health(tmp_path)["ts"]
        time.sleep(1.6)
        assert _health(tmp_path)["ts"] > ts0  # тик живёт, ts ползёт
    finally:
        kill_bridge(p)


# ---- sk status -------------------------------------------------------------------------

def test_status_json_bridge_active_extension_offline(bridge):
    r = run_sk(bridge, "status", "--json")
    assert r.returncode == 1  # мост жив, расширения нет — не «всё хорошо»
    obj = json_of(r)
    assert obj["bridge"] == "active" and obj["extension"] == "offline"
    assert obj["hub"] == str(bridge)


def test_status_json_connected_exit_zero(bridge, ext):
    deadline = time.time() + 3
    while time.time() < deadline and _health(bridge)["extension"] != "connected":
        time.sleep(0.1)
    r = run_sk(bridge, "status", "--json")
    assert r.returncode == 0, r.stdout
    obj = json_of(r)
    assert obj["extension"] == "connected" and obj["ws_count"] == 1
    # SK-49: peer активного расширения — «агент бьёт не туда» видно прямо в sk status
    assert str(obj["peer"]).startswith("127.0.0.1:")


def test_status_down_without_health(tmp_path):
    r = run_sk(tmp_path, "status", "--json")
    assert r.returncode == 2
    obj = json_of(r)
    assert obj["bridge"] == "down" and "health_err" in obj


def test_status_stale_health_is_not_active(tmp_path):
    (tmp_path / "health.json").write_text(json.dumps(
        {"bridge": "active", "extension": "connected",
         "ts": int(time.time()) - 3600, "ws_count": 1}) + "\n")
    r = run_sk(tmp_path, "status", "--json")
    assert r.returncode == 2  # протухший health = мост лежит, «connected» не верим
    assert json_of(r)["bridge"] == "stale"


def test_status_human_readable_smoke(bridge):
    r = run_sk(bridge, "status")
    assert "bridge" in r.stdout and "extension" in r.stdout and "agentd" in r.stdout


# ---- sk events --------------------------------------------------------------------------

def test_events_ts_stamp_and_since(bridge):
    m = MockExt(bridge, reply={})
    try:
        m.send({"v": 1, "type": "stop"})
        time.sleep(0.5)
    finally:
        m.close()
    lines = [ln for ln in (bridge / "events.jsonl").read_text().splitlines() if ln.strip()]
    stop_lines = [json.loads(ln) for ln in lines if '"stop"' in ln]
    assert stop_lines and isinstance(stop_lines[-1].get("ts"), int)  # штамп моста
    r = run_sk(bridge, "events", "--since", str(int(time.time()) - 60))
    assert any('"stop"' in ln for ln in r.stdout.splitlines())
    r = run_sk(bridge, "events", "--since", str(int(time.time()) + 3600))
    assert r.stdout.strip() == ""  # из будущего событий не бывает
    # агентд-совместимость: рамка осталась рамкой (type на месте, поле не съедено)
    assert stop_lines[-1]["type"] == "stop"
