#!/usr/bin/env python3
"""SK-46 живая приёмка: стабильность канала на ПРОДЕ (:8791, юнит-юнит systemd).

Аудит №2 22.10, проблема 5: мост дважды убили SIGKILL; канал должен переживать
это молча и честно: systemd поднимает, событие пишется, алерт приходит человеку,
флэппинг виден числом.

Сценарий (прода; юнит уже перезагружен с дроп-ином SK-46):
  S0  стенд: мост активен, расширение пользователя подключено, агентд жив
  S1  SIGKILL MainPID -> systemd поднимает НОВЫЙ процесс (Restart=on-failure, backoff)
  S2  events.jsonl: новый bridge_unclean_exit (ts >= момента килла)
  S3  agentd.log: алерт поставлен в очередь
  S4  канал восстановился сам: health.json extension=connected (расширение
      переподключается с backoff, ждём до 90 с)
  S5  reconnects_per_hour < 2 (до наших handshake'ов; шторм давал 12/70 мин)
  S6  legacy_hello живьём: hello+token -> close -> reconnect hb-первым -> принят,
      в bridge.log строка legacy_hello; затем сразу отпускаем слот
  S7  алерт живьём: сообщение в inbox -> В outbox сначала кадр-алерт («нечисто»),
      потом уже задачи; алерт зеркалится в chats.jsonl; запущенную тестовую
      задачу гасим hub/stop
  S8  штатный systemctl restart -> НЕТ нового bridge_unclean_exit (graceful-метка)
  S9  юнит: OOMPolicy=continue, Restart=on-failure, backoff 5..30, OOMScoreAdjust=-250
  S10 флэппинг в простое: 2 замера reconnects_per_hour с интервалом 60 с — дельта 0

Запуск:  python3 tests/sk46_acc.py
"""
import json
import os
import pathlib
import subprocess
import sys
import time

import websockets

HOME = pathlib.Path.home()
REPO = pathlib.Path(__file__).resolve().parent.parent
HUB = REPO / "bridge" / "hub"
TOKEN = (HUB / "token").read_text().strip()
URL = "ws://127.0.0.1:8791/ws"

RESULTS = []


def note(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  OK " if ok else "  FAIL") + " " + name + (f" — {detail}" if detail else ""), flush=True)


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)


def unit_prop(p):
    r = sh(f"systemctl --user show sidekick-bridge -p {p}")
    return r.stdout.strip().split("=", 1)[1]


def main_pid():
    return int(unit_prop("MainPID"))


def events():
    p = HUB / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def health():
    return json.loads((HUB / "health.json").read_text(encoding="utf-8"))


def log_text(name):
    p = HUB / name
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def wait(pred, timeout, step=0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


async def recv_type(ws, t, timeout=5):
    import asyncio
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if m.get("type") == t:
            return m


async def hs(ws):
    import asyncio
    await ws.send(json.dumps({"v": 1, "type": "hello", "token": TOKEN,
                              "ext": "hermes-sidekick", "ver": "0.2"}))
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


async def s6_legacy_hello():
    """Сначала штатный handshake (парит 127.0.0.1), потом reconnect hb-первым."""
    async with websockets.connect(URL) as ws:
        ack = await hs(ws)
        if not ack.get("ok"):
            return False, f"штатный handshake не прошёл: {ack}"
    await _asleep(0.3)
    async with websockets.connect(URL) as ws:
        await ws.send(json.dumps({"v": 1, "type": "hb"}))   # ПЕРВЫЙ кадр — hb
        ack = json.loads(await asyncio_wait(ws))
        ok = ack.get("ok") is True and ack.get("type") == "hello_ack"
    await _asleep(0.2)
    return ok, json.dumps(ack, ensure_ascii=False)[:120]


async def asyncio_wait(ws):
    import asyncio
    return await asyncio.wait_for(ws.recv(), 5)


async def _asleep(s):
    import asyncio
    await asyncio.sleep(s)


def main():
    import asyncio

    print("== SK-46 живая приёмка (прод :8791) ==", flush=True)

    # S0 стенд
    active = sh("systemctl --user is-active sidekick-bridge").stdout.strip() == "active"
    agentd = sh("systemctl --user is-active sidekick-agentd").stdout.strip() == "active"
    h0 = health()
    note("S0 мост активен, агентд жив, расширение подключено",
         active and agentd and h0.get("extension") == "connected",
         f"ext={h0.get('extension')} peer={h0.get('peer')}")
    if not (active and agentd):
        print("стенд не поднят — приёмка невозможна"); return 1

    # S1 SIGKILL как харнесс-зачистка 22.10
    pid0 = main_pid()
    ev_n0 = len(events())
    t_kill = time.time()
    os.kill(pid0, 9)
    print(f"  … SIGKILL pid={pid0}, ждём systemd (RestartSec=5 + backoff)", flush=True)
    ok1 = wait(lambda: main_pid() not in (0, pid0), 45)
    pid1 = main_pid()
    note("S1 SIGKILL -> systemd поднял новый процесс", ok1, f"{pid0} -> {pid1}")

    # S2 событие нечистого выхода
    ok2 = wait(lambda: any(e.get("type") == "bridge_unclean_exit"
                           and (e.get("ts") or 0) >= t_kill - 2
                           for e in events()[ev_n0:]), 15)
    note("S2 events.jsonl: bridge_unclean_exit после килла", ok2)

    # S3 агентд поставил алерт в очередь
    ok3 = wait(lambda: "bridge_unclean_exit" in log_text("agentd.log")
               and "очередь" in log_text("agentd.log"), 15)
    note("S3 agentd.log: алерт в очередь", ok3)

    # S4 канал восстановился сам (расширение пользователя, backoff до 60 с)
    ok4 = wait(lambda: health().get("extension") == "connected", 90)
    note("S4 расширение переподключилось само", ok4, f"peer={health().get('peer')}")

    # S5 метрика флэппинга (до наших handshake'ов)
    rph = health().get("reconnects_per_hour", 99)
    note("S5 reconnects_per_hour < 2 после восстановления", rph < 2, f"rph={rph}")

    # S6 legacy_hello живьём
    ok6, detail6 = asyncio.run(s6_legacy_hello())
    ok6b = "legacy_hello" in log_text("bridge.log")
    note("S6 reconnect hb-первым принят (legacy_hello) + строка в логе",
         ok6 and ok6b, detail6)
    wait(lambda: health().get("extension") == "connected", 90)  # отдаём слот пользователю

    # S7 алерт живьём: сообщение в inbox -> алерт-кадр доходит до человека.
    # outbox мост дренирует тиком 0.1 с, поэтому наблюдаем зеркало chats.jsonl
    # (мост пишет туда каждый agent-кадр из outbox — то, что видит сайдбар).
    chats_p = HUB / "chats.jsonl"
    chats_before = len(chats_p.read_text(encoding="utf-8", errors="replace").splitlines()) \
        if chats_p.exists() else 0
    with open(HUB / "inbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "chat", "role": "user", "chat": "default",
                            "text": "SK-46 приёмка: это тестовое сообщение алерта, ответь одним словом ок"},
                           ensure_ascii=False) + "\n")

    def new_chats():
        if not chats_p.exists():
            return []
        lines = chats_p.read_text(encoding="utf-8", errors="replace").splitlines()[chats_before:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    def scan():
        return any("нечисто" in str(c.get("text", "")) for c in new_chats())
    ok7 = wait(scan, 25)
    alert_line = next((c for c in new_chats() if "нечисто" in str(c.get("text", ""))), None)
    note("S7a алерт в чате по сообщению пользователя", ok7,
         str(alert_line.get("text", ""))[:120] if alert_line else "")
    # порядок: алёрт предшествует любому agent-ответу задачи на это сообщение
    idx_alert = next((i for i, c in enumerate(new_chats())
                      if "нечисто" in str(c.get("text", ""))), None)
    idx_task = next((i for i, c in enumerate(new_chats())
                     if c.get("role") == "agent" and "нечисто" not in str(c.get("text", ""))
                     and str(c.get("text", "")).strip()), None)
    note("S7b алерт идёт раньше ответа задачи", ok7 and idx_alert is not None
         and (idx_task is None or idx_alert < idx_task),
         f"alert@{idx_alert} task@{idx_task}")
    # гасим тестовую задачу
    (HUB / "stop").write_text("1", encoding="utf-8")
    time.sleep(3)

    # S8 штатный restart — без алертов
    ev_n1 = len(events())
    sh("systemctl --user restart sidekick-bridge")
    ok8a = wait(lambda: health().get("bridge") == "active", 30)
    time.sleep(1.5)
    new_unclean = [e for e in events()[ev_n1:] if e.get("type") == "bridge_unclean_exit"]
    note("S8 systemctl restart (graceful) НЕ даёт события", ok8a and not new_unclean,
         f"новых событий: {len(new_unclean)}")
    wait(lambda: health().get("extension") == "connected", 90)

    # S9 юнит-свойства
    props = {p: unit_prop(p) for p in
             ("OOMPolicy", "Restart", "RestartUSec", "RestartMaxDelayUSec", "OOMScoreAdjust")}
    ok9 = (props["OOMPolicy"] == "continue" and props["Restart"] == "on-failure"
           and props["RestartUSec"] == "5s" and props["RestartMaxDelayUSec"] == "30s"
           and props["OOMScoreAdjust"] == "-250")
    note("S9 юнит: continue/on-failure/5..30s/-250", ok9, str(props))

    # S10 флэппинг в простое: дельта счётчика за 60 с = 0
    a = health().get("reconnects_per_hour", -1)
    time.sleep(60)
    b = health().get("reconnects_per_hour", -1)
    note("S10 в простое 60 с метрика не растёт (нет флэппинга)", b == a, f"{a} -> {b}")

    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n== ИТОГ: {len(RESULTS) - len(bad)}/{len(RESULTS)} OK ==", flush=True)
    for n in bad:
        print("  FAIL:", n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
