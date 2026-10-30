// SK-72 (переделка SK-62) живая приёмка MCP-адаптера: window actor для стенда :8797.
// Паттерн — sk35_actor.sys.mjs (permDom/clickBtn/seedToken) + sk36_actor.sys.mjs (scanSteps).
// Ops возвращают JSON-строку {v:...} | {err:...}.
export class Sk62ProbeParent extends JSWindowActorParent {}
export class Sk62ProbeChild extends JSWindowActorChild {
  receiveMessage({ name, data }) {
    if (name !== "sk62:op") return undefined;
    const d = this.contentWindow && this.contentWindow.document;
    if (!d) return JSON.stringify({ err: "no-document" });
    const pw = Cu.waiveXrays(this.contentWindow);
    try {
      if (data.op === "href") return JSON.stringify({ v: String(d.location.href).slice(0, 120) });

      // карточка пермиса в чате: счётчик открытых + тексты
      if (data.op === "permDom") {
        const cards = [...d.querySelectorAll(".perm")];
        return JSON.stringify({ v: {
          cards: cards.length, open: d.querySelectorAll(".perm:not(.done)").length,
          texts: cards.map((c) => String(c.textContent || "").replace(/\s+/g, " ").trim().slice(0, 90)),
        } });
      }

      // клик по кнопке открытой карточки пермиса: arg = класс (ok/alw/no)
      if (data.op === "clickBtn") {
        const card = d.querySelector(".perm:not(.done)");
        if (!card) return JSON.stringify({ err: "no-card" });
        const b = card.querySelector(".btns button." + String(data.arg));
        if (!b) return JSON.stringify({ err: "no-btn:" + String(data.arg) });
        b.click(); return JSON.stringify({ v: "clicked:" + b.textContent.trim().slice(0, 40) });
      }

      // значение поля на странице агента (доказательство исполнения action_do вживую)
      if (data.op === "pageVal") {
        const el = d.querySelector(String(data.arg));
        if (!el) return JSON.stringify({ err: "no-el:" + String(data.arg) });
        return JSON.stringify({ v: String(el.value === undefined ? el.textContent : el.value) });
      }

      // текст всей панели (доказательство кадров ленты в человекочитом канале)
      if (data.op === "bodyHas") {
        const t = String(d.body ? d.body.innerText : "");
        return JSON.stringify({ v: t.includes(String(data.arg)) });
      }

      // открыть список истории чатов (кнопка 🕘)
      if (data.op === "histOpen") {
        const b = d.getElementById("histBtn");
        if (!b) return JSON.stringify({ err: "no-histBtn" });
        b.click(); return JSON.stringify({ v: "clicked" });
      }

      // кликнуть по строке истории, содержащей arg (переключение чата)
      if (data.op === "histPick") {
        const rows = [...d.querySelectorAll("#histlist .hrow, #histlist > *")];
        const row = rows.find((r) => String(r.textContent || "").includes(String(data.arg)));
        if (!row) return JSON.stringify({ err: "no-row:" + String(data.arg),
                                          rows: rows.map((r) => String(r.textContent || "").replace(/\s+/g, " ").slice(0, 60)).slice(0, 8) });
        row.click(); return JSON.stringify({ v: "picked" });
      }

      // карточка задачи в активной ленте: класс (done!), заголовок, число шагов
      if (data.op === "taskCard") {
        const card = d.querySelector(".m.task");
        if (!card) return JSON.stringify({ v: { found: false } });
        const rows = [...card.querySelectorAll(".tksteps .srow")].map((r) =>
          String((r.querySelector(".tx") || {}).textContent || "").trim());
        return JSON.stringify({ v: { found: true, cls: String(card.className),
          header: String((card.querySelector(".tktx") || {}).textContent || "").trim(),
          steps: rows } });
      }

      // журнал колокола: открыть (клик bellBtn) и вернуть текст строк
      if (data.op === "bellRows") {
        const b = d.getElementById("bellBtn");
        if (b && d.querySelector("#bellpanel[hidden]")) b.click();
        const rows = [...d.querySelectorAll("#bellrows .brow, #bellrows > *")];
        return JSON.stringify({ v: rows.map((r) => String(r.textContent || "").replace(/\s+/g, " ").trim().slice(0, 120)) });
      }

      if (data.op === "seedToken") {
        const inp = d.getElementById("tok"), btn = d.getElementById("save");
        if (!inp || !btn) return JSON.stringify({ err: "no-fields" });
        inp.value = String(data.arg);
        btn.click();
        return JSON.stringify({ v: "seeded" });
      }

      if (data.op === "storageGet") {
        const k = String(data.arg);
        return pw.browser.storage.local.get(k).then(
          (got) => JSON.stringify({ v: got && Object.prototype.hasOwnProperty.call(got, k) ? got[k] : null }),
          (e) => JSON.stringify({ err: "st:" + String(e && e.message || e).slice(0, 140) }));
      }

      return JSON.stringify({ err: "unknown-op:" + data.op });
    } catch (e) {
      return JSON.stringify({ err: "actor-err:" + String(e && e.message || e).slice(0, 160) });
    }
  }
}
