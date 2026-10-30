// SK-71 приёмка: test-only window actor (runtime-registered, никогда не shipped).
// Прецедент sk37_actor: фиксированный словарь операций, НИКАКОГО new Function —
// CSP расширения блокирует динамический код внутри actor-песочницы.
export class Sk71ProbeParent extends JSWindowActorParent {}

export class Sk71ProbeChild extends JSWindowActorChild {
  receiveMessage({ name, data }) {
    if (name !== "sk71:op") return undefined;
    const win = this.contentWindow;
    if (!win) return JSON.stringify({err: "no-contentWindow"});
    const d = win.document;
    const pw = Cu.waiveXrays(win);
    const wrap = (x) => {
      try { return JSON.stringify({v: x === undefined ? null : x}); }
      catch (e) { return JSON.stringify({err: "unserializable"}); }
    };
    const txt = (sel) => { const e = d.querySelector(sel); return e ? String(e.textContent || "").trim() : ""; };
    try {
      if (data.op === "href") return String(d.location.href);

      if (data.op === "uiState") {
        // сайдбар: живые подписи интерфейса + маркер first-paint (окно не перезагружается
        // при смене языка — ts не меняется = live-switch, а не reload)
        if (!win.__sk71ts) win.__sk71ts = Date.now();
        const de = d.documentElement;
        return wrap({
          href: String(d.location.href).slice(0, 90),
          ts: win.__sk71ts,
          langAttr: String(de.lang || ""),
          theme: String(de.dataset ? (de.dataset.theme || "system") : "system"),
          ttl: txt("#empttl"),
          sub: txt("#emsub").slice(0, 120),
          ph: d.getElementById("in") ? d.getElementById("in").placeholder : "",
          phin: txt("#phin"),
          go: d.getElementById("go") ? d.getElementById("go").title : "",
          newLbl: txt("#newBtn"),
          themeLbl: txt("#themeLbl"),
          settings: txt("#optBtn"),
        });
      }

      if (data.op === "clickTheme") {
        const mb = d.getElementById("menuBtn"), tb = d.getElementById("themeBtn");
        if (!mb || !tb) return wrap("no-menu-btns");
        mb.click();
        tb.click();
        return wrap(String(d.documentElement.dataset.theme || "system"));
      }

      if (data.op === "optState") {
        const sel = d.getElementById("lang");
        return wrap({
          href: String(d.location.href).slice(0, 90),
          langVal: sel ? sel.value : "none",
          tokH: txt("#h-tok"),
          save: txt("#save"),
          title: String(d.title || ""),
          bell: txt("#bellTest"),
        });
      }

      if (data.op === "langSet") {
        // честная смена языка через <select> (change = юзер-путь options.js)
        const sel = d.getElementById("lang");
        if (!sel) return wrap("no-lang-select");
        sel.value = String(data.arg);
        if (sel.value !== String(data.arg)) return wrap("value-rejected:" + sel.value);
        sel.dispatchEvent(new win.Event("change", {bubbles: true}));
        return wrap("changed:" + sel.value);
      }

      if (data.op === "overlay") {
        const host = d.getElementById("hermes-sidekick-host");
        const sr = host && host.shadowRoot;
        if (!sr) return wrap({err: "no-host-shadow", href: String(d.location.href).slice(0, 60)});
        const pv = sr.querySelector(".pv");
        return wrap({
          pvVisible: !!pv && pw.getComputedStyle(pv).display !== "none",
          label: pv ? String(pv.textContent || "").trim().slice(0, 80) : "",
        });
      }

      if (data.op === "overlayKick") {
        pw.browser.runtime.sendMessage({ type: "sk71_overlay_probe" });
        return wrap("kicked");
      }

      if (data.op === "storageGet") {
        const k = String(data.arg);
        return pw.browser.storage.local.get(k).then(
          (got) => wrap(got && Object.prototype.hasOwnProperty.call(got, k) ? got[k] : null),
          (e) => JSON.stringify({err: "st:" + String(e && e.message || e).slice(0, 140)}));
      }

      if (data.op === "seedCheck") return wrap(String(d.title || ""));

      if (data.op === "storageSet") {
        return pw.browser.storage.local.set(data.arg).then(
          () => wrap("set"), (e) => wrap({err: "st:" + String(e && e.message || e).slice(0, 120)}));
      }

      if (data.op === "storageRemove") {
        return pw.browser.storage.local.remove(data.arg).then(
          () => wrap("removed"), (e) => wrap({err: "st:" + String(e && e.message || e).slice(0, 120)}));
      }

      if (data.op === "bgPing") {
        // фон жив? просим словарь прямо (i18n_get) — тихий ответ = жив
        return pw.browser.runtime.sendMessage({ type: "i18n_get" }).then(
          (r) => wrap({ lang: r && r.lang, keys: r && r.dict ? Object.keys(r.dict).length : 0,
                        fb: r && r.fallback ? Object.keys(r.fallback).length : 0 }),
          (e) => wrap({ err: "bg-silent:" + String(e && e.message || e).slice(0, 120) }));
      }

      if (data.op === "wsCheck") {
        return pw.browser.runtime.sendMessage({ type: "ws-check" }).then(
          (r) => wrap(r),
          (e) => wrap({ err: "bg-silent:" + String(e && e.message || e).slice(0, 120) }));
      }

      if (data.op === "seedToken") {
        const inp = d.getElementById("tok"), btn = d.getElementById("save");
        if (!inp || !btn) return wrap("no-fields");
        inp.value = String(data.arg);
        btn.click();
        return wrap("seeded");
      }

      return JSON.stringify({err: "unknown-op:" + data.op});
    } catch (e) {
      return JSON.stringify({err: "actor-err:" + String(e && e.message || e).slice(0, 160)});
    }
  }
}
