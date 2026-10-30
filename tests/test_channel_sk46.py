"""SK-46 — стабильность канала: legacy_hello, reconnects_per_hour, unclean-exit.

Аудит №2 (22.10), проблема 5: мост дважды убили SIGKILL за день; старые сборки
расширения при переподключении гоняли hb раньше hello и ловили 4001-флэппинг
(12 переходов ext_online↔offline за 70 минут).

Три слоя контракта:
  1. Мост: hello остаётся основным; hb-первым принимается как legacy_hello
     только с IP, завершившего token-handshake в окне SIDEKICK_LEGACY_HELLO_WINDOW_S
     (пары переживают рестарт через .paired_ips). health.json несёт
     reconnects_per_hour; ран без clean-exit метки — событие bridge_unclean_exit.
  2. Расширение: structural hello-first — send() живёт только для promote'нутого
     (_handshaked) сокета; alarm шлёт hb только по нему же. Юнит под node с
     mock WebSocket (блоки ws-client/ws-alarm помечены маркерами).
  3. systemd: OOMPolicy=continue + Restart=on-failure + backoff — дроп-ин в
     репозитории, тест сверяет с установленным (дрейф = красный тест).

Хаб фикстуры `bridge` генерит токен сам (conftest.start_bridge ждёт token-файл) —
тесты штатного handshake читают его через tok(), а не константой.
"""
import asyncio
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess

import pytest
import websockets

from conftest import ROOT, kill_bridge, start_bridge, token_of  # noqa: F401

from conftest import bridge  # noqa: F401  (pytest-фикстура)

PORT = 8799
TOKEN = "sk-test-0123456789abcdef-0123456789abcdef"


def url():
    return f"ws://127.0.0.1:{PORT}/ws"


def tok(home):
    return token_of(pathlib.Path(home))


async def recv(ws):
    return await asyncio.wait_for(ws.recv(), 5)


async def recv_type(ws, t):
    """Пропускает попутные кадры (state/TASK_STATE replay при коннекте) и
    ждёт кадр нужного типа; чужой close честно всплывает ConnectionClosed."""
    while True:
        m = json.loads(await recv(ws))
        if m.get("type") == t:
            return m


async def alive(ws):
    """hb односторонний (мост его молча потребляет, ack не предусмотрен
    контрактом) — живость сокета проверяем audit_get: дешёвый ответ моста."""
    await ws.send(json.dumps({"v": 1, "type": "audit_get", "limit": 1}))
    return await recv_type(ws, "audit_data")


async def hs(ws, tok_=TOKEN):
    """Штатный handshake hello+token; возвращает ack."""
    await ws.send(json.dumps({"v": 1, "type": "hello", "token": tok_,
                              "ext": "hermes-sidekick", "ver": "0.2"}))
    return json.loads(await recv(ws))


def events(home):
    p = pathlib.Path(home) / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def health(home):
    return json.loads((pathlib.Path(home) / "health.json").read_text(encoding="utf-8"))


def fresh_home(tmp_path, name="hub", with_token=True):
    home = tmp_path / name
    home.mkdir()
    if with_token:
        (home / "token").write_text(TOKEN, encoding="utf-8")
    return home


# ------------------------------------------------------------- legacy_hello ---

def test_hello_first_still_primary(bridge):
    async def _t():
        """Основной путь не тронут: hello+token → ack ok, hb работает."""
        async with websockets.connect(url()) as ws:
            ack = await hs(ws, tok(bridge))
            assert ack["ok"] is True and ack.get("v") == 1
            await ws.send(json.dumps({"v": 1, "type": "hb"}))   # hb: consume, без ack (контракт)
            assert (await alive(ws))["type"] == "audit_data"    # сокет жив
    asyncio.run(_t())




def test_legacy_hello_accepted(bridge):
    async def _t():
        """После успешного handshake тот же IP может reconnect'нуться hb-первым
        (старые сборки, шторм 22.10) — принимается, канал работает дальше."""
        async with websockets.connect(url()) as ws:
            assert (await hs(ws, tok(bridge)))["ok"] is True
        await asyncio.sleep(0.2)
        async with websockets.connect(url()) as ws:
            await ws.send(json.dumps({"v": 1, "type": "hb"}))       # ПЕРВЫЙ кадр — hb
            ack = json.loads(await recv(ws))
            assert ack["ok"] is True and ack["type"] == "hello_ack", ack
            assert ack.get("v") == 1                                  # контракт v1 жив
            await ws.send(json.dumps({"v": 1, "type": "hb"}))   # hb: consume, без ack (контракт)
            assert (await alive(ws))["type"] == "audit_data"    # сокет жив
    asyncio.run(_t())




def test_hb_first_unpaired_rejected(tmp_path):
    async def _t():
        """Свежий хаб, IP ещё не завершал handshake — отказ молчаливый (SK-41:
        RST → 1006, без опознавательного reason). legacy_hello не создаёт
        token-free путь для неподключённых машин."""
        home = fresh_home(tmp_path, with_token=False)
        p = start_bridge(home)
        try:
            async with websockets.connect(url()) as ws:
                await ws.send(json.dumps({"v": 1, "type": "hb"}))
                with pytest.raises(websockets.ConnectionClosed) as ei:
                    await recv(ws)
                assert ei.value.code == 1006
        finally:
            kill_bridge(p)
    asyncio.run(_t())




def test_legacy_window_zero_disables(tmp_path):
    async def _t():
        """SIDEKICK_LEGACY_HELLO_WINDOW_S=0 — фича полностью выключена: даже
        спаренный IP с hb-первым получает отказ (SK-41: молчаливый RST → 1006)."""
        home = fresh_home(tmp_path)
        p = start_bridge(home, env_extra={"SIDEKICK_LEGACY_HELLO_WINDOW_S": "0"})
        try:
            async with websockets.connect(url()) as ws:
                assert (await hs(ws))["ok"] is True
            await asyncio.sleep(0.2)
            async with websockets.connect(url()) as ws:
                await ws.send(json.dumps({"v": 1, "type": "hb"}))
                with pytest.raises(websockets.ConnectionClosed) as ei:
                    await recv(ws)
                assert ei.value.code == 1006
        finally:
            kill_bridge(p)
    asyncio.run(_t())




def test_legacy_wrong_version_rejected(bridge):
    async def _t():
        """legacy_hello уважает версию: hb c v=99 первым → ack bad_version + 4002."""
        async with websockets.connect(url()) as ws:
            assert (await hs(ws, tok(bridge)))["ok"] is True
        await asyncio.sleep(0.2)
        async with websockets.connect(url()) as ws:
            await ws.send(json.dumps({"v": 99, "type": "hb"}))
            ack = json.loads(await recv(ws))
            assert ack["ok"] is False and ack.get("err") == "bad_version", ack
            with pytest.raises(websockets.ConnectionClosed) as ei:
                await recv(ws)
            assert ei.value.code == 4002
    asyncio.run(_t())




def test_legacy_wrong_token_still_rejected(bridge):
    async def _t():
        """legacy_hello не принимает НИКАКОЙ первый кадр — только hb. hello с
        чужим токеном первым остаётся hello-путём (bad token → ack + 4003)."""
        async with websockets.connect(url()) as ws:
            assert (await hs(ws, tok(bridge)))["ok"] is True
        await asyncio.sleep(0.2)
        async with websockets.connect(url()) as ws:
            await ws.send(json.dumps({"v": 1, "type": "hello", "token": "wrong",
                                      "ext": "x", "ver": "0.2"}))
            ack = json.loads(await recv(ws))
            assert ack["ok"] is False and ack.get("err") == "bad_token", ack
            with pytest.raises(websockets.ConnectionClosed) as ei:
                await recv(ws)
            assert ei.value.code == 4003
    asyncio.run(_t())




def test_paired_ips_survive_restart(tmp_path):
    async def _t():
        """.paired_ips переживает рестарт моста: после SIGTERM-остановки и подъёма
        на том же хабе reconnect hb-первым принимается без нового hello."""
        home = fresh_home(tmp_path)
        p = start_bridge(home)
        try:
            async with websockets.connect(url()) as ws:
                assert (await hs(ws))["ok"] is True
        finally:
            kill_bridge(p)
        assert (home / ".paired_ips").exists(), "память пар не передалась на диск"
        await asyncio.sleep(0.5)
        p2 = start_bridge(home)
        try:
            async with websockets.connect(url()) as ws:
                await ws.send(json.dumps({"v": 1, "type": "hb"}))
                ack = json.loads(await recv(ws))
                assert ack["ok"] is True, "после рестарта legacy_hello не принят"
        finally:
            kill_bridge(p2)
    asyncio.run(_t())




# ------------------------------------------------- reconnects_per_hour -------

def test_reconnects_per_hour_metric(bridge):
    async def _t():
        """Каждый успешный handshake — тик метики; флэппинг 12-раз за час виден
        числом в health.json, а не грепом логов."""
        assert health(bridge)["reconnects_per_hour"] == 0
        async with websockets.connect(url()) as ws:
            assert (await hs(ws, tok(bridge)))["ok"] is True
        assert health(bridge)["reconnects_per_hour"] == 1
        async with websockets.connect(url()) as ws:
            assert (await hs(ws, tok(bridge)))["ok"] is True
        assert health(bridge)["reconnects_per_hour"] == 2
        # legacy_hello тоже тикает (это такой же reconnect)
        async with websockets.connect(url()) as ws:
            await ws.send(json.dumps({"v": 1, "type": "hb"}))
            assert json.loads(await recv(ws))["ok"] is True
        assert health(bridge)["reconnects_per_hour"] == 3
    asyncio.run(_t())




def test_rejected_handshake_not_counted(bridge):
    async def _t():
        """Метрика считает успешные handshake, не отказы (иначе шторм отказов
        искажал бы метрику реконнектов)."""
        async with websockets.connect(url()) as ws:
            await ws.send(json.dumps({"v": 1, "type": "hello", "token": "wrong",
                                      "ext": "x", "ver": "0.2"}))
            ack = json.loads(await recv(ws))          # ack приходит перед закрытием
            assert ack["ok"] is False
            with pytest.raises(websockets.ConnectionClosed):
                await recv(ws)
        assert health(bridge)["reconnects_per_hour"] == 0
    asyncio.run(_t())




# ------------------------------------------------------ unclean exit ---------

def test_unclean_exit_event(tmp_path):
    async def _t():
        """SIGKILL без clean-exit метки → следующий старт пишет bridge_unclean_exit
        в events.jsonl (агентд превратит в алерт чата) и помечает метку съеденной.
        Пре-записанный токен = «хоб не первый ран», поэтому первое событие
        легитимно уже на первом старте теста — считаем прирост относительно него."""
        home = fresh_home(tmp_path)
        p = start_bridge(home)
        try:
            await asyncio.sleep(0.5)
            base = len([e for e in events(home) if e.get("type") == "bridge_unclean_exit"])
            assert base == 1, "пре-записанный токен без метки = прошлый ран мёртв — ждём 1 событие"
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)   # харнесс-зачистка «как есть»
            p.wait(timeout=10)
            await asyncio.sleep(0.5)
            p2 = start_bridge(home)
            try:
                await asyncio.sleep(0.5)
                evs = [e for e in events(home) if e.get("type") == "bridge_unclean_exit"]
                assert len(evs) == base + 1, f"событий нечистого выхода: {evs}"
                assert evs[-1].get("ts")
                assert not (home / ".clean_exit").exists(), "метка не израсходована"
            finally:
                kill_bridge(p2)
        finally:
            kill_bridge(p)          # после SIGKILL уже мёртв — kill_bridge честно проглотит
    asyncio.run(_t())




def test_clean_exit_no_event(tmp_path):
    async def _t():
        """SIGTERM (systemd stop/restart) — чистый выход: метка есть, следующий
        ран без события. Ложных алертов на штатные рестарты быть не должно."""
        home = fresh_home(tmp_path)
        p = start_bridge(home)
        await asyncio.sleep(0.5)
        # пре-записанный токен = «не первый ран»: событие на первом старте
        # легитимно — важно, чтобы штатный SIGTERM-рестарт НЕ добавил нового
        base = len([e for e in events(home) if e.get("type") == "bridge_unclean_exit"])
        kill_bridge(p)                                # SIGTERM → graceful
        assert (home / ".clean_exit").exists(), "graceful-метка не записана"
        await asyncio.sleep(0.5)
        p2 = start_bridge(home)
        try:
            await asyncio.sleep(0.5)
            assert len([e for e in events(home)
                        if e.get("type") == "bridge_unclean_exit"]) == base, "штатный рестарт дал алерт"
            assert not (home / ".clean_exit").exists(), "метка не израсходована"
        finally:
            kill_bridge(p2)
    asyncio.run(_t())




def test_stale_paused_removed_on_start(tmp_path):
    async def _t():
        """PAUSED — флаг живого hand-off; процесс умер — флаг врал бы (задачи
        вечно становились бы «на паузе»). Старт обязан его сожрать."""
        home = fresh_home(tmp_path)
        (home / "paused").write_text("1", encoding="utf-8")   # имя — как у PAUSED-константы моста
        p = start_bridge(home)
        try:
            await asyncio.sleep(0.5)
            assert not (home / "paused").exists()
        finally:
            kill_bridge(p)
    asyncio.run(_t())




# ------------------------------------------------------------- systemd -------

REPO_DROPIN = pathlib.Path(ROOT) / "bridge" / "sidekick-bridge.service.d" / "sk46-stability.conf"
INSTALLED_DROPIN = (pathlib.Path(os.environ.get("HOME", os.path.expanduser("~")))
                    / ".config/systemd/user/sidekick-bridge.service.d"
                    / "sk46-stability.conf")


def test_dropin_in_repo_and_installed():
    """Дроп-ин лежит в репозитории (источник истины) и установлен; установленный
    обязан совпадать — рассинхон юнита = красный тест, а не «само рассосётся»."""
    assert REPO_DROPIN.exists(), "дроп-ин пропал из репозитория"
    txt = REPO_DROPIN.read_text(encoding="utf-8")
    assert "OOMPolicy=continue" in txt
    assert "Restart=on-failure" in txt
    assert re.search(r"RestartSec=\d+", txt) and re.search(r"RestartMaxDelaySec=\d+", txt), \
        "нет экспоненциального backoff (RestartSec + RestartMaxDelaySec)"
    if INSTALLED_DROPIN.exists():
        assert INSTALLED_DROPIN.read_text(encoding="utf-8") == txt, \
            "установленный дроп-ин отличается от репозитория — переустановить"


# ------------------------------------------------- structural hello-first ----

BG = pathlib.Path(ROOT, "extension", "background.js").read_text(encoding="utf-8")
AGENTD = pathlib.Path(ROOT, "agentd", "sidekick_agentd.py").read_text(encoding="utf-8")
BRIDGE_SRC = pathlib.Path(ROOT, "bridge", "sidekick_bridge.py").read_text(encoding="utf-8")


def test_source_contract():
    """Мост: legacy_hello и метрики на месте. Расширение: send() и alarm
    защищены _handshaked — «hb раньше hello» структурно невозможно. Агентд:
    bridge_unclean_exit обработан."""
    assert "legacy_hello" in BRIDGE_SRC
    assert "reconnects_per_hour" in BRIDGE_SRC
    assert "bridge_unclean_exit" in BRIDGE_SRC
    assert "bridge_unclean_exit" in AGENTD
    assert "pending_bridge_alert" in AGENTD
    assert "ws._handshaked" in BG
    assert "sock._handshaked = true" in BG


# ------------------------------------------------ node-юнит клиента ----------

CLIENT_RE = re.compile(r"// SK-46-BEGIN ws-client.*?// SK-46-END ws-client", re.S)
ALARM_RE = re.compile(r"// SK-46-BEGIN ws-alarm.*?// SK-46-END ws-alarm", re.S)


def test_ws_client_unit_under_node():
    """Блоки ws-client/ws-alarm под node с mock WebSocket:
      S1 hello — единственный кадр до ack;
      S2 open-but-not-acked: alarm не шлёт hb и не поднимает второй сокет;
      S3 send() до ack — false, ни одного кадра;
      S4 после ack: alarm шлёт hb;
      S5 реконнект: новый сокет, снова hello первым;
      S6 ack ok:false → сокет закрыт, статус token_rejected."""
    m, a = CLIENT_RE.search(BG), ALARM_RE.search(BG)
    assert m and a, "маркеры SK-46-BEGIN/END исчезли из background.js"
    node = shutil.which("node")
    if not node:
        pytest.skip("node отсутствует")
    script = (
        "class MockWS {\n"
        "  static inst = [];\n"
        "  constructor(u){ this.url=u; this.readyState=0; this.sent=[]; this.closed=null;\n"
        "    this.onopen=this.onmessage=this.onclose=this.onerror=null; MockWS.inst.push(this); }\n"
        "  send(s){ if(this.readyState!==1) throw new Error('send@'+this.readyState); this.sent.push(JSON.parse(s)); }\n"
        "  close(c){ if(this.readyState>1) return; this.readyState=3; this.closed=c??1006;\n"
        "    if(this.onclose) this.onclose({code:this.closed}); }\n"
        "  _open(){ this.readyState=1; if(this.onopen) this.onopen(); }\n"
        "  _msg(o){ if(this.onmessage) this.onmessage({data:JSON.stringify(o)}); }\n"
        "}\n"
        "globalThis.WebSocket = MockWS;\n"
        "globalThis.ws=null; globalThis.wsUrl=''; globalThis.backoff=2000;\n"
        "globalThis.optionsShown=false; globalThis.trying=false; globalThis.failStreak=0;\n"
        "globalThis.BRIDGE_ST='offline'; globalThis.AGST='unknown';\n"
        "globalThis.PROTO_V=1; globalThis.FALLBACK_SERVER='ws://test';\n"
        "globalThis.BACKOFF_MIN=2000; globalThis.BACKOFF_MAX=60000;\n"
        "globalThis.REPLACED_BACKOFF_MS=15000; globalThis.OPEN_TIMEOUT_MS=4000; globalThis.ACK_TIMEOUT_MS=1500;\n"
        "globalThis.__statuses=[]; globalThis.__opts=0;\n"
        "globalThis.log=()=>{}; globalThis.badge=()=>{}; globalThis.sendToUi=()=>{}; globalThis.route=()=>{};\n"
        "globalThis.status=(t)=>{ BRIDGE_ST=t; __statuses.push(t); };\n"
        "globalThis.agState=(s)=>{ AGST=s; };\n"
        "globalThis.getToken=async()=>'tok'; globalThis.serverList=async()=>['ws://test'];\n"
        "globalThis.browser={ alarms:{ create(){}, onAlarm:{ addListener(cb){ globalThis.__alarm=cb; } } },\n"
        "  runtime:{ openOptionsPage(){ __opts++; return Promise.resolve(); }, onMessage:{ addListener(){} } } };\n"
        + m.group(0) + "\n" + a.group(0) + "\n"
        "const tick=()=>new Promise(r=>setTimeout(r,0));\n"
        "const T=(c,m)=>{ if(!c){ console.log('FAIL',m); process.exit(1); } };\n"
        "async function main(){\n"
        "  // S1: hello — единственный кадр до ack\n"
        "  connect(); for(let i=0;i<30&&!MockWS.inst.length;i++) await tick();\n"
        "  const s1=MockWS.inst[0]; T(s1,'нет сокета'); s1._open();\n"
        "  for(let i=0;i<10;i++) await tick();\n"
        "  T(s1.sent.length===1 && s1.sent[0].type==='hello','S1: до ack отправлено не hello: '+JSON.stringify(s1.sent));\n"
        "  T(ws===null,'S1: ws promoted до ack');\n"
        "  // S2: open-but-not-acked: alarm — ни hb, ни второго сокета\n"
        "  __alarm(); await tick();\n"
        "  T(s1.sent.length===1,'S2: hb ушёл до ack');\n"
        "  T(MockWS.inst.length===1,'S2: alarm поднял второй сокет');\n"
        "  // S3: send() до ack — отказ\n"
        "  T(send({type:'hb'})===false,'S3: send() пропустил кадр до ack');\n"
        "  T(s1.sent.length===1,'S3: кадр всё же ушёл');\n"
        "  // S4: ack ok → promote → hb по alarm\n"
        "  s1._msg({ok:true,type:'hello_ack',v:1}); await tick();\n"
        "  T(ws===s1 && s1._handshaked===true,'S4: не promoted после ack');\n"
        "  __alarm(); await tick();\n"
        "  T(s1.sent.length===2 && s1.sent[1].type==='hb','S4: hb после ack не ушёл');\n"
        "  // S5: реконнект — новый сокет, снова hello первым\n"
        "  s1.close(1006); await tick();\n"
        "  __alarm(); for(let i=0;i<30&&MockWS.inst.length<2;i++) await tick();\n"
        "  const s2=MockWS.inst[1]; T(s2,'S5: нет второго сокета'); s2._open();\n"
        "  for(let i=0;i<10;i++) await tick();\n"
        "  T(s2.sent.length===1 && s2.sent[0].type==='hello','S5: реконнект не с hello: '+JSON.stringify(s2.sent));\n"
        "  // S6: ack ok:false сам ничего не решает; вердикт — КОД ЗАКРЫТИЯ от моста\n"
        "  s2._msg({ok:false,type:'hello_ack'}); await tick();\n"
        "  T(s2.closed===null && !__statuses.includes('token_rejected'),'S6: поспешил вердиктом по ack');\n"
        "  s2.close(4003); await tick();\n"
        "  T(__statuses.includes('token_rejected'),'S6: нет статуса token_rejected по коду 4003');\n"
        "  console.log('OK');\n"
        "}\n"
        "main().then(()=>process.exit(0)).catch(e=>{ console.log('ERR',e&&e.message); process.exit(1); });\n"
    )
    r = subprocess.run([node, "-e", script],
                       capture_output=True, text=True, timeout=30)
    assert "OK" in r.stdout, f"node: {r.stdout}{r.stderr}"
