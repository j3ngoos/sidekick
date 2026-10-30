// SK-41: window actor — чтение страницы и инъекция script-элемента.
// Паттерн sk36/sk39 (FF158: CPOW удалены, Marionette content-контекст не следует
// за сменой вкладки из chrome-кода — actor.browsingContext всегда «этот» фрейм).
// Ops возвращают JSON-строку {v:...} | {err:...}.
export class Sk41ProbeParent extends JSWindowActorParent {}
export class Sk41ProbeChild extends JSWindowActorChild {
  receiveMessage({ name, data }) {
    if (name !== "sk41:op") return undefined;
    const win = this.contentWindow;
    if (!win) return JSON.stringify({ err: "no-contentWindow" });
    const d = win.document;
    try {
      if (data.op === "ready") return JSON.stringify({ v: String(d.readyState) });
      if (data.op === "href") return JSON.stringify({ v: String(d.location.href).slice(0, 120) });

      if (data.op === "info") {
        const txt = (d.body && d.body.innerText) || "";
        return JSON.stringify({ v: { title: String(d.title || ""), href: String(d.location.href),
                                     n: txt.length, txt: txt.slice(0, 20000) } });
      }

      if (data.op === "inject") { // script-элемент = page-world исполнение (CSP применяется)
        const s = d.createElement("script");
        s.textContent = String(data.arg);
        (d.head || d.documentElement).appendChild(s);
        s.remove();
        return JSON.stringify({ v: "injected" });
      }

      return JSON.stringify({ err: "unknown-op:" + data.op });
    } catch (e) {
      return JSON.stringify({ err: String(e && e.message || e).slice(0, 160) });
    }
  }
}
