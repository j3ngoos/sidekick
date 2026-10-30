#!/usr/bin/env python3
"""sk38_acc.py — живая приёмка SK-38 (шапка/композер: меню наверху, иконка,
стоп-в-кнопке, живой плейсхолдер).

Стенд: изолированный мост :8797 + тест-копия расширения (FALLBACK_SERVER->:8797)
+ сайдбар в firefox-158 (Marionette-актёр sk38_actor). Прод :8791 не трогаем;
в конце — переустановка prod-аддона. Прецедент sk30_acc.py.

Фазы (карточка t_7cf9bfad):
  S1 dark: шапка = иконка+заголовок без статуса и «Hermes»; бар = композер +
     ОДНА кнопка; «⋯» в шапке; эмодзи-скан; glass-слои.
  S2 меню раскрывается вниз от шапки (геометрия rect), Esc закрывает.
  S3 заголовок чата: РЕАЛЬНЫЙ Enter -> кадр в chats.jsonl, ttl = первый запрос
     с обрезком «…»; «Новый чат»; переключение чата меняет ttl.
  S4 морф send<->stop на РЕАЛЬНОМ снапшоте задач (outbox task-кадры): running =
     квадрат на красном + aria; клик -> hub/stop + audit ev:stop; возврат;
     Esc = тот же стоп (второй ev:stop).
  S5 живой плейсхолдер: ротация текста; пауза на фокусе и на вводе.
  S6 пакованные иконки грузятся со страницы (16/32/48/96).
  S7 иконка в тулбаре: пин виджета CustomizableUI -> list-style-image =
     icons/icon-32.png (не дефолтный пазл), скрин.
  S8 light: скрины + контраст (ttl/кнопка/плейсхолдер/меню).
  Z  уборка: тема, вкладки, мост, prod-аддон, :8791.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
S = pathlib.Path(os.environ.get("SK38_SCRATCH", str(HOME / ".hermes/cache/scratch/sk38")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
SHOTS = S / "shots"
ACTOR = pathlib.Path(__file__).resolve().parent / "sk38_actor.sys.mjs"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
results = {}

sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
from mario import Marionette  # noqa: E402


def note(name, ok, detail=""):
    results[name] = {"ok": bool(ok), "detail": str(detail)[:400]}
    print(("PASS " if ok else "FAIL ") + name + " — " + str(detail)[:220], flush=True)


def ss_count(port):
    r = subprocess.run(["ss", "-tn", "state", "established", f"( sport = :{port} )"],
                       capture_output=True, text=True)
    return max(0, len(r.stdout.strip().splitlines()) - 1)


def listener_pid(port):
    r = subprocess.run(["ss", "-tlnp", f"( sport = :{port} )"], capture_output=True, text=True)
    m2 = re.search(r"pid=(\d+)", r.stdout)
    return int(m2.group(1)) if m2 else None


def xkey(key):
    wid = subprocess.run(["xdotool", "search", "--class", "firefox"],
                         env=ENV, capture_output=True, text=True).stdout.split()
    if not wid:
        return "no-window"
    subprocess.run(["xdotool", "windowactivate", "--sync", wid[0]], env=ENV, capture_output=True)
    subprocess.run(["xdotool", "key", key], env=ENV, capture_output=True)
    return "sent:" + key


# ---- chrome-JS блоки (тот же механизм, что sk30) ------------------------------------------

SPARE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "win.gBrowser.addTrustedTab('about:blank'); return 'spare';})();")
SETUP_JS = r"""
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const ph = Services.io.getProtocolHandler('resource').QueryInterface(Ci.nsIResProtocolHandler);
    ph.setSubstitution('sk38', await Services.io.newURI('file://%s'));
    try { ChromeUtils.unregisterWindowActor('Sk38Probe'); } catch (e) {}
    ChromeUtils.registerWindowActor('Sk38Probe', {
      child: { esModuleURI: arguments[0] }, parent: { esModuleURI: arguments[0] },
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
    const host = win.document.getElementById('sidebar');
    if (!host) return resolve({ok: false, err: 'no-sidebar-host'});
    let inner = null;
    try {
      inner = host.contentWindow.document.querySelector('browser#webext-panels-browser')
        || host.contentWindow.document.querySelector('browser,iframe');
    } catch (e) { return resolve({ok: false, err: 'host-not-loaded'}); }
    const gw = inner && inner.browsingContext.currentWindowGlobal;
    if (!gw) return resolve({ok: false, err: 'no gw'});
    const val = await gw.getActor('Sk38Probe').sendQuery('sk38:op', payload);
    resolve({ok: true, val});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""
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
SEED_RELOAD_JS = """
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const win = Services.wm.getMostRecentWindow('navigator:browser');
    let t = null;
    for (const x of win.gBrowser.tabs)
      if (x.linkedBrowser.currentURI.spec.includes('options.html')) { t = x; break; }
    if (!t) return resolve('no-options-tab');
    const gw = t.linkedBrowser.browsingContext.currentWindowGlobal;
    let seed = 'none';
    for (let i = 0; i < 6 && seed !== 'seeded'; i++) {
      seed = String(await gw.getActor('Sk38Probe')
        .sendQuery('sk38:op', {op: 'seedToken', token: arguments[0]}));
      if (seed !== 'seeded') await new Promise(r => setTimeout(r, 700));
    }
    resolve('seed=' + seed);
  } catch (e) { resolve('ERR:' + String(e && e.message || e).slice(0, 150)); }
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
THEME_JS = "Services.prefs.setIntPref('ui.systemUsesDarkTheme', %d); return 'pref=' + %d;"
GET_THEME_JS = "return Services.prefs.prefHasUserValue('ui.systemUsesDarkTheme') ? Services.prefs.getIntPref('ui.systemUsesDarkTheme') : 'unset';"
SET_THEME_JS = ("if (%s === 'unset') Services.prefs.clearUserPref('ui.systemUsesDarkTheme');"
                "else Services.prefs.setIntPref('ui.systemUsesDarkTheme', %s); return 'restored';")
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
FOCUS_SIDEBAR_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                    "const host = win.document.getElementById('sidebar');"
                    "if (!host) return 'no-host';"
                    "let inner = null;"
                    "try { inner = host.contentWindow.document.querySelector('browser');"
                    "} catch (e) { return 'host-not-loaded'; }"
                    "if (!inner) return 'no-inner';"
                    "inner.focus();"
                    "return 'focused-inner'; })();")
# SK-38 S7: пиним виджет browserAction на nav-bar и читаем его иконку.
# id виджета = id аддона без не-буквенно-цифровых, дефисы, + "-browser-action".
PIN_WIDGET_JS = r"""
return (() => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  // CustomizableUI — глобал окна браузера (importESModule режет sandbox Marionette)
  const CUI = win.CustomizableUI;
  const wid = "hermes-sidekick_hermes_local-browser-action";
  try { CUI.addWidgetToArea(wid, CUI.AREA_NAVBAR); } catch (e) {}
  const w = CUI.getWidget(wid);
  const node = win.document.getElementById(wid);
  if (!node) return JSON.stringify({pinned: false, why: 'no-node', area: w && w.areaType});
  const ic = node.querySelector('.toolbarbutton-icon') || node;
  const img = win.getComputedStyle(ic).listStyleImage || '';
  const r = node.getBoundingClientRect();
  return JSON.stringify({pinned: true, area: w && w.areaType, img: String(img).slice(0, 320),
    visible: r.width > 8 && r.height > 8});
})();
"""
UNPIN_WIDGET_JS = r"""
return (() => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  try { win.CustomizableUI.removeWidgetFromArea("hermes-sidekick_hermes_local-browser-action"); } catch (e) {}
  return 'unpinned';
})();
"""


def set_theme(m, v):
    m.script(THEME_JS % (v, v))


def install(m, xpi):
    r = m.ascript(ADDON_INSTALL_JS, args=[str(pathlib.Path(xpi).resolve())])
    if not (isinstance(r, dict) and r.get("ok")):
        raise RuntimeError(f"installTemporaryAddon: {r}")
    return r


def seed_and_reload(m, token):
    sr = "none"
    for _ in range(3):
        sr = m.script(SEED_RELOAD_JS, [token])
        if isinstance(sr, str) and sr.startswith("seed=seeded"):
            return sr
        time.sleep(1.2)
    return str(sr)


def real_key(m, key):
    m.script(FOCUS_SIDEBAR_JS)
    return xkey(key)


def actor_op(m, op, **kw):
    try:
        return m.ascript(OP_JS, args=[json.dumps({"op": op, **kw})])
    except Exception as e:
        return {"ok": False, "err": str(e)[:120]}


def op_val(m, op, **kw):
    r = actor_op(m, op, **kw)
    if not (isinstance(r, dict) and r.get("ok")):
        return {"_err": r.get("err", "?")}
    v = r.get("val")
    try:
        return json.loads(v) if isinstance(v, str) and v.startswith(("{", "[")) else v
    except (json.JSONDecodeError, TypeError):
        return v


def sidebar_alive(m):
    r = actor_op(m, "href")
    return isinstance(r, dict) and r.get("ok") and "moz-extension" in str(r.get("val"))


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


def shot(name):
    p = SHOTS / f"{name}.png"
    subprocess.run(["scrot", "-o", str(p)], env=ENV, capture_output=True)
    return p.exists()


def outbox(frame):
    with open(HUB / "outbox.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(frame, ensure_ascii=False) + "\n")


def ph_settled(m):
    # выборка может попасть в 170-мс окно .off (fade) — пересэмплируем
    s = op_val(m, "phSample")
    for _ in range(3):
        if str(s.get("opacity")) == "1":
            return s
        time.sleep(0.4)
        s = op_val(m, "phSample")
    return s


def stop_events():
    evs = []
    try:
        for ln in open(HUB / "audit.jsonl", encoding="utf-8"):
            try:
                o = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if o.get("ev") == "stop":
                evs.append(o)
    except FileNotFoundError:
        pass
    return evs


# ---- контраст по WCAG ----------------------------------------------------------------------

def _rgba(s):
    m2 = re.findall(r"[\d.]+", s or "")
    if not m2:
        return None
    r, g, b = float(m2[0]), float(m2[1]), float(m2[2])
    a = float(m2[3]) if len(m2) > 3 else 1.0
    return (r, g, b, a)


def over(top, bot):
    if not top or not bot:
        return None
    r, g, b, a = top
    br, bg, bb, _ = bot
    return (r * a + br * (1 - a), g * a + bg * (1 - a), b * a + bb * (1 - a), 1.0)


def lum(c):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def contrast_checks(comp, dark):
    out = []
    page = (24, 26, 33, 1.0) if dark else (250, 250, 252, 1.0)
    def g(sel, key):
        v = comp.get(sel)
        return _rgba(v.get(key)) if isinstance(v, dict) else None
    ttl_c, hd_bg = g("#ttl", "color"), g("#hd", "background")
    go_c, go_bg = g("#go", "color"), g("#go", "background")
    ph_c, bar_bg = g(".ph", "color"), g("#bar", "background")
    mi_c, menu_bg = g("#menu .mi", "color"), g("#menu", "background")
    if ttl_c and hd_bg:
        out.append(("шапка: заголовок чата на glass", ratio(ttl_c, over(hd_bg, page)), 4.5))
    if go_c and go_bg:
        out.append(("композер: иконка на accent-кнопке", ratio(go_c, go_bg), 3.0))
    if ph_c and bar_bg:
        out.append(("плейсхолдер на подложке поля", ratio(ph_c, over(bar_bg, page)), 3.0))
    if mi_c and menu_bg:
        out.append(("меню: текст на glass", ratio(mi_c, over(menu_bg, page)), 7.0))
    return out


# -------------------------------------------------------------------------------------------

def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    if HUB.exists():
        shutil.rmtree(HUB)
    HUB.mkdir(parents=True)
    shutil.copy(HOME / "sidekick/bridge/hub/token", HUB / "token")
    prod_before = ss_count(8791)
    br, m, httpd = None, None, None
    theme0 = "unset"
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
        assert 'for (const u of [saved, FALLBACK_SERVER])' in src
        src = src.replace('for (const u of [saved, FALLBACK_SERVER])',
                          'for (const u of [FALLBACK_SERVER])')
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
        note("S0a тест-мост слушает :8797", listener_pid(PORT) == br.pid, f"pid={listener_pid(PORT)}")
        t1 = time.time()
        while time.time() - t1 < 40:
            if listener_pid(MAR_PORT):
                break
            time.sleep(1.0)
        m = Marionette("127.0.0.1", MAR_PORT)
        m.script(SPARE_TAB_JS)
        r = m.ascript(SETUP_JS % (str(ACTOR.parent) + "/"),
                      args=[f"resource://sk38/{ACTOR.name}?v={int(time.time())}"])
        note("S0c актёр зарегистрирован", (r or {}).get("ok"), r)
        r0 = install(m, EXT_XPI)
        sb = ensure_sidebar_open(m)
        note("S0d сайдбар открыт", sb, sb)
        token = (HUB / "token").read_text().strip()
        loc = str(op_val(m, "extUrl") or "")
        ext_url = loc.split("sidebar.html")[0] if "sidebar.html" in loc else (r0.get("url") or "")
        if ext_url:
            m.script(OPEN_TAB_JS, [ext_url + "options.html"])
            time.sleep(1.5)
            sr = seed_and_reload(m, token)
            note("S0a seed-танец (токен через UI опций)", sr,
                 isinstance(sr, str) and sr.startswith("seed=seeded"))
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0b расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод: было {prod_before})")
        m.script(CLOSE_TAB_JS, args=["moz-extension"])
        time.sleep(0.5)
        theme0 = m.script(GET_THEME_JS)

        # ---------- S1: тёмная тема — геометрия шапки/бара ----------
        set_theme(m, 1)
        time.sleep(1.2)
        st = op_val(m, "chrome")
        note("S1a бар = композер + ОДНА кнопка; «⋯» в шапке",
             st.get("barBtns") == 1 and st.get("hdBtns") == 1, f"bar={st.get('barBtns')} hd={st.get('hdBtns')}")
        note("S1b шапка: иконка видна, заголовок «Новый чат», без статуса и «Hermes»",
             st.get("hdicVisible") and st.get("ttl") == "Новый чат"
             and "Думает" not in str(st.get("hdText")) and "выполняет" not in str(st.get("hdText"))
             and "Hermes" not in str(st.get("hdText")),
             f"ttl={st.get('ttl')!r} hdText={str(st.get('hdText'))[:60]!r}")
        note("S1c кнопка в покое = отправка (aria), running=false, плейсхолдер жив",
             st.get("goRunning") is False and "Отправить" in str(st.get("goAria"))
             and len(str(st.get("phText"))) > 3,
             f"aria={st.get('goAria')!r} ph={st.get('phText')!r}")
        note("S1d эмодзи в chrome UI нет; иконки — inline-SVG спрайт",
             st.get("emoji") == [] and st.get("svgCount", 0) >= 10 and st.get("spriteCount", 0) >= 30,
             f"emoji={st.get('emoji')} svg={st.get('svgCount')} sym={st.get('spriteCount')}")
        note("S1e glass-слоя (hd/bar/menu) с blur; меню скрыто",
             all("blur" in str(st.get(k)) for k in ("bfHd", "bfBar", "bfMenu"))
             and st.get("menuHidden") is True and st.get("ariaExpanded") == "false",
             [st.get("bfHd"), st.get("bfMenu")])
        shot("A-header-dark")

        # ---------- S2: меню раскрывается ВНИЗ от шапки ----------
        op_val(m, "clickMenuBtn")
        time.sleep(0.35)
        mr = op_val(m, "menuRect")
        gap = (mr.get("menuTop", 999) - mr.get("hdBottom", 0)) if isinstance(mr, dict) else 999
        note("S2a меню под шапкой: top>=hd.bottom и зазор <=16px (не от композера)",
             isinstance(mr, dict) and mr.get("menuVisible") and 0 <= gap <= 16,
             f"menuTop={mr.get('menuTop')} hdBottom={mr.get('hdBottom')} gap={gap}")
        shot("B-menu-dark")
        op_val(m, "pressKey", key="Escape", target="menu")
        time.sleep(0.25)
        note("S2b Esc закрывает меню", op_val(m, "chrome").get("menuHidden") is True)

        # ---------- S3: заголовок чата ----------
        q1 = "пришли пожалуйста полный список всех задач на эту неделю и обязательно напомни про дедлайны в пятницу вечером"
        assert len(q1) > 65, len(q1)
        exp1 = q1[:60].rstrip() + "…"
        op_val(m, "focusComposer", text=q1)
        time.sleep(0.3)
        real_key(m, "Return")
        time.sleep(0.9)
        ttl1 = op_val(m, "chrome").get("ttl")
        mirrored = False
        try:
            for ln in open(HUB / "chats.jsonl", encoding="utf-8"):
                if "полный список всех задач" in ln:
                    mirrored = True
        except FileNotFoundError:
            pass
        note("S3a РЕАЛЬНЫЙ Enter: кадр в chats.jsonl, ttl = первый запрос с обрезком «…»",
             mirrored and ttl1 == exp1, f"ttl={ttl1!r} exp={exp1!r} mirror={mirrored}")
        shot("C-title-dark")
        op_val(m, "clickNew")
        time.sleep(0.5)
        note("S3b «Новый чат» -> ttl снова «Новый чат»",
             op_val(m, "chrome").get("ttl") == "Новый чат")
        op_val(m, "focusComposer", text="вторая короткая проверка")
        time.sleep(0.3)
        real_key(m, "Return")
        time.sleep(0.9)
        op_val(m, "openHist")
        time.sleep(1.6)
        hr = op_val(m, "clickHrow", skipCur=True)
        time.sleep(1.4)
        ttl_sw = op_val(m, "chrome").get("ttl")
        note("S3c переключение на старый чат: ttl = его первый запрос (CHATREQ)",
             hr == "clicked" and ttl_sw == exp1, f"row={hr} ttl={ttl_sw!r}")
        op_val(m, "closeViews")
        time.sleep(0.4)

        # ---------- S4: морф send<->stop на снапшоте задач ----------
        stop0 = HUB / "stop"
        had_stop = stop0.exists()
        outbox({"v": 1, "type": "task", "id": "sk38acc", "title": "SK-38 морф",
                "state": "running", "chat": "c-sk38", "ts": int(time.time())})
        t1 = time.time()
        gs = {}
        while time.time() - t1 < 6:
            gs = op_val(m, "goState")
            if gs.get("running"):
                break
            time.sleep(0.4)
        shot("D-running-dark")
        note("S4a running-кадр: кнопка = квадрат на красном, aria «Стоп задача»",
             gs.get("running") is True and "Стоп" in str(gs.get("aria"))
             and gs.get("stopOp") in ("1", 1) and gs.get("sendOp") in ("0", 0)
             and gs.get("bg") == "rgb(220, 38, 38)",
             f"run={gs.get('running')} aria={gs.get('aria')!r} bg={gs.get('bg')} send={gs.get('sendOp')} stop={gs.get('stopOp')} tr={gs.get('transition')}")
        op_val(m, "clickGo")
        time.sleep(0.8)
        ev1 = [e for e in stop_events() if e.get("task") == "sk38acc"]
        note("S4b клик по running-кнопке = стоп: hub/stop + audit ev:stop",
             stop0.exists() and len(ev1) >= 1, f"stopfile={stop0.exists()} events={len(ev1)}")
        outbox({"v": 1, "type": "task", "id": "sk38acc", "state": "paused",
                "chat": "c-sk38", "ts": int(time.time())})
        t1 = time.time()
        gs2 = {}
        while time.time() - t1 < 6:
            gs2 = op_val(m, "goState")
            if not gs2.get("running"):
                break
            time.sleep(0.4)
        note("S4c terminal-кадр: кнопка вернулась в отправку (морф обратно)",
             gs2.get("running") is False and "Отправить" in str(gs2.get("aria"))
             and gs2.get("sendOp") in ("1", 1), f"aria={gs2.get('aria')!r} send={gs2.get('sendOp')}")
        # Esc = тот же стоп
        outbox({"v": 1, "type": "task", "id": "sk38acc2", "title": "SK-38 esc",
                "state": "running", "chat": "c-sk38", "ts": int(time.time())})
        t1 = time.time()
        while time.time() - t1 < 6:
            if op_val(m, "goState").get("running"):
                break
            time.sleep(0.4)
        op_val(m, "focusComposer", text="")
        time.sleep(0.2)
        op_val(m, "pressKey", key="Escape")
        time.sleep(0.8)
        ev2 = [e for e in stop_events() if e.get("task") == "sk38acc2"]
        note("S4d Esc в документе = тот же стоп (ev:stop task=sk38acc2)",
             len(ev2) >= 1, f"events={len(ev2)}")
        outbox({"v": 1, "type": "task", "id": "sk38acc2", "state": "failed",
                "chat": "c-sk38", "ts": int(time.time())})
        time.sleep(0.8)
        shot("E-back-to-send")
        if not had_stop:
            stop0.unlink(missing_ok=True)

        # ---------- S5: живой плейсхолдер ----------
        op_val(m, "blurIn")
        p1 = op_val(m, "phSample")
        time.sleep(3.6)
        p2 = ph_settled(m)
        note("S5a покой: текст плейсхолдера ротируется, opacity=1",
             p1.get("text") != p2.get("text") and str(p2.get("opacity")) == "1",
             f"{p1.get('text')!r} -> {p2.get('text')!r}")
        op_val(m, "focusComposer", text="")
        f1 = op_val(m, "phSample")
        time.sleep(3.3)
        f2 = op_val(m, "phSample")
        note("S5b фокус без текста: ротация на паузе",
             f1.get("text") == f2.get("text"), f"{f1.get('text')!r} == {f2.get('text')!r}")
        op_val(m, "focusComposer", text="печатаю")
        t1 = op_val(m, "phSample")
        time.sleep(3.3)
        t2 = op_val(m, "phSample")
        note("S5c ввод текста: оверлей не мешает (тот же текст, поле с значением)",
             t1.get("text") == t2.get("text") and op_val(m, "composerValue") == "печатаю",
             f"{t1.get('text')!r} val={op_val(m, 'composerValue')!r}")
        op_val(m, "focusComposer", text="")
        op_val(m, "blurIn")
        r1 = op_val(m, "phSample")
        time.sleep(3.6)
        r2 = ph_settled(m)
        note("S5d blur+пусто: ротация возобновилась",
             r1.get("text") != r2.get("text"), f"{r1.get('text')!r} -> {r2.get('text')!r}")

        # ---------- S6: пакованные иконки грузятся со страницы ----------
        ic = op_val(m, "iconLoad", sizes=[16, 32, 48, 96])
        note("S6 иконки 16/32/48/96 отдаются расширением и декодируются",
             isinstance(ic, dict) and ic.get("16") == "16x16" and ic.get("32") == "32x32"
             and ic.get("48") == "48x48" and ic.get("96") == "96x96", ic)

        # ---------- S7: иконка в тулбаре (не дефолтный пазл) ----------
        pin = json.loads(m.script(PIN_WIDGET_JS) or "{}")
        time.sleep(0.6)
        shot("F-toolbar-icon")
        note("S7a виджет запинен на юнифайд-тулбар и виден",
             pin.get("pinned") and pin.get("visible") and pin.get("area") in ("nav-bar", "toolbar"), pin)
        note("S7b иконка тулбара = пакованный тайл из icons/ (не дефолтный пазл)",
             "icons/icon-" in str(pin.get("img")) and "moz-extension" in str(pin.get("img")),
             pin.get("img"))
        m.script(UNPIN_WIDGET_JS)

        # ---------- S8: светлая тема ----------
        set_theme(m, 0)
        time.sleep(1.2)
        shot("G-light-header")
        op_val(m, "clickMenuBtn")
        time.sleep(0.35)
        shot("H-light-menu")
        comp = op_val(m, "computed", sels=["#ttl", "#hd", "#go", ".ph", "#bar", "#menu", "#menu .mi"])
        cc = contrast_checks(comp if isinstance(comp, dict) else {}, dark=False)
        note("S8a computed-данные для контраста (light) собраны", bool(cc),
             f"pairs={len(cc)} raw={json.dumps(comp, ensure_ascii=False)[:160]}")
        for label, got, need in cc:
            note(f"S8b контраст (light): {label} >= {need}", got >= need, f"{got:.2f}:1")
        op_val(m, "pressKey", key="Escape", target="menu")
        time.sleep(0.2)
        set_theme(m, 1)
        time.sleep(0.8)
        comp = op_val(m, "computed", sels=["#ttl", "#hd", "#go", ".ph", "#bar", "#menu", "#menu .mi"])
        cc = contrast_checks(comp if isinstance(comp, dict) else {}, dark=True)
        for label, got, need in cc:
            note(f"S8c контраст (dark): {label} >= {need}", got >= need, f"{got:.2f}:1")

        note("Z1 все фазы отработали", all(v["ok"] for v in results.values()),
             [k for k, v in results.items() if not v["ok"]])
    finally:
        # ---------- Z: уборка ----------
        try:
            if m:
                m.script(SET_THEME_JS % (json.dumps(theme0), json.dumps(theme0) if theme0 != "unset" else "'unset'"))
        except Exception as e:
            print("WARN theme restore:", e, flush=True)
        try:
            if m:
                m.script(CLOSE_TAB_JS, args=["moz-extension"])
        except Exception:
            pass
        try:
            if m:
                m.script(SPARE_TAB_JS)
        except Exception:
            pass
        try:
            if m:
                rp = install(m, PROD_XPI)
                url = rp.get("url") or ""
                if not url:
                    ensure_sidebar_open(m)
                    loc = str(op_val(m, "extUrl") or "")
                    if "sidebar.html" in loc:
                        url = loc.split("sidebar.html")[0]
                if url:
                    m.script(OPEN_TAB_JS, [url + "options.html"])
                    time.sleep(1.5)
                    seed_and_reload(m, (HUB / "token").read_text().strip())
                    m.script(CLOSE_TAB_JS, args=["moz-extension"])
                    t2 = time.time()
                    while time.time() - t2 < 20 and ss_count(8791) == 0:
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
        t1 = time.time()
        while time.time() - t1 < 30 and ss_count(8791) < 1:
            time.sleep(1.0)
        note("Z2 прод-аддон переустановлен, :8791 восстановлен",
             ss_count(8791) >= 1,
             f"@8791={ss_count(8791)} (было {prod_before})")
        fails = [k for k, v in results.items() if not v["ok"]]
        print(f"\nИТОГ: {sum(1 for v in results.values() if v['ok'])}/{len(results)} PASS"
              + (f"; FAIL: {fails}" if fails else ""), flush=True)
        (S / "acc_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
