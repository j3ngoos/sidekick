"""sidekick-think — стриминг «мыслей» агента для sidekick-agentd (SK-21).

Пишет reasoning-дельты потока модели в файл, путь которого даёт родительский
процесс через SIDEKICK_THINK_FILE (agentd ставит env на свой временный файл
для каждого запуска). Без env плагин — no-op: обычные запуски профиля
(sidekick-agent cron до паузы, ручные отладочные сессии) не пишут ничего.

Требует plugins.stream_reasoning_deltas: true в конфиге профиля (иначе Hermes
не кладёт reasoning-дельты в очередь хука). Аддитивно: контракт моста не
трогает, файл трогает только этот плагин и агентд-читатель.
"""
from __future__ import annotations

import json
import os
import threading
import time

_lock = threading.Lock()
_stream_seen: set[str] = set()


def _emit(kind: str, payload: dict) -> None:
    path = os.environ.get("SIDEKICK_THINK_FILE") or ""
    if not path:
        return
    rec = {
        "ts": round(time.time(), 3),
        "kind": kind,
        "session_id": payload.get("session_id", ""),
        "turn_id": payload.get("turn_id", ""),
        "iteration": payload.get("iteration", 0),
        "delta": payload.get("delta", ""),
    }
    try:
        line = json.dumps(rec, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    try:
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
    except OSError:
        pass  # best-effort наблюдатель: файловая проблема не должна ронять стрим


def _on_stream_delta(**payload) -> None:
    if payload.get("kind") == "reasoning":
        _emit("delta", payload)


def _on_stream_start(**payload) -> None:
    _emit("stream_start", payload)


def _on_stream_end(**payload) -> None:
    # final_text в payload — вся видимая ответа; в think-ленту не пишем (финал
    # идёт из result-события stream-json), но маркер конца потока полезен
    # читателю для «итерация N закончилась».
    _emit("stream_end", payload)


def register(ctx) -> None:
    ctx.register_hook("on_stream_start", _on_stream_start)
    ctx.register_hook("on_stream_delta", _on_stream_delta)
    ctx.register_hook("on_stream_end", _on_stream_end)
