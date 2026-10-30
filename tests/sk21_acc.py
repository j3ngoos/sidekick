#!/usr/bin/env python3
"""SK-21 приёмка: живой прогон sidekick-agentd через прод-мост :8791.

Клиентом WS входим в мост, шлём chat-рамки (мост сам допишет inbox.jsonl —
путь реального сайдбара), параллельно захватываем широковещательные outbox-
кадры с таймстампами. Сценарии карточки: латентность первого кадра, think до
финала, очередь двух сообщений, hub/stop, рестарт юнита посреди задачи,
бэклолог из 3.

Запуск: .venv/bin/python tests/sk21_acc.py <s1|s2|s3|s4|s5|all>
Результат: JSON в /tmp/sk21-acc/<scenario>.json + печать.
"""
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time

import websockets

HUB = pathlib.Path(os.environ.get("SIDEKICK_REPO", str(pathlib.Path.home() / "sidekick"))) / "bridge" / "hub"
TOKEN = (HUB / "token").read_text().strip()
URL = "ws://127.0.0.1:8791"
OUT = pathlib.Path("/tmp/sk21-acc")
OUT.mkdir(exist_ok=True)


class Capture:
    def __init__(self):
        self.frames = []          # (ts, frame)
        self.ws = None
        self._task = None

    async def start(self):
        self.ws = await websockets.connect(URL, max_size=2 ** 23)
        await self.ws.send(json.dumps({"v": 1, "type": "hello", "token": TOKEN}))
        ack = json.loads(await asyncio.wait_for(self.ws.recv(), 5))
        assert ack.get("ok"), f"handshake failed: {ack}"

        async def pump():
            try:
                async for raw in self.ws:
                    try:
                        self.frames.append((time.time(), json.loads(raw)))
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass
        self._task = asyncio.create_task(pump())

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self.ws:
            await self.ws.close()

    def since(self, ts):
        return [(t, f) for t, f in self.frames if t >= ts]

    def task_for(self, marker, ts):
        """task-frame с title, содержащим marker, после ts -> (ts, tid)."""
        for t, f in self.since(ts):
            if f.get("type") == "task" and marker in str(f.get("title", "")):
                return t, f["id"]
        return None, None

    def frames_for(self, tid, ts=0.0):
        return [(t, f) for t, f in self.since(ts) if f.get("id") == tid or f.get("task") == tid]

    async def wait_task_done(self, tid, ts, timeout):
        end = time.time() + timeout
        while time.time() < end:
            for t, f in self.frames_for(tid, ts):
                if f.get("type") == "task" and f.get("state") in ("done", "failed"):
                    return t, f
            await asyncio.sleep(0.2)
        return None, None


def metrics(cap, marker, ts_send, tid):
    fr = cap.frames_for(tid, ts_send)
    first = fr[0][0] if fr else None
    thinks = [(t, f) for t, f in fr if f.get("type") == "think"]
    chats = [(t, f) for t, f in fr if f.get("type") == "chat"]
    steps = [(t, f) for t, f in fr if f.get("type") == "step"]
    term = [(t, f) for t, f in fr if f.get("type") == "task" and f.get("state") in ("done", "failed")]
    return {
        "marker": marker,
        "tid": tid,
        "first_frame_s": round(first - ts_send, 2) if first else None,
        "first_think_s": round(thinks[0][0] - ts_send, 2) if thinks else None,
        "think_frames": len(thinks),
        "think_before_final_chat": bool(thinks and chats and thinks[0][0] < chats[-1][0]),
        "steps": [{"n": f.get("n"), "text": f.get("text"), "state": f.get("state")}
                  for _, f in steps if f.get("state") != "queued"],
        "final_chat": chats[-1][1].get("text", "")[:300] if chats else None,
        "final_chat_s": round(chats[-1][0] - ts_send, 2) if chats else None,
        "task_state": term[-1][1].get("state") if term else None,
        "task_done_s": round(term[-1][0] - ts_send, 2) if term else None,
    }


async def send(cap, text):
    ts = time.time()
    await cap.ws.send(json.dumps({"v": 1, "type": "chat", "role": "user", "text": text}))
    return ts


async def wait_tid(cap, marker, ts, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        t, tid = cap.task_for(marker, ts)
        if tid:
            return t, tid
        await asyncio.sleep(0.1)
    return None, None


def save(name, res):
    (OUT / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False, indent=1))


def systemctl(*args):
    subprocess.run(["systemctl", "--user", *args], check=True)


async def s1():
    cap = Capture()
    await cap.start()
    text = "SK21-S1 маркер-альфа: посчитай в уме 17*23, сначала подумай вслух, потом ответь одним числом"
    ts = await send(cap, text)
    t_first, tid = await wait_tid(cap, "SK21-S1", ts)
    assert tid, "нет task-кадра за 30 с"
    await cap.wait_task_done(tid, ts, 240)
    m = metrics(cap, "SK21-S1", ts, tid)
    m["check_first_le_5s"] = m["first_frame_s"] is not None and m["first_frame_s"] <= 5
    m["check_think_deltas_before_chat"] = m["think_before_final_chat"]
    m["check_final_chat_present"] = bool(m["final_chat"])
    m["check_steps_present"] = len(m["steps"]) >= 1
    await cap.stop()
    save("s1", m)


async def s2():
    cap = Capture()
    await cap.start()
    t1 = await send(cap, "SK21-S2A маркер-бриavo: скажи вслух слово А-готово (подумай перед ответом)")
    await asyncio.sleep(3)
    t2 = await send(cap, "SK21-S2B маркер-бриavo: скажи вслух слово Б-готово")
    _, tid1 = await wait_tid(cap, "SK21-S2A", t1)
    _, tid2 = await wait_tid(cap, "SK21-S2B", t1)
    assert tid1 and tid2, f"tid1={tid1} tid2={tid2}"
    # второе не начало исполняться раньше финала первого: очередь
    r1 = await cap.wait_task_done(tid1, t1, 300)
    r2 = await cap.wait_task_done(tid2, t1, 300)
    m1, m2 = metrics(cap, "S2A", t1, tid1), metrics(cap, "S2B", t1, tid2)
    start2 = min((t for t, f in cap.frames_for(tid2, t1)), default=None)
    m2["started_after_first_finished"] = bool(
        start2 and r1[0] and start2 >= r1[0] - 1.0)
    res = {"first": m1, "second": m2,
           "check_both_processed": bool(m1["final_chat"] and m2["final_chat"]),
           "check_queue_serial": m2["started_after_first_finished"]}
    await cap.stop()
    save("s2", res)


async def s3():
    cap = Capture()
    await cap.start()
    text = ("SK21-S3 маркер-чарли: очень длинная задача — пересчитывай словами числа от 1 до 200, "
            "каждое на новой строке, это стресс-тест, не останавливайся")
    ts = await send(cap, text)
    _, tid = await wait_tid(cap, "SK21-S3", ts)
    assert tid
    # ждём, пока задача реально в работе (think-поток пошёл), и жмём ■ Stop
    end = time.time() + 60
    while time.time() < end:
        if any(f.get("type") == "think" for _, f in cap.frames_for(tid, ts)):
            break
        await asyncio.sleep(0.2)
    (HUB / "stop").touch()
    t_stop = time.time()
    t_done, frame = await cap.wait_task_done(tid, ts, 60)
    m = metrics(cap, "S3", ts, tid)
    m["stop_to_failed_s"] = round(t_done - t_stop, 2) if t_done else None
    m["check_task_failed"] = (frame or {}).get("state") == "failed"
    m["check_stop_consumed"] = not (HUB / "stop").exists()
    m["check_daemon_alive"] = subprocess.run(
        ["systemctl", "--user", "is-active", "sidekick-agentd"],
        capture_output=True, text=True).stdout.strip() == "active"
    await cap.stop()
    save("s3", m)


async def s4():
    cap = Capture()
    await cap.start()
    text = ("SK21-S4 маркер-дельта: длинная задача — пересчитывай словами числа от 1 до 150, "
            "каждое на новой строке")
    ts = await send(cap, text)
    _, tid = await wait_tid(cap, "SK21-S4 маркер", ts)
    assert tid
    end = time.time() + 60
    while time.time() < end:
        if any(f.get("type") in ("think", "step") for _, f in cap.frames_for(tid, ts)):
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(2)
    t_restart = time.time()
    systemctl("restart", "sidekick-agentd")
    await asyncio.sleep(3)
    await cap.stop()          # мост мог отвалить WS при рестарте — переподключаемся
    await cap.start()
    t2 = await send(cap, "SK21-S4B маркер-дельта: скажи слово после-рестарта-ок")
    # прерванная задача: курсор не коммитился → после рестарта перечитывается (re-pickup)
    await asyncio.sleep(3)
    cands = [f for _, f in cap.since(t_restart) if f.get("type") == "task"
             and "SK21-S4 маркер" in str(f.get("title", ""))]
    tid_rep = cands[-1]["id"] if cands else None
    # развязка очереди: ■ Stop гасит re-picked задачу, очередь едет дальше к S4B
    (HUB / "stop").touch()
    _, tid_b = await wait_tid(cap, "SK21-S4B", t2, 90)
    assert tid_b, "после рестарта и развязки следующее сообщение не обработано"
    await cap.wait_task_done(tid_b, t2, 240)
    mb = metrics(cap, "S4B", t2, tid_b)
    res = {"after_restart": mb,
           "check_next_message_processed": bool(mb["final_chat"]),
           "interrupted_reprocessed_tid": tid_rep,
           "check_interrupted_repickup": bool(tid_rep)}
    await cap.stop()
    save("s4", res)


async def s5():
    systemctl("stop", "sidekick-agentd")
    cap = Capture()
    await cap.start()
    ts = {}
    for i, mk in enumerate(["SK21-S5-один", "SK21-S5-два", "SK21-S5-три"]):
        ts[mk] = await send(cap, f"{mk} маркер-эхо: запомни слово {i+1} и ответь «принято {i+1}»")
        await asyncio.sleep(0.5)
    await cap.stop()
    systemctl("start", "sidekick-agentd")
    await asyncio.sleep(1)
    cap2 = Capture()
    await cap2.start()
    # ждём три финальных chat с маркерами (общий таймер — на весь бэклолог)
    order = []
    end = time.time() + 420
    seen = {}
    while time.time() < end and len(order) < 3:
        for t, f in cap2.frames:
            if f.get("type") == "task" and "SK21-S5" in str(f.get("title", "")):
                seen.setdefault(f["id"], f["title"])
        for tid, title in list(seen.items()):
            if tid not in [o[0] for o in order]:
                td, fr = await cap2.wait_task_done(tid, 0, 1)
                if fr:
                    ch = [f for _, f in cap2.frames_for(tid) if f.get("type") == "chat"]
                    order.append((tid, title, fr.get("state"), ch[-1].get("text", "")[:80] if ch else None))
        await asyncio.sleep(0.5)
    await cap2.stop()
    titles = [t for _, t, _, _ in order]
    exp = ["один", "два", "три"]
    res = {"tasks": order, "check_all_three": len(order) == 3,
           "check_order": [any(e in t for t in titles) for e in exp] == [True] * 3
           and [next((i for i, t in enumerate(titles) if e in t), 99) for e in exp] == [0, 1, 2]}
    save("s5", res)


SCEN = {"s1": s1, "s2": s2, "s3": s3, "s4": s4, "s5": s5}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(SCEN) if which == "all" else [which]
    for n in names:
        print(f"===== {n} =====", flush=True)
        asyncio.run(SCEN[n]())
