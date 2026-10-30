"""Bridge test harness: a fresh bridge process per test on SIDEKICK_PORT=8799, tmp hub home.

Per runbook «Параметризация для тестов»: tests never touch the prod bridge on :8791.
"""
import os, signal, socket, subprocess, sys, time

import pytest

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8799


def token_of(home):
    return (home / "token").read_text().strip()


def start_bridge(home, env_extra=None):
    # порт должен быть свободен: живой мост предыдущего теста обманывает нижнюю
    # проверку порта (токен-файл пишется на import, bind падает -> CRASH-ретраи),
    # и тест уезжает handshake-отказом в старый процесс
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        except OSError:
            break
        time.sleep(0.1)
    env = dict(os.environ, SIDEKICK_HOME=str(home), SIDEKICK_PORT=str(PORT))
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})
    p = subprocess.Popen([PY, "bridge/sidekick_bridge.py"], cwd=str(ROOT), env=env,
                         start_new_session=True)
    for _ in range(50):
        if (home / "token").exists():
            break
        time.sleep(0.1)
    else:
        kill_bridge(p)
        pytest.fail("bridge did not create the token file")
    for _ in range(50):  # token exists at import time — also wait until the port listens
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        kill_bridge(p)
        pytest.fail(f"bridge did not start listening on :{PORT}")
    return p


def kill_bridge(p):
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        p.wait(timeout=5)


def refire_noext(home, method=None):
    """Гарниз против харнесс-гонки «rpc_in drain раньше подключения мока».

    Тесты пишут паттерн `rid = rpc_call(...); mock_...()`: мост опрашивает rpc_in
    тиком 0.1 с и под нагрузкой успевает ответить no_extension ДО того, как мок
    добавится в CLIENTS. Это ложный отказ: мок подключён и ждёт кадр. Мок-хелперы
    зовут refire: снимаем строку no_extension из rpc_out (иначе read_rpc_out по
    rid вернёт её первой) и перезаряжаем исходный кадр — тот же rid, контракт
    не тронут. Матчинг по id (uuid4 уникален; поля method в строке rpc_out нет —
    оно живёт только в аудите). Ничего не делаем, если ошибки нет: легитимные
    no_extension-тесты (без подключённого мока) сюда не доходят."""
    import json
    import pathlib
    out = pathlib.Path(home) / "rpc_out.jsonl"
    frames_p = pathlib.Path(home) / ".rpc_frames.jsonl"
    if not out.exists() or not frames_p.exists():
        return False
    try:
        frames = [json.loads(l) for l in frames_p.read_text().splitlines() if l.strip()]
    except ValueError:
        return False
    if not frames:
        return False
    last = frames[-1]
    lines = out.read_text().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        try:
            o = json.loads(lines[i])
        except ValueError:
            continue
        if o.get("err") == "no_extension" and o.get("id") == last.get("id"):
            del lines[i]
            out.write_text("".join(l + "\n" for l in lines))
            with open(home / "rpc_in.jsonl", "a") as fh:
                fh.write(json.dumps(last, ensure_ascii=False) + "\n")
            return True
    return False


@pytest.fixture
def bridge(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)
