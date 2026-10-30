#!/usr/bin/env python3
"""SK-35 + SK-51 живая приёмка: долгая задача сворачивает сайдбар/группу, bell по завершении.

Стенд: изолированный мост :8797 + тест-копия расширения (FALLBACK_SERVER->:8797,
в bell.js вживлён storage-пробник — только тестовый XPI) в firefox-158
(Marionette 2929). Прод :8791 не трогаем; в конце — переустановка prod-аддона.
Прецеденты: sk37_acc.py (пробник bell/стенд), sk43_acc.py (шина), sk38_acc.py (сайдбар).

SK-51 (v0.7): триггер сворачивания — ЯВНЫЙ кадр агента long:true; эвристики
10 с больше нет. Механика (закрывание панели, ⏳/✓, bell, perm-разворот) — SK-35.

Фазы:
  S0 стенд: мост :8797, XPI, токен, страница, сайдбар открыт
  A1 долгая по явному long:true: сайдбар сам закрылся за <2 с, группа свёрнута
     с ⏳ (скрин a1)
  A2 done: bell pending+бейдж 🔔, XUL-уведомление на экране, группа ✓, журнал с
     событием «Задача сделана» (скрин a2)
  A3 perm посреди долгой: группа РАЗВЕРНУЛАСЬ (⏳ держим), карточка в чате,
     deny -> human_deny; failed -> bell «не сделана» + журнал (скрин a3)
  A4 интерактив 42 с БЕЗ long:true: сайдбар открыт, группа не свёрнута, ⏳ нет,
     по done bell НЕ звонит (скрин a4)
  A5 колокольчик: счётчик непрочитанного, дропдаун с тремя событиями; открытие
     НЕ читает (SK-50); «Прочитать всё» — явная кнопка: снимает счётчик и
     звонок задачи (скрин a5)
  A6 клик по событию: чат задачи + карточка с .flash (скрин a6)
  A7 ручное сворачивание: collapseBtn -> панель закрыта (скрин a7)
  Z  уборка: вкладки, prod-аддон, :8791, тест-Firefox погашен по PID (SK-49)
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
S = pathlib.Path(os.environ.get("SK35_SCRATCH", str(HOME / ".hermes/cache/scratch/sk35")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
SHOTS = S / "shots"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
TEST_PORT = 8899
PAGE_URL = f"http://127.0.0.1:{TEST_PORT}/youtube.html?v=sk35r1"
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


# ---- SK-50: эмуляция фокуса окна — storage-override (setFocus в акторе
#      + якорь bellWindowFocused в сборке XPI): xul focus на X11-стенде
#      недостижим (замер 26.10), X-хелперы удалены. ---


# ---- RPC через файловую шину моста + outbox-рамки ----------------------------

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
    """task/step кадры — как их шлёт Hermes: в outbox.jsonl, мост бродкастит.
    Пачка — ОДНИМ append: мост читает и трёт файл в pump (0.15 с), между read и
    truncate второй append мог бы потеряться."""
    if isinstance(frames, dict):
        frames = [frames]
    with open(HUB / "outbox.jsonl", "a", encoding="utf-8") as f:
        for fr in frames:
            f.write(json.dumps({"v": 1, **fr}, ensure_ascii=False) + "\n")


def task_running(tid, title, chat, long=False):
    fr = {"type": "task", "id": tid, "title": title, "state": "running", "chat": chat}
    if long:
        fr["long"] = True  # SK-51: явный «ухожу в долгую» — единственный триггер сворачивания
    return [fr,
            {"type": "step", "task": tid, "n": 1, "text": "Думаю…", "state": "active", "chat": chat}]


def task_terminal(tid, state):
    return {"type": "task", "id": tid, "state": state}


def out_until(frames, pred, timeout=14, every=2.0):
    """Доставка с переподтверждением: кадр может попасть в окно read→truncate
    моста; tlTask/tlStep идемпотентны — повтор безопасен. pred() == True стоп."""
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
    """Сколько раз мост уже забрал outbox (подсчёт pump-строк bridge.log).
    Рост после append = наши байты прочитаны до truncate — гонка окна read→
    truncate ловится, а не молча проходит."""
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
    out(frames)  # один повтор: возможно, append попал в окно truncate
    t0 = time.time()
    while time.time() - t0 < timeout:
        if sent_count() > n0:
            return True
        time.sleep(0.3)
    return False


# ---- Marionette-блоки (прецедент sk37_acc.py) --------------------------------

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


def group_state(m):
    g = op(m, "options", "groupState")
    return g if isinstance(g, dict) else {"found": False, "_err": str(g)[:120]}


def bell_dom(m):
    d = op(m, "sidebar", "bellDom")
    return d if isinstance(d, dict) else {"_err": str(d)[:120]}


def bell_probe(m):
    p = op(m, "options", "storageGet", arg="__sk35_bell", timeout=25)
    return p if isinstance(p, dict) else {"err": str(p)[:120]}


def bell_log(m):
    lg = op(m, "options", "storageGet", arg="sk35BellLog", timeout=25)
    return lg if isinstance(lg, list) else []


def dbg_timeline(m):
    t = op(m, "options", "storageGet", arg="__sk35dbg", timeout=25)
    return t if isinstance(t, list) else []


def sidebar_open_state(m):
    # ВАЖНО: именно строка "true"/"false" — bool-сравнение со "true" давало
    # ложные «закрыт»/«не открыт» на всём прогоне
    return str(m.script(
        "return String(Services.wm.getMostRecentWindow('navigator:browser').SidebarController.isOpen);"))


def sidebar_alive(m):
    d = bell_dom(m)
    return "sidebar.html" in str(d.get("href", ""))


def sidebar_force_open(m, attempts=6):
    """Сайдбар обязан быть жив: сессия-restore может оставить isOpen=true с МЁРТВЫМ
    панелем (UUID временного аддона прошлой жизни) — SC.show на isOpen коротит,
    поэтому каждый заход: hide -> show -> ждём sidebar.html в документа."""
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


def wait_sidebar_closed(m, timeout=18):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if sidebar_open_state(m) != "true":
            return True
        time.sleep(0.4)
    return False


def wait_group(m, pred, timeout=18):
    t0 = time.time()
    while time.time() - t0 < timeout:
        g = group_state(m)
        if g.get("found") and pred(g):
            return g
        time.sleep(0.5)
    return None


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
        # SK-35 диагностика: таймлайн событий долгой задачи в storage (только тест-XPI)
        dbg_defs = ("const SK35DBG=[];function sk35dbg(s){SK35DBG.push(s+'@'+(Date.now()%10000000));"
                    "try{browser.storage.local.set({__sk35dbg:SK35DBG.slice(-80)})}catch(e){}}\n")
        a_ = "const LONGS = new Set();"
        assert a_ in src, "якорь LONGS не найден"
        src = src.replace(a_, dbg_defs + a_)
        a_ = "function tlTask(msg) {"
        assert a_ in src
        src = src.replace(a_, a_ + '\n  if (String(msg.id||"").startsWith("sk35")) sk35dbg("tl:" + msg.id + ":" + msg.state);')
        a_ = "if (msg.long === true && !LONGS.has(id)) markLong(id).catch(() => {});"
        assert a_ in src, "якорь long:true-триггера (SK-51) не найден"
        src = src.replace(a_, 'if (msg.long === true && !LONGS.has(id)) { sk35dbg("fire:" + id); '
                              'markLong(id).catch((er) => sk35dbg("mlerr:" + (er && er.message || er))); }')
        a_ = '  sendToUi({ type: "sidebar_collapse" });'
        assert a_ in src, "якорь collapse-send не найден"
        src = src.replace(a_, '  sk35dbg("collapse-send:" + id);\n' + a_)
        a_ = "  const gid = await ownGroupId();\n  if (gid == null) return;\n  try {\n    const g = await browser.tabGroups.get(gid);\n    await browser.tabGroups.update(gid, { collapsed: true, title: bellGroupTitle(g.title, \"long\") });"
        assert a_ in src, "якорь markLong-gid не найден"
        src = src.replace(a_, '  const gid = await ownGroupId();\n  sk35dbg("ml-gid:" + gid + " AG:" + JSON.stringify(AG));\n'
                              '  if (gid == null) return;\n  try {\n    const g = await browser.tabGroups.get(gid);\n'
                              '    await browser.tabGroups.update(gid, { collapsed: true, title: bellGroupTitle(g.title, "long") });\n'
                              '    sk35dbg("ml-done:" + id);')
        a_ = "async function finishLong(id, state) {\n  LONGS.delete(id);"
        assert a_ in src, "якорь finishLong не найден"
        src = src.replace(a_, 'async function finishLong(id, state) {\n  sk35dbg("fin:" + id + ":" + state);\n  LONGS.delete(id);')
        a_ = "async function expandGroupForPerm() {"
        assert a_ in src
        src = src.replace(a_, 'async function expandGroupForPerm() {\n  sk35dbg("expand LONGS:" + LONGS.size);')
        a_ = "  AG = { groupId: gid, windowId };"
        assert a_ in src, "якорь ensureGroup-AG не найден"
        src = src.replace(a_, a_ + '\n  sk35dbg("eg-set:" + gid + " longNow:" + (LONGS.size > 0));')
        bj.write_text(src, encoding="utf-8")
        # bell-пробник (тестовый XPI): pending/badge/focused/notified — в storage.
        # SK-50: bell.js переехал на фокус-правило (bellWindowFocused вместо
        # sidebarAction.isOpen) — пробник кладёт focused; причину create()-отказа
        # харнесс ловит в self.__sk35cerr (раньше catch глотал).
        bl = EXT_SRC / "bell.js"
        bsrc = bl.read_text(encoding="utf-8")
        anchor = """      bellEventLog("notify_fail", id, e, "sync:" + String((err && err.message) || err));
    }
  }
}"""
        assert anchor in bsrc, "якорь bellRefresh не найден"
        bsrc = bsrc.replace(anchor, """      self.__sk35cerr = "sync:" + String((err && err.message) || err);
      bellEventLog("notify_fail", id, e, "sync:" + String((err && err.message) || err));
    }
  }
  try { browser.storage.local.set({__sk35_bell: {pending: [...BELL.pending.keys()],
    focused, notified: [...BELL.pending.values()].filter(e => e.notified).length,
    badge: bellBadgeText(BELL.pending.size), cerr: String(self.__sk35cerr || ""), ts: Date.now()}}); } catch (e) {}
}""")
        anchor3 = ('.catch((er) => { e.notified = false;\n'
                   '          bellEventLog("notify_fail')
        assert anchor3 in bsrc, "якорь create-catch не найден"
        bsrc = bsrc.replace(anchor3, ('.catch((er) => { e.notified = false;\n'
                                       '          self.__sk35cerr = "promise:" + String((er && er.message) || er);\n'
                                       '          bellEventLog("notify_fail'))
        anchor2 = "  if (browser.notifications) browser.notifications.clear(String(id)).catch(() => {});"
        assert anchor2 in bsrc, "якорь bellClear не найден"
        bsrc = bsrc.replace(anchor2, anchor2 + (
            "\n  try { browser.storage.local.set({__sk35_bell: {pending: [...BELL.pending.keys()], "
            "focused: null, notified: [...BELL.pending.values()].filter(e => e.notified).length, "
            "badge: bellBadgeText(BELL.pending.size), ts: Date.now()}}); } catch (e) {}"))
        # SK-50: bellWindowFocused уважает storage-override __sk50_force_focus —
        # на X11-стенде xul focus молчит (замер 26.10), приёмка подаёт фокус им.
        anchorF = ('async function bellWindowFocused() {\n  try {\n'
                   '    const wins = await browser.windows.getAll({ windowTypes: ["normal"] });')
        assert anchorF in bsrc, "якорь bellWindowFocused не найден"
        bsrc = bsrc.replace(anchorF, """async function bellWindowFocused() {
  try { const g = await browser.storage.local.get("__sk50_force_focus");
        if (g && typeof g.__sk50_force_focus === "boolean") return g.__sk50_force_focus; } catch (e) {}
  try {
    const wins = await browser.windows.getAll({ windowTypes: ["normal"] });""")
        bsrc += ("\nif (browser.runtime.onMessage) browser.runtime.onMessage.addListener((m) => {"
                 "if (m && m.type === '__sk35_clean') { BELL_LOG.length = 0; BELL.pending.clear();"
                 "browser.storage.local.remove(['sk35BellLog', '__sk35_bell', '__sk35dbg', '__sk50_force_focus']); }});\n")
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
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0d2 токен посеян (options.html)", seeded == "seeded", seeded)
        note("S0d3 журнал/пробники очищены", op(m, "options", "storageClean") == "clean")
        m.script(OPEN_TAB_JS, [PAGE_URL])
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0e расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод: было {prod_before})")
        if ext_url:
            m.script(OPEN_TAB_JS, [ext_url + "options.html"])
            time.sleep(1.0)
            m.script(OPEN_TAB_JS, [PAGE_URL])
        ok0 = ensure_sidebar_open(m)
        note("S0f сайдбар открыт", ok0)

        # ---------- A1: долгая задача сворачивает сайдбар и группу ----------
        bind = rpc("tabs.open_grouped", {"task": "sk35-a1", "url": PAGE_URL})
        note("A1a вкладка/группа Sidekick создана", bind.get("ok") is True, str(bind)[:120])
        g0 = wait_group(m, lambda g: True, timeout=8)
        note("A1b группа на виду (до долгой)", bool(g0) and g0.get("collapsed") is False, g0)
        task_running_frames = task_running("sk35-a1", "Долгая задача А1", "sk35-chat-a1", long=True)
        note("A1b2 running+long:true кадр доставлен мостом", out_delivered(task_running_frames))
        t_col = time.time()
        closed = wait_sidebar_closed(m, timeout=18)
        dt_col = time.time() - t_col
        flip = []
        tf = time.time()
        while time.time() - tf < 3:
            flip.append(sidebar_open_state(m))
            time.sleep(0.5)
        gl = wait_group(m, lambda g: g.get("collapsed") and str(g.get("title", "")).startswith("⏳"),
                        timeout=10)
        note("A1c долгая по long:true: сайдбар сам свернулся за <2 с (window.close из документа)",
             closed and dt_col < 2.0, f"сворот {dt_col:.1f} с; isOpen-сэмпл 3с: " + ",".join(flip))
        note("A1d группа свёрнута с ⏳", bool(gl), gl if gl else dbg_timeline(m))
        note("A1e скрин свёрнутого состояния", shot("a1-collapsed"))

        # ---------- A2: done -> bell + журнал + ✓ ----------
        # SK-50: всплывашка задачи выходит при НЕ-фокусе окна. На X11-стенде
        # xul focus недостижим (замер 26.10) — подаём состояние фокуса
        # storage-override (setFocus), bellRefresh на пуше его уважает.
        op(m, "options", "setFocus", False)
        out_until(task_terminal("sk35-a1", "done"),
                  lambda: "sk35-a1" in (bell_probe(m).get("pending") or []), timeout=14)
        probe = bell_probe(m)
        note("A2a bell pending с id задачи, бейдж 🔔, фокус выключен (focused=false)",
             "sk35-a1" in (probe.get("pending") or []) and probe.get("badge") == "🔔"
             and probe.get("focused") is False, probe)
        alerts = []
        ta = time.time()
        while time.time() - ta < 8:
            # XUL-документ alert.xhtml НЕ имеет document.body — текст в value
            # у #alertTitleLabel/#alertTextLabel (замер 23.10: body===null, отсюда «[]»)
            alerts = json.loads(m.script("""
return (() => { const out = [];
  for (const w of Services.wm.getEnumerator('alert:alert')) {
    try {
      const d = w.document;
      const t = d.getElementById('alertTitleLabel');
      const x = d.getElementById('alertTextLabel');
      out.push((String(t && t.value || '') + ' — ' + String(x && x.value || '')).slice(0, 160));
    } catch (e) { out.push('err:' + String(e.message).slice(0, 60)); }
  }
  return JSON.stringify(out);})();"""))
            # окно alert.xhtml открывается раньше, чем дорисуются value —
            # ждём именно текст, а не факт окна
            if any("сделана" in str(a).lower() or "hermes" in str(a).lower() for a in alerts):
                break
            time.sleep(0.5)
        note("A2d всплывающее уведомление на экране при не-фокусе (XUL alert, SK-50)",
             any("сделана" in str(a).lower() or "hermes" in str(a).lower() for a in alerts),
             alerts if alerts else f"cerr={probe.get('cerr')!r} notified={probe.get('notified')}")
        op(m, "options", "setFocus", True)
        op(m, "options", "probeRefresh")
        gd = wait_group(m, lambda g: str(g.get("title", "")) == "✓ Sidekick", timeout=8)
        note("A2b группа помечена ✓ и остаёт свёрнутой", bool(gd) and gd.get("collapsed"), gd)
        t1 = time.time()
        lg = []
        while time.time() - t1 < 8:
            lg = bell_log(m)
            if any(e.get("id") == "sk35-a1" and e.get("kind") == "task_done" for e in lg):
                break
            time.sleep(0.5)
        ev1 = next((e for e in lg if e.get("id") == "sk35-a1"), {})
        note("A2c журнал: «Задача сделана» непрочитана, чат задачи записан",
             ev1.get("kind") == "task_done" and ev1.get("read") is False
             and ev1.get("chat") == "sk35-chat-a1" and "А1" in str(ev1.get("sub", "")), ev1)
        note("A2e скрин bell-состояния", shot("a2-bell"))

        # ---------- A3: perm посреди долгой разворачивает группу ----------
        note("A3a0 running+long:true кадр А3 доставлен", out_delivered(
            task_running("sk35-a3", "Долгая задача А3", "sk35-chat-a3", long=True)))
        g3c = wait_group(m, lambda g: g.get("collapsed") and str(g.get("title", "")).startswith("⏳"),
                         timeout=18)
        note("A3a вторая долгая: группа снова ⏳-свёрнута", bool(g3c), g3c)
        pbox = {}
        pth = rpc_async("perm.check", {"url": PAGE_URL, "action": "submit",
                                       "perm": "send_message", "task": "sk35-a3",
                                       "name": "Комментарий «SK-35 живой»"}, pbox)
        g3e = wait_group(m, lambda g: g.get("collapsed") is False, timeout=10)
        note("A3b пермисс развернул группу (единственный легальный повод)", bool(g3e), g3e)
        note("A3c ⏳ при этом удержан", bool(g3e) and str(g3e.get("title", "")).startswith("⏳"),
             g3e and g3e.get("title"))
        shot("a3-perm-expanded")
        ok_sb = ensure_sidebar_open(m)
        t1 = time.time()
        dom = {}
        while time.time() - t1 < 12:
            dom = bell_dom(m)
            dperm = op(m, "sidebar", "clickBtn", arg="__probe__")  # no-card -> карточка ещё не приехала
            if isinstance(dperm, dict) and "no-btn" in str(dperm.get("err", "")):
                break
            time.sleep(0.6)
        note("A3d карточка пермиса доехала в чат", ok_sb and "no-btn" in str(
            op(m, "sidebar", "clickBtn", arg="__probe__").get("err", "")), dom)
        clicked = op(m, "sidebar", "clickBtn", arg="no")
        pth.join(timeout=60)
        r = pbox.get("r", {})
        note("A3e вердикт с карточки: deny -> human_deny",
             str(clicked) == "clicked:Отклонить" and "human_deny" in str(r), str(r)[:160])
        out_until(task_terminal("sk35-a3", "failed"),
                  lambda: any(e.get("kind") == "task_failed" and e.get("id") == "sk35-a3"
                              for e in bell_log(m)), timeout=14)
        lg = bell_log(m)
        note("A3f журнал пополнился «Нужно разрешение» и «не сделана»",
             any(e.get("kind") == "perm" for e in lg)
             and any(e.get("kind") == "task_failed" and e.get("id") == "sk35-a3" for e in lg),
             [(e.get("kind"), e.get("id")) for e in lg])
        note("A3g вердикт прочитал perm-событие журнала (человек его видел)",
             all(e.get("read") for e in lg if e.get("kind") == "perm"),
             [e.get("read") for e in lg if e.get("kind") == "perm"])

        # ---------- A4: интерактивный диалог БЕЗ long:true не сворачивается ничто ----------
        # SK-51: 10-с эвристики больше нет — задача живёт сколько угодно, панель и
        # группа не тронуты; по done такой задаче bell не звонит (она не была долгой).
        ensure_sidebar_open(m)
        lg_before = len(bell_log(m))
        g_pre = group_state(m)  # базлайн: группа могла быть легально припаркована SK-07 после A3
        note("A4a0 running-кадр А4 доставлен (без long:true)", out_delivered(
            task_running("sk35-a4", "Интерактив А4", "sk35-chat-a4")))
        time.sleep(42)  # SK-51: «по времени» — не сигнал; старый LONG_MS=10000 давно пройден
        sb_mid = sidebar_open_state(m)
        g_mid = group_state(m)
        note("A4b интерактив 40+ с: сайдбар не закрылся", sb_mid == "true",
             "" if sb_mid == "true" else dbg_timeline(m))
        note("A4c интерактив 40+ с: группа НЕ тронута (тот же заголовок/сворот), ⏳ не появлялся",
             g_mid.get("found") and g_mid.get("title") == g_pre.get("title")
             and g_mid.get("collapsed") == g_pre.get("collapsed")
             and "⏳" not in str(g_mid.get("title", "")), {"pre": g_pre, "mid": g_mid})
        note("A4a1 done-кадр А4 доставлен", out_delivered(task_terminal("sk35-a4", "done")))
        time.sleep(4)
        lg4 = bell_log(m)
        note("A4d не-долгая: bell и журнал её не коснулись",
             all(e.get("id") != "sk35-a4" for e in lg4) and len(lg4) == lg_before,
             [e.get("id") for e in lg4])
        note("A4e скрин интерактива (сайдбар открыт)", shot("a4-short-open"))

        # ---------- A5: колокольчик: счётчик и дропдаун ----------
        ensure_sidebar_open(m)
        d5 = bell_dom(m)
        note("A5a счётчик непрочитанного на колокольчике",
             d5.get("bellnHidden") is False and int(d5.get("bellnText") or 0) >= 2, d5)
        op(m, "sidebar", "clickBell")
        time.sleep(1.2)
        d5b = bell_dom(m)
        texts = " | ".join(d5b.get("texts") or [])
        note("A5b дропдаун: три события (сделана / нужно разрешение / не сделана)",
             d5b.get("panelHidden") is False and d5b.get("rows", 0) >= 3
             and "Задача сделана" in texts and "Нужно разрешение" in texts
             and "не сделана" in texts, texts[:200])
        t1 = time.time()
        d5c = {}
        while time.time() - t1 < 8:
            d5c = bell_dom(m)
            if d5c.get("panelHidden") is False:
                break
            time.sleep(0.5)
        # SK-50: открытие журнала НЕ снимает непрочитанное («посмотреть» ≠ «ликвидировать»)
        note("A5c открытие журнала оставляет непрочитанное целым (SK-50)",
             d5c.get("bellnHidden") is False and int(d5c.get("bellnText") or 0) >= 2
             and int(d5c.get("unread") or 0) >= 2, d5c)
        t1 = time.time()
        d5d = {}
        while time.time() - t1 < 8:
            op(m, "sidebar", "clickReadAll")
            d5d = bell_dom(m)
            if d5d.get("bellnHidden") is True:
                break
            time.sleep(0.5)
        note("A5c2 «Прочитать всё» (явная кнопка) снимает непрочитанное",
             d5d.get("bellnHidden") is True, d5d)
        t1 = time.time()
        probe5 = {}
        while time.time() - t1 < 8:
            probe5 = bell_probe(m)
            if "sk35-a1" not in (probe5.get("pending") or []):
                break
            time.sleep(0.5)
        note("A5d прочтение сняло bell завершённой задачи",
             "sk35-a1" not in (probe5.get("pending") or []), probe5)
        note("A5e скрин открытого журнала", shot("a5-bell-panel"))

        # ---------- A6: клик по событию -> чат задачи + flash карточки ----------
        op(m, "sidebar", "clickRow", arg="task_done")
        time.sleep(2.5)
        chat = op(m, "sidebar", "storageGet", arg="sk23Chat")
        cf = op(m, "sidebar", "cardFlash")
        note("A6a клик по «Задача сделана» переключил на чат задачи",
             chat == "sk35-chat-a1", chat)
        note("A6b карточка задачи подсвечена (flash)",
             isinstance(cf, dict) and cf.get("tasks", 0) >= 1 and cf.get("flash", 0) >= 1, cf)
        note("A6c скрин перехода к карточке", shot("a6-card-flash"))

        # ---------- A7: ручное сворачивание ----------
        ensure_sidebar_open(m)
        op(m, "sidebar", "clickCollapse")
        note("A7 кнопка сворачивания закрывает панель", wait_sidebar_closed(m, timeout=6))
        shot("a7-manual-collapse")

        # ---------- регресс: прод-шина не тронута ----------
        note("R1 прод :8791 за время приёмки не получил тестовых коннектов",
             ss_count(8791) <= prod_before, f"@8791={ss_count(8791)} было {prod_before}")
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
        (S / "sk35_acc_results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
