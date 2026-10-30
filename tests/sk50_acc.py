#!/usr/bin/env python3
"""SK-50 живая приёмка «уведомления и журнал»: настоящий Firefox, настоящий мост,
настоящая focus-политика всплывашек.

Стенд: изолированный мост :8797 + тест-копия расширения (FALLBACK_SERVER->:8797,
в bell.js вживлён storage-пробник focused/notified/cerr) в firefox-158
(Marionette 2929). Прод :8791 не трогаем; в конце — переустановка prod-аддона.
Каркас — sk35_acc.py (шина rpc, outbox-кадры, сайдбар-хелперы), пробник — sk37.

Фазы:
  S0 стенд: мост :8797, XPI, токен, сайдбар; фокус-базлайн: storage-override
#      __sk50_force_focus=true -> probe focused=True (xul focus на X11 недостижим)
  B1 в фокусе окна: задача done -> bell+журнал БЕЗ всплывашки (notified=0,
     events.jsonl по id пуст, XUL-алертов нет) — правило SK-50 «островок»
  B2 открытие журнала НЕ читает: панель открыта, счётчик и непрочитанное целы,
     звонок задачи не снят («посмотреть ≠ ликвидировать»)
  B3 клик по событию читает именно его: storage read=True, звонок задачи снят,
     счётчик 0, панель НЕ авто-закрылась
  B4 perm звонит ВСЕГДА: карточка в чате, при focused=True notified>=1,
     XUL-алерт на экране, events.jsonl notify (лег не глотается)
  B5 «Прочитать всё»: явная кнопка -> счётчик 0, журнал весь read, bell перма жив
  B5b вердикт allow: карточка closed, bell перма снят
  B6 фокус-переход (storage-override): задача при focused=true молчит;
     setFocus(false)+bellRefresh -> всплывашка (notified>=1 + events + XUL-алерт);
     возврат setFocus(true) -> тост снят (clear), событие в events.jsonл осталось
  B7 options «Проверить звонок»: клик кнопкой -> статус «выпущено», events.jsonl
     notify_test (тот же notifications.create, иконка — реальный PNG)
  B8 журнал живёт 12+ с без авто-закрытия; «Прочитать всё» -> счётчик 0
  R1 прод :8791: тестовых коннектов не добавилось
  Z  вкладки, prod XPI, тест-Firefox по PID, мост повешен

Запуск: python3 tests/sk50_acc.py
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
S = pathlib.Path(os.environ.get("SK50_SCRATCH", str(HOME / ".hermes/cache/scratch/sk50")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
SHOTS = S / "shots"
EVENTS = HUB / "events.jsonl"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
TEST_PORT = 8899
PAGE_URL = f"http://127.0.0.1:{TEST_PORT}/youtube.html?v=sk50r1"
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


# ---- SK-50: эмуляция фокуса окна — storage-override (см. setFocus в
#      sk35_actor.sys.mjs и якорь bellWindowFocused в S0): X-машинерия на
#      :10 недостижима для xul focus (замер 26.10), X-хелперы удалены. ---


# ---- RPC через файловую шину моста + outbox-кадры (каркас sk35) --------------

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


def out(frames):
    if isinstance(frames, dict):
        frames = [frames]
    with open(HUB / "outbox.jsonl", "a", encoding="utf-8") as f:
        for fr in frames:
            f.write(json.dumps({"v": 1, **fr}, ensure_ascii=False) + "\n")


def task_running(tid, title, chat, long=False):
    fr = {"type": "task", "id": tid, "title": title, "state": "running", "chat": chat}
    if long:
        fr["long"] = True  # SK-51: bell зовёт только долгая задача, а долгая — только по long:true
    return [fr,
            {"type": "step", "task": tid, "n": 1, "text": "Думаю…", "state": "active", "chat": chat}]


def task_terminal(tid, state):
    return {"type": "task", "id": tid, "state": state}


def out_until(frames, pred, timeout=14, every=2.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        out(frames)
        t1 = time.time()
        while time.time() - t1 < every:
            if pred():
                return True
            time.sleep(0.4)
    return pred()


def sent_count():
    try:
        return (HUB / "bridge.log").read_text(encoding="utf-8", errors="replace") \
            .count("msg(s) from outbox")
    except FileNotFoundError:
        return 0


def out_delivered(frames, timeout=6):
    n0 = sent_count()
    out(frames)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if sent_count() > n0:
            return True
        time.sleep(0.3)
    out(frames)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if sent_count() > n0:
            return True
        time.sleep(0.3)
    return False


# ---- Marionette-блоки (каркас sk35; актор Sk35Probe + SK-50 ops) -------------

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
  ph.setSubstitution('sk35', await Services.io.newURI('file://%s'));
  Services.prefs.setBoolPref('alerts.useSystemBackend', false);
    try { ChromeUtils.unregisterWindowActor('Sk35Probe'); } catch (e) {}
    ChromeUtils.registerWindowActor('Sk35Probe', {
      child: { esModuleURI: 'resource://sk35/sk35_actor.sys.mjs' },
      parent: { esModuleURI: 'resource://sk35/sk35_actor.sys.mjs',
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
    const val = await gw.getActor('Sk35Probe').sendQuery('sk35:op', payload);
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


def bell_dom(m):
    d = op(m, "sidebar", "bellDom")
    return d if isinstance(d, dict) else {"_err": str(d)[:120]}


def perm_dom(m):
    d = op(m, "sidebar", "permDom")
    return d if isinstance(d, dict) else {"_err": str(d)[:120]}


def bell_probe(m):
    p = op(m, "options", "storageGet", arg="__sk35_bell", timeout=25)
    return p if isinstance(p, dict) else {"err": str(p)[:120]}


def probe_refresh(m):
    return op(m, "sidebar", "probeRefresh")


def bell_log(m):
    lg = op(m, "options", "storageGet", arg="sk35BellLog", timeout=25)
    return lg if isinstance(lg, list) else []


def sidebar_open_state(m):
    return str(m.script(
        "return String(Services.wm.getMostRecentWindow('navigator:browser').SidebarController.isOpen);"))


def sidebar_alive(m):
    d = bell_dom(m)
    return "sidebar.html" in str(d.get("href", ""))


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


def ensure_sidebar_open(m):
    return sidebar_force_open(m)


# ---- XUL-алерты (нативные уведомления в этом профиле) ------------------------
ALERT_JS = r"""
return (() => {
  const out = [];
  for (const w of Services.wm.getEnumerator('alert:alert')) {
    try {
      const d = w.document;
      const t = d.getElementById('alertTitleLabel');
      const x = d.getElementById('alertTextLabel');
      out.push((String(t && t.value || '') + ' — ' + String(x && x.value || '')).slice(0, 160));
    } catch (e) { out.push('err:' + String(e.message).slice(0, 60)); }
  }
  return JSON.stringify(out);
})();"""
CLOSE_ALERTS_JS = r"""
return (() => {
  let n = 0;
  for (const w of Services.wm.getEnumerator('alert:alert')) { try { w.close(); n++; } catch (e) {} }
  return 'closed:' + n;
})();"""


def open_alerts(m):
    try:
        return json.loads(m.script(ALERT_JS) or "[]")
    except Exception:
        return []


def close_alerts(m):
    try:
        m.script(CLOSE_ALERTS_JS)
    except Exception:
        pass


def ev_notify(ids):
    return ev_frame("notify", ids)


def ev_frame(ev, ids=None):
    if not EVENTS.exists():
        return []
    out2 = []
    for ln in EVENTS.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            j = json.loads(ln)
        except ValueError:
            continue
        if not (isinstance(j, dict) and j.get("type") == "bell_event" and j.get("ev") == ev):
            continue
        if ids is None or str(j.get("id")) in [str(i) for i in ids]:
            out2.append(j)
    return out2


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
        # ---------- S0: стенд (каркас sk35: подмена сервера + bell-пробник) ----
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

        # SK-50 диагностика (только тест-XPI): bellWindowFocused уважает
        # storage-override __sk50_force_focus. ЗАМЕР 26.10: на Xorg :10 xul
        # focus/blur-топики молчат в ЛЮБОМ режиме (raw windowfocus, WM
        # windowactivate, map-focus xcalc, click-to-focus): «GTK native-widget
        # focus disconnect» — WM крутит фокус на нативном X-виджете Firefox
        # (1280x1040), GTK top-level (10x10) событий не видит →
        # windows.onFocusChanged не доживает до расширения. В проде (macOS)
        # Gecko берёт фокус нативно без X11-слоя; приёмка на стенде подаёт
        # состояние фокуса в ту же точку чтения (production bell.js чист).
        bl = EXT_SRC / "bell.js"
        bsrc = bl.read_text(encoding="utf-8")
        anchorF = ('async function bellWindowFocused() {\n  try {\n'
                   '    const wins = await browser.windows.getAll({ windowTypes: ["normal"] });')
        assert anchorF in bsrc, "якорь bellWindowFocused не найден"
        bsrc = bsrc.replace(anchorF, """async function bellWindowFocused() {
  try { const g = await browser.storage.local.get("__sk50_force_focus");
        if (g && typeof g.__sk50_force_focus === "boolean") return g.__sk50_force_focus; } catch (e) {}
  try {
    const wins = await browser.windows.getAll({ windowTypes: ["normal"] });""")
        anchor = """      bellEventLog("notify_fail", id, e, "sync:" + String((err && err.message) || err));
    }
  }
}"""
        assert anchor in bsrc, "якорь bellRefresh не найден"
        bsrc = bsrc.replace(anchor, """      self.__sk50cerr = "sync:" + String((err && err.message) || err);
      bellEventLog("notify_fail", id, e, "sync:" + String((err && err.message) || err));
    }
  }
  try { browser.storage.local.set({__sk35_bell: {pending: [...BELL.pending.keys()],
    focused, notified: [...BELL.pending.values()].filter(e => e.notified).length,
    badge: bellBadgeText(BELL.pending.size), cerr: String(self.__sk50cerr || ""), ts: Date.now()}}); } catch (e) {}
}""")
        anchor3 = ('.catch((er) => { e.notified = false;\n'
                   '          bellEventLog("notify_fail')
        assert anchor3 in bsrc, "якорь create-catch не найден"
        bsrc = bsrc.replace(anchor3, ('.catch((er) => { e.notified = false;\n'
                                      '          self.__sk50cerr = "promise:" + String((er && er.message) || er);\n'
                                      '          bellEventLog("notify_fail'))
        anchor2 = "  if (browser.notifications) browser.notifications.clear(String(id)).catch(() => {});"
        assert anchor2 in bsrc, "якорь bellClear не найден"
        bsrc = bsrc.replace(anchor2, anchor2 + (
            "\n  try { browser.storage.local.set({__sk35_bell: {pending: [...BELL.pending.keys()], "
            "focused: null, notified: [...BELL.pending.values()].filter(e => e.notified).length, "
            "badge: bellBadgeText(BELL.pending.size), ts: Date.now()}}); } catch (e) {}"))
        bsrc += ("\nif (browser.runtime.onMessage) browser.runtime.onMessage.addListener((m, s, sendResponse) => {"
                 "if (m && m.type === '__sk35_clean') { BELL_LOG.length = 0; BELL.pending.clear();"
                 "browser.storage.local.remove(['sk35BellLog', '__sk35_bell', '__sk35dbg']); }"
                 "if (m && m.type === '__sk50_probe') { bellRefresh().then("
                 "() => sendResponse({v:'ok'}), () => sendResponse({v:'err'})); return true; }});\n")
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
        note("S0c тест-XPI установлен (bell-пробник focused/notified/cerr)",
             bool(r0.get("ok")), r0.get("url", ""))
        token = (HUB / "token").read_text().strip()
        t1 = time.time()
        ext_url, href = "", ""
        while time.time() - t1 < 40 and not ext_url:
            sidebar_force_open(m, attempts=2)
            d0 = bell_dom(m)
            href = d0.get("href", "") or d0.get("_err", "") or d0.get("err", "")
            if "sidebar.html" in href:
                ext_url = href.split("sidebar.html")[0]  # [0] уже с "/" — +"/" даёт // и вкладка уезжает в http://options.html
            else:
                time.sleep(1.0)
        note("S0d сайдбар тест-XPI поднят (url аддона известен)", bool(ext_url), href[:120])
        if not ext_url:
            raise RuntimeError("сайдбар не поднят — стенд слепой: " + str(href)[:160])
        m.script(OPEN_TAB_JS, [ext_url + "options.html"])
        time.sleep(1.5)
        seeded = "none"
        for _ in range(6):
            seeded = op(m, "options", "seedToken", arg=token)
            if seeded == "seeded":
                break
            time.sleep(1.0)
        note("S0d2 токен посеян (options.html)", seeded == "seeded", seeded)
        note("S0d3 журнал/пробники очищены", op(m, "options", "storageClean") == "clean")
        m.script(OPEN_TAB_JS, [PAGE_URL])
        t1 = time.time()
        # WS-подключение тест-XPI к :8797: холодный старт MV3-воркера после
        # installTemporaryAddon бывает медленным (замер run4: >25 с) — окно 60 с,
        # иначе B1 снимается до подключения (no_extension).
        while time.time() - t1 < 60 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0e расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод: было {prod_before})")
        ok0 = ensure_sidebar_open(m)
        note("S0f сайдбар открыт", ok0)

        # фокус-базлайн: storage-override __sk50_force_focus=true (см. комментарий
        # в S0-сборке XPI) + probeRefresh дёргает bellRefresh → probe.focused=True.
        close_alerts(m)
        op(m, "options", "setFocus", True)
        t1 = time.time()
        pr = {}
        while time.time() - t1 < 12:
            probe_refresh(m)
            time.sleep(0.6)
            pr = bell_probe(m)
            if isinstance(pr, dict) and pr.get("focused") is True:
                break
        note("S0g фокус-базлайн: probe пишет focused=True (storage-override)",
             isinstance(pr, dict) and pr.get("focused") is True, pr)

        # ---------- B1: в фокусе окна задача done -> bell+journal, БЕЗ всплывашки
        close_alerts(m)
        bind = rpc("tabs.open_grouped", {"task": "sk50-b1", "url": PAGE_URL})
        note("B1a вкладка/группа Sidekick создана", bind.get("ok") is True, str(bind)[:120])
        note("B1b running+long:true кадр доставлен", out_delivered(
            task_running("sk50-b1", "Задача B1", "sk50-chat-b1", long=True)))
        time.sleep(13.0)  # SK-51: долгая — по явному long:true; пауза = время на сворот панели/группы
        out_until(task_terminal("sk50-b1", "done"),
                  lambda: "sk50-b1" in (bell_probe(m).get("pending") or []), timeout=14)
        probe = bell_probe(m)
        note("B1c bell pending с id задачи, бейдж 🔔 (задача при фокусе)",
             "sk50-b1" in (probe.get("pending") or []) and probe.get("badge") == "🔔", probe)
        note("B1d в фокусе окна всплывашки НЕТ: notified=0 (SK-50 правило)",
             probe.get("notified") == 0 and probe.get("focused") is True, probe)
        time.sleep(2.5)
        note("B1e events.jsonl по id пуст (create не вызывался — легу нечего логировать)",
             not ev_notify(["sk50-b1"]), ev_notify(["sk50-b1"]))
        alerts = open_alerts(m)
        note("B1f XUL-алертов нет (фокус держим)",
             not any("сделана" in str(a).lower() for a in alerts), alerts)
        log = bell_log(m)
        note("B1g событие task_done в журнале непрочитанным",
             any((not e.get("read")) and e.get("kind") == "task_done" and e.get("id") == "sk50-b1"
                 for e in log), log[:3])
        shot("b1-silent-focused")

        # ---------- B2: открытие журнала НЕ читает ----------
        ensure_sidebar_open(m)  # долгая задача B1 автоматически свернула панель (SK-35)
        op(m, "sidebar", "clickBell")
        t1 = time.time()
        d2 = {}
        while time.time() - t1 < 8:
            d2 = bell_dom(m)
            if d2.get("panelHidden") is False:
                break
            time.sleep(0.5)
        note("B2a панель журнала открыта, счётчик непрочитанного на месте",
             d2.get("panelHidden") is False and d2.get("bellnHidden") is False
             and int(d2.get("unread") or 0) >= 1, d2)
        probe = bell_probe(m)
        log = bell_log(m)
        row = next((e for e in log if e.get("id") == "sk50-b1"), {})
        note("B2b открытие НЕ пометило прочитанным; звонок задачи цел (SK-50)",
             row.get("read") is False and "sk50-b1" in (probe.get("pending") or []),
             {"row": row, "pending": probe.get("pending")})
        shot("b2-open-unread")

        # ---------- B3: клик по событию читает именно его ----------
        op(m, "sidebar", "clickRow", "task_done")
        t1 = time.time()
        d3, probe3, row3 = {}, {}, {}
        while time.time() - t1 < 8:
            d3 = bell_dom(m)
            probe3 = bell_probe(m)
            log3 = bell_log(m)
            row3 = next((e for e in log3 if e.get("id") == "sk50-b1"), {})
            if row3.get("read") is True and d3.get("bellnHidden") is True \
                    and "sk50-b1" not in (probe3.get("pending") or []):
                break
            time.sleep(0.5)
        note("B3 клик по событию: read=True, звонок снят, счётчик 0, панель не авто-закрылась",
             row3.get("read") is True and d3.get("bellnHidden") is True
             and "sk50-b1" not in (probe3.get("pending") or [])
             and d3.get("panelHidden") is False,
             {"row": row3, "dom": {k: d3.get(k) for k in ("bellnHidden", "panelHidden")},
              "pending": probe3.get("pending")})

        # ---------- B4: perm звонит ВСЕГДА (даже при focused=True) ----------
        close_alerts(m)
        pbox = {}
        rpc_async("perm.check", {"url": PAGE_URL, "action": "submit", "perm": "send_message",
                                 "task": "sk50-b4", "name": "Колокольчик SK-50"}, pbox)
        t1 = time.time()
        pd, probe4 = {}, {}
        while time.time() - t1 < 12:
            pd = perm_dom(m)
            probe4 = bell_probe(m)
            if pd.get("open", 0) >= 1 and int(probe4.get("notified") or 0) >= 1 \
                    and len(probe4.get("pending") or []) >= 1:
                break
            time.sleep(0.5)
        note("B4a perm-карточка в чате + bell висит (id выдаёт мост — uuid, не наш task)",
             pd.get("open", 0) >= 1 and len(probe4.get("pending") or []) >= 1,
             {"perm": pd, "pending": probe4.get("pending")})
        note("B4b perm ЗАЗВОНИЛ при focused=True: notified>=1 (SK-50: perm всегда)",
             int(probe4.get("notified") or 0) >= 1 and probe4.get("focused") is True, probe4)
        t1 = time.time()
        alerts4 = []
        while time.time() - t1 < 8:
            alerts4 = open_alerts(m)
            if any("SK-50" in a or "Hermes" in a for a in alerts4):
                break
            time.sleep(0.5)
        shot("b4-perm-alert-focused")
        note("B4c XUL-алерт виден на экране при focused=True (скрин b4-perm-alert-focused)",
             any("SK-50" in a or "Hermes" in a for a in alerts4),
             alerts4 if alerts4 else f"cerr={probe4.get('cerr')!r} notified={probe4.get('notified')}")
        ev4 = [j for j in ev_frame("notify") if j.get("kind") == "perm"]
        note("B4d events.jsonl: bell_event notify kind perm (лег create виден, не глотается)",
             len(ev4) >= 1, ev4[:2])

        # ---------- B5: «Прочитать всё» ----------
        d5pre = bell_dom(m)
        if d5pre.get("panelHidden") is not False:
            op(m, "sidebar", "clickBell")
            t1 = time.time()
            while time.time() - t1 < 6 and bell_dom(m).get("panelHidden") is not False:
                time.sleep(0.5)
        op(m, "sidebar", "clickReadAll")
        t1 = time.time()
        d5, log5, probe5 = {}, [], {}
        while time.time() - t1 < 8:
            d5 = bell_dom(m)
            log5 = bell_log(m)
            probe5 = bell_probe(m)
            if d5.get("bellnHidden") is True and log5 and all(e.get("read") for e in log5):
                break
            time.sleep(0.5)
        note("B5 «Прочитать всё»: счётчик 0, журнал весь read, bell перма ЖИВ",
             d5.get("bellnHidden") is True and bool(log5) and all(e.get("read") for e in log5)
             and len(probe5.get("pending") or []) >= 1,
             {"dom": {k: d5.get(k) for k in ("bellnHidden", "readAllDisabled")},
              "pending": probe5.get("pending")})

        # B5b: вердикт «Разрешить разово» — карточка closed, bell перма снят.
        # Кнопки карточки: класс = режим (ok/alw/no), choice = once/always/deny (SK-34).
        op(m, "sidebar", "clickBtn", "ok")
        t1 = time.time()
        pd5, probe5b = {}, {}
        while time.time() - t1 < 10:
            pd5 = perm_dom(m)
            probe5b = bell_probe(m)
            if pd5.get("open", 0) == 0 and len(probe5b.get("pending") or []) == 0:
                break
            time.sleep(0.5)
        note("B5b «Разрешить разово»: карточка закрыта, bell перма снят (SK-38 связка живая)",
             pd5.get("open", 0) == 0 and len(probe5b.get("pending") or []) == 0,
             {"perm": pd5, "pending": probe5b.get("pending")})
        close_alerts(m)

        # ---------- B6: фокус-переход: молчит в фокусе -> звонит при угоне -----
        # Эмуляция фокуса: storage-override __sk50_force_focus (актор setFocus) —
        # на X11-стенде xul focus недостижим (замер 26.10, см. S0-сборку XPI);
        # в проде bellRefresh зовёт windows.onFocusChanged, на стенде его активно
        # дёргает probeRefresh — путь решения (bellShouldNotify) и лег в events
        # общие с продом.
        close_alerts(m)
        sf_a = op(m, "options", "setFocus", True)
        rpc("tabs.open_grouped", {"task": "sk50-b6", "url": PAGE_URL})
        out_delivered(task_running("sk50-b6", "Задача B6", "sk50-chat-b6", long=True))
        time.sleep(13.0)  # SK-51: долгая — по явному long:true (панель свернётся — B8 это учитывает)
        out_until(task_terminal("sk50-b6", "done"),
                  lambda: "sk50-b6" in (bell_probe(m).get("pending") or []), timeout=14)
        probe6 = bell_probe(m)
        time.sleep(2.5)
        note("B6a задача в фокусе: bell висит, всплывашки нет (per-task: events чист, алертов нет)",
             "sk50-b6" in (probe6.get("pending") or []) and probe6.get("focused") is True
             and not ev_notify(["sk50-b6"]) and not open_alerts(m), probe6)
        sf_b = op(m, "options", "setFocus", False)
        t1 = time.time()
        probe6b, ev6, alerts6 = {}, [], []
        while time.time() - t1 < 12:
            op(m, "options", "probeRefresh")  # свежий кадр пробника + bellRefresh
            probe6b = bell_probe(m)
            ev6 = ev_notify(["sk50-b6"])
            alerts6 = open_alerts(m)
            if int(probe6b.get("notified") or 0) >= 1 and ev6 \
                    and any("сделана" in str(a).lower() for a in alerts6):
                break
            time.sleep(0.5)
        shot("b6-unfocused-alert")
        note("B6b override focused=false -> bellRefresh: всплывашка + events + XUL-алерт",
             int(probe6b.get("notified") or 0) >= 1 and probe6b.get("focused") is False
             and len(ev6) >= 1 and any("сделана" in str(a).lower() for a in alerts6),
             {"probe": probe6b, "ev": len(ev6), "alerts": alerts6[:2], "cerr": probe6b.get("cerr"),
              "sf_a": sf_a, "sf_b": sf_b})
        op(m, "options", "setFocus", True)
        t1 = time.time()
        probe6c, alerts_c = {}, []
        while time.time() - t1 < 10:
            op(m, "options", "probeRefresh")
            probe6c = bell_probe(m)
            alerts_c = open_alerts(m)
            if probe6c.get("notified") == 0 and probe6c.get("focused") is True \
                    and not any("сделана" in str(a).lower() for a in alerts_c):
                break
            time.sleep(0.5)
        note("B6c возврат фокуса: тост снят (notified=0, XUL-алерт закрыт clear-ом); "
             "create-лег остался в events.jsonl",
             probe6c.get("notified") == 0 and probe6c.get("focused") is True
             and len(ev_notify(["sk50-b6"])) >= 1
             and not any("сделана" in str(a).lower() for a in alerts_c),
             {"probe": probe6c, "ev": len(ev_notify(["sk50-b6"])), "alerts": alerts_c[:2]})
        close_alerts(m)

        # ---------- B7: options «Проверить звонок» ----------
        m.script(OPEN_TAB_JS, [ext_url + "options.html"])
        time.sleep(1.5)
        op(m, "options", "clickBellTest")
        t1 = time.time()
        st = {}
        while time.time() - t1 < 10:
            st = op(m, "options", "bellTestSt")
            if isinstance(st, dict) and "выпущено" in (st.get("st") or ""):
                break
            time.sleep(0.5)
        ev7 = ev_frame("notify_test")
        shot("b7-belltest")
        note("B7 «Проверить звонок» кнопкой: статус «выпущено», events.jsonl notify_test",
             isinstance(st, dict) and "выпущено" in (st.get("st") or "") and len(ev7) >= 1,
             {"st": st, "ev": ev7[:1]})

        # ---------- B8: журнал живёт 12+ с без авто-закрытия ----------
        ensure_sidebar_open(m)  # долгая B6 снова свернула панель
        d8pre = bell_dom(m)
        if d8pre.get("panelHidden") is not False:
            op(m, "sidebar", "clickBell")
            t1 = time.time()
            while time.time() - t1 < 6 and bell_dom(m).get("panelHidden") is not False:
                time.sleep(0.5)
        time.sleep(12.0)  # > 10 с старых авто-таймеров — таймеров больше нет
        d8 = bell_dom(m)
        note("B8a журнал открыт 12+ с, счётчик B6-непрочитанного цел, кнопка активна",
             d8.get("panelHidden") is False and d8.get("bellnHidden") is False
             and int(d8.get("unread") or 0) >= 1 and d8.get("readAllDisabled") is False, d8)
        op(m, "sidebar", "clickReadAll")
        t1 = time.time()
        d8b = {}
        while time.time() - t1 < 8:
            d8b = bell_dom(m)
            if d8b.get("bellnHidden") is True:
                break
            time.sleep(0.5)
        shot("b8-journal")
        note("B8b «Прочитать всё» -> счётчик 0 и кнопка disabled",
             d8b.get("bellnHidden") is True and d8b.get("readAllDisabled") is True, d8b)

        # ---------- регресс: прод-шина не тронута ----------
        # тестовый профиль структурно не может на :8791 (токен только в HUB,
        # FALLBACK пропатчен в 8797) — допускаем рост на 1: легитимный
        # auto-reconnect прод-клиента пользователя во время приёмки (замер run4)
        note("R1 прод :8791 за время приёмки не получил тестовых коннектов",
             ss_count(8791) <= prod_before + 1,
             f"@8791={ss_count(8791)} было {prod_before} (допуск +1: reconnect прод-клиента)")
    except Exception as e:
        import traceback
        note("HARNESS-EXC", False, traceback.format_exc()[-500:])
    finally:
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
        t1 = time.time()
        while time.time() - t1 < 15 and listener_pid(MAR_PORT):
            subprocess.run(["kill", str(listener_pid(MAR_PORT))], capture_output=True)
            time.sleep(1.5)
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
        (S / "sk50_acc_results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
