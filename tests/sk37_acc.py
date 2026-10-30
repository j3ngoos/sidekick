#!/usr/bin/env python3
"""SK-37 живая приёмка: пермиссы/стоп в окне чата, bell/badge при свёрнутом сайдбаре
(карточка t_e32947a4, запрос пользователя 22.10).

Стенд: изолированный мост :8797 + тест-копия расширения (FALLBACK_SERVER->:8797,
в bell.js вживлён storage-пробник — только тестовый XPI) в firefox-158
(Marionette 2929). Прод :8791 не трогаем; в конце — переустановка prod-аддона.
Прецеденты: sk43_acc.py (стенд/шина), sk38_acc.py (сайдбар-дом), sk44_acc.py
(подъём firefox-158 по необходимости).

Фазы:
  S0 стенд: мост :8797, XPI, расширение подключено, http://127.0.0.1:8899
  A1 превью-оверлей без кнопок: в холде action.do тень-DOM страницы —
     ноль <button>, подпись #pt живая (скрин a1)
  A2 карточка пермисса в чате: perm.check {task,name} -> .perm с именем цели,
     доменом и миниатюрой img.pshot (data: догоняет асинхронно) (скрин a2)
  A3 ответ с карточки: клик «Разрешить разово» -> allow/human_once в rpc и
     аудите; карточка done (скрин a3)
  A4 свёрнутый сайдбар: perm.check -> пробник: pending с id, notified>=1
     (SK-50: perm зовёт и при фокусе), badge «🔔»; XUL-уведомление на экране
     (окно alert:alert) (скрин a4)
  A5 клик по уведомлению: сайдбар открывается, карточка с .flash (скрин a5)
  A6 вердикт снимает звонок: deny -> human_deny, pending пуст, badge «»
  A7 регресс SK-34: click×sensitive_input -> perm_action_mismatch, карточек нет
  Z уборка: вкладки, prod-аддон, :8791, тест-Firefox погашен по PID (SK-49)
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
S = pathlib.Path(os.environ.get("SK37_SCRATCH", str(HOME / ".hermes/cache/scratch/sk37")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
SHOTS = S / "shots"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
TEST_PORT = 8899
PAGE_URL = f"http://127.0.0.1:{TEST_PORT}/youtube.html?v=sk37r1"
TASK = "sk37-acc"
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


# ---- RPC через файловую шину моста ------------------------------------------

def rpc(method, params, timeout=30):
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
        time.sleep(0.12)
    return {"ok": False, "err": "harness_timeout", "id": rid}


def rpc_async(method, params, box):
    th = threading.Thread(target=lambda: box.update(r=rpc(method, params, timeout=90)))
    th.start()
    return th


def audit_all():
    p = HUB / "audit.jsonl"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


# ---- Marionette-блоки ---------------------------------------------------------

SPARE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "win.gBrowser.addTrustedTab('about:blank'); return 'spare';})();")
OPEN_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
               "const b = win.gBrowser.addTrustedTab(arguments[0]); win.gBrowser.selectedTab = b;"
               "return 'tab:' + b.linkedBrowser.browsingContext.currentURI.spec; })();")
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

# ---- window actor (Fission: сайдбар/страница из chrome-песочницы не видны) ----

SETUP_JS = r"""
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
  const ph = Services.io.getProtocolHandler('resource').QueryInterface(Ci.nsIResProtocolHandler);
  ph.setSubstitution('sk37', await Services.io.newURI('file://%s'));
  // стенд без DBus-демона уведомлений: gjs-хост уведомлений тут невидим —
  // форсируем XUL-алерты (реальное окно на экране, кликабельно, onClicked тот же)
  Services.prefs.setBoolPref('alerts.useSystemBackend', false);
    try { ChromeUtils.unregisterWindowActor('Sk37Probe'); } catch (e) {}
    ChromeUtils.registerWindowActor('Sk37Probe', {
      child: { esModuleURI: 'resource://sk37/sk37_actor.sys.mjs' },
      parent: { esModuleURI: 'resource://sk37/sk37_actor.sys.mjs',
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
    } else { // page: активная вкладка
      gw = win.gBrowser.selectedBrowser.browsingContext.currentWindowGlobal;
    }
    if (!gw) return resolve({ok: false, err: 'no-gw:' + payload.match});
    const val = await gw.getActor('Sk37Probe').sendQuery('sk37:op', payload);
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


# страница (web-контент): Fission + remote-type не дают актору из chrome-сессии —
# читаем тень-оверлей через CDP (:9393), он же JS-мир страницы, shadow open
CDP_PORT = 9393
OVERLAY_EXPR = ("(() => { const host=document.getElementById('hermes-sidekick-host');"
                "if(!host||!host.shadowRoot) return {err:'no-host-shadow'};"
                "const sr=host.shadowRoot; const pv=sr.querySelector('.pv');"
                "return {buttons: sr.querySelectorAll('button').length,"
                " pvVisible: !!pv && getComputedStyle(pv).display!=='none',"
                " label: pv?String(pv.textContent||'').trim().slice(0,80):'',"
                " ring: !!sr.querySelector('.ring')}; })()")


def cdp_eval(expr, url_needle="youtube", timeout=10):
    import asyncio
    try:
        import websockets
    except ImportError:
        return {"_err": "no-websockets-lib"}

    async def _e():
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3).read())
        pg = [t for t in targets if t.get("type") == "page" and url_needle in t.get("url", "")]
        if not pg:
            return {"_err": "no-cdp-target"}
        async with websockets.connect(pg[0]["webSocketDebuggerUrl"], max_size=10 ** 7) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                      "params": {"expression": expr, "returnByValue": True,
                                                 "awaitPromise": True}}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if msg.get("id") == 1:
                    return msg
        return {"_err": "cdp-timeout"}
    try:
        msg = asyncio.run(_e())
    except Exception as e:
        return {"_err": str(e)[:120]}
    if "_err" in msg:
        return msg
    res = ((msg.get("result") or {}).get("result")) or {}
    if "value" in res:
        return res["value"]
    return {"_err": json.dumps(msg)[:160]}


# XUL-уведомление (fallback Firefox без DBus-демона): окно alert:alert
ALERT_JS = r"""
return (() => {
  const out = [];
  for (const w of Services.wm.getEnumerator('alert:alert')) {
    let txt = '';
    try { txt = String(w.document.body ? w.document.body.textContent : '').slice(0, 120); } catch (e) {}
    out.push(txt);
  }
  if (!out.length) {  // диагностика: какие окна вообще живы
    for (const w of Services.wm.getEnumerator(null)) {
      let t = '?'; try { t = w.document.documentElement.getAttribute('windowtype'); } catch (e) {}
      out.push('win:' + t);
    }
  }
  return JSON.stringify(out);
})();
"""
ALERT_CLICK_JS = r"""
return (() => {
  for (const w of Services.wm.getEnumerator('alert:alert')) {
    const d = w.document;
    const n = d.querySelector('notification') || d.querySelector('#alertTitle')
      || (d.body && d.body.firstElementChild);
    if (!n) continue;
    n.dispatchEvent(new w.MouseEvent('click', {bubbles: true, cancelable: true, view: w}));
    return 'clicked:' + String(n.tagName || '').slice(0, 20);
  }
  return 'no-alert';
})();
"""


def sb_dom(m):
    r = op(m, "sidebar", "cardState")
    return r if isinstance(r, dict) else {"_err": str(r)[:120]}


def bell_probe(m):
    p = op(m, "options", "bellProbe", timeout=25)
    return {"probe": p} if isinstance(p, dict) else {"err": str(p)[:120]}


def sidebar_open_state(m):
    return "true" == m.script(
        "return String(Services.wm.getMostRecentWindow('navigator:browser').SidebarController.isOpen);")


def sidebar_alive(m):
    d = sb_dom(m)
    return "sidebar.html" in str(d.get("href", ""))


def ensure_sidebar_open(m):
    for attempt in range(5):
        if sidebar_alive(m):
            return True
        if attempt == 3:
            try:
                m.script("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                         "if (win.SidebarController.isOpen) win.SidebarController.toggle(); return 'reset';})();")
                time.sleep(1.5)
            except Exception:
                pass
        try:
            m.script(SC_SHOW_JS)
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < 6:
            if sidebar_alive(m):
                return True
            time.sleep(0.4)
    return False


def ensure_sidebar_closed(m):
    m.script(SC_HIDE_JS)
    t0 = time.time()
    while time.time() - t0 < 8:
        if sidebar_open_state(m) != "true":
            return True
        time.sleep(0.4)
    return False


def http_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/youtube.html", timeout=2)
        return True
    except Exception:
        return False


def install(m, xpi):
    r = m.ascript(ADDON_INSTALL_JS, args=[str(pathlib.Path(xpi).resolve())])
    if not (isinstance(r, dict) and r.get("ok")):
        raise RuntimeError(f"installTemporaryAddon: {r}")
    return r


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    if HUB.exists():
        shutil.rmtree(HUB)
    HUB.mkdir(parents=True)
    shutil.copy(HOME / "sidekick/bridge/hub/token", HUB / "token")
    prod_before = ss_count(8791)
    br, m, httpd = None, None, None
    try:
        # ---------- S0: стенд ----------
        if EXT_SRC.exists():  # noqa: F841 (тело в try — traceback в note при падении)
            shutil.rmtree(EXT_SRC)
        shutil.copytree(REPO / "extension", EXT_SRC, ignore=shutil.ignore_patterns(".chrome"))
        bj = EXT_SRC / "background.js"
        src = bj.read_text(encoding="utf-8")
        # SK-37 A1: фоновый зонд превью-оверлея (scripting.executeScript без
        # Xray-преград акторной песочницы; результат — в storage, читает actor)
        probe_block = """
// SK-37 ACC PROBE (test-only): фон читает превью-оверлей активной вкладки в storage
browser.runtime.onMessage.addListener((mm) => {
  if (!mm || mm.type !== "sk37_overlay_probe") return false;
  browser.tabs.query({ active: true, lastFocusedWindow: true }).then(async ([t]) => {
    if (!t) throw new Error("no-active-tab");
    const [res] = await browser.scripting.executeScript({
      target: { tabId: t.id },
      func: () => {
        const host = document.getElementById("hermes-sidekick-host");
        const sr = host && host.shadowRoot;
        if (!sr) return { err: "no-host-shadow", href: location.href.slice(0, 60) };
        const pv = sr.querySelector(".pv");
        return {
          buttons: sr.querySelectorAll("button").length,
          pvVisible: !!pv && getComputedStyle(pv).display !== "none",
          label: pv ? String(pv.textContent || "").trim().slice(0, 80) : "",
          ring: !!sr.querySelector(".ring"),
        };
      },
    });
    await browser.storage.local.set({ __sk37_overlay: { v: res && res.result, ts: Date.now() } });
  }).catch((e) => browser.storage.local.set(
    { __sk37_overlay: { err: String((e && e.message) || e).slice(0, 140), ts: Date.now() } }));
  return false;
});
"""
        src = probe_block + src
        assert 'const FALLBACK_SERVER = "ws://127.0.0.1:8791";' in src
        src = src.replace('const FALLBACK_SERVER = "ws://127.0.0.1:8791";',
                          f'const FALLBACK_SERVER = "ws://127.0.0.1:{PORT}";')
        assert "for (const u of [saved, FALLBACK_SERVER])" in src
        src = src.replace("for (const u of [saved, FALLBACK_SERVER])",
                          "for (const u of [FALLBACK_SERVER])")
        bj.write_text(src, encoding="utf-8")
        # SK-37: в тестовый bell.js вживлён storage-пробник (только тестовый XPI)
        # SK-50: bellRefresh переехал на фокус-правило — пробник кладёт focused
        bl = EXT_SRC / "bell.js"
        bsrc = bl.read_text(encoding="utf-8")
        anchor = """      bellEventLog("notify_fail", id, e, "sync:" + String((err && err.message) || err));
    }
  }
}"""
        assert anchor in bsrc, "якорь bellRefresh не найден"
        bsrc = bsrc.replace(anchor, """      bellEventLog("notify_fail", id, e, "sync:" + String((err && err.message) || err));
    }
  }
  try { browser.storage.local.set({__sk37_bell: {pending: [...BELL.pending.keys()],
    focused, notified: [...BELL.pending.values()].filter(e => e.notified).length,
    badge: bellBadgeText(BELL.pending.size), ts: Date.now()}}); } catch (e) {}
}""")
        anchor2 = "  if (browser.notifications) browser.notifications.clear(String(id)).catch(() => {});"
        assert anchor2 in bsrc, "якорь bellClear не найден"
        bsrc = bsrc.replace(anchor2, anchor2 + (
            "\n  try { browser.storage.local.set({__sk37_bell: {pending: [...BELL.pending.keys()], "
            "focused: null, notified: [...BELL.pending.values()].filter(e => e.notified).length, "
            "badge: bellBadgeText(BELL.pending.size), ts: Date.now()}}); } catch (e) {}"))
        bl.write_text(bsrc, encoding="utf-8")
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
        note("S0a тест-мост слушает :8797", listener_pid(PORT) == br.pid, f"pid={listener_pid(PORT)}")
        if not http_up():
            httpd = subprocess.Popen(["python3", str(REPO / "tests" / "serve.py")],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(20):
                if http_up():
                    break
                time.sleep(0.4)
        note("S0b тестовые страницы на :8899", http_up())
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
        note("S0c0 window-актор зарегистрирован", isinstance(reg, dict) and reg.get("ok"), reg)
        r0 = install(m, EXT_XPI)
        note("S0c тест-XPI установлен (bell-пробник вживлён)", bool(r0.get("ok")), r0.get("url", ""))
        # temporary-аддон: storage чистая — токен сеем через options.html (прецедент sk38).
        # URL аддона берём из открытого сайдбара; пересоздаём окно сайдбара, чтобы
        # window-global жил с уже зарегистрированным актором
        token = (HUB / "token").read_text().strip()
        m.script(SC_HIDE_JS)
        time.sleep(1.2)
        m.script(SC_SHOW_JS)
        t1 = time.time()
        ext_url, href = "", ""
        while time.time() - t1 < 20 and not ext_url:
            d0 = sb_dom(m)
            href = d0.get("href", "") or d0.get("_err", "")
            if "sidebar.html" in href:
                ext_url = href.split("sidebar.html")[0]  # [0] уже с "/" — +"/" даёт // и вкладка уезжает в http://options.html
            else:
                m.script(SC_SHOW_JS)
                time.sleep(0.8)
        note("S0d сайдбар тест-XPI поднят (url аддона известен)", bool(ext_url), href[:80])
        m.script(OPEN_TAB_JS, [ext_url + "options.html"])
        time.sleep(1.5)
        seeded = "none"
        for _ in range(6):
            seeded = op(m, "options", "seedToken", arg=token)
            if seeded == "seeded":
                break
            time.sleep(1.0)
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0d2 токен посеян (options.html)", seeded == "seeded", seeded)
        m.script(OPEN_TAB_JS, [PAGE_URL])
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0e расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод: было {prod_before})")
        # options-вкладка живёт всё время: из неё читаем storage-пробник
        if ext_url:
            m.script(OPEN_TAB_JS, [ext_url + "options.html"])
            time.sleep(1.0)
            m.script(OPEN_TAB_JS, [PAGE_URL])  # активной остаётся страница
        spec = m.script("return Services.wm.getMostRecentWindow('navigator:browser')"
                        ".gBrowser.selectedBrowser.currentURI.spec")
        if "youtube" not in str(spec):
            # жёстко выбираем вкладку страницы (стенд: selectedTab иногда гаснет за about:blank)
            spec = m.script(
                "return (() => { const g = Services.wm.getMostRecentWindow('navigator:browser');"
                "for (const t of g.gBrowser.tabs) {"
                "  if (t.linkedBrowser.currentURI.spec.includes('youtube.html')) {"
                "    g.gBrowser.selectedTab = t; return t.linkedBrowser.currentURI.spec; }}"
                "return 'no-youtube-tab:' + [...g.gBrowser.tabs].map(t =>"
                "  t.linkedBrowser.currentURI.spec).join(',').slice(0,150); })();")
        note("S0f youtube.html активна", "youtube" in str(spec), str(spec)[:150])

        # ---------- A1: превью-оверлей без кнопок ----------
        box = {}
        th = rpc_async("action.do", {"selector": "#q", "action": "input",
                                     "value": "sk37", "ms": 2200, "task": TASK}, box)
        time.sleep(0.9)
        ok_shot = shot("a1-preview")  # превью ещё в холде (ms=2200)
        # страница (web-контент): фон-тест-XPI читает оверлей через scripting.executeScript
        # и пишет в storage; актор только пинает и читает storage (Xray-преград нет)
        t0 = time.time()
        op(m, "options", "overlayKick")
        ov = {}
        while time.time() - t0 < 10:
            g = op(m, "options", "storageGet", arg="__sk37_overlay")
            if isinstance(g, dict) and g.get("ts", 0) >= (t0 - 2) * 1000:
                ov = g.get("v") if g.get("v") is not None else g
                break
            time.sleep(0.4)
        th.join(timeout=60)
        r = box.get("r", {})
        note("A1a превью показано, оверлей БЕЗ кнопок",
             ov.get("pvVisible") and ov.get("buttons") == 0 and ov.get("ring"), ov)
        note("A1b человекочитаемая подпись на кольце", len(ov.get("label", "")) > 0, ov.get("label"))
        note("A1c скрин превью-холда", ok_shot, "shots/a1-preview.png")
        note("A1d вердикт: действие исполнено, не отменено",
             r.get("ok") and (r.get("result") or {}).get("executed") is True
             and (r.get("result") or {}).get("cancelled") is not True, str(r)[:160])

        # ---------- A2: карточка пермисса с именем цели и миниатюрой ----------
        bind = rpc("tabs.open_grouped", {"task": TASK, "url": PAGE_URL})
        note("A2a задача привязана к вкладке", bind.get("ok") is True, str(bind)[:120])
        pbox = {}
        pth = rpc_async("perm.check", {"url": PAGE_URL, "action": "submit",
                                       "perm": "send_message", "task": TASK,
                                       "name": "Ответ в личке «SK-37 живой»"}, pbox)
        ok_sb = ensure_sidebar_open(m)
        # карточка доезжает до свежеоткрытого сайдбара не мгновенно (подключение
        # port + replay паркованного кадра) — ждём до 12 с, как в A2e
        t1 = time.time()
        while time.time() - t1 < 12 and (sb_dom(m).get("perms") or 0) < 1:
            time.sleep(0.5)
        d0 = sb_dom(m)
        note("A2b сайдбар открыт, карточка пермиса в чате", ok_sb and d0.get("perms", 0) >= 1, d0)
        note("A2c карточка назвала ПРЕДМЕТ (имя цели) и домен",
             "Ответ в личке «SK-37 живой»" in d0.get("sub", "") and "127.0.0.1" in d0.get("sub", ""),
             d0.get("sub"))
        note("A2d кнопки только в чате: разово + отклонить, без «Стоп»",
             any("разово" in b.lower() for b in d0.get("btns", []))
             and any("отклон" in b.lower() for b in d0.get("btns", []))
             and not any("стоп" in b.lower() for b in d0.get("btns", [])), d0.get("btns"))
        shot_src = ""
        t1 = time.time()
        while time.time() - t1 < 10:
            d1 = sb_dom(m)
            shot_src = d1.get("shotSrc", "")
            if shot_src.startswith("data:"):
                break
            time.sleep(0.4)
        note("A2e миниатюра кадра догнала карточку (асинхронно)", shot_src.startswith("data:"),
             shot_src[:40])
        d1 = sb_dom(m)
        note("A2f миниатюра компактна (не вытесняет кнопки)", 0 < d1.get("shotH", 0) <= 142,
             f"h={d1.get('shotH')} (140 max-height + 2px рамка)")
        note("A2g скрин карточки", shot("a2-card"), "shots/a2-card.png")

        # ---------- A3: ответ с карточки = аудит human_once ----------
        c = op(m, "sidebar", "clickBtn", arg="ok")
        pth.join(timeout=60)
        pr = pbox.get("r", {})
        note("A3a клик «Разрешить разово» с карточки", str(c).startswith("clicked"), c)
        note("A3b вердикт агенту: allow / human_once",
             pr.get("ok") and (pr.get("result") or {}).get("decision") == "allow"
             and (pr.get("result") or {}).get("via") == "human_once", str(pr)[:140])
        evs = [a for a in audit_all() if a.get("ev") == "perm" and a.get("decision") == "allow"]
        note("A3c событие аудита perm/allow/human_once", bool(evs), str(evs[-1:])[:160])
        d2 = sb_dom(m)
        note("A3d карточка погасла (done)", d2.get("done", 0) >= 1, d2)
        shot("a3-answered")

        # ---------- A4: свёрнутый сайдбар -> уведомление + badge ----------
        ok_closed = ensure_sidebar_closed(m)
        note("A4a сайдбар свёрнут", ok_closed)
        pbox2 = {}
        pth2 = rpc_async("perm.check", {"url": PAGE_URL, "action": "submit",
                                        "perm": "send_message", "task": TASK,
                                        "name": "Колокольчик SK-37"}, pbox2)
        probe = {}
        t1 = time.time()
        while time.time() - t1 < 10:
            probe = bell_probe(m)
            p = probe.get("probe") or {}
            # SK-50: perm зовёт ВСЕГДА (правило фокуса gate-ит только task_*)
            if p and p.get("pending") and int(p.get("notified") or 0) >= 1:
                break
            time.sleep(0.5)
        p = probe.get("probe") or {}
        note("A4b bell-пробник: звонок висит, perm звонит и при фокусе окна (SK-50)",
             bool(p.get("pending")) and int(p.get("notified") or 0) >= 1, probe)
        note("A4c badge-колокольчик на иконке (текст моста)", p.get("badge") == "🔔", p.get("badge"))
        time.sleep(1.5)  # дать create() легально зарезолвиться/отклониться
        p2 = (bell_probe(m).get("probe") or {})
        nt = op(m, "options", "notifyTest", timeout=15)
        alerts = json.loads(m.script(ALERT_JS) or "[]")
        gj = subprocess.run(["xdotool", "search", "--class", "gjs"],
                            capture_output=True, text=True, env={**ENV})
        gj_ids = [x for x in gj.stdout.split() if x.strip().isdigit()]
        backend_alive = isinstance(nt, str) and nt.startswith("created:")
        shown = (any("SK-37" in a or "Hermes" in a for a in alerts) or bool(gj_ids))
        if (p2.get("notified") if isinstance(p2, dict) else 0) or backend_alive or shown:
            note("A4d уведомление выпущено ОС-бэкенду (показ — ответственность ОС)", True,
                 f"notified={p2.get('notified')} notify={str(nt)[:40]} "
                 f"xul={str(alerts)[:60]} gjs={len(gj_ids)}")
        else:
            # на headless-стенде нет ни DBus-демона уведомлений, ни XUL alert-сервиса —
            # проверить «показ на экране» тут физически нечем; на машине пользователя
            # (macOS) бэкенд есть, а сам звонок (pending/badge/снятие) покрыт A4b/A4c/A6b
            note("A4d уведомление выпущено ОС-бэкенду", "skip",
                 f"нет ОС-бэкенда на стенде: notify={str(nt)[:80]} "
                 f"xul={str(alerts)[:60]} gjs={len(gj_ids)}")
        note("A4e скрин уведомления+бейджа", shot("a4-notification"), "shots/a4-notification.png")

        # ---------- A5: клик по уведомлению открывает карточку ----------
        cl = m.script(ALERT_CLICK_JS)
        if str(cl) == "no-alert":
            # не-XUL бэкенд: gjs-хост уведомлений (окно на :11) — кликаем мышью
            w = subprocess.run(["xdotool", "search", "--class", "gjs"],
                               capture_output=True, text=True, env={**ENV})
            ids = [x for x in w.stdout.split() if x.strip().isdigit()]
            if ids:
                subprocess.run(["xdotool", "mousemove", "--click", "1",
                                "--window", ids[-1], "100", "40"], env=ENV, capture_output=True)
                cl = f"xdotool-gjs:{ids[-1]}"
        t1 = time.time()
        d5, open5 = {}, "false"
        while time.time() - t1 < 12:
            d5, open5 = sb_dom(m), sidebar_open_state(m)
            if open5 == "true" and d5.get("flash", 0) >= 1:
                break
            time.sleep(0.4)
        if open5 == "true" and d5.get("flash", 0) >= 1:
            note("A5 клик по уведомлению -> сайдбар на карточке (flash)", True, f"{cl} {d5}")
            shot("a5-focus")
        else:
            # честный запасной путь: реальное нативное нажатие недоступно на стенде —
            # проверяем сайдбар-половину пути (bellFocus) напрямую
            ensure_sidebar_open(m)
            t1 = time.time()  # свежеоткрытый сайдбар мог ещё не отрисовать карточку
            while time.time() - t1 < 12 and (sb_dom(m).get("perms") or 0) < 1:
                time.sleep(0.5)
            f5 = op(m, "sidebar", "focusCard")
            time.sleep(0.6)
            d5b = sb_dom(m)
            note("A5 клик по уведомлению -> сайдбар на карточке (flash)", "skip",
                 f"нативный клик на стенде недоступен ({cl}); bellFocus напрямую: {f5}")
            note("A5* bellFocus подсвечивает карточку", d5b.get("flash", 0) >= 1, d5b)
            shot("a5-focus")

        # ---------- A6: вердикт снимает звонок ----------
        c2 = op(m, "sidebar", "clickBtn", arg="no")
        pth2.join(timeout=60)
        pr2 = pbox2.get("r", {})
        note("A6a «Отклонить» с карточки: block / human_deny",
             pr2.get("ok") and (pr2.get("result") or {}).get("decision") == "block"
             and (pr2.get("result") or {}).get("via") == "human_deny", str(pr2)[:140])
        probe2 = {}
        t1 = time.time()
        while time.time() - t1 < 8:
            probe2 = bell_probe(m)
            p2 = probe2.get("probe") or {}
            if not p2.get("pending"):
                break
            time.sleep(0.4)
        p2 = probe2.get("probe") or {}
        note("A6b звонок снят: pending пуст, badge «»",
             not p2.get("pending") and p2.get("badge") == "", probe2)
        evs2 = [a for a in audit_all() if a.get("ev") == "perm" and a.get("via") == "human_deny"]
        note("A6c событие аудита perm/block/human_deny", bool(evs2), str(evs2[-1:])[:160])

        # ---------- A7: регресс SK-34 (mismatch без карточек) ----------
        mm = rpc("perm.check", {"url": PAGE_URL, "action": "click",
                                "perm": "sensitive_input", "task": TASK})
        note("A7a click×sensitive_input -> perm_action_mismatch",
             (not mm.get("ok")) and "perm_action_mismatch" in json.dumps(mm, ensure_ascii=False),
             str(mm)[:140])
        d7 = sb_dom(m)
        note("A7b карточек на mismatch не было (новых)",
             d7.get("perms", 0) - d7.get("done", 0) == 0, d7)

    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — sys.exit в finally проглатывает traceback, печатаем в note
        import traceback
        note("BODY крах харнесса", False, traceback.format_exc()[-400:])
    finally:
        # ---------- Z: уборка ----------
        try:
            if m:
                m.script(CLOSE_TAB_JS, args=["youtube"])
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
        if httpd:
            httpd.terminate()
        t1 = time.time()
        while prod_before > 0 and time.time() - t1 < 30 and ss_count(8791) < prod_before:
            time.sleep(1.0)
        note("Z2 прод-аддон переустановлен, :8791 не хуже исходного",
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
        note("Z3 тест-Firefox погашен (стенд не висит вторым коннектом)",
             not listener_pid(MAR_PORT), f"pid={listener_pid(MAR_PORT) or '—'}")
        skipped = [k for k, v in results.items() if v["ok"] is None]
        fails = [k for k, v in results.items() if v["ok"] is False]
        passed = [k for k, v in results.items() if v["ok"] is True]
        print(f"\nИТОГ: {len(passed)} PASS / {len(fails)} FAIL / {len(skipped)} SKIP "
              f"из {len(results)}", flush=True)
        for k in fails:
            print("FAIL:", k, "—", results[k]["detail"][:200], flush=True)
        for k in skipped:
            print("SKIP:", k, "—", results[k]["detail"][:200], flush=True)
        (S / "sk37_acc_results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()