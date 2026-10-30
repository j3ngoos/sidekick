// SK-36 живая приёмка: window actor для DOM-скана шагов в сайдбаре (Fission).
// Паттерн — sk35_actor.sys.mjs. Ops возвращают JSON-строку {v:...} | {err:...}.
export class Sk36ProbeParent extends JSWindowActorParent {}
export class Sk36ProbeChild extends JSWindowActorChild {
  receiveMessage({ name, data }) {
    if (name !== "sk36:op") return undefined;
    const win = this.contentWindow;
    if (!win) return JSON.stringify({ err: "no-contentWindow" });
    const d = win.document;
    const pw = Cu.waiveXrays(win);
    try {
      if (data.op === "href") return JSON.stringify({ v: String(d.location.href).slice(0, 90) });

      // DOM-скан карточки задачи: ряды шагов + «подробнее» + открытый кат raw
      if (data.op === "scanSteps") {
        const card = d.querySelector(".m.task");
        if (!card) return JSON.stringify({ v: { found: false } });
        const rows = [...card.querySelectorAll(".tksteps .srow")].map((r) => {
          const tx = r.querySelector(".tx");
          const more = r.querySelector("button.smore");
          const raw = r.querySelector("pre.sraw");
          return {
            cls: String(r.className || ""),
            tx: String(tx ? tx.textContent : "").trim(),
            more: !!more, moreText: more ? String(more.textContent).trim() : "",
            raw: raw ? String(raw.textContent) : null,
          };
        });
        return JSON.stringify({ v: {
          found: true,
          header: String((card.querySelector(".tktx") || {}).textContent || "").trim(),
          open: card.classList.contains("open"),
          rows,
          // весь текст карточки — для скана «голого JSON/селекторов в ленте»
          allText: String(card.textContent || ""),
        } });
      }

      // раскрыть карточку (клик по заголовку), если свёрнута
      if (data.op === "openCard") {
        const card = d.querySelector(".m.task");
        if (!card) return JSON.stringify({ err: "no-card" });
        if (!card.classList.contains("open")) card.querySelector(".tkhd").click();
        return JSON.stringify({ v: "opened" });
      }

      // клик «подробнее»/«скрыть» на ряду с префиксом "N. "
      if (data.op === "clickMore") {
        const want = String(data.arg) + ". ";
        const row = [...d.querySelectorAll(".m.task .tksteps .srow")]
          .find((r) => String((r.querySelector(".tx") || {}).textContent || "").trim().startsWith(want));
        if (!row) return JSON.stringify({ err: "no-row:" + data.arg });
        const b = row.querySelector("button.smore");
        if (!b) return JSON.stringify({ err: "no-more:" + data.arg });
        b.click();
        return JSON.stringify({ v: "clicked:" + String(b.textContent).trim() });
      }

      if (data.op === "storageGet") {
        const k = String(data.arg);
        return pw.browser.storage.local.get(k).then(
          (got) => JSON.stringify({ v: got && Object.prototype.hasOwnProperty.call(got, k) ? got[k] : null }),
          (e) => JSON.stringify({ err: "st:" + String(e && e.message || e).slice(0, 140) }));
      }

      if (data.op === "storageClean") {
        return pw.browser.storage.local.remove(["sk23Chat"]).then(
          () => JSON.stringify({ v: "clean" }),
          (e) => JSON.stringify({ err: "stc:" + String(e && e.message || e).slice(0, 140) }));
      }

      if (data.op === "seedToken") {
        const inp = d.getElementById("tok"), btn = d.getElementById("save");
        if (!inp || !btn) return JSON.stringify({ err: "no-fields" });
        inp.value = String(data.arg);
        btn.click();
        return JSON.stringify({ v: "seeded" });
      }

      return JSON.stringify({ err: "unknown-op:" + data.op });
    } catch (e) {
      return JSON.stringify({ err: "actor-err:" + String(e && e.message || e).slice(0, 160) });
    }
  }
}
