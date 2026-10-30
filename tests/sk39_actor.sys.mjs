// SK-39 живая приёмка: window actor — скан оверлея в shadow DOM, fps, состояние страницы.
// Паттерн — sk36_actor.sys.mjs. Ops возвращают JSON-строку {v:...} | {err:...}.
export class Sk39ProbeParent extends JSWindowActorParent {}
export class Sk39ProbeChild extends JSWindowActorChild {
  receiveMessage({ name, data }) {
    if (name !== "sk39:op") return undefined;
    const win = this.contentWindow;
    if (!win) return JSON.stringify({ err: "no-contentWindow" });
    const d = win.document;
    // expando-переменные страницы (__clicks, __fps*) через Xray не видны — нужен waive
    const pw = Cu.waiveXrays(win);
    try {
      if (data.op === "href") return JSON.stringify({ v: String(d.location.href).slice(0, 90) });

      if (data.op === "seedToken") { // options.html: посев токена моста (прецедент sk36)
        const inp = d.getElementById("tok"), btn = d.getElementById("save");
        if (!inp || !btn) return JSON.stringify({ err: "no-fields" });
        inp.value = String(data.arg);
        btn.click();
        return JSON.stringify({ v: "seeded" });
      }

      if (data.op === "ready") {
        const h = d.getElementById("hermes-sidekick-host");
        return JSON.stringify({ v: !!(h && h.shadowRoot && h.shadowRoot.getElementById("c")) });
      }

      // полный снимок оверлея: курсор/спиннер/кольцо/пузырёк/ripple/свечение
      if (data.op === "scan") {
        const h = d.getElementById("hermes-sidekick-host");
        if (!h || !h.shadowRoot) return JSON.stringify({ v: { host: false } });
        const r = h.shadowRoot;
        const cs = (el) => (el ? win.getComputedStyle(el) : null);
        const cur = r.getElementById("c");
        const curcs = cs(cur);
        const spin = r.querySelector(".spin");
        const ring = r.getElementById("r");
        const pv = r.getElementById("p");
        const rp = [...r.querySelectorAll(".rp")];
        const glw = [...r.querySelectorAll(".glw")];
        return JSON.stringify({ v: {
          host: true,
          cur: cur ? { cls: String(cur.className), tr: String(cur.style.transform || ""),
                       td: curcs ? curcs.transitionDuration : "", on: cur.classList.contains("on"),
                       wait: cur.classList.contains("waiting"), itext: cur.classList.contains("s-text") } : null,
          spin: spin ? { disp: cs(spin).display, anim: cs(spin).animationName } : null,
          curiAnim: (() => { const i = r.querySelector(".curi"); return i ? cs(i).animationName : ""; })(),
          ring: ring ? { cls: String(ring.className), tr: String(ring.style.transform || "") } : null,
          pv: pv ? { cls: String(pv.className), text: String(pv.textContent || "").slice(0, 60) } : null,
          rp: rp.length,
          rp0: rp.length ? { bc: cs(rp[0]).borderColor, op: cs(rp[0]).opacity,
                             anims: rp[0].getAnimations().length } : null,
          glw: glw.length,
          fxKids: (r.getElementById("f") || {}).childElementCount || 0,
        } });
      }

      // состояние страницы: клики, задержка ripple после клика, значение поля
      if (data.op === "page") {
        const inp = d.getElementById("inp");
        return JSON.stringify({ v: {
          clicks: pw.__clicks || 0, rpSeen: pw.__rpSeen === undefined ? -2 : pw.__rpSeen,
          inp: inp ? String(inp.value) : null,
          btnColor: win.getComputedStyle(d.getElementById("btn")).color,
        } });
      }

      // fps: не блокирующий rAF-счётчик (start → нагрузка → read)
      if (data.op === "fpsStart") { pw.__fpsStart(); return JSON.stringify({ v: "started" }); }
      if (data.op === "fpsRead") {
        const ms = win.performance.now() - (pw.__f0 || 0);
        return JSON.stringify({ v: { frames: pw.__fn || 0, ms: Math.round(ms),
                                     fps: ms > 50 ? +(((pw.__fn || 0) / ms) * 1000).toFixed(1) : 0 } });
      }

      // prefers-reduced-motion глазами страницы
      if (data.op === "rmq") return JSON.stringify({ v: win.matchMedia("(prefers-reduced-motion: reduce)").matches });

      return JSON.stringify({ err: "unknown-op:" + data.op });
    } catch (e) {
      return JSON.stringify({ err: "actor-err:" + String(e && e.message || e).slice(0, 160) });
    }
  }
}
