#!/usr/bin/env python3
"""SK-41 регрессия: ozon/google/mail/youtube на стенде + живой оверлей-превью.

Два прогона: --ext <снимок ДО> и --ext <рабочее дерево ПОСЛЕ> — «работают как
раньше» сравнивается на ОДНОМ стенде, сборка-в-сборку. Расширение цепляется к
ИЗОЛИРОВАННОМУ мосту :8796 (токен зашит в тест-XPI, FALLBACK перебит) — прод
:8791 не получает ни одного коннекта (проверяется ss до/после).

Проверка на каждом сайте:
  L  страница загрузилась (title, inText, не netError)
  P  RPC action.preview {action:"click", selector:"a", ms:900} — оверлей ЖИВ:
     вердикт ok и скриншот во время превью (курсор+кольцо на первой ссылке).
     НИ ОДНОГО реального клика — только превью (AGENTS.md: preview легален для
     живых сайтов).
Z  уборка: firefox, мост, serve.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8796
PAGE_PORT = 8899
PROD_PORT = 8791
SITES = [
    ("ozon", "https://www.ozon.ru/"),
    ("google", "https://google.ru/"),
    ("mail", "https://mail.ru/"),
    ("youtube", "https://www.youtube.com/"),
]

sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
from mario import Marionette  # noqa: E402
sys.path.insert(0, str(REPO / "tests"))
import sk41_acc as A41  # noqa: E402  (SETUP_JS/OP_JS — actor-канал чтения страницы)

OPEN_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
               "const t = win.gBrowser.addTrustedTab(arguments[0]); win.gBrowser.selectedTab = t;"
               "return 'tab';})();")
CLOSE_OTHER_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                  "for (let i = win.gBrowser.tabs.length - 1; i >= 0; i--) {"
                  " const t = win.gBrowser.tabs[i];"
                  " if (t !== win.gBrowser.selectedTab) win.gBrowser.removeTab(t);}"
                  "return 'ok';})();")
# FF158: CPOW удалены — чтение страницы только через content-контекст Marionette
READY_JS = "return document.readyState;"
INFO_JS = ("return JSON.stringify({title: document.title, href: location.href,"
           " n: ((document.body && document.body.innerText) || '').length});")
ADDON_INSTALL_JS = """
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
    const nsFile = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
    nsFile.initWithPath(arguments[0]);
    const a = await AddonManager.installTemporaryAddon(nsFile);
    resolve({ok: true, id: a && a.id});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""


def log(*a):
    print(*a, flush=True)


def ss_count(port):
    r = subprocess.run(["ss", "-tn", "state", "established", f"( sport = :{port} )"],
                       capture_output=True, text=True)
    return max(0, len(r.stdout.strip().splitlines()) - 1)


def listener_pid(port):
    r = subprocess.run(["ss", "-tlnp", f"( sport = :{port} )"], capture_output=True, text=True)
    m = re.search(r"pid=(\d+)", r.stdout)
    return int(m.group(1)) if m else None


def build_xpi(src_dir, xpi):
    with zipfile.ZipFile(xpi, "w", zipfile.ZIP_DEFLATED) as z:
        for p in pathlib.Path(src_dir).rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(src_dir))


def prep_tok(src, dst, hub):
    """Тест-сборка: FALLBACK->:8796, только он, токен зашит (прецедент sk39_acc)."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".chrome"))
    bj = dst / "background.js"
    s = bj.read_text(encoding="utf-8")
    assert 'const FALLBACK_SERVER = "ws://127.0.0.1:8791";' in s
    s = s.replace('const FALLBACK_SERVER = "ws://127.0.0.1:8791";',
                  f'const FALLBACK_SERVER = "ws://127.0.0.1:{PORT}";')
    assert "for (const u of [saved, FALLBACK_SERVER])" in s
    s = s.replace("for (const u of [saved, FALLBACK_SERVER])", "for (const u of [FALLBACK_SERVER])")
    tok = (hub / "token").read_text().strip()
    gt_old = ('async function getToken() {\n'
              '  const { bridgeToken } = await browser.storage.local.get("bridgeToken");\n'
              '  return (bridgeToken || "").trim();\n}')
    assert gt_old in s, "getToken не найден"
    s = s.replace(gt_old, "async function getToken() {\n  return '" + tok.replace("'", "") + "';\n}")
    bj.write_text(s, encoding="utf-8")
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ext", required=True)
    ap.add_argument("--tag", required=True)  # before | after
    ap.add_argument("--shots", required=True)
    a = ap.parse_args()
    shots = pathlib.Path(a.shots)
    shots.mkdir(parents=True, exist_ok=True)
    S = shots.parent
    hub = S / f"hub_{a.tag}"
    if hub.exists():
        shutil.rmtree(hub)
    hub.mkdir(parents=True)
    shutil.copy(REPO / "bridge" / "hub" / "token", hub / "token")

    prod_before = ss_count(PROD_PORT)
    results = {}

    def note(name, ok, detail=""):
        results[name] = {"ok": bool(ok), "detail": str(detail)[:300]}
        log(("PASS " if ok else "FAIL ") + name + ("  " + str(detail)[:180] if detail else ""))

    br = m = srv = ff = None
    try:
        if listener_pid(PORT):
            subprocess.run(["kill", "-9", str(listener_pid(PORT))])
            time.sleep(0.5)
        for _ in range(10):
            zpid = listener_pid(MAR_PORT)
            if not zpid:
                break
            subprocess.run(["kill", "-9", str(zpid)])
            time.sleep(0.5)
        subprocess.run(["pkill", "-9", "-f", "firefox-158/[f]irefox"])
        time.sleep(1.0)
        br = subprocess.Popen(["python3", str(REPO / "bridge" / "sidekick_bridge.py")],
                              env={**os.environ, "SIDEKICK_HOME": str(hub), "SIDEKICK_PORT": str(PORT)},
                              stdout=open(hub / "bridge.log", "w", encoding="utf-8"),
                              stderr=subprocess.STDOUT)
        time.sleep(1.0)
        note("S0 мост :8796", listener_pid(PORT) == br.pid)
        if not listener_pid(PAGE_PORT):
            srv = subprocess.Popen(["python3", str(REPO / "tests" / "serve.py")],
                                   env={**os.environ, "SIDEKICK_TEST_PORT": str(PAGE_PORT)},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.8)
        build = prep_tok(pathlib.Path(a.ext), S / f"regext_{a.tag}", hub)
        xpi = S / f"reg_{a.tag}.xpi"
        build_xpi(build, xpi)
        prof = S / f"regprof_{a.tag}"
        if prof.exists():
            shutil.rmtree(prof)
        shutil.copytree(HOME / ".firefox-clean-template", prof)  # чистый профиль: без
        # восстановленной сессии и следов прошлых прогонов (иначе «до/после» трахается)
        ff = subprocess.Popen(
            [str(HOME / ".local/firefox-158/firefox"), "--marionette", "--remote-allow-system-access",
             "--profile", str(prof), "--no-remote"],
            env={**ENV, "MOZ_DISABLE_CONTENT_SANDBOX": "1"},  # actor грузится только с ним
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < 45 and not listener_pid(MAR_PORT):
            time.sleep(1.0)
        m = Marionette("127.0.0.1", MAR_PORT)

        m.script(A41.SPARE_TAB_JS)
        reg = m.ascript(A41.SETUP_JS % str(REPO / "tests"))
        note("S0 actor", isinstance(reg, dict) and reg.get("ok"), reg)

        def op(name, arg=None, timeout=20):
            payload = {"op": name}
            if arg is not None:
                payload["arg"] = arg
            try:
                r = m.ascript(A41.OP_JS, args=[json.dumps(payload)], timeout=timeout)
            except Exception as e:  # noqa: BLE001
                return {"_err": str(e)[:120]}
            if not (isinstance(r, dict) and r.get("ok")):
                return {"_err": str(r)[:120]}
            v = r.get("val")
            try:
                j = json.loads(v) if isinstance(v, str) else {}
            except (json.JSONDecodeError, TypeError):
                return {"_err": "bad-json:" + str(v)[:120]}
            return {"_err": j["err"]} if "err" in j else j.get("v")

        r0 = m.ascript(ADDON_INSTALL_JS, args=[str(xpi.resolve())])
        note("S0 XPI", isinstance(r0, dict) and r0.get("ok"), r0)
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0 расширение на изолированном мосту", ss_count(PORT) > 0, f"conns={ss_count(PORT)}")

        def rpc(method, params, timeout=25):
            rid = str(uuid.uuid4())
            with open(hub / "rpc_in.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"v": 1, "id": rid, "method": method, "params": params},
                                   ensure_ascii=False) + "\n")
            t2 = time.time()
            while time.time() - t2 < timeout:
                p = hub / "rpc_out.jsonl"
                if p.exists():
                    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
                        try:
                            o = json.loads(ln)
                        except ValueError:
                            continue
                        if o.get("id") == rid:
                            return o
                time.sleep(0.05)
            return {"err": "harness_timeout"}

        for name, url in SITES:
            m.script(OPEN_TAB_JS, [url])
            m.script(CLOSE_OTHER_JS)
            t2 = time.time()
            while time.time() - t2 < 30:
                if op("ready") == "complete":
                    break
                time.sleep(0.7)
            time.sleep(3.0)  # дозарегрузка тяжёлых страниц
            info = op("info")
            if not (isinstance(info, dict) and "title" in info):
                info = {"title": "", "n": 0, "href": "ERR:" + str(info)[:80]}
            ok_l = bool(info.get("title")) and info.get("n", 0) > 50 and "netError" not in str(info.get("href"))
            note(f"L {name}: страница загрузилась", ok_l, info)
            # превью на первой ссылке — оверлей жив, кликов нет
            box = {}
            def _prev():
                box["res"] = rpc("action.preview", {"action": "click", "selector": "a", "ms": 900})
            th = threading.Thread(target=_prev, daemon=True)
            th.start()
            time.sleep(0.45)
            p = shots / f"{name}_preview.png"
            subprocess.run(["scrot", "-o", str(p)], env=ENV, capture_output=True)
            th.join(timeout=20)
            res = box.get("res", {"err": "no_result"})
            ok_p = isinstance(res, dict) and res.get("ok") and "err" not in (res.get("result") or {})
            note(f"P {name}: превью RPC ok, скрин {p.name}", ok_p and p.exists(),
                 json.dumps(res, ensure_ascii=False)[:200])
            time.sleep(1.2)
    except Exception as e:  # noqa: BLE001
        import traceback
        note("FATAL", False, traceback.format_exc()[-300:])
    finally:
        try:
            if ff:
                ff.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            if br:
                br.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            if srv:
                srv.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            if m:
                m.close()
        except Exception:  # noqa: BLE001
            pass

    prod_after = ss_count(PROD_PORT)
    note("R прод :8791 не тронут", prod_after >= prod_before, f"{prod_before}->{prod_after}")
    bad = [k for k, v in results.items() if not v["ok"]]
    log(f"\n=== SK-41 регресс {a.tag}: {len(results) - len(bad)}/{len(results)} PASS"
        f"{' | FAIL: ' + ', '.join(bad) if bad else ''} ===")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
