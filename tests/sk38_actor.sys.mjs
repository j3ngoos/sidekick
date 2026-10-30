// SK-38 приёмка: test-only window actor (runtime-registered, никогда не shipped).
// Живёт в сайдбар-документе: читает chrome UI v3 (шапка=иконка+заголовок,
// морф-кнопка, живой плейсхолдер, меню под шапкой), кликает/жмёт клавиши,
// грузит пакованные иконки через <img>. Механизм — прецедент sk30/31/32/34.
const EMOJI = /[\u{1F000}-\u{1FAFF}\u2600-\u27BF\u27F0-\u27FF\u2B00-\u2BFF\uFF0B\uFE0F]/u;

export class Sk38ProbeParent extends JSWindowActorParent {}

export class Sk38ProbeChild extends JSWindowActorChild {
  receiveMessage({ name, data }) {
    if (name !== "sk38:op") return undefined;
    const win = this.contentWindow;
    const d = win.document;
    const cs = (sel) => { const e = d.querySelector(sel); return e ? win.getComputedStyle(e) : null; };
    try {
      if (data.op === "href") return String(d.location.href);
      if (data.op === "extUrl") { try { return win.location.href; } catch (e) { return "ERR"; } }

      if (data.op === "chrome") {
        const zones = ["#hd", "#bar", "#menu"];
        const emoji = [];
        for (const z of zones) for (const el of d.querySelectorAll(z)) {
          const t = (el.textContent || "").trim();
          if (EMOJI.test(t)) emoji.push(z + ": " + t.slice(0, 60));
        }
        const bf = (sel) => { const c = cs(sel); return c ? (c.backdropFilter || c.webkitBackdropFilter || "none") : "no-el"; };
        const hdic = d.querySelector("#hdic");
        const hb = hdic ? hdic.getBoundingClientRect() : null;
        const ph = d.querySelector("#phin");
        const go = d.querySelector("#go");
        return JSON.stringify({
          menuHidden: d.querySelector("#menu").hidden,
          ariaExpanded: d.querySelector("#menuBtn").getAttribute("aria-expanded"),
          barBtns: d.querySelectorAll("#bar button").length,
          hdBtns: d.querySelectorAll("#hd button").length,
          emoji,
          svgCount: d.querySelectorAll("svg.icn use").length,
          spriteCount: d.querySelectorAll("#spr symbol").length,
          bfHd: bf("#hd"), bfBar: bf("#bar"), bfMenu: bf("#menu"),
          theme: d.documentElement.dataset.theme || "system",
          bodyBg: win.getComputedStyle(d.body).backgroundImage.slice(0, 40),
          ttl: (d.querySelector("#ttl").textContent || "").trim(),
          hdText: (d.querySelector("#hd").textContent || "").trim(),
          hdicVisible: !!hb && hb.width >= 12 && hb.height >= 12,
          goRunning: go.classList.contains("running"),
          goAria: go.getAttribute("aria-label"),
          goBg: cs("#go").backgroundColor,
          phText: ph ? (ph.textContent || "") : "no-el",
          phOpacity: ph ? cs("#phin").opacity : "no-el",
          inpPlaceholder: d.querySelector("#in").getAttribute("placeholder") || "",
          focused: d.activeElement ? (d.activeElement.id || d.activeElement.tagName) : "",
        });
      }

      if (data.op === "menuRect") {
        const r = d.querySelector("#menu").getBoundingClientRect();
        const h = d.querySelector("#hd").getBoundingClientRect();
        return JSON.stringify({ menuTop: Math.round(r.top), menuLeft: Math.round(r.left),
          hdBottom: Math.round(h.bottom), menuVisible: !d.querySelector("#menu").hidden });
      }

      if (data.op === "goState") {
        const go = d.querySelector("#go");
        const send = go.querySelector(".ic-send"), stop = go.querySelector(".ic-stop");
        return JSON.stringify({
          running: go.classList.contains("running"),
          aria: go.getAttribute("aria-label"),
          bg: cs("#go").backgroundColor,
          transition: cs("#go").transitionDuration,
          sendOp: send ? cs("#go .ic-send").opacity : "no-el",
          stopOp: stop ? cs("#go .ic-stop").opacity : "no-el",
        });
      }

      if (data.op === "phSample") {
        const ph = d.querySelector("#phin");
        return JSON.stringify({ text: ph ? (ph.textContent || "") : "no-el",
          opacity: ph ? cs("#phin").opacity : "no-el" });
      }

      if (data.op === "iconLoad") {
        // пакованные иконки грузятся со страницы расширения (img-src 'self')
        const sizes = data.sizes || [16, 32, 48, 96];
        return new Promise((resolve) => {
          const out = {}; let left = sizes.length;
          for (const s of sizes) {
            const img = d.createElement("img");
            img.onload = () => { out[s] = img.naturalWidth + "x" + img.naturalHeight; if (--left === 0) resolve(JSON.stringify(out)); };
            img.onerror = () => { out[s] = "ERR"; if (--left === 0) resolve(JSON.stringify(out)); };
            img.src = new win.URL("icons/icon-" + s + ".png", d.baseURI).href;
          }
        });
      }

      if (data.op === "clickGo") { d.querySelector("#go").click(); return "clicked"; }
      if (data.op === "clickNew") { d.querySelector("#newBtn").click(); return "clicked"; }
      if (data.op === "openHist") { d.querySelector("#histBtn").click(); return "clicked"; }
      if (data.op === "clickHrow") {
        const rows = Array.from(d.querySelectorAll("#histlist .hrow"));
        const want = data.skipCur ? rows.find((r) => !r.classList.contains("cur")) : rows[data.n || 0];
        if (!want) return "no-row:" + rows.length;
        want.click(); return "clicked";
      }
      if (data.op === "blurIn") { d.querySelector("#in").blur(); return "blurred"; }
      if (data.op === "closeViews") {
        for (const id of ["histclose", "audclose"]) {
          const b = d.getElementById(id);
          if (b && b.offsetParent !== null) b.click();
        }
        return "closed";
      }
      if (data.op === "clickMenuBtn") { d.querySelector("#menuBtn").click(); return "clicked"; }
      if (data.op === "clickMenuItem") {
        for (const b of d.querySelectorAll("#menu .mi")) {
          if ((b.textContent || "").includes(data.has)) { b.click(); return "clicked:" + b.id; }
        }
        return "no-item:" + data.has;
      }
      if (data.op === "focusComposer") {
        const i = d.querySelector("#in");
        i.focus(); i.value = data.text || "";
        i.dispatchEvent(new win.Event("input", { bubbles: true }));
        return "focused";
      }
      if (data.op === "composerValue") return String(d.querySelector("#in").value);
      if (data.op === "pressKey") {
        const t = data.target === "menu" ? d.querySelector("#menu") : (d.activeElement || d);
        t.dispatchEvent(new win.KeyboardEvent("keydown", { key: data.key, bubbles: true, cancelable: true }));
        return "sent:" + data.key;
      }

      if (data.op === "computed") {
        const out = {};
        for (const sel of data.sels) {
          const e = d.querySelector(sel); if (!e) { out[sel] = "no-el"; continue; }
          const c = win.getComputedStyle(e);
          out[sel] = { color: c.color, background: c.backgroundColor, backgroundImage: c.backgroundImage.slice(0, 30) };
        }
        return JSON.stringify(out);
      }

      if (data.op === "seedToken") {
        try {
          const dd = this.contentWindow.document;
          const inp = dd.getElementById("tok"), btn = dd.getElementById("save");
          if (!inp || !btn) return "no-fields";
          inp.value = String(data.token);
          btn.click();
          return "seeded";
        } catch (e) { return "ERR:" + String(e && e.message || e).slice(0, 120); }
      }

      return "unknown-op";
    } catch (e) { return "actor-err:" + String(e && e.message || e).slice(0, 160); }
  }
}
