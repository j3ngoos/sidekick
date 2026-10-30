#!/usr/bin/env python3
"""SK-36 живая приёмка: шаги обычным языком — русские подписи, сырое под катом.

Стенд: изолированный мост :8798 + тест-копия расширения (только подмена
FALLBACK_SERVER->:8798, кода-пробников НЕТ — рендерится продакшн feed.js) в
firefox-158 (Marionette 2929). Прод :8791 не трогаем; в конце — переустановка
prod-аддона. Прецеденты: sk35_acc.py (стенд/шина), sk37_acc.py (пробник).

Фазы:
  S0 стенд: мост :8798, XPI, токен, сайдбар открыт, чат default
  A1 задача с 7 типами шагов (открытие/клик/ввод/скриншот-marks/слабый/caption/
     терминал): все подписи по-русски, DOM-скан — голых JSON/селекторов/секретов
     в ленте нет, у сырых шагов есть «подробнее», .sraw скрыт
  A2 «подробнее» раскрывает сырьё (method+params+значение), «скрыть» убирает
  A3 refine-кадр модели (тот же n, без state) перекрашивает подпись, raw/state
     НЕ стёрты (merge background.js)
  A4 caption агента приоритетнее текста шаблона в DOM
  A5 живой модельный путь: caption_refine к :8087 — русская фраза, замер latency;
     сам caption llama-server харнесс поднимает, если :8087 не слушается (в finally
     гасит только свою копию) — приёмка воспроизводима с чистого листа
  A6 шаблоны: p99 генерации подписи ≤50 мс (200 вызовов step_parts)
  A7 скрин раскрытой карточки с сырьём
  R1 прод :8791 за время приёмки не получил тестовых коннектов
  Z  уборка: вкладки, prod-аддон, :8791, тест-Firefox погашен по PID
"""
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
S = pathlib.Path(os.environ.get("SK36_SCRATCH", str(HOME / ".hermes/cache/scratch/sk36")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
SHOTS = S / "shots"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8798
results = {}

sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
from mario import Marionette  # noqa: E402


def note(name, ok, detail=""):
    ok_v = None if ok == "skip" else bool(ok)
    results[name] = {"ok": ok_v, "detail": str(detail)[:400]}
    tag = "PASS " if ok_v else ("SKIP " if ok_v is None else "FAIL ")
    print(tag + name + ("  " + str(detail)[:180] if detail else ""), flush=True)


def ss_count(port):
    r = subprocess.run(["ss", "-tn", "state", "established", f"( sport = :{port} )"],
                       capture_output=True, text=True)
    return max(0, len(r.stdout.strip().splitlines()) - 1)


def listener_pid(port):
    r = subprocess.run(["ss", "-tlnp", f"( sport = :{port} )"], capture_output=True, text=True)
    m2 = re.search(r"pid=(\d+)", r.stdout)
    return int(m2.group(1)) if m2 else None


def shot(name):
    p = SHOTS / f"{name}.png"
    subprocess.run(["scrot", "-o", str(p)], env=ENV, capture_output=True)
    return p.exists()


CAP_SRV = [str(HOME / "src/llama.cpp/build/bin/llama-server"),
           "-m", str(HOME / "models/caption/qwen2.5-1.5b-instruct-q4_k_m.gguf"),
           "-c", "1024", "-t", "8", "-ngl", "0",
           "--host", "127.0.0.1", "--port", "8087", "--alias", "sidekick-caption"]


def caption_up():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8087/v1/models", timeout=2):
            return True
    except Exception:
        return False


# ---- outbox-рамки (как их шлёт агентд/Hermes) ---------------------------------

def out(frames):
    if isinstance(frames, dict):
        frames = [frames]
    with open(HUB / "outbox.jsonl", "a", encoding="utf-8") as f:
        for fr in frames:
            f.write(json.dumps({"v": 1, **fr}, ensure_ascii=False) + "\n")


def sent_count():
    try:
        return (HUB / "bridge.log").read_text(encoding="utf-8", errors="replace") \
            .count("msg(s) from outbox")
    except FileNotFoundError:
        return 0


def out_delivered(frames, timeout=8):
    n0 = sent_count()
    out(frames)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if sent_count() > n0:
            return True
        time.sleep(0.2)
    out(frames)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if sent_count() > n0:
            return True
        time.sleep(0.2)
    return False


# ---- Marionette-блоки (прецедент sk35_acc.py) ----------------------------------

SPARE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "win.gBrowser.addTrustedTab('about:blank'); return 'spare';})();")
OPEN_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
               "const b = win.gBrowser.addTrustedTab(arguments[0]); win.gBrowser.selectedTab = b;"
               "return 'tab'; })();")
CLOSE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "for (let i = win.gBrowser.tabs.length - 1; i >= 0; i--) {"
                " const u = win.gBrowser.tabs[i].linkedBrowser.currentURI.spec;"
                " if (u.includes(arguments[0]) && win.gBrowser.tabs.length > 1)"
                " win.gBrowser.removeTab(win.gBrowser.tabs[i]);}"
                " if (win.gBrowser.tabs.length === 0) win.gBrowser.addTrustedTab('about:blank');"
                "return 'closed';})();")
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
SC_SHOW_JS = r"""
return new Promise(resolve => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const SC = win.SidebarController;
  if (SC.isOpen) return resolve('already-open');
  const ge = SC.getExtensions ? Array.from(SC.getExtensions()) : [];
  const ext = ge.find(e => /hermes|sidekick/i.test(String(e.extensionId || '')));
  if (!ext) return resolve('no-extension-sidebar');
  SC.show(ext.commandID).then(r => resolve('show->' + r), e => resolve('ERR:' + e.message));
});
"""
SC_HIDE_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
              "const SC = win.SidebarController; if (!SC.isOpen) return 'already-closed';"
              "SC.hide(); return 'hidden';})();")
SETUP_JS = r"""
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
  const ph = Services.io.getProtocolHandler('resource').QueryInterface(Ci.nsIResProtocolHandler);
  ph.setSubstitution('sk36', await Services.io.newURI('file://%s'))
    ;
    try { ChromeUtils.unregisterWindowActor('Sk36Probe'); } catch (e) {}
    ChromeUtils.registerWindowActor('Sk36Probe', {
      child: { esModuleURI: 'resource://sk36/sk36_actor.sys.mjs' },
      parent: { esModuleURI: 'resource://sk36/sk36_actor.sys.mjs',
                matches: ['<all_urls>', 'moz-extension://*/*'],
                remoteTypes: ['*'] },
    });
    resolve({ok: true});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""
OP_JS = r"""
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const payload = JSON.parse(arguments[0]);
    const win = Services.wm.getMostRecentWindow('navigator:browser');
    let gw = null;
    if (payload.match === 'sidebar') {
      const host = win.document.getElementById('sidebar');
      if (!host) return resolve({ok: false, err: 'no-sidebar-host'});
      let inner = null;
      try {
        inner = host.contentWindow.document.querySelector('browser#webext-panels-browser')
          || host.contentWindow.document.querySelector('browser,iframe');
      } catch (e) { return resolve({ok: false, err: 'host-not-loaded'}); }
      if (!inner) return resolve({ok: false, err: 'no-inner-browser'});
      gw = inner.browsingContext.currentWindowGlobal;
    } else if (payload.match === 'options') {
      for (const t of win.gBrowser.tabs)
        if (t.linkedBrowser.currentURI.spec.includes('options.html')) {
          gw = t.linkedBrowser.browsingContext.currentWindowGlobal; break; }
    } else {
      gw = win.gBrowser.selectedBrowser.browsingContext.currentWindowGlobal;
    }
    if (!gw) return resolve({ok: false, err: 'no-gw:' + payload.match});
    const val = await gw.getActor('Sk36Probe').sendQuery('sk36:op', payload);
    resolve({ok: true, val});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 160)}); }
})();
"""


def op(m, match, name, arg=None, timeout=20):
    payload = {"op": name, "match": match}
    if arg is not None:
        payload["arg"] = arg
    try:
        r = m.ascript(OP_JS, args=[json.dumps(payload)], timeout=timeout)
    except Exception as e:
        return {"_err": str(e)[:120]}
    if not (isinstance(r, dict) and r.get("ok")):
        return {"_err": r.get("err", "?") if isinstance(r, dict) else str(r)[:120]}
    v = r.get("val")
    try:
        j = json.loads(v) if isinstance(v, str) else {}
    except (json.JSONDecodeError, TypeError):
        return {"_err": "bad-json:" + str(v)[:120]}
    return j if "err" in j else j.get("v")


def scan(m):
    s = op(m, "sidebar", "scanSteps")
    return s if isinstance(s, dict) else {"found": False, "_err": str(s)[:120]}


def sidebar_alive(m):
    d = op(m, "sidebar", "href")
    return "sidebar.html" in str(d.get("v", d) if isinstance(d, dict) else d)


def sidebar_force_open(m, attempts=6):
    for _ in range(attempts):
        if sidebar_alive(m):
            return True
        try:
            m.script(SC_HIDE_JS)
        except Exception:
            pass
        time.sleep(0.6)
        try:
            m.script(SC_SHOW_JS)
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < 7:
            if sidebar_alive(m):
                return True
            time.sleep(0.4)
    return False


def install(m, xpi):
    r = m.ascript(ADDON_INSTALL_JS, args=[str(pathlib.Path(xpi).resolve())])
    if not (isinstance(r, dict) and r.get("ok")):
        raise RuntimeError(f"installTemporaryAddon: {r}")
    return r


def wait_scan(m, pred, timeout=15):
    t0 = time.time()
    s = {}
    while time.time() - t0 < timeout:
        # сайдбар в тест-профиле живёт своей жизнью (restore/SW-перезапуск) —
        # прецедент sk35: перед каждым DOM-фазом убеждаемся, что он жив
        if not sidebar_alive(m):
            sidebar_force_open(m, attempts=2)
        s = scan(m)
        if s.get("found") and pred(s):
            return s
        time.sleep(0.4)
    return s if s.get("found") else None


CYR = re.compile(r"[а-яА-ЯёЁ]")
BANNED = ["action.do", "sk rpc", "selector", "tabIds", "{\"", "}", "secret123",
          "секретный/путь", "rpc_in", "printf", "page.screenshot", "tabs.open_grouped",
          "http://", "https://", "#cart"]


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    if HUB.exists():
        shutil.rmtree(HUB)
    HUB.mkdir(parents=True)
    shutil.copy(HOME / "sidekick/bridge/hub/token", HUB / "token")
    prod_before = ss_count(8791)
    br, m, cap_proc = None, None, None
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
                              env={**os.environ, "SIDEKICK_HOME": str(HUB), "SIDEKICK_PORT": str(PORT)},
                              stdout=open(HUB / "bridge.log", "w", encoding="utf-8"),
                              stderr=subprocess.STDOUT)
        time.sleep(1.0)
        note("S0a тест-мост слушает :8798", listener_pid(PORT) == br.pid, f"pid={listener_pid(PORT)}")
        # caption-модель для A5: харнесс сам поднимает CPU llama-server, если его
        # не поднял sidekick-caption.service (прод так и живёт: нет модели = шаблон)
        if not caption_up():
            cap_proc = subprocess.Popen(CAP_SRV, env={**os.environ, "CUDA_VISIBLE_DEVICES": ""},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t1 = time.time()
        while time.time() - t1 < 40 and not caption_up():
            time.sleep(0.5)
        note("S0i caption-модель :8087 жива (CPU llama-server)", caption_up(),
             "поднята харнессом" if cap_proc else "уже работала (сервис)")
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
        reg = m.ascript(SETUP_JS % str(REPO / "tests"))
        note("S0b window-актор зарегистрирован", isinstance(reg, dict) and reg.get("ok"), reg)
        r0 = install(m, EXT_XPI)
        note("S0c тест-XPI установлен (прод-код без пробников)", bool(r0.get("ok")), r0.get("url", ""))
        token = (HUB / "token").read_text().strip()
        t1 = time.time()
        ext_url, hrefv = "", ""
        while time.time() - t1 < 40 and not ext_url:
            sidebar_force_open(m, attempts=2)
            d0 = op(m, "sidebar", "href")
            hrefv = str(d0.get("v", d0) if isinstance(d0, dict) else d0)
            if "sidebar.html" in hrefv:
                ext_url = hrefv.split("sidebar.html")[0] + "/"
            else:
                time.sleep(1.0)
        note("S0d сайдбар тест-XPI поднят", bool(ext_url), hrefv[:120])
        if not ext_url:
            raise RuntimeError("сайдбар не поднят — стенд слепой: " + str(hrefv)[:160])
        m.script(OPEN_TAB_JS, [ext_url + "options.html"])
        time.sleep(1.5)
        seeded = "none"
        for _ in range(6):
            seeded = op(m, "options", "seedToken", arg=token)
            if seeded == "seeded":
                break
            time.sleep(1.0)
        note("S0e токен посеян (options.html)", seeded == "seeded", seeded)
        note("S0f чат default (sk23Chat снят)", op(m, "options", "storageClean") == "clean")
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0g расширение подключено к :8798", ss_count(PORT) >= 1,
             f"@8798={ss_count(PORT)} @8791={ss_count(8791)} (прод: было {prod_before})")
        note("S0h сайдбар открыт", sidebar_force_open(m))

        # ---------- A1: 7 типов шагов, все подписи русские, сырьё спрятано ----------
        tid = "sk36-a1"
        frames = [{"type": "task", "id": tid, "title": "Собрать корзину", "state": "running"},
                  {"type": "step", "task": tid, "n": 1, "state": "active",
                   "text": "открываю ozon.ru", "raw": "sk rpc tabs.open_grouped '{\"url\":\"https://ozon.ru/cart\"}'"},
                  {"type": "step", "task": tid, "n": 2, "state": "done",
                   "text": "кликаю «Купить» на ozon.ru", "raw": "sk rpc action.do '{\"action\":\"click\",\"name\":\"Купить\",\"selector\":\"#cart\"}'"},
                  {"type": "step", "task": tid, "n": 3, "state": "done",
                   "text": "печатаю в «Логин»", "raw": "sk rpc action.do '{\"action\":\"input\",\"name\":\"Логин\",\"value\":\"secret123\"}'"},
                  {"type": "step", "task": tid, "n": 4, "state": "done",
                   "text": "размечаю кнопки на экране", "raw": "sk rpc page.screenshot '{\"marks\":true}'"},
                  {"type": "step", "task": tid, "n": 5, "state": "done",
                   "text": "📡 работаю через мост", "raw": "sk rpc action.do '{\"action\":\"drag\",\"x\":10,\"y\":20}'"},
                  {"type": "step", "task": tid, "n": 6, "state": "done",
                   "text": "📡 работаю через мост", "caption": "тащу товар в корзину",
                   "raw": "sk rpc page.custom '{\"method\":\"drag\",\"selector\":\"#cart-item\"}'"},
                  {"type": "step", "task": tid, "n": 7, "state": "done",
                   "text": "🖥 выполняю команду в терминале", "raw": "ls -la /секретный/путь"}]
        note("A1a кадры доставлены мостом", out_delivered(frames))
        s1 = wait_scan(m, lambda s: len(s.get("rows") or []) >= 7, timeout=15)
        note("A1b карточка с 7 шагами в DOM", bool(s1) and len(s1.get("rows") or []) >= 7,
             s1 and len(s1.get("rows") or []))
        rows = (s1 or {}).get("rows") or []
        bad_ru = [r["tx"] for r in rows if not CYR.search(r["tx"])]
        note("A1c все подписи по-русски", bool(rows) and not bad_ru, bad_ru)
        banned_hits = [b for b in BANNED for r in rows if b in r["tx"]]
        note("A1d в подписях нет голых JSON/селекторов/секретов", not banned_hits, banned_hits)
        nmore = sum(1 for r in rows if r["more"])
        nraw_open = sum(1 for r in rows if r["raw"] is not None)
        note("A1e у сырых шагов есть «подробнее», кат скрыт по умолчанию",
             nmore >= 6 and nraw_open == 0, f"more={nmore} raw_open={nraw_open}")
        note("A1f скрин ленты со скрытым сырьём", shot("a1-feed-hidden"))

        # ---------- A2: «подробнее» раскрывает сырьё ----------
        sidebar_force_open(m, attempts=2)
        op(m, "sidebar", "openCard")
        time.sleep(0.6)
        c2 = op(m, "sidebar", "clickMore", arg="3")
        time.sleep(0.6)
        s2 = scan(m)
        row3 = next((r for r in s2.get("rows") or [] if r["tx"].startswith("3. ")), {})
        note("A2a «подробнее» открыл raw с method+params+значением",
             str(c2).startswith("clicked") and row3.get("raw") and "action.do" in row3["raw"]
             and "secret123" in row3["raw"], str(c2) + " " + str(row3.get("raw"))[:120])
        note("A2b скрин раскрытого сырья", shot("a2-raw-open"))
        op(m, "sidebar", "clickMore", arg="3")
        time.sleep(0.6)
        s2b = scan(m)
        row3b = next((r for r in s2b.get("rows") or [] if r["tx"].startswith("3. ")), {})
        note("A2c «скрыть» убирает raw", row3b.get("raw") is None, row3b)

        # ---------- A3: refine-кадр (модель) перекрашивает шаг, raw/state целы ----------
        note("A3a refine-кадр доставлен", out_delivered(
            [{"type": "step", "task": tid, "n": 5, "text": "тяну мышью на странице"}]))
        s3 = wait_scan(m, lambda s: any(r["tx"].startswith("5. ") and "тяну мышью" in r["tx"]
                                        for r in s.get("rows") or []), timeout=12)
        row5 = next((r for r in (s3 or {}).get("rows") or [] if r["tx"].startswith("5. ")), {})
        note("A3b подпись шага 5 обновилась моделью", "тяну мышью" in row5.get("tx", ""), row5)
        note("A3c raw и state шага 5 НЕ стёрты refine-кадром",
             row5.get("more") and "done" in row5.get("cls", ""), row5)

        # ---------- A4: caption агента приоритетна ----------
        row6 = next((r for r in (s3 or {}).get("rows") or [] if r["tx"].startswith("6. ")), {})
        note("A4 caption агента в ленте вместо шаблона",
             "тащу товар в корзину" in row6.get("tx", "") and "работаю через мост" not in row6.get("tx", ""),
             row6)

        # ---------- A5: живой модельный путь (caption_refine -> :8087) ----------
        spec = importlib.util.spec_from_file_location("A5agentd", str(REPO / "agentd" / "sidekick_agentd.py"))
        A = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(A)
        A._CAPTION_CACHE.clear()
        lat, phrase = [], None
        for payload in ({"action": "swipe", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}},
                        {"action": "drag", "sid": "el42"}):
            t0 = time.monotonic()
            r = A.caption_refine("action.do", payload)
            lat.append((time.monotonic() - t0) * 1000)
            phrase = phrase or r
        ok5 = bool(phrase) and CYR.search(phrase) and "http" not in str(phrase)
        note("A5a модель доступна, подпись русская (бюджет-честный: None допустим только по таймауту)",
             ok5 or all(x > 480 for x in lat), f"phrase={phrase!r} lat={[round(x) for x in lat]}")
        # честность бюджета: либо фраза в него уложилась, либо честный None-отказ
        # (клиент отвалился на бюджете, а не завис — допуск 200 мс на сокет/парсинг)
        note("A5b бюджет ≤500 мс честный: фраза в бюджете либо None-отказ у границы",
             ok5 or all(480 < x <= A.CAPTION_BUDGET_MS + 200 for x in lat),
             f"lat={[round(x) for x in lat]} мс load={os.getloadavg()[0]:.1f}")
        # модельный путь жив: поднимаем бюджет — модель обязана дать русскую фразу
        A._CAPTION_CACHE.clear()
        A.CAPTION_BUDGET_MS = 4000.0
        r5c = A.caption_refine("perm.check", {"perm": "teleport"})
        note("A5c модельный путь жив (бюджет 4 с): модель даёт русскую фразу",
             bool(r5c) and CYR.search(r5c) and not re.search(r"[a-zA-Z]", r5c), r5c)

        # ---------- A6: шаблоны — задержка подписи ----------
        lats = []
        for i in range(200):
            cmd = (f"sk rpc action.do '{{\"action\":\"click\",\"name\":\"Кнопка {i}\"}}'")
            t0 = time.perf_counter()
            A.step_parts("terminal", {"command": cmd})
            lats.append((time.perf_counter() - t0) * 1000)
        lats.sort()
        p99 = lats[int(len(lats) * 0.99) - 1]
        note("A6 генерация подписи шаблоном p99 ≤50 мс", p99 <= 50,
             f"p99={p99:.2f} мс max={lats[-1]:.2f} мс (200 вызовов)")

        # ---------- A7: финальный скрин карточки с сырьём и refine ----------
        sidebar_force_open(m, attempts=2)
        op(m, "sidebar", "clickMore", arg="7")
        time.sleep(0.5)
        note("A7 скрин карточки (refine + raw шага 7)", shot("a7-card-final"))

        # ---------- регресс: прод-шина не тронута ----------
        # Тест-XPI физически не знает про :8791 (FALLBACK переписан, saved-URL
        # вырезан) — проверяем это статически по собранному XPI; счётчик прод-
        # коннектов — живой браузер пользователя (Mac), его флуктуации не баг.
        with zipfile.ZipFile(EXT_XPI) as z:
            bsrc = z.read("background.js").decode("utf-8")
        fb = re.search(r'const FALLBACK_SERVER = "([^"]+)"', bsrc)
        note("R1 тест-XPI не содержит :8791 (прод-шина недосяжна со стенда)",
             bool(fb) and fb.group(1) == f"ws://127.0.0.1:{PORT}"
             and "saved, FALLBACK" not in bsrc,
             f"FALLBACK={fb and fb.group(1)}; @8791={ss_count(8791)} (было {prod_before})")
    except Exception as e:
        import traceback
        note("HARNESS-EXC", False, traceback.format_exc()[-500:])
    finally:
        try:
            if m:
                m.script(CLOSE_TAB_JS, args=["moz-extension"])
                m.script(SPARE_TAB_JS)
        except Exception as e:
            print("WARN tabs:", e, flush=True)
        try:
            if m:
                install(m, PROD_XPI)
                t2 = time.time()
                while time.time() - t2 < 20 and ss_count(8791) == 0 and prod_before > 0:
                    time.sleep(0.5)
        except Exception as e:
            print("WARN prod reinstall:", e, flush=True)
        try:
            if m:
                m.close()
        except Exception:
            pass
        if br:
            br.terminate()
            try:
                br.wait(timeout=5)
            except subprocess.TimeoutExpired:
                br.kill()
        if cap_proc:  # чужую (сервисную) модель не трогаем — только свою
            cap_proc.terminate()
            try:
                cap_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cap_proc.kill()
        t1 = time.time()
        while prod_before > 0 and time.time() - t1 < 30 and ss_count(8791) < prod_before:
            time.sleep(1.0)
        note("Z1 прод-аддон переустановлен, :8791 не хуже исходного",
             ss_count(8791) >= prod_before,
             f"@8791={ss_count(8791)} (было {prod_before}; 0 = браузер пользователя сейчас не подключён)")
        fpid = listener_pid(MAR_PORT)
        if fpid:
            subprocess.run(["kill", str(fpid)], capture_output=True)
            t1 = time.time()
            while time.time() - t1 < 15 and listener_pid(MAR_PORT):
                time.sleep(0.5)
            if listener_pid(MAR_PORT):
                subprocess.run(["kill", "-9", str(listener_pid(MAR_PORT))], capture_output=True)
                time.sleep(1.0)
        note("Z2 тест-Firefox погашен (стенд не висит вторым коннектом)",
             not listener_pid(MAR_PORT), f"pid={listener_pid(MAR_PORT) or '—'}")
        skipped = [k for k, v in results.items() if v["ok"] is None]
        fails = [k for k, v in results.items() if v["ok"] is False]
        passed = [k for k, v in results.items() if v["ok"] is True]
        print(f"\nИТОГ: {len(passed)} PASS / {len(fails)} FAIL / {len(skipped)} SKIP "
              f"из {len(results)}", flush=True)
        for k in fails:
            print("FAIL:", k, "—", results[k]["detail"][:200], flush=True)
        (S / "sk36_acc_results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
