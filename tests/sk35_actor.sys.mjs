// SK-35 живая приёмка: window actor для состояний сайдбара/группы (Fission).
// Паттерн — sk37_actor.sys.mjs. Ops возвращают JSON-строку {v:...} | {err:...}.
export class Sk35ProbeParent extends JSWindowActorParent {}
export class Sk35ProbeChild extends JSWindowActorChild {
  receiveMessage({ name, data }) {
    if (name !== "sk35:op") return undefined;
    const win = this.contentWindow;
    if (!win) return JSON.stringify({ err: "no-contentWindow" });
    // У webext-панелей сайдбара this.contentDocument — null (прецедент sk37:
    // документ берут через win.document, а page-API — через waiveXrays)
    const d = win.document;
    const pw = Cu.waiveXrays(win);
    try {
      if (data.op === "href") return JSON.stringify({ v: String(d.location.href).slice(0, 90) });

      if (data.op === "groupState") {
        // заголовок группы агента (снимать префиксы НЕ здесь — харнесс сверяет буквально)
        return pw.browser.tabGroups.query({}).then((gs) => {
          const g = gs.find((x) => String(x.title || "").includes("Sidekick"));
          const v = g ? { found: true, title: String(g.title), collapsed: !!g.collapsed, id: g.id }
                      : { found: false };
          return JSON.stringify({ v });
        }, (e) => JSON.stringify({ err: "tg:" + String(e && e.message || e).slice(0, 120) }));
      }

      if (data.op === "bellDom") {
        const btn = d.getElementById("bellBtn"), n = d.getElementById("belln"),
              panel = d.getElementById("bellpanel"), rows = d.getElementById("bellrows"),
              ra = d.getElementById("bellReadAll");
        if (!btn || !panel) return JSON.stringify({ err: "no-bell-ui" });
        const rr = rows ? [...rows.querySelectorAll(".brow")] : [];
        return JSON.stringify({ v: {
          href: String(d.location.href).slice(0, 90),
          bellnHidden: !!n.hidden, bellnText: String(n.textContent || "").trim(),
          panelHidden: !!panel.hidden,
          rows: rr.length, unread: rr.filter((b) => b.classList.contains("unread")).length,
          readAllDisabled: ra ? !!ra.disabled : null,
          texts: rr.map((b) => String(b.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60)),
        } });
      }

      if (data.op === "clickBell") {
        const b = d.getElementById("bellBtn"); if (!b) return JSON.stringify({ err: "no-bellBtn" });
        b.click(); return JSON.stringify({ v: "clicked" });
      }

      if (data.op === "clickCollapse") {
        const b = d.getElementById("collapseBtn"); if (!b) return JSON.stringify({ err: "no-collapseBtn" });
        b.click(); return JSON.stringify({ v: "clicked" });
      }

      if (data.op === "clickRow") {
        const row = d.querySelector('.brow[data-kind="' + String(data.arg) + '"]');
        if (!row) return JSON.stringify({ err: "no-row:" + String(data.arg) });
        row.click(); return JSON.stringify({ v: "clicked:" + String(data.arg) });
      }

      if (data.op === "clickReadAll") {
        const b = d.getElementById("bellReadAll");
        if (!b) return JSON.stringify({ err: "no-bellReadAll" });
        b.click(); return JSON.stringify({ v: "clicked-read-all" });
      }

      if (data.op === "cardFlash") {
        return JSON.stringify({ v: {
          tasks: d.querySelectorAll(".m.task").length,
          flash: d.querySelectorAll(".m.task.flash").length,
        } });
      }

      if (data.op === "clickBtn") {
        const card = d.querySelector(".perm:not(.done)");
        if (!card) return JSON.stringify({ err: "no-card" });
        const b = card.querySelector(".btns button." + String(data.arg));
        if (!b) return JSON.stringify({ err: "no-btn:" + String(data.arg) });
        b.click(); return JSON.stringify({ v: "clicked:" + b.textContent.trim().slice(0, 30) });
      }

      // ---- SK-50: карточка пермиса в чате (счётчик открытых/закрытых) ----
      if (data.op === "permDom") {
        const cards = [...d.querySelectorAll(".perm")];
        return JSON.stringify({ v: {
          cards: cards.length, open: d.querySelectorAll(".perm:not(.done)").length,
          texts: cards.map((c) => String(c.textContent || "").replace(/\s+/g, " ").trim().slice(0, 70)),
        } });
      }

      // ---- SK-50: options «Проверить звонок» — клик + честный статус из DOM ----
      if (data.op === "clickBellTest") {
        const b = d.getElementById("bellTest");
        if (!b) return JSON.stringify({ err: "no-bellTest" });
        b.click(); return JSON.stringify({ v: "clicked" });
      }
      if (data.op === "bellTestSt") {
        const st = d.getElementById("bellSt"), hint = d.getElementById("bellHint");
        if (!st) return JSON.stringify({ err: "no-bellSt" });
        return JSON.stringify({ v: { st: String(st.textContent || "").trim().slice(0, 160),
                                     hintHidden: hint ? !!hint.hidden : null } });
      }

      // диагностика стенда: жив ли ОС-бэкенд уведомлений у этого Firefox (SK-37 паттерн)
      if (data.op === "notifyTest") {
        return pw.browser.notifications.create("sk50-diag-" + Date.now(), {
          type: "basic", title: "SK-50", message: "diag",
          iconUrl: pw.browser.runtime.getURL("icons/icon-96.png"),
        }).then((id) => JSON.stringify({ v: "created:" + id }),
                (e) => JSON.stringify({ v: "rejected:" + String(e && e.message || e).slice(0, 140) }));
      }

      // SK-50: active trigger bellRefresh (probe baseline without bell events)
      if (data.op === "probeRefresh") {
        try { const pr = pw.browser.runtime.sendMessage({ type: "__sk50_probe" });
              if (pr && pr.catch) pr.catch(() => {}); } catch (e) {}
        return JSON.stringify({ v: "ping" });
      }

      // ---- SK-50: эмуляция фокуса окна для X11-стенда ------------------------
      // Замер 26.10: на Xorg :10 xul focus/blur-топики молчат в любом режиме
      // (raw windowfocus / windowactivate / map-focus / click-to-focus) —
      // «GTK native-widget focus disconnect»: Firefox рендерит в нативный
      // X-виджет (1280x1040), WM управляет им, а GTK top-level (10x10) фокуса
      // не видит. windows.onFocusChanged не доживает до расширения. В проде
      // (macOS) Gecko берёт фокус нативно без X11-слоя, а на стенде приёмка
      // подаёт состояние фокуса storage-override-ом в ту же точку чтения —
      // bellWindowFocused(). Production-код override-а не видит (патч только
      // в тест-XPI).
      if (data.op === "setFocus") {
        const v = (data.arg === true || data.arg === "true");
        return pw.browser.storage.local.set({ __sk50_force_focus: v })
          .then(() => JSON.stringify({ v: "focus:" + v }),
                (e) => JSON.stringify({ v: "err:" + String(e && e.message || e).slice(0, 80) }));
      }

      if (data.op === "storageGet") {
        const k = String(data.arg);
        return pw.browser.storage.local.get(k).then(
          (got) => JSON.stringify({ v: got && Object.prototype.hasOwnProperty.call(got, k) ? got[k] : null }),
          (e) => JSON.stringify({ err: "st:" + String(e && e.message || e).slice(0, 140) }));
      }

      if (data.op === "storageClean") {
        try { const pr = pw.browser.runtime.sendMessage({ type: "__sk35_clean" });
              if (pr && pr.catch) pr.catch(() => {}); } catch (e) {}
        return pw.browser.storage.local.remove(
          ["sk35BellLog", "__sk35_bell", "__sk50_fe", "__sk50_force_focus",
           "__sk50dbg", "sk23Chat"]).then(
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
