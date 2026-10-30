#!/usr/bin/env python3
"""SK-48 живая приёмка: пост-чек без зрения (page.media/page.dom checked/post_state) +
верификация handoff-«Готово» ожидаемым состоянием.

Аудит №2 22.10, проблемы 10-11: «Выбрать все» на Ozon — executed:true при незакрытом
чекбоксе (заметили через 8 минут); handoff «нажми Play» → «Готово» → видео на паузе →
задача уехала виртуально выполненной.

Стенд: изолированный мост :8797 + тест-копия расширения (FALLBACK_SERVER->:8797,
fallback-токен) в firefox-158 (Marionette 2929). Прод :8791 не трогаем; в конце —
переустановка prod-аддона. Прецеденты: sk44_acc.py, sk46_acc.py, sk47_acc.py.

Фазы:
  S0  стенд: мост :8797, XPI, httpd :8899 (media.html/noop.html/clip.webm), вкладки
  M1  плейбук «запусти видео» на ЖИВОМ content.js: page.media показывает paused;
      mute→Play (action.do) → page.media paused:false и currentTime растёт; src не
      едет; rect/name/readyState честные; title-канал страницы сходится
  M2  silent-noop на ЖИВОМ content.js (Ozon-страница): page.dom несёт checked у
      тумблеров и не несёт у кнопки; клик по label реального чекбокса — post_state
      changed:true; клик по label Ozon-ловушки (preventDefault) — changed:false +
      waited_ms; aria-switch рабочий/заглушка; обычная кнопка — без post_state
  M3  handoff expect на живом мосту (оркестрация): тест-XPI сносится, харнесс —
      единственный WS-клиент (политика одного клиента, SK-46/SK-49: второй hello
      вытесняет расширение, поэтому лег-скрипт здесь, а живое чтение страницы
      доказано в M1/M2/M4). handoff.request {expect:video_playing} → баннер; resume
      при паузе → мост сам шлёт page.media (~vf1), затем кадр (~shot1) и ВЕРНУВШИЙСЯ
      баннер с note+shot+attempt; rpc агента заблокирован, hub/paused держится;
      handoff.status без shot; второй resume на «заигравшем» → {resumed,verified:true},
      пауза снята; аудит pause/handoff_verify/reverify
  M4  youtube live: page.media на реальном youtube.com — metadata (duration>0),
      геометрия, имя; src нет; попытка плейбука кликом по видео (состояние — в
      детали, автоплей-политика синтетику может не пускать: это и есть причина,
      почему гадать по кадру нельзя, а читать состояние — можно)
  M5  аудит: post_state-записи (changed true и false) grep'ом; handoff_verify
      not_reached+reached; reverify
  Z   уборка: вкладки, тест-XPI -> prod-аддон, :8797 по PID, httpd только если
      поднимали

Запуск:  python3 tests/sk48_acc.py
"""
import asyncio
import base64
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

import websockets

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
S = pathlib.Path(os.environ.get("SK48_SCRATCH", str(HOME / ".hermes/cache/scratch/sk48")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
TEST_PORT = 8899
MEDIA_URL = f"http://127.0.0.1:{TEST_PORT}/media.html?v={os.environ.get('SK48_V', 'sk48c')}"
NOOP_URL = f"http://127.0.0.1:{TEST_PORT}/noop.html"
YT_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
results = {}

sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
from mario import Marionette  # noqa: E402


def note(name, ok, detail=""):
    ok_v = None if ok == "skip" else bool(ok)
    results[name] = {"ok": ok_v, "detail": str(detail)[:1500]}
    print(("PASS " if ok_v else ("SKIP " if ok_v is None else "FAIL ")) + name +
          ("  " + str(detail)[:200] if detail else ""), flush=True)


def listener_pid(port):
    r = subprocess.run(["ss", "-tlnp", f"( sport = :{port} )"], capture_output=True, text=True)
    m2 = re.search(r"pid=(\d+)", r.stdout)
    return int(m2.group(1)) if m2 else None


def ss_count(port):
    r = subprocess.run(["ss", "-tn", "state", "established", f"( sport = :{port} )"],
                       capture_output=True, text=True)
    return max(0, len(r.stdout.strip().splitlines()) - 1)


# ---- RPC через файловую шину моста (тот же контракт, что видит агент) ----------------

def rpc(method, params, timeout=60):
    rid = str(uuid.uuid4())
    with open(HUB / "rpc_in.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"v": 1, "id": rid, "method": method, "params": params},
                           ensure_ascii=False) + "\n")
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            data = (HUB / "rpc_out.jsonl").read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            data = ""
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("id") == rid:
                return d
        time.sleep(0.1)
    return {"ok": False, "err": "harness_timeout", "id": rid}


def page_state(task):
    """title-канал страницы («sk48: {json}») через page.text."""
    r = rpc("page.text", {"maxChars": 10, "task": task}, timeout=40)
    if not r.get("ok"):
        return {"_err": str(r.get("err"))[:160]}
    t = str((r.get("result") or {}).get("title") or "")
    if not t.startswith("sk48:"):
        return {"_err": t[:120]}
    try:
        return json.loads(t[5:])
    except ValueError:
        return {"_err": "bad-title:" + t[:120]}


def media(task, timeout=40):
    return rpc("page.media", {"task": task}, timeout=timeout)


def dom(task):
    r = rpc("page.dom", {"task": task}, timeout=40)
    return r.get("result") or {}


def entry_by(d, sel):
    for e in (d.get("elements") or []) + ((d.get("more") or {}).get("elements") or []):
        if e.get("selector") == sel:
            return e
    return None


def img_dims(raw):
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            m = raw[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return w, h
            if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7 or m == 0x01:
                i += 2
                continue
            seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
            i += 2 + seg
        raise ValueError("JPEG SOF не найден")
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", raw[16:24])
        return w, h
    raise ValueError("не PNG/JPEG")


def dataurl_bytes(durl):
    return base64.b64decode(durl.split(",", 1)[1])


def read_audit():
    p = HUB / "audit.jsonl"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


# ---- Marionette: вкладки, XPI ----------------------------------------------------------

SPARE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "win.gBrowser.addTrustedTab('about:blank'); return 'spare';})();")
CLOSE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "for (let i = win.gBrowser.tabs.length - 1; i >= 0; i--) {"
                " const u = win.gBrowser.tabs[i].linkedBrowser.currentURI.spec;"
                " if (u.includes(arguments[0]) && win.gBrowser.tabs.length > 1)"
                " win.gBrowser.removeTab(win.gBrowser.tabs[i]);}"
                " if (win.gBrowser.tabs.length === 0) win.gBrowser.addTrustedTab('about:blank');"
                "return 'closed';})();")
NAV_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
              "for (const t of win.gBrowser.tabs)"
              " if (t.linkedBrowser.currentURI.spec.includes(arguments[0]))"
              " { t.linkedBrowser.loadURI(arguments[1], {triggeringPrincipal:"
              " Services.scriptSecurityManager.getSystemPrincipal()}); return 'navigated'; }"
              " return 'not-found';})();")

ADDON_INSTALL_JS = """
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
    const nsFile = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
    nsFile.initWithPath(arguments[0]);
    const a = await AddonManager.installTemporaryAddon(nsFile);
    const byId = await AddonManager.getAddonByID("hermes-sidekick@hermes.local");
    const ext = a || byId;
    const uri = (a && a.resourceURI) || (byId && byId.resourceURI) || null;
    if (!uri) {
      try {
        const { ExtensionManager } =
          ChromeUtils.importESModule("resource://gre/modules/ExtensionParent.sys.mjs");
        const e = ExtensionManager.get("hermes-sidekick@hermes.local");
        if (e && e.baseURI)
          return resolve({ok: true, id: e.id, url: e.baseURI.spec});
      } catch (e) {}
    }
    resolve({ok: true, id: ext && ext.id, url: uri ? uri.spec : ""});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""

ADDON_UNINSTALL_JS = """
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
    const a = await AddonManager.getAddonByID("hermes-sidekick@hermes.local");
    if (!a) return resolve({ok: true, gone: true});
    await a.uninstall();
    resolve({ok: true});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""


def install(m, xpi):
    r = m.ascript(ADDON_INSTALL_JS, args=[str(pathlib.Path(xpi).resolve())])
    if not (isinstance(r, dict) and r.get("ok")):
        raise RuntimeError(f"installTemporaryAddon: {r}")
    return r


def http_up():
    try:
        urllib.request.urlopen(f"{MEDIA_URL}", timeout=2)
        return True
    except Exception:
        return False


# ---- M3: харнесс — единственный WS-клиент, скрипует верификационные леги -------------

async def m3_session(tok, shot_dataurl, paused_media, playing_media):
    """Один сеанс: handoff c expect → «Готово» при паузе → ре-пуш с note+shot →
    «Готово» на играющем → verified:true.

    Клиент харнесса занимает WS-слот расширения (политика одного клиента), агентский
    RPC идёт через файловую шину моста — как у реального агента: баннер приходит
    в WS-слот, вердикт/статус/отказ — в rpc_out.jsonl. Верификационные леги
    (~vf*/~shot*) мост шлёт в WS-слот; на них отвечает помпа по скрипту."""
    obs = {"banners": [], "legs": [], "err": ""}
    uri = f"ws://127.0.0.1:{PORT}"
    async with websockets.connect(uri, open_timeout=5) as ws:
        await ws.send(json.dumps({"v": 1, "type": "hello", "token": tok}))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 5))
        obs["ack"] = ack.get("ok") is True

        async def send(obj):
            await ws.send(json.dumps(obj, ensure_ascii=False))

        def file_out_sync(rid, timeout):
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data = (HUB / "rpc_out.jsonl").read_text(encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    data = ""
                for ln in data.splitlines():
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("id") == rid:
                        return d
                time.sleep(0.15)
            return None

        async def wait_file(rid, timeout):
            return await asyncio.get_event_loop().run_in_executor(None, file_out_sync, rid, timeout)

        async def pump():
            # живёт весь сеанс: отвечает на верификационные леги, собирает баннеры
            while True:
                f = json.loads(await ws.recv())
                if f.get("type") == "handoff" and f.get("id") == rid:
                    obs["banners"].append(f)
                    continue
                if f.get("type") != "rpc":
                    continue
                lid, meth = str(f.get("id", "")), f.get("method")
                obs["legs"].append((lid, meth))
                if lid.endswith("~shot1") and meth == "page.screenshot":
                    await send({"v": 1, "type": "rpc_result", "id": lid, "ok": True,
                                "result": {"url": "about:sk48", "w": 320, "h": 240, "scale": 1,
                                           "dataUrl": shot_dataurl}})
                elif lid.endswith("~vf1") and meth == "page.media":
                    await send({"v": 1, "type": "rpc_result", "id": lid, "ok": True,
                                "result": paused_media})
                elif lid.endswith("~vf2") and meth == "page.media":
                    await send({"v": 1, "type": "rpc_result", "id": lid, "ok": True,
                                "result": playing_media})

        # рука с expect — баннер обязан прийти в WS-слот
        rid = str(uuid.uuid4())
        with open(HUB / "rpc_in.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"v": 1, "id": rid, "method": "handoff.request",
                                "params": {"what": "нажми Play на плеере", "task": "sk48-m3",
                                           "expect": {"kind": "video_playing"}}},
                               ensure_ascii=False) + "\n")
        pumptask = asyncio.create_task(pump())
        try:
            t0 = time.time()
            while time.time() - t0 < 10 and not obs["banners"]:
                await asyncio.sleep(0.1)
            obs["banner1"] = obs["banners"][0] if obs["banners"] else {}

            # «Готово» при паузе: мост сам читает page.media (~vf1), берёт кадр (~shot1)
            await send({"v": 1, "type": "resume", "id": rid, "task": "sk48-m3"})
            t0 = time.time()
            while time.time() - t0 < 20 and len(obs["banners"]) < 2:
                await asyncio.sleep(0.1)
            obs["banner2"] = obs["banners"][1] if len(obs["banners"]) > 1 else {}

            # rpc агента во время удержанной паузы — заблокирован (файловая шина)
            rblk = str(uuid.uuid4())
            with open(HUB / "rpc_in.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"v": 1, "id": rblk, "method": "page.text",
                                    "params": {"task": "sk48-m3"}}, ensure_ascii=False) + "\n")
            blk = await wait_file(rblk, 6)
            obs["blocked"] = (blk or {}).get("err")

            # handoff.status: expect/note/attempt — да, shot — нет
            rstat = str(uuid.uuid4())
            with open(HUB / "rpc_in.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"v": 1, "id": rstat, "method": "handoff.status",
                                    "params": {}}, ensure_ascii=False) + "\n")
            st = await wait_file(rstat, 6)
            obs["status"] = (st or {}).get("result") or {}

            obs["paused_during"] = (HUB / "paused").exists()

            # человек «починил» — второй resume, на этот раз играет
            await send({"v": 1, "type": "resume", "id": rid, "task": "sk48-m3"})
            v = await wait_file(rid, 20)
            obs["verdict"] = (v or {}).get("result") or (v or {}).get("err")
        finally:
            pumptask.cancel()
            try:
                await pumptask
            except BaseException:
                pass
    return obs


def main():
    S.mkdir(parents=True, exist_ok=True)
    if HUB.exists():
        shutil.rmtree(HUB)
    HUB.mkdir(parents=True)
    shutil.copy(HOME / "sidekick/bridge/hub/token", HUB / "token")
    tok = (HUB / "token").read_text(encoding="utf-8").strip()
    prod_before = ss_count(8791)
    br, m, httpd = None, None, None
    ext_uninstalled = False
    try:
        # ---------- S0: стенд ----------
        if EXT_SRC.exists():
            shutil.rmtree(EXT_SRC)
        shutil.copytree(REPO / "extension", EXT_SRC, ignore=shutil.ignore_patterns(".chrome"))
        bj = EXT_SRC / "background.js"
        src = bj.read_text(encoding="utf-8")
        assert 'const FALLBACK_SERVER = "ws://127.0.0.1:8791";' in src
        src = src.replace('const FALLBACK_SERVER = "ws://127.0.0.1:8791";',
                          f'const FALLBACK_SERVER = "ws://127.0.0.1:{PORT}";')
        assert "for (const u of [saved, FALLBACK_SERVER])" in src
        src = src.replace("for (const u of [saved, FALLBACK_SERVER])",
                          "for (const u of [FALLBACK_SERVER])")
        old_get = ('  const { bridgeToken } = await browser.storage.local.get("bridgeToken");\n'
                   '  return (bridgeToken || "").trim();')
        assert old_get in src, "getToken не узнал — патч токена не применён"
        src = src.replace(old_get,
                          '  const { bridgeToken } = await browser.storage.local.get("bridgeToken");\n'
                          f'  return (bridgeToken || "{tok}").trim(); // SK-48 стенд: fallback-токен')
        bj.write_text(src, encoding="utf-8")
        with zipfile.ZipFile(EXT_XPI, "w", zipfile.ZIP_DEFLATED) as z:
            for p in EXT_SRC.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(EXT_SRC))
        with zipfile.ZipFile(PROD_XPI, "w", zipfile.ZIP_DEFLATED) as z:
            for p in (REPO / "extension").rglob("*"):
                if p.is_file() and ".chrome" not in str(p):
                    z.write(p, p.relative_to(REPO / "extension"))
        for _ in range(10):
            zpid = listener_pid(PORT)
            if not zpid:
                break
            subprocess.run(["kill", "-9", str(zpid)])
            time.sleep(0.4)
        br = subprocess.Popen(["python3", str(REPO / "bridge" / "sidekick_bridge.py")],
                              env={**os.environ, "SIDEKICK_HOME": str(HUB),
                                   "SIDEKICK_PORT": str(PORT)},
                              stdout=open(HUB / "bridge.log", "w", encoding="utf-8"),
                              stderr=subprocess.STDOUT)
        time.sleep(1.0)
        note("S0a тест-мост слушает :8797", listener_pid(PORT) == br.pid,
             f"pid={listener_pid(PORT)}")
        if not http_up():
            httpd = subprocess.Popen(["python3", str(REPO / "tests" / "serve.py")],
                                     env=ENV, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            for _ in range(10):
                if http_up():
                    break
                time.sleep(0.4)
        note("S0b media.html/noop.html на :8899", http_up())
        # детерминизм: несколько installTemporaryAddon подряд (S0c/M3g/Z прошлых прогонов)
        # ломают contentScripts-регистрацию Firefox — контент-скрипт перестаёт подниматься
        # на новых вкладках. Свежий браузер снимает класс проблем оптом. Убийство — по
        # бинарнику через /proc (не pkill -f: паттерн матчится на собственную командную
        # строку убийцы и сносит сам себя).
        ffbin = str(HOME / ".local/firefox-158/firefox")
        for _ in range(12):
            if not listener_pid(MAR_PORT):
                break
            for d in os.listdir("/proc"):
                if not d.isdigit():
                    continue
                try:
                    if os.readlink(f"/proc/{d}/exe") == ffbin:
                        os.kill(int(d), 9)
                except OSError:
                    pass
            time.sleep(1.2)
        # убитый браузер оставляет sessionstore — следующий старт восстановит вкладки
        # прошлых прогонов (мёртвые content-скрипты, лишние табы)
        for f in (HOME / ".firefox-test-profile").glob("sessionstore*"):
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink(missing_ok=True)
        if not listener_pid(MAR_PORT):
            subprocess.Popen(
                [str(HOME / ".local/firefox-158/firefox"), "--marionette",
                 "--remote-debugging-port=9393", "--remote-allow-system-access",
                 "--profile", str(HOME / ".firefox-test-profile"), "--no-remote"],
                env={**ENV, "MOZ_DISABLE_CONTENT_SANDBOX": "1"},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t1 = time.time()
        while time.time() - t1 < 40 and not listener_pid(MAR_PORT):
            time.sleep(1.0)
        m = Marionette("127.0.0.1", MAR_PORT)
        m.script(SPARE_TAB_JS)
        r0 = install(m, EXT_XPI)
        note("S0c тест-XPI (SK-48) установлен", bool(r0.get("ok")), str(r0)[:120])
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0d расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод: было {prod_before})")
        og = rpc("tabs.open_grouped", {"url": MEDIA_URL + "#paused", "task": "sk48-a",
                                       "title": "SK48"})
        st0 = page_state("sk48-a")
        for _ in range(20):
            if st0.get("ready"):
                break
            time.sleep(0.5)
            st0 = page_state("sk48-a")
        note("S0e media.html#paused открыта (state-канал жив)", og.get("ok") and st0.get("ready") is True,
             f"st={st0}")
        # кадр для M3: реальный JPEG со страницы (баннер покажет настоящую картинку)
        sq = rpc("page.screenshot", {"task": "sk48-a", "max_width": 640})
        shot_dataurl = (sq.get("result") or {}).get("dataUrl", "")
        note("S0f кадр-доказательство снят (JPEG)", shot_dataurl.startswith("data:image/"),
             f"{len(shot_dataurl)} b64")

        # ---------- M1: плейбук «запусти видео» на живом content.js ----------
        m1 = media("sk48-a")
        r1 = m1.get("result") or {}
        vids = r1.get("videos") or []
        v0 = vids[0] if vids else {}
        meta_ok = (m1.get("ok") and r1.get("count", 0) >= 1 and v0.get("paused") is True
                   and (v0.get("duration") or 0) > 0 and (v0.get("rect") or {}).get("w", 0) >= 2
                   and v0.get("name") == "тестовый плеер")
        note("M1a page.media: живое видео на паузе, metadata/rect/aria-имя",
             meta_ok, f"v0={v0}")
        note("M1b src не отдаётся (может нести токенты)",
             "src" not in json.dumps(r1, ensure_ascii=False), str(r1)[:120])
        # плейбук: активная вкладка → mute → Play. Без activate Firefox держит
        # play() в background-вкладке на удержании (promise pending до вывода вьюпорта) —
        # человек тоже смотрит на вкладку, когда жмёт Play (плейбук AGENTS.md п.14).
        rpc("tabs.activate", {"task": "sk48-a"})
        time.sleep(0.4)
        pm = rpc("action.do", {"selector": "#mute", "action": "click", "task": "sk48-a", "ms": 150})
        tm = page_state("sk48-a")
        p2 = rpc("action.do", {"selector": "#play", "action": "click", "task": "sk48-a", "ms": 150})
        time.sleep(1.2)
        m2r = (media("sk48-a").get("result") or {})
        w0 = (m2r.get("videos") or [{}])[0]
        time.sleep(1.2)
        m3r = (media("sk48-a").get("result") or {})
        w1 = (m3r.get("videos") or [{}])[0]
        time.sleep(2.0)  # поздний старт: play() мог остаться pending
        w2 = ((media("sk48-a").get("result") or {}).get("videos") or [{}])[0]
        stt = page_state("sk48-a")
        started = any(x.get("paused") is False for x in (w0, w1, w2))
        progress = w2.get("currentTime", -1) > w0.get("currentTime", -2)
        note("M1c плейбук mute→Play: paused:false и currentTime растёт (без vision)",
             p2.get("ok") and started and progress and stt.get("paused") is False,
             f"mute={str(pm)[:150]} after-mute-title={tm} play={str(p2)[:150]} "
             f"t {w0.get('currentTime')} → {w1.get('currentTime')} → {w2.get('currentTime')} "
             f"paused {w0.get('paused')} → {w1.get('paused')} → {w2.get('paused')} title={stt}")
        rpc("action.do", {"selector": "#pause", "action": "click", "task": "sk48-a", "ms": 150})

        # ---------- M2: silent-noop на живом content.js ----------
        og2 = rpc("tabs.open_grouped", {"url": NOOP_URL, "task": "sk48-b", "title": "SK48-noop"})
        stb = page_state("sk48-b")
        for _ in range(20):
            if stb.get("ready", True) and "real" in stb:
                break
            time.sleep(0.5)
            stb = page_state("sk48-b")
        d = dom("sk48-b")
        e_real = entry_by(d, "#realcb")
        e_noop = entry_by(d, "#noopcb")
        e_sw = entry_by(d, "#sw")
        e_plain = entry_by(d, "#plain")
        note("M2a page.dom: checked у чекбокса/switch, hitTarget у пикселя, ключа нет у кнопки",
             og2.get("ok") and (e_real or {}).get("checked") is False
             and (e_noop or {}).get("checked") is False and (e_noop or {}).get("hitTarget")
             and (e_sw or {}).get("checked") is False and "checked" not in (e_plain or {}),
             f"real={e_real} noop_hit={(e_noop or {}).get('hitTarget')} "
             f"sw={e_sw} plain_checked={'checked' in (e_plain or {})}")
        c1 = rpc("action.do", {"selector": "#realcb", "action": "click", "task": "sk48-b",
                               "ms": 120}).get("result") or {}
        ps1 = c1.get("post_state") or {}
        note("M2b рабочий чекбокс: post_state changed:true",
             c1.get("executed") and ps1.get("changed") is True
             and ps1.get("before") is False and ps1.get("after") is True, ps1)
        c2 = rpc("action.do", {"selector": "#nooplbl", "action": "click", "task": "sk48-b",
                               "ms": 120}).get("result") or {}
        ps2 = c2.get("post_state") or {}
        note("M2c Ozon-ловушка (label preventDefault): executed:true + changed:false + waited_ms "
             "— silent-noop виден без vision",
             c2.get("executed") and ps2.get("changed") is False
             and ps2.get("before") is False and ps2.get("after") is False
             and ps2.get("waited_ms", 0) >= 250, ps2)
        c3 = rpc("action.do", {"selector": "#sw", "action": "click", "task": "sk48-b",
                               "ms": 120}).get("result") or {}
        ps3 = c3.get("post_state") or {}
        c4 = rpc("action.do", {"selector": "#sw-noop", "action": "click", "task": "sk48-b",
                               "ms": 120}).get("result") or {}
        ps4 = c4.get("post_state") or {}
        c5 = rpc("action.do", {"selector": "#plain", "action": "click", "task": "sk48-b",
                               "ms": 120}).get("result") or {}
        note("M2d aria-switch: рабочий changed:true (kind switch), заглушка changed:false",
             ps3.get("changed") is True and ps3.get("kind") == "switch"
             and ps4.get("changed") is False and ps4.get("kind") == "switch",
             f"sw={ps3} swn={ps4}")
        note("M2e обычная кнопка: post_state не появляется",
             c5.get("executed") and "post_state" not in c5, c5)
        stb2 = page_state("sk48-b")
        note("M2f title-канал сходится с post_state (real=true, noop=false, sw=true)",
             stb2.get("real") is True and stb2.get("noop") is False
             and stb2.get("sw") == "true" and stb2.get("swn") == "false", stb2)

        # ---------- M3: handoff expect — оркестрация моста (живой мост, скрипт-клиент) --
        # Политика одного клиента (SK-46/49): второй hello вытесняет расширение, поэтому
        # на время M3 тест-XPI снимается; живое чтение страницы доказано M1/M2/M4.
        un = m.ascript(ADDON_UNINSTALL_JS)
        ext_uninstalled = True
        t1 = time.time()
        while time.time() - t1 < 15 and ss_count(PORT) > 0:
            time.sleep(0.4)
        note("M3.0 тест-XPI снят, слот клиента свободен", bool(un.get("ok")) and ss_count(PORT) == 0,
             f"@8797={ss_count(PORT)}")
        paused_media = {"url": MEDIA_URL, "count": 1, "vp": {"w": 1280, "h": 720},
                        "videos": [{"paused": True, "ended": False, "muted": False, "volume": 1,
                                    "currentTime": 0, "duration": 3.0, "readyState": 4,
                                    "rect": {"x": 0, "y": 0, "w": 320, "h": 240},
                                    "name": "тестовый плеер"}]}
        playing_media = json.loads(json.dumps(paused_media))
        playing_media["videos"][0].update({"paused": False, "currentTime": 1.4, "muted": True})
        obs = asyncio.run(m3_session(tok, shot_dataurl, paused_media, playing_media))
        b1, b2 = obs.get("banner1") or {}, obs.get("banner2") or {}
        aud = read_audit()
        note("M3a handoff c expect: баннер, hub/paused, rpc агента paused_for_human",
             obs.get("ack") and b1.get("what") == "нажми Play на плеере"
             and obs.get("blocked") == "paused_for_human" and obs.get("paused_during"),
             f"b1={str(b1)[:120]} blocked={obs.get('blocked')}")
        note("M3b «Готово» при паузе → мост сам читает (~vf1) и берёт кадр (~shot1)",
             ("vf1", "page.media") in [(str(a).split("~")[-1], b) for a, b in obs["legs"]]
             and any(l[1] == "page.screenshot" for l in obs["legs"]), obs["legs"])
        note("M3c баннер ВЕРНУЛСЯ с note+shot+attempt (тот же id руки)",
             b2.get("id") == b1.get("id") and b2.get("attempt") == 1
             and "паузе" in (b2.get("note") or "")
             and (b2.get("shot") or "").startswith("data:image/")
             and max(img_dims(dataurl_bytes(b2["shot"]))) >= 100,
             f"attempt={b2.get('attempt')} note={str(b2.get('note'))[:80]} "
             f"shot={len(b2.get('shot') or '')}b")
        st = obs.get("status") or {}
        note("M3d handoff.status: expect/note/attempt есть, shot — нет; пауза держится",
             st.get("paused") is True and st.get("attempt") == 1
             and st.get("expect") == {"kind": "video_playing"} and "shot" not in st
             and obs.get("paused_during"), st)
        note("M3e второй resume на играющем → {resumed:true, verified:true}, пауза снята",
             obs.get("verdict") == {"resumed": True, "verified": True}
             and not (HUB / "paused").exists(), obs.get("verdict"))
        note("M3f аудит: pause+expect, handoff_verify not_reached/reached, reverify",
             any(a.get("ev") == "handoff" and a.get("decision") == "pause"
                 and a.get("expect") == "video_playing" for a in aud)
             and any(a.get("ev") == "handoff_verify" and a.get("decision") == "not_reached" for a in aud)
             and any(a.get("ev") == "handoff" and a.get("decision") == "reverify" for a in aud)
             and any(a.get("ev") == "handoff_verify" and a.get("decision") == "reached" for a in aud),
             [a for a in aud if a.get("ev") in ("handoff", "handoff_verify")][-4:])
        r0b = install(m, EXT_XPI)
        ext_uninstalled = False
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("M3g тест-XPI поднят обратно, канал :8797 живой", bool(r0b.get("ok")) and ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)}")

        # ---------- M4: youtube live ----------
        # Смысл M4 — честное состояние на реальной странице без vision: если сеть/плеер
        # не дали метаданных, page.media обязан это показать (rs=0, paused), а не «всё ок».
        og3 = rpc("tabs.open_grouped", {"url": YT_URL, "task": "sk48-yt", "title": "SK48-yt"})
        yv, t0 = {}, time.time()
        while time.time() - t0 < 45:
            y = media("sk48-yt", timeout=30)
            yv = y.get("result") or {}
            if (yv.get("count") or 0) >= 1 and ((yv.get("videos") or [{}])[0].get("duration") or 0) > 0:
                break
            time.sleep(3)
        if not ((yv.get("videos") or [{}])[0].get("duration") or 0) > 0:
            # одна перезагрузка: youtube иногда стартует без метаданных (свежая вкладка, без tabs.close
            # — тот под перм-гейтом close_tabs и не должен будить человека)
            og3 = rpc("tabs.open_grouped", {"url": YT_URL + "&reload=1", "task": "sk48-yt2",
                                            "title": "SK48-yt2"})
            t0 = time.time()
            while time.time() - t0 < 45:
                y = media("sk48-yt2", timeout=30)
                yv = y.get("result") or {}
                ytask = "sk48-yt2"
                if (yv.get("count") or 0) >= 1 and ((yv.get("videos") or [{}])[0].get("duration") or 0) > 0:
                    break
                time.sleep(3)
        else:
            ytask = "sk48-yt"
        ytask = locals().get("ytask") or "sk48-yt"
        yv0 = (yv.get("videos") or [{}])[0]
        ytitle = page_state(ytask)
        struct_ok = (yv.get("count", 0) >= 1 and isinstance(yv0.get("paused"), bool)
                     and (yv0.get("rect") or {}).get("w", 0) >= 2
                     and "src" not in json.dumps(yv, ensure_ascii=False))
        meta = (yv0.get("duration") or 0) > 0
        note("M4a page.media на реальном youtube.com: структура+геометрия честные, src нет "
             "(метаданные — как доехали: rs=0 значит плеер реально не грузится, и это видно "
             "без vision)",
             og3.get("ok") and struct_ok,
             f"meta={meta} dur={yv0.get('duration')} paused={yv0.get('paused')} "
             f"rs={yv0.get('readyState')} rect={yv0.get('rect')} name={str(yv0.get('name'))[:40]} "
             f"title={str(ytitle)[:100]}")
        if yv0:
            rpc("tabs.activate", {"task": ytask})
            time.sleep(0.4)
            rect = yv0.get("rect") or {}
            cx = int(rect.get("x", 0) + rect.get("w", 0) / 2)
            cy = int(rect.get("y", 0) + rect.get("h", 0) / 2)
            before = yv0.get("paused")
            rpc("action.do", {"x": cx, "y": cy, "action": "click", "task": ytask, "ms": 200})
            time.sleep(2.0)
            y2 = (media(ytask).get("result") or {}).get("videos") or [{}]
            after = y2[0].get("paused")
            note("M4b клик по видео + page.media: состояние прочитано честно "
                 "(смена paused или честное «не переключилось» — синтетический клик "
                 "YouTube-плеер может не пустить; именно поэтому состояние читают, а не гадают)",
                 isinstance(after, bool), f"paused {before} → {after}")

        # ---------- M5: аудит grep'ом ----------
        aud = read_audit()
        ps_rows = [a for a in aud if a.get("post_state")]
        ps_true = [a for a in ps_rows if a["post_state"].get("changed") is True]
        ps_false = [a for a in ps_rows if a["post_state"].get("changed") is False]
        note("M5a post_state в аудите: есть changed:true и changed:false (noop виден grep'ом)",
             len(ps_true) >= 1 and len(ps_false) >= 1,
             f"rows={len(ps_rows)} true={len(ps_true)} false={len(ps_false)} "
             f"sample={str(ps_false[:1])[:160]}")
        raw = (HUB / "audit.jsonl").read_text(encoding="utf-8", errors="replace")
        note("M5b grep «post_state» по audit.jsonl находит noop-клики",
             raw.count('"post_state"') >= 2, f"{raw.count(chr(34) + 'post_state' + chr(34))} вхождений")
        shot_screen = None
    except Exception as ex:  # noqa: BLE001
        note("EXCEPTION", False, f"{type(ex).__name__}: {ex}")
        raise
    finally:
        # ---------- Z: уборка ----------
        try:
            if m:
                m.script(CLOSE_TAB_JS, args=["media.html"])
                m.script(CLOSE_TAB_JS, args=["noop.html"])
                m.script(CLOSE_TAB_JS, args=["youtube.com"])
                if ext_uninstalled:
                    install(m, EXT_XPI)
                install(m, PROD_XPI)  # прод-аддон обратно (стенд остаётся живым на проде-коде)
        except Exception as e:  # noqa: BLE001
            print("Z-предупреждение:", e, file=sys.stderr)
        if br:
            br.terminate()
        if httpd:
            httpd.terminate()
        fails = [k for k, v in results.items() if v["ok"] is False]
        print(f"\nИТОГ: {sum(1 for v in results.values() if v['ok'])} PASS / {len(fails)} FAIL "
              f"из {len(results)}", flush=True)
        (S / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
        sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
