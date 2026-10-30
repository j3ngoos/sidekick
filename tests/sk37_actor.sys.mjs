// SK-37 приёмка: test-only window actor (runtime-registered, никогда не shipped).
// Прецедент sk38_actor: фиксированный словарь операций, НИКАКОГО new Function —
// CSP расширения блокирует динамический код внутри actor-песочницы.
export class Sk37ProbeParent extends JSWindowActorParent {}

export class Sk37ProbeChild extends JSWindowActorChild {
  receiveMessage({ name, data }) {
    if (name !== "sk37:op") return undefined;
    const win = this.contentWindow;
    if (!win) return JSON.stringify({err: "no-contentWindow"});
    const d = win.document;
    // Xray: chrome-актор не видит page-свойства sandbox-глобала (browser, bellFocus) —
    // снимаем Xray для обращений к API самой страницы
    const pw = Cu.waiveXrays(win);
    const wrap = (x) => {
      try { return JSON.stringify({v: x === undefined ? null : x}); }
      catch (e) { return JSON.stringify({err: "unserializable"}); }
    };
    try {
      if (data.op === "href") return String(d.location.href);

      if (data.op === "overlay") {
        // превью-оверлей content script: shadow-хост в DOM страницы (SK-37: кнопок нет)
        const host = d.getElementById("hermes-sidekick-host");
        const sr = host && host.shadowRoot;
        if (!sr) return wrap({err: "no-host-shadow", href: String(d.location.href).slice(0, 60)});
        const pv = sr.querySelector(".pv");
        return wrap({
          buttons: sr.querySelectorAll("button").length,
          pvVisible: !!pv && pw.getComputedStyle(pv).display !== "none",
          label: pv ? String(pv.textContent || "").trim().slice(0, 80) : "",
          ring: !!sr.querySelector(".ring"),
        });
      }

      if (data.op === "notifyTest") {
        // диагностика стенда: жив ли вообще ОС-бэкенд уведомлений у этого Firefox
        return pw.browser.notifications.create("sk37-diag-" + Date.now(), {
          type: "basic", title: "SK-37", message: "diag",
          iconUrl: pw.browser.runtime.getURL("icons/icon-96.png"),
        }).then((id) => wrap("created:" + id),
                (e) => wrap("rejected:" + String(e && e.message || e).slice(0, 140)));
      }

      if (data.op === "overlayProbe") {
        // parent-актор на webIsolated в этой сборке не поднимается (remote type
        // mismatch) — читаем превью-оверлей штатным путём расширения:
        // scripting.executeScript в активной вкладке (isolated world = world
        // контент-скрипта; shadow-хост открыт, DOM общий)
        // tabs.query возвращает не нативный Promise (Xray прячет then от chrome) —
        // снимаем Xray с промиса, иначе «Permission denied to access property "then"»
        const qp = Cu.waiveXrays(pw.browser.tabs.query({ active: true, lastFocusedWindow: true }));
        return qp.then(async ([t]) => {
          if (!t) return JSON.stringify({ err: "no-active-tab" });
          const [res] = await Cu.waiveXrays(pw.browser.scripting.executeScript({
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
          }));
          return wrap(res && res.result);
        }, (e) => JSON.stringify({ err: "scripting:" + String(e && e.message || e).slice(0, 140) }));
      }

      if (data.op === "overlayKick") {
        // фоновый зонд превью: fire-and-forget сообщение фону, результат — в storage
        pw.browser.runtime.sendMessage({ type: "sk37_overlay_probe" });
        return wrap("kicked");
      }

      if (data.op === "storageGet") {
        const k = String(data.arg);
        return pw.browser.storage.local.get(k).then(
          (got) => wrap(got && Object.prototype.hasOwnProperty.call(got, k) ? got[k] : null),
          (e) => JSON.stringify({err: "st:" + String(e && e.message || e).slice(0, 140)}));
      }

      if (data.op === "cardState") {
        const card = d.querySelector(".perm:not(.done)") || d.querySelector(".perm");
        const img = card ? card.querySelector("img.pshot") : null;
        return wrap({
          href: String(d.location.href).slice(0, 90),
          perms: d.querySelectorAll(".perm").length,
          done: d.querySelectorAll(".perm.done").length,
          flash: d.querySelectorAll(".perm.flash").length,
          sub: card ? String(card.querySelector(".sub") ? card.querySelector(".sub").textContent : "").slice(0, 160) : "",
          shotSrc: img ? String(img.getAttribute("src") || "").slice(0, 24) : "",
          shotH: img ? Math.round(img.getBoundingClientRect().height) : 0,
          btns: card ? [...card.querySelectorAll(".btns button")].map(b => b.textContent.trim().slice(0, 40)) : [],
        });
      }

      if (data.op === "clickBtn") {
        const card = d.querySelector(".perm:not(.done)");
        if (!card) return wrap("no-card");
        const b = card.querySelector(".btns button." + String(data.arg));
        if (!b) return wrap("no-btn:" + String(data.arg));
        b.click();
        return wrap("clicked:" + b.textContent.trim().slice(0, 30));
      }

      if (data.op === "focusCard") {
        const card = d.querySelector(".perm:not(.done)");
        if (!card) return wrap("no-card");
        pw.bellFocus(card.dataset.id, "perm");
        return wrap("focused:" + String(card.dataset.id).slice(0, 8));
      }

      if (data.op === "seedToken") {
        const inp = d.getElementById("tok"), btn = d.getElementById("save");
        if (!inp || !btn) return wrap("no-fields");
        inp.value = String(data.arg);
        btn.click();
        return wrap("seeded");
      }

      if (data.op === "bellProbe") {
        return pw.browser.storage.local.get("__sk37_bell").then(
          (got) => wrap(got && got.__sk37_bell || null),
          (e) => JSON.stringify({err: "storage:" + String(e && e.message || e).slice(0, 140)}));
      }

      return JSON.stringify({err: "unknown-op:" + data.op});
    } catch (e) {
      return JSON.stringify({err: "actor-err:" + String(e && e.message || e).slice(0, 160)});
    }
  }
}
