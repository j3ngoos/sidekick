#!/usr/bin/env python3
"""SK-43 живая приёмка: семантика пермисов, capabilities, русские подписи превью
(карточка t_da060999, аудит №2 22.10 инцидент B и проблемы 6/8).

Стенд: изолированный мост :8797 + тест-копия расширения (FALLBACK_SERVER->:8797)
в firefox-158 (Marionette 2929). Прод :8791 не трогаем; в конце — переустановка
prod-аддона. Прецеденты: sk42_acc.py, sk38_acc.py.

Фазы:
  S0 стенд: мост, XPI, расширение на :8797, http://127.0.0.1:8899/youtube.html
  A capabilities: полный контракт rpc + CLI sk capabilities; отвечает в паузе
  B mismatch: click/rightclick/... × sensitive_input/send_message/send_email ->
    perm_action_mismatch + hint, карточек нет; легитимный input+sensitive_input ->
    карточка есть (скрин), эволюционирует в block/ask_timeout
  C подписи: аватар «Account menu» -> «Меню аккаунта» (скрин в холде),
    «empty area» на видео-карточке -> заголовок, key остаётся клавишей,
    русское имя агента не перекрашивается, пункт меню и point-клик — из DOM
  D регресс: scroll без превью-кадров, скролл виден в viewport.sy
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
S = pathlib.Path(os.environ.get("SK43_SCRATCH", str(HOME / ".hermes/cache/scratch/sk43")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
SHOTS = S / "shots"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
TEST_PORT = 8899
PAGE_URL = f"http://127.0.0.1:{TEST_PORT}/youtube.html?v=sk43r2"
results = {}

sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
from mario import Marionette  # noqa: E402


def note(name, ok, detail=""):
    ok_v = None if ok == "skip" else bool(ok)
    results[name] = {"ok": ok_v, "detail": str(detail)[:400]}
    print(("PASS " if ok_v else ("SKIP " if ok_v is None else "FAIL ")) + name +
          ("  " + str(detail)[:180] if detail else ""), flush=True)


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


# ---- RPC через файловую шину моста (тот же контракт, что видит агент) --------

_rpc_off = 0


def rpc(method, params, timeout=30):
    # скан всего rpc_out по id (без общего смещения): приёмка живёт в несколько
    # потоков — фоновый perm.check читает ту же шину параллельно с action.do
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


def previews():
    return [a for a in audit_all() if a.get("ev") == "preview"]


def state(timeout=30):
    """документированное состояние страницы: title = sk43:{json} (page.text)."""
    r = rpc("page.text", {"maxChars": 10}, timeout=timeout)
    t = str((r.get("result") or {}).get("title") or "")
    if not t.startswith("sk43:"):
        return {"_err": t[:120]}
    try:
        return json.loads(t[5:])
    except ValueError:
        return {"_err": "bad-title:" + t[:120]}


def dom():
    return rpc("page.dom", {}, timeout=30).get("result") or {}


def entry_by(dom_res, needle):
    for e in (dom_res.get("elements") or []) + ((dom_res.get("more") or {}).get("elements") or []):
        if needle in str(e.get("selector") or "") or needle in str(e.get("name") or ""):
            return e
    return None


def center(e):
    r = e["rect"]
    return int(r["x"] + r["w"] / 2), int(r["y"] + r["h"] / 2)


def action_do_hold(params, shot_name, hold_after=0.55):
    """action.do в фоне; скриншот в середине превью-холда (ms задан в params)."""
    box = {}
    th = threading.Thread(target=lambda: box.update(r=rpc("action.do", params, timeout=40)))
    th.start()
    time.sleep(hold_after)
    ok_shot = shot(shot_name)
    th.join(timeout=45)
    return box.get("r", {"ok": False, "err": "thread_timeout"}), ok_shot


# ---- Marionette-блоки (стенд: вкладки, установка XPI) -------------------------

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


def install(m, xpi):
    r = m.ascript(ADDON_INSTALL_JS, args=[str(pathlib.Path(xpi).resolve())])
    if not (isinstance(r, dict) and r.get("ok")):
        raise RuntimeError(f"installTemporaryAddon: {r}")
    return r


def http_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/youtube.html", timeout=2)
        return True
    except Exception:
        return False


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
        if not http_up():
            httpd = subprocess.Popen(["python3", str(REPO / "tests" / "serve.py")],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(20):
                if http_up():
                    break
                time.sleep(0.4)
        note("S0b тестовые страницы на :8899", http_up())
        t1 = time.time()
        while time.time() - t1 < 40 and not listener_pid(MAR_PORT):
            time.sleep(1.0)
        m = Marionette("127.0.0.1", MAR_PORT)
        m.script(SPARE_TAB_JS)
        r0 = install(m, EXT_XPI)
        note("S0c тест-XPI установлен", bool(r0.get("ok")), r0.get("url", ""))
        m.script(OPEN_TAB_JS, [PAGE_URL])
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        if ss_count(PORT) == 0:
            url = (r0.get("url") or "").rsplit("sidebar.html", 1)[0]
            if url:
                m.script(OPEN_TAB_JS, [url + "options.html"])
                time.sleep(1.5)
                t1 = time.time()
                while time.time() - t1 < 25 and ss_count(PORT) == 0:
                    time.sleep(0.5)
                m.script(CLOSE_TAB_JS, args=["moz-extension"])
        note("S0e расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод: было {prod_before})")
        st0 = state(timeout=25)
        note("S0f youtube.html загружена (state-канал жив)", "_err" not in st0, st0)
        shot("s0-page")

        # ---------- A: capabilities ----------
        cap = rpc("capabilities", {})
        c = cap.get("result") or {}
        note("A1 capabilities: контракт v1", cap.get("ok") and c.get("v") == 1,
             str(cap)[:160])
        meths = c.get("methods") or {}
        bl, fw = set(meths.get("bridge_local") or []), set(meths.get("forwarded") or [])
        note("A1a методы: bridge_local полон",
             {"perm.check", "grants.set", "handoff.request", "capabilities"} <= bl, sorted(bl))
        note("A1b методы: forwarded полон",
             {"page.dom", "page.text", "action.preview", "tabs.list"} <= fw, sorted(fw))
        verbs = set((c.get("action_do") or {}).get("actions") or [])
        note("A1c глаголы action.do (9)",
             {"click", "input", "submit", "key", "scroll", "rightclick", "hover",
              "dblclick", "move"} == verbs, sorted(verbs))
        perms = c.get("perms") or {}
        types = {t["perm"]: t for t in (perms.get("types") or [])}
        always = {k for k, t in types.items() if t.get("always_human")}
        note("A1d типы пермисов: always_human = шесть + close_tabs",
             always == {"sensitive_input", "payment", "oauth", "2fa", "send_message",
                        "send_email", "close_tabs"},
             sorted(always))
        note("A1e границы: sensitive_input только input/submit",
             types.get("sensitive_input", {}).get("ops") == ["input", "submit"],
             types.get("sensitive_input"))
        note("A1f mismatch_err и превью-правило",
             perms.get("mismatch_err") == "perm_action_mismatch"
             and "DOM" in str((c.get("preview") or {}).get("label"))
             and "label_src" in str((c.get("preview") or {}).get("label")),
             str(perms.get("rule"))[:120])
        cli = subprocess.run(["python3", str(REPO / "bin" / "sk"), "capabilities"],
                             capture_output=True, text=True, timeout=40,
                             env={**os.environ, "SIDEKICK_HOME": str(HUB)})
        try:
            cj = json.loads(cli.stdout.strip().splitlines()[-1])
        except Exception:
            cj = {}
        note("A2 CLI sk capabilities = тот же контракт",
             cj.get("ok") and (cj.get("result") or {}).get("methods") == meths,
             (cli.stderr or cli.stdout)[:160])
        (HUB / "paused").touch()
        cap_p = rpc("capabilities", {})
        pc = rpc("perm.check", {"domain": "youtube.com", "action": "click"})
        note("A3 capabilities отвечает в hand-off паузе, perm.check — paused_for_human",
             cap_p.get("ok") and pc.get("err") == "paused_for_human",
             f"cap={cap_p.get('ok')} pc={pc.get('err')}")
        (HUB / "paused").unlink(missing_ok=True)

        # ---------- B: mismatch ----------
        b1 = rpc("perm.check", {"domain": "youtube.com", "action": "click",
                                "perm": "sensitive_input", "task": "sk43-b1"})
        hint = str(b1.get("hint") or "")
        note("B1 click+sensitive_input -> perm_action_mismatch + hint",
             b1.get("ok") is False and b1.get("err") == "perm_action_mismatch"
             and "sk capabilities" in hint and "input" in hint,
             f"{b1.get('err')} hint={hint[:120]}")
        pairs = [("rightclick", "send_message"), ("dblclick", "sensitive_input"),
                 ("hover", "send_email"), ("move", "sensitive_input"),
                 ("key", "send_message"), ("scroll", "sensitive_input"),
                 ("rightclick", "send_email")]
        bad = []
        for op, pm in pairs:
            r = rpc("perm.check", {"domain": "youtube.com", "action": op, "perm": pm})
            if not (r.get("ok") is False and r.get("err") == "perm_action_mismatch"):
                bad.append((op, pm, r.get("err") or r.get("ok")))
        note("B2 все обычные глаголы × чувствительные perm — mismatch", not bad, bad)
        evp = [a for a in audit_all() if a.get("ev") == "perm"
               and a.get("action") in {"sensitive_input", "send_message", "send_email"}
               and a.get("decision") == "ask"]
        note("B3 ни одной карточки на mismatch (аудит чист)", not evp, evp[:2])
        shot("s-b-nocards")

        # легитимный sensitive: карточка есть, живёт до ask_timeout (60 с)
        box = {}
        th = threading.Thread(
            target=lambda: box.update(r=rpc("perm.check", {"domain": "youtube.com",
                                                            "action": "input",
                                                            "perm": "sensitive_input",
                                                            "task": "sk43-b4"}, timeout=120)),
            daemon=True)
        th.start()
        time.sleep(1.5)
        # карточка висит: вердикта ещё нет, мост залогировал perm_ask
        # (ev:perm в аудите появляется только на развязке — ask не пишется)
        blog = (HUB / "bridge.log").read_text(encoding="utf-8", errors="replace")
        note("B4 input+sensitive_input -> карточка человеку висит (perm_ask в логе, вердикта нет)",
             "perm_ask youtube.com / sensitive_input" in blog and not box,
             [l for l in blog.splitlines() if "perm_ask" in l][-1:] if "perm_ask" in blog else "нет perm_ask в логе")
        shot("s-b4-card")

        # ---------- C: русские подписи превью ----------
        r, ok1 = action_do_hold({"selector": "#avatar-btn", "action": "click",
                                 "name": "Account menu", "ms": 1600}, "s-c1-avatar")
        pv = previews()
        st = state()
        last = pv[-1] if pv else {}
        note("C1 аватар: ASCII-имя агента -> «Меню аккаунта» из DOM",
             r.get("ok") and (r.get("result") or {}).get("executed")
             and last.get("label_src") == "dom" and "Меню аккаунта" in str(last.get("label"))
             and st.get("menuOpen") is True and "avatar" in (st.get("clicks") or []),
             f"label={last.get('label')} src={last.get('label_src')} st={st}")
        note("C1a скриншот холда сделан", ok1)

        r, ok2 = action_do_hold({"selector": "#vcard-1", "action": "click",
                                 "name": "empty area", "ms": 1600}, "s-c2-vcard")
        pv = previews()
        st = state()
        last = pv[-1] if pv else {}
        note("C2 «empty area» на видео-карточке -> заголовок из видимого текста",
             r.get("ok") and last.get("label_src") == "dom"
             and "Как собрать стенд Sidekick" in str(last.get("label"))
             and "vcard-1" in (st.get("clicks") or []),
             f"label={last.get('label')} src={last.get('label_src')}")
        note("C2a скриншот холда сделан", ok2)

        r, ok3 = action_do_hold({"action": "key", "key": "Enter", "ms": 1300}, "s-c3-key")
        last = previews()[-1] if previews() else {}
        note("C3 key: подпись остаётся клавишей (label_src agent)",
             r.get("ok") and last.get("label_src") == "agent"
             and "Enter" in str(last.get("label")),
             f"label={last.get('label')} src={last.get('label_src')}")

        r, ok4 = action_do_hold({"selector": "#q", "action": "input", "value": "стенд",
                                 "name": "Введите запрос", "ms": 1300}, "s-c4-input")
        last = previews()[-1] if previews() else {}
        note("C4 русское имя агента не перекрашивается (label_src agent)",
             r.get("ok") and last.get("label_src") == "agent"
             and "Введите запрос" in str(last.get("label")),
             f"label={last.get('label')} src={last.get('label_src')}")

        r, ok5 = action_do_hold({"selector": "#mi-settings", "action": "click",
                                 "name": "Settings", "ms": 1300}, "s-c5-menuitem")
        last = previews()[-1] if previews() else {}
        st = state()
        note("C5 пункт меню: «Settings» -> «Настройки», клик дошёл (меню закрылось)",
             r.get("ok") and last.get("label_src") == "dom"
             and "Настройки" in str(last.get("label"))
             and "mi-settings" in (st.get("clicks") or []) and st.get("menuOpen") is False,
             f"label={last.get('label')} st={st}")

        d = dom()
        e = entry_by(d, "#vcard-2")
        if not e:
            # page.dom его не отдал — координаты из contentDocument через Marionette
            try:
                rect = m.script("return (() => { const doc = Services.wm.getMostRecentWindow('navigator:browser')"
                                ".gBrowser.selectedBrowser.contentDocument;"
                                " const r = doc.getElementById('vcard-2').getBoundingClientRect();"
                                " return {x: r.x, y: r.y, w: r.width, h: r.height}; })();")
                if isinstance(rect, dict) and rect.get("w"):
                    e = {"selector": "#vcard-2", "rect": rect}
            except Exception:
                pass
        if e:
            rx = e["rect"]
            px, py = int(rx["x"] + 30), int(rx["y"] + rx["h"] - 15)  # нижняя подпись карточки
            r, ok6 = action_do_hold({"x": px, "y": py, "action": "click", "ms": 1300},
                                    "s-c6-point")
            last = previews()[-1] if previews() else {}
            st = state()
            note("C6 point-клик: цель подписана из DOM (русский текст под кольцом)",
                 r.get("ok") and (r.get("result") or {}).get("executed")
                 and last.get("label_src") == "dom"
                 and ("Джев ТВ" in str(last.get("label")) or "Топ-5" in str(last.get("label")))
                 and "vcard-2" in (st.get("clicks") or []),
                 f"label={last.get('label')} src={last.get("label_src")} st={st}")
        else:
            note("C6 point-клик: цель подписана из DOM", "skip", "#vcard-2 нет в page.dom")

        # ---------- D: регресс ----------
        n0 = len(previews())
        r = rpc("action.do", {"action": "scroll", "dy": 400}, timeout=30)
        n1 = len(previews())
        sy = ((dom().get("viewport") or {}).get("sy")) or 0
        note("D1 scroll: исполнен, превью-кадров не добавил, viewport.sy>0",
             r.get("ok") and (r.get("result") or {}).get("executed") and n1 == n0 and sy > 0,
             f"exec={ (r.get('result') or {}).get('executed')} pv+{n1 - n0} sy={sy}")
        shot("s-d1-scroll")

        # B4 хвост: карточка, открытая до фазы C, к этому времени уже протухла
        th.join(timeout=90)
        rb = box.get("r") or {}
        rr = rb.get("result") or {}
        evp2 = [a for a in audit_all() if a.get("ev") == "perm"
                and a.get("action") == "sensitive_input" and a.get("decision") == "block"]
        note("B4b карточка sensitive_input протухла в block/ask_timeout (и в аудите)",
             rb.get("ok") and rr.get("decision") == "block" and rr.get("via") == "ask_timeout"
             and bool(evp2),
             rb)

    finally:
        # ---------- Z: уборка ----------
        try:
            if m:
                m.script(CLOSE_TAB_JS, args=["youtube"])
                m.script(SPARE_TAB_JS)
        except Exception as e:
            print("WARN tabs:", e, flush=True)
        try:
            if m:
                install(m, PROD_XPI)
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
        if httpd:
            httpd.terminate()
        t1 = time.time()
        while time.time() - t1 < 30 and ss_count(8791) < 1:
            time.sleep(1.0)
        note("Z2 прод-аддон переустановлен, :8791 восстановлен",
             ss_count(8791) >= 1,
             f"@8791={ss_count(8791)} (было {prod_before})")
        # SK-49: тест-Firefox не должен оставаться вторым коннектом к проду :8791 —
        # мост теперь вытесняет первым пришедшим hello (close 4009), но браузеру
        # пользователя лучше вернуть маршрут сразу, без ожидания пинг-понга.
        # Убиваем ПО PID: слушатель порта Marionette — и есть процесс firefox;
        # pkill-паттерн («firefox-158/firefox --marionette») матчит и собственный
        # shell харнеса — SIGTERM себе.
        fpid = listener_pid(MAR_PORT)
        if fpid:
            subprocess.run(["kill", str(fpid)], capture_output=True)
            t1 = time.time()
            while time.time() - t1 < 15 and listener_pid(MAR_PORT):
                time.sleep(0.5)
            if listener_pid(MAR_PORT):
                subprocess.run(["kill", "-9", str(listener_pid(MAR_PORT))],
                               capture_output=True)
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
        (S / "sk43_acc_results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
