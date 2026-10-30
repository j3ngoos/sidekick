// Hermes Sidekick overlay — shadow DOM, isolated world.
(() => {
  if (window.__hermesSidekick) return; window.__hermesSidekick = true;
  // SK-71: content scripts cannot fetch extension URLs — the dictionary comes
  // from the background ("i18n_get"), and "i18n_changed" re-pulls it live.
  I18N.initFromRuntime();
  I18N.watchRuntime();
  // SK-41: оверлей создаётся ЛЕНИВО — на первом ensure() (preview/cursor/toast/
  // marks), а не на каждой загрузке страницы: в пассивном серфинге наших DOM-узлов
  // нет вовсе, а MutationObserver-евристы страницы не могут поймать «появление»
  // хоста — его просто не происходит до первого действия. shadow root — CLOSED:
  // страница не читает начинку оверлея через host.shadowRoot (open-корень был
  // свободной книгой для любого querySelector('#hermes-sidekick-host')).
  let host = null, root = null;
  const TEMPLATE = `
  <style>
    /* SK-16: the overlay mirrors the SK-13 tokens from theme.css. A content script cannot
       link an extension stylesheet (that needs web_accessible_resources — out of scope),
       so the accent scheme is restated here: same names, same values, same
       prefers-color-scheme split. Components below use var() only (SK-13 hygiene). */
    :host,:root{--accent:#7c3aed;--accent-soft:rgba(124,58,237,.12);
      --err:#c11c1c;--err-soft:rgba(193,28,28,.10);--on-accent:#fff;
      --stop:#dc2626;--stop-hot:#b91c1c;--ink-on-solid:#fff;--drop-ring:#fff;--shadow:#00000059;
      --glass-border:rgba(255,255,255,.12);--glass-hi:rgba(255,255,255,.08)}
    @media (prefers-color-scheme: dark){
      :host,:root{--accent:#a78bfa;--accent-soft:rgba(167,139,250,.16);
      --err:#f87171;--err-soft:rgba(248,113,113,.13);--on-accent:#1b1b20}}
    /* SK-39: движение оверлея — только transform/opacity (композитинг, 60 fps).
       Курсор/пузырёк/кольцо едут на translate3d; форма и «дыхание» живут на .curi,
       чтобы transform позиции не конфликтовал с transform формы (стрелка — поворот).
       Пружина подлёта — ease-out-back 300 мс; prefers-reduced-motion → мгновенные
       состояния без движения. */
    .cur{position:fixed;left:0;top:0;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;
      background:var(--accent-soft);box-shadow:0 0 12px var(--accent);
      transform:translate3d(-64px,-64px,0);opacity:0;will-change:transform;
      transition:transform .3s cubic-bezier(.34,1.56,.64,1),opacity .25s ease}
    .cur.on{opacity:1}
    .cur.slide{transition-duration:.24s;transition-timing-function:cubic-bezier(.2,.8,.2,1)} /* цепочка превью: мягче, без overshoot-войн */
    .curi{position:absolute;inset:0}
    .cur.on .curi{animation:sk39breath 3.4s ease-in-out infinite} /* лёгкое «дыхание» в покое */
    .sh{position:absolute}
    .sh-arrow{inset:0;border-radius:50% 50% 50% 0;background:var(--accent);
      border:2px solid var(--drop-ring);transform:rotate(-45deg)} /* указатель: клик */
    .sh-text{display:none;left:50%;top:50%;width:2px;height:18px;margin:-9px 0 0 -1px;
      background:var(--accent);border-radius:1px} /* I-beam: ввод */
    .sh-text::before,.sh-text::after{content:"";position:absolute;left:50%;width:10px;height:2px;
      margin-left:-5px;background:var(--accent);border-radius:1px}
    .sh-text::before{top:-1px}
    .sh-text::after{bottom:-1px}
    .cur.s-text .sh-arrow{display:none}
    .cur.s-text .sh-text{display:block}
    .spin{display:none;position:absolute;inset:-6px;border-radius:50%; /* спиннер: ожидание вердикта */
      border:2px solid var(--accent-soft);border-top-color:var(--accent);animation:sk39spin .8s linear infinite}
    .cur.waiting .spin{display:block}
    @keyframes sk39spin{to{transform:rotate(360deg)}}
    @keyframes sk39breath{50%{transform:scale(1.07)}}
    .lbl{position:fixed;left:0;top:0;margin:-6px 0 0 14px;font:600 11px system-ui;color:var(--on-accent);
      background:var(--accent);padding:2px 7px;border-radius:8px;opacity:0;white-space:nowrap;will-change:transform;
      transition:transform .3s cubic-bezier(.34,1.56,.64,1),opacity .25s ease}
    .lbl.on{opacity:.95}
    .ring{position:fixed;left:0;top:0;border:3px solid var(--accent);background:var(--accent-soft);border-radius:6px;
      opacity:0;pointer-events:none;will-change:transform;
      transition:transform .3s cubic-bezier(.2,.8,.2,1),opacity .3s ease}
    .ring.on{opacity:1}
    .ring.no{border-color:var(--err);background:var(--err-soft)}
    .ring.pt{border-radius:50%} /* SK-24: point preview — a circle at (x,y), not an element box */
    /* SK-39: ripple-кольца клика и мягкая подсветка hover — WAAPI трогает только
       transform/opacity; клик исполняется ДО появления колец (визуальный отчёт, не гейт) */
    .fx{position:fixed;inset:0;pointer-events:none;overflow:hidden}
    .rp{position:fixed;left:0;top:0;width:88px;height:88px;margin:-44px 0 0 -44px;box-sizing:border-box;
      border:3px solid var(--accent);border-radius:50%;opacity:0;pointer-events:none;will-change:transform,opacity}
    .glw{position:fixed;left:0;top:0;border-radius:10px;background:var(--accent-soft);
      box-shadow:0 0 22px 4px var(--accent-soft);opacity:0;pointer-events:none;will-change:opacity}
    @media (prefers-reduced-motion: reduce){
      .cur,.lbl,.pv,.ring{transition:none}
      .cur.on .curi,.spin{animation:none}
    }
    .toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%) translateY(80px);
      font:500 13px system-ui;color:#fff;background:#111827e6;padding:9px 16px;border-radius:12px;
      transition:transform .3s ease;max-width:70vw}
    .toast.on{transform:translateX(-50%) translateY(0)}
    .pv{position:fixed;display:none;align-items:center;gap:8px;font:600 12px system-ui;box-sizing:border-box;
      color:var(--on-accent);background:color-mix(in srgb, var(--accent) 88%, transparent);
      -webkit-backdrop-filter:blur(16px) saturate(1.3);backdrop-filter:blur(16px) saturate(1.3);
      border:1px solid var(--glass-border);
      padding:6px 8px 6px 10px;border-radius:12px;
      box-shadow:0 4px 16px var(--shadow), inset 0 1px 0 var(--glass-hi);pointer-events:auto;max-width:min(320px,85vw);
      will-change:transform;transition:transform .3s cubic-bezier(.2,.8,.2,1),opacity .25s ease}
    .pv.on{display:flex}
    .pv.slide{transition-duration:.24s}
    .pv span{min-width:0;overflow-wrap:anywhere} /* SK-16: long labels wrap (was nowrap) */
    /* SK-37: кнопок на превью-оверлее нет — только кольцо + человеческая подпись.
       Вердикты человека («Разрешить/Отклонить/Стоп») живут ТОЛЬКО в окне чата:
       на курсоре кадр живёт доли секунды, кнопка под курсором — ложная надежда. */
    /* SK-16: the bubble rides the cursor; the arrow points at the target (--ax tracks it) */
    .pv::after{content:"";position:absolute;left:var(--ax,50%);width:0;height:0;
      border:7px solid transparent;transform:translateX(-50%)}
    .pv.below::after{top:-14px;border-bottom-color:color-mix(in srgb, var(--accent) 88%, transparent)}
    .pv.above::after{bottom:-14px;border-top-color:color-mix(in srgb, var(--accent) 88%, transparent)}
    .pv.cancelled{background:color-mix(in srgb, var(--err) 88%, transparent)}
    .pv.cancelled.below::after{border-bottom-color:color-mix(in srgb, var(--err) 88%, transparent)}
    .pv.cancelled.above::after{border-top-color:color-mix(in srgb, var(--err) 88%, transparent)}
    /* SK-26: SoM marks — numbered badges in an isolated layer (pointer-events:none,
       the whole host is already z-max; sk17HitPoint skips it for hit-tests). */
    .som{position:fixed;inset:0;pointer-events:none;display:none}
    .som.on{display:block}
    .somb{position:fixed;display:flex;align-items:center;gap:4px;box-sizing:border-box;
      font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,system-ui;color:#fff;background:#111827e6;
      border:1px solid var(--accent);border-radius:6px;padding:2px 6px 2px 3px;
      pointer-events:none;white-space:nowrap;box-shadow:0 2px 8px var(--shadow)}
    .somb i{font-style:normal;display:inline-flex;align-items:center;justify-content:center;
      min-width:16px;height:16px;padding:0 3px;border-radius:4px;background:var(--accent);
      color:var(--on-accent);font:700 10px/1 ui-monospace,Menlo,monospace}
    .somb span{max-width:18ch;overflow:hidden;text-overflow:ellipsis;opacity:.92}
  </style>
  <div class="cur" id="c"><div class="curi"><i class="sh sh-arrow"></i><i class="sh sh-text"></i><i class="spin"></i></div></div><div class="lbl" id="l"></div>
  <div class="ring" id="r"></div><div class="toast" id="t"></div>
  <div class="pv" id="p"><span id="pt"></span></div>
  <div class="som" id="m"></div><div class="fx" id="f"></div>`;
  const ensure = () => {
    if (root && host && host.isConnected) return root;
    host = document.createElement("div");
    host.id = "hermes-sidekick-host"; // deterministic handle for the acceptance harness
    host.style.cssText = "all:initial;position:fixed;z-index:2147483647;top:0;left:0;width:0;height:0;pointer-events:none";
    root = host.attachShadow({ mode: "closed" });
    root.innerHTML = TEMPLATE;
    (document.documentElement || document).appendChild(host);
    return root;
  };
  const $ = (id) => ensure().getElementById(id);
  let toastTimer;

  // ---- SK-39: слой движения -----------------------------------------------------------
  // Всё ездит на transform/opacity (композитинг), пружина 300 мс; prefers-reduced-motion
  // сводит к мгновенным состояниям. Анимации — ВИЗУАЛЬНЫЙ ОТЧЁТ об уже исполненном
  // действии: ни одна ветка не гейтит клик и ни одна не умеет уронить путь действия
  // (fire-and-forget, каждый обработчик в try/catch).
  const sk39RM = () => { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; } };
  const place = (el, x, y) => { el.style.transform = "translate3d(" + x + "px," + y + "px,0)"; };
  const sk39Rect = (el) => { // центр цели; нет rect (нетрансформируемый/экзотика) — (0,0)
    try { const q = el.getBoundingClientRect(); return [Math.round(q.left + q.width / 2), Math.round(q.top + q.height / 2)]; }
    catch (e) { return [0, 0]; }
  };
  function sk39Shape(kind) { $("c").classList.toggle("s-text", kind === "text"); } // text = I-beam (ввод), иначе указатель
  const sk39Wait = (on) => { $("c").classList.toggle("waiting", !!on); }; // спиннер ожидания вердикта
  function sk39Park(x, y) { // приковать курсор к точке исполнения
    const c = $("c");
    if (previewRelease) { place(c, x, y); return; } // превью держит курсор — только точка
    if (sk39Held === previewSeq) return; // превью уже отпустил курсор — не воскрешать
    place(c, x, y);
    c.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => c.classList.remove("on"), 2500);
  }
  function sk39AccentOf(el) { // акцент из темы страницы: первый хроматический цвет вверх ≤3 уровней
    try {
      const w = el.ownerDocument && el.ownerDocument.defaultView;
      if (!w) return "";
      for (let n = el, i = 0; n && i < 4; n = n.parentElement, i++) {
        const cs = w.getComputedStyle(n);
        for (const v of [cs.color, cs.backgroundColor, cs.borderColor]) {
          const m2 = /rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/.exec(v || "");
          if (!m2) continue;
          const a = m2[4] === undefined ? 1 : +m2[4];
          const r = +m2[1], g = +m2[2], b = +m2[3];
          const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
          if (a > 0.6 && mx >= 60 && mx - mn >= Math.max(28, mx * 0.25)) return "rgb(" + r + "," + g + "," + b + ")";
        }
      }
    } catch (e) { /* чужая страница: остаётся дефолтный акцент оверлея */ }
    return "";
  }
  function sk39Ripple(x, y, color, dbl) { // ripple: scale(.28→1)+opacity(.95→0), 620–900 мс, 2–3 кольца
    try {
      const layer = $("f");
      if (!layer || layer.childElementCount > 24) return; // шквал кликов: визуал сыпется первым
      const n = dbl ? 4 : 3;
      for (let i = 0; i < n; i++) {
        const ring = document.createElement("div");
        ring.className = "rp";
        if (color) ring.style.borderColor = color;
        layer.appendChild(ring);
        const base = "translate3d(" + x + "px," + y + "px,0)";
        if (sk39RM()) { // мгновенное состояние без движения
          ring.style.transform = base + " scale(.7)";
          ring.style.opacity = ".55";
          setTimeout(() => ring.remove(), 200);
          continue;
        }
        const a = ring.animate(
          [{ transform: base + " scale(.28)", opacity: 0.95 },
           { transform: base + " scale(1)", opacity: 0 }],
          { duration: 620 + i * 130, delay: i * 90, easing: "cubic-bezier(.15,.7,.3,1)", fill: "both" });
        a.finished.then(() => ring.remove(), () => ring.remove());
      }
    } catch (e) { /* визуал не имеет права сломать действие */ }
  }
  function sk39Recoil(el) { // «отдача» scale(.98); composite:add не воюет со своими transform-ами страницы
    if (!el || sk39RM()) return;
    try {
      const tag = String(el.tagName || "").toUpperCase();
      if (tag === "BODY" || tag === "HTML" || !el.animate) return;
      const w = el.ownerDocument && el.ownerDocument.defaultView;
      if (!w || w.getComputedStyle(el).display === "inline") return; // non-replaced inline не трансформируется
      const opt = { duration: 140, easing: "cubic-bezier(.3,1.2,.4,1)", direction: "alternate", iterations: 2 };
      try { el.animate([{ transform: "scale(0.98)" }], Object.assign({ composite: "add" }, opt)); }
      catch (e2) { el.animate([{ transform: "scale(0.98)" }, { transform: "scale(1)" }], opt); }
    } catch (e) { /* страница хозяйка своих transform */ }
  }
  function sk39Glow(el) { // hover: мягкая подсветка цели (только opacity)
    try {
      if (!el || !el.getBoundingClientRect) return;
      const b = el.getBoundingClientRect();
      if (!b.width || !b.height) return;
      const layer = $("f");
      if (!layer) return;
      const g = document.createElement("div");
      g.className = "glw";
      g.style.left = b.left - 4 + "px";
      g.style.top = b.top - 4 + "px";
      g.style.width = b.width + 8 + "px";
      g.style.height = b.height + 8 + "px";
      layer.appendChild(g);
      if (sk39RM()) { g.style.opacity = ".9"; setTimeout(() => g.remove(), 300); return; }
      const a = g.animate([{ opacity: 0 }, { opacity: 0.9, offset: 0.3 }, { opacity: 0 }],
        { duration: 900, easing: "ease-out", fill: "both" });
      a.finished.then(() => g.remove(), () => g.remove());
    } catch (e) { /* визуал */ }
  }
  function sk39Fx(kind, x, y, el) {
    // Вызывается ПОСЛЕ исполнения действия (или для hover) — никогда до.
    // x==null — центр цели (считаем здесь: исполнитель из node-юнита знает только sk39Fx).
    try {
      if (x == null && el) { const q = sk39Rect(el); x = q[0]; y = q[1]; }
      if (kind === "hover" || kind === "move") { if (el) sk39Glow(el); return; }
      if (kind === "input") { sk39Shape("text"); sk39Park(x, y); return; }
      sk39Shape("arrow");
      sk39Ripple(x, y, el ? sk39AccentOf(el) : "", kind === "dblclick");
      if (el) sk39Recoil(el);
      sk39Park(x, y);
    } catch (e) { /* визуал */ }
  }

  // ---- SK-03: page context (page.text / page.dom) — built per request, nothing cached,
  // nothing sent until the bridge explicitly asks (SPA re-renders => fresh selectors). ----
  const MAX_TEXT = 20000, MAX_ELS = 800; // SK-27: 400 резал порталы/диалоги (аудит 21.10)
  // SK-42 п.1: пустой contenteditable — это тоже true (HTML-атрибут без значения),
  // поэтому [contenteditable="true"] не годился: композер Telegram Web — пустой
  // атрибут (инцидент A: page.dom не показывал поле, агент бился вслепую).
  const INTERACTIVE_SEL = 'a[href],button,input,select,textarea,summary,[contenteditable]:not([contenteditable="false"]),[onclick],[tabindex],[role]';
  const ROLES_INTERACTIVE = new Set(["button", "link", "checkbox", "radio", "textbox", "combobox",
    "listbox", "menuitem", "menuitemcheckbox", "menuitemradio", "searchbox", "switch", "tab",
    "slider", "spinbutton", "option", "menu"]);
  const IMPLICIT_ROLE = {
    A: (el) => (el.href ? "link" : null),
    BUTTON: () => "button",
    SELECT: (el) => (el.multiple || el.size > 1 ? "listbox" : "combobox"),
    TEXTAREA: () => "textbox",
    SUMMARY: () => "button",
    IFRAME: () => "iframe",
    INPUT: (el) => ({ checkbox: "checkbox", radio: "radio", range: "slider", number: "spinbutton",
      submit: "button", reset: "button", button: "button", image: "button", file: "button",
      hidden: null }[el.type || "text"] || "textbox"),
  };
  // SK-27: flat/cssPath/nameOf переехали в SK-27-BEGIN helpers (node-юнит вытаскивает
  // блок целиком и гоняет извлечение имён / полноту путей / детерминированность sid).
  function roleOf(el) {
    const r = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
    if (r) return ROLES_INTERACTIVE.has(r) ? r : null;
    const f = IMPLICIT_ROLE[el.tagName];
    const got = f ? f(el) : null;
    if (got) return got;
    if (el.isContentEditable) return "textbox";
    if (el.hasAttribute("onclick") || (el.tabIndex >= 0 && el.tagName !== "IFRAME")) return "generic";
    return null;
  }
  // ---- SK-27: helpers — имена, полные css-пути, стабильные id --------------------------
  // (tests/test_speed_sk27.py вытаскивает этот блок verbatim в node-юнит; правки — с ним в
  // паре. Функции-объявления и const видны всему IIFE: вызовы случаются после его исполнения.)
  // SK-27-BEGIN helpers
  const flat = (n, cap = 80) => {
    if (!n) return "";
    // SK-27: innerText — РЕНДЕРЕННЫЙ текст (в браузере он не видит <title> внутри svg и
    // скрытые узлы): svg-иконка без надписи остаётся безымянной и честно доезжает до
    // svg-fallback/none+hint, а не притворяется nameSrc:"text". undefined innerText —
    // не-DOM мок (node-юнит) → textContent.
    const raw = n.innerText !== undefined && n.innerText !== null ? n.innerText : (n.textContent || "");
    const t = String(raw).replace(/\s+/g, " ").trim();
    return t.length > cap ? t.slice(0, cap) + "…" : t;
  };
  // Доступное имя элемента + ИСТОЧНИК (спека SK-27 п.1, порядок fallback ровно такой):
  // aria-label → text → title → alt → placeholder → value кнопок → aria-labelledby
  // (разыменовать) → ближайший label/легенда → текст соседнего span в родителе-кнопке →
  // svg <title>/aria-label внутри svg. Пусто — nameSrc:"none" + hint (svg-icon, class-
  // подсказка): агент видит «чем бы это кликнуть», а не гадает вслепую (аудит: 40% пустых).
  function elName(el) {
    const g = (a) => { try { return String((el.getAttribute && el.getAttribute(a)) || ""); } catch (e) { return ""; } };
    let t;
    t = g("aria-label").trim(); if (t) return { name: t, nameSrc: "aria-label" };
    t = flat(el); if (t) return { name: t, nameSrc: "text" };
    t = g("title").trim(); if (t) return { name: t, nameSrc: "title" };
    t = g("alt").trim(); if (t) return { name: t, nameSrc: "alt" };
    t = g("placeholder").trim(); if (t) return { name: t, nameSrc: "placeholder" };
    if (el.tagName === "INPUT" && ["submit", "button", "reset", "image"].includes(el.type)) {
      t = String(el.value || "").trim(); if (t) return { name: t, nameSrc: "value" };
    }
    const lb = g("aria-labelledby").trim();
    if (lb) {
      try {
        const ref = el.ownerDocument && el.ownerDocument.getElementById(lb.split(/\s+/)[0]);
        t = flat(ref); if (t) return { name: t, nameSrc: "aria-labelledby" };
      } catch (e) { /* битая ссылка — идём дальше по цепочке */ }
    }
    try {
      if (el.labels && el.labels.length) { t = flat(el.labels[0]); if (t) return { name: t, nameSrc: "label" }; }
      const lab = el.closest && el.closest("label");
      if (lab) { t = flat(lab); if (t) return { name: t, nameSrc: "label" }; }
      const fs = el.closest && el.closest("fieldset");
      const lg = fs && fs.querySelector && fs.querySelector("legend");
      if (lg) { t = flat(lg); if (t) return { name: t, nameSrc: "legend" }; }
    } catch (e) { /* non-DOM mock */ }
    // соседний span в родителе-кнопке: <button><i role=button/><span>Файл</span></button>
    try {
      const par = el.parentElement;
      const parBtn = par && (par.tagName === "BUTTON" || par.tagName === "A" ||
        /^(button|link|menuitem)$/.test((par.getAttribute && par.getAttribute("role")) || ""));
      if (parBtn && el.tagName !== "BUTTON" && el.tagName !== "A") {
        for (const sib of Array.prototype.slice.call(par.children)) {
          if (sib === el || (sib.contains && sib.contains(el))) continue;
          if (sib.tagName === "SPAN") { t = flat(sib); if (t) return { name: t, nameSrc: "sibling" }; }
        }
      }
    } catch (e) { /* non-DOM mock */ }
    // <title>/aria-label внутри svg — иконочные кнопки без aria (аудит: «не выбрать»)
    try {
      const tag = String(el.tagName || "").toLowerCase();
      const svg = tag === "svg" ? el : (el.querySelector ? el.querySelector("svg") : null);
      if (svg) {
        const st = svg.querySelector && svg.querySelector("title");
        t = st ? flat(st) : "";
        if (t) return { name: t, nameSrc: "svg" };
        t = String((svg.getAttribute && svg.getAttribute("aria-label")) || "").trim();
        if (t) return { name: t, nameSrc: "svg" };
      }
    } catch (e) { /* non-DOM mock */ }
    // имя пустое: hint — чем пахнет цель (svg-иконка, class/name-подсказка), а не пустая строка
    let hint = "";
    try {
      const tag = String(el.tagName || "").toLowerCase();
      const svgish = tag === "svg" || (el.querySelector && el.querySelector("svg"));
      const cls = (typeof el.className === "string" ? el.className : g("class")).trim();
      const first = cls.split(/\s+/).filter(Boolean)[0] || "";
      hint = (svgish ? "svg-icon" : "no-text") +
        (el.name ? " name=" + String(el.name).slice(0, 40) : "") +
        (first ? " class=" + first.slice(0, 40) : "");
    } catch (e) { hint = "no-text"; }
    return { name: "", nameSrc: "none", hint };
  }
  function nameOf(el) { return elName(el).name; }
  function cssPath(el, doc) {
    if (el.id) {
      const sel = "#" + CSS.escape(el.id);
      try { if (doc.querySelectorAll(sel).length === 1) return sel; } catch (e) { /* fall through */ }
    }
    if (el.name && ["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName)) {
      const sel = `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
      try { if (doc.querySelectorAll(sel).length === 1) return sel; } catch (e) { /* fall through */ }
    }
    // SK-27 (баг аудита): прежний обрыв на 8 частях делал пути для вложенных глубже 8
    // уровней НЕуникальными (агент: «селекторы сломаны», 41 no_element против 102 ok).
    // Полный nth-of-type-путь от html уникален по построению — обрыва нет.
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== doc.documentElement) {
      const parent = node.parentElement;
      if (!parent) break;
      const sibs = Array.prototype.filter.call(parent.children, (c) => c.tagName === node.tagName);
      parts.unshift(sibs.length > 1
        ? `${node.tagName.toLowerCase()}:nth-of-type(${sibs.indexOf(node) + 1})`
        : node.tagName.toLowerCase());
      node = parent;
    }
    parts.unshift("html");
    return parts.join(">");
  }
  // sid — детерминированный короткий id элемента: FNV-1a от (путь|tag|имя). Один и тот же
  // элемент в повторных page.dom получает тот же sid (агент не путается между снапшотами);
  // навигация/перестройка DOM — резолвер честно отвечает sid_stale (реестр в content).
  function sk27Sid(path, tag, name) {
    const s = path + "|" + tag + "|" + String(name || "").replace(/\s+/g, " ").trim(); // нормализация ЕДИНАЯ: page.dom и SoM-карта считают один и тот же sid
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    return h.toString(36).padStart(8, "0").slice(-8); // 8 символов base36: коллизии ~1e-7 на 1000 элементов
  }
  // SK-27-END helpers
  // SK-04 sensitive-field heuristics (card: input[type=password|card], autocomplete,
  // bank domains). Everything here is DETECTION ONLY — the bridge decides, the human answers.
  const SENS_NAME_RE = /cc[-_]?|card|cvv|cvc|credit|debit|iban|pin\b|платеж|платёж|карт|счёт|счет/i;
  const SENS_AUTOCOMPLETE_RE = /^(cc|address-|one-time-code|new-password|current-password|password)/i;
  // Bank/payment/auth hosts — page-level marker so the agent knows to pass action:"payment";
  // the bridge re-checks its own list in perm.check regardless (defense in depth).
  const SENSITIVE_HOSTS = ["sberbank.ru", "sbolbank.ru", "vtb.ru", "alfabank.ru", "tinkoff.ru",
    "tbank.ru", "tpbank.ru", "gazprombank.ru", "bankpsb.ru", "psb.ru", "rsbi.ru", "mtsbank.ru",
    "akbars.ru", "avangabank.ru", "sovcombank.ru", "pochtabank.ru", "ubrr.ru", "paypal.com",
    "stripe.com", "yoomoney.ru", "gosuslugi.ru", "nalog.ru", "accounts.google.com",
    "appleid.apple.com", "account.live.com", "login.microsoftonline.com"];
  const AUTH_PATH_RE = /(oauth|authorize|signin|sign-in|signup|sign-up|login|log-in|password|2fa|mfa|otp|payment|checkout|billing)/i;
  function isSensitive(el, name) {
    if (el.type === "password") return true;
    const hay = name + " " + (el.name || "") + " " + (el.id || "") + " " +
      (el.placeholder || "") + " " + (el.getAttribute("aria-label") || "");
    if (SENS_NAME_RE.test(hay)) return true;
    if (SENS_AUTOCOMPLETE_RE.test((el.getAttribute("autocomplete") || "").trim())) return true;
    // numeric keypad + card-ish label = almost certainly a card/CVC field
    if (el.inputMode === "numeric" && /cvv|cvc|card|карт|номер|iban/i.test(hay)) return true;
    return false;
  }
  function sensitiveSite() {
    const host = location.hostname.toLowerCase();
    if (SENSITIVE_HOSTS.some((k) => host === k || host.endsWith("." + k))) return "bank_or_auth_host";
    if (AUTH_PATH_RE.test(location.pathname)) return "auth_or_payment_path";
    return null;
  }
  function isVisible(el, win) {
    const cs = win.getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || cs.visibility === "collapse") return false;
    const b = el.getBoundingClientRect();
    return b.width > 0 && b.height > 0;
  }
  // SK-27: cssPath переехал в helpers-блок выше (уникальность без обрыва на 8 частях).
  // ---- SK-27: page.dom v2 — приоритет отдачи, more, near, sid, hit-target, nameSrc ------
  // Снапшот собирается заново на каждый запрос (SK-03), но реестр sid живёт между ними:
  // action.do {sid} резолвится здесь же, где элемент получил свой id. Навигация убивает
  // и реестр (новый content-экземпляр), и url-сверку резолвера; SPA-перестройка — только
  // isConnected/url: честный sid_stale вместо слепого no_element.
  const sidReg = new Map(); // sid -> {el, url, name} — элементы ПОСЛЕДНЕГО page_dom вкладки
  function sidRegSeed(n) { if (sidReg.size > 6000 + n) sidReg.clear(); }
  // Резолвер action.do/preview {sid}: элемент жив, документ не сменился — целимся;
  // элемент умер/отсоединён или вкладка уплыла с url — честный sid_stale (агент обязан
  // переснять page.dom, а не угадывать). Резолвер — единственный владелец реестра.
  function sk27ResolveSid(sid, doc) {
    const rec = sidReg.get(String(sid || ""));
    if (!rec) return { err: "sid_stale" };
    let alive = false;
    try { alive = rec.el && rec.el.isConnected === true; } catch (e) { alive = false; }
    if (!alive || rec.url !== location.href) return { err: "sid_stale" };
    return { el: rec.el, name: rec.name };
  }
  function sk27FrameOffset(el) {
    // начало документа el в верхнем вьюпорте (0,0 для топ-документа); cross-origin — как есть
    let dx = 0, dy = 0;
    try {
      let win = el.ownerDocument && el.ownerDocument.defaultView, guard = 0;
      while (win && win !== window && guard++ < 10) {
        const fe = win.frameElement;
        if (!fe) break;
        const b = fe.getBoundingClientRect();
        dx += b.left; dy += b.top;
        win = fe.ownerDocument && fe.ownerDocument.defaultView;
      }
    } catch (e) { /* cross-origin frame: offset неизвестен — кольцо сядет близко к цели */ }
    return { dx, dy };
  }
  function sk27InOverlay(el, win) {
    // открытый диалог/оверлей/портал (аудит: кнопка «Удалить» Ozon жила в portal в конце
    // DOM и уезжала за лимит): role=dialog/alertdialog/aria-modal/<dialog> — быстрый путь;
    // портал без ролей — fixed-предок с высоким z-index (React-порталы именно такие).
    try {
      if (el.closest && el.closest('[role="dialog"],[role="alertdialog"],[aria-modal="true"],dialog')) return true;
      for (let n = el, d = 0; n && n.nodeType === 1 && d < 6; n = n.parentElement, d++) {
        const cs = win.getComputedStyle(n);
        if (cs.position === "fixed") {
          const z = parseInt(cs.zIndex, 10);
          if (Number.isFinite(z) && z >= 100) return true;
        }
      }
    } catch (e) { /* non-DOM mock */ }
    return false;
  }
  function sk27Rect(b) {
    return { x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width), h: Math.round(b.height) };
  }
  // SK-48-BEGIN snapshot (tests/test_media_sk48.py extracts this block for the node unit)
  function sk48CheckedOf(el) {
    // SK-48: состояние чекбокса/тумблера прямо в снапшоте — пост-чек silent-noop клика
    // (Ozon «Выбрать все»: executed:true, а галочки нет) и handoff expect {kind:"dom"}
    // читают его, не поднимая vision. undefined — цель не тумблер (ключ просто не едет).
    // Паритет со sk48ToggleState (post_state в action.do): нативный контрол, label→control
    // (клик по label в Ozon), aria-роли и aria-pressed/checked/expanded.
    try {
      const tag = String(el.tagName || "").toUpperCase();
      if (tag === "INPUT" && (el.type === "checkbox" || el.type === "radio")) return !!el.checked;
      const g = (n, a) => { try { return n.getAttribute ? n.getAttribute(a) : null; } catch (e) { return null; } };
      const tri = (v) => (v === "true" ? true : v === "false" ? false : v === "mixed" ? "mixed" : null);
      let lab = null;
      if (tag === "LABEL") lab = el;
      else if (el.closest) lab = el.closest("label");
      if (lab && lab.control && (lab.control.type === "checkbox" || lab.control.type === "radio"))
        return !!lab.control.checked;
      const host = (el.closest && el.closest('[role="checkbox"],[role="switch"],' +
        '[role="menuitemcheckbox"],[role="menuitemradio"],[aria-pressed],[aria-checked],[aria-expanded]')) || el;
      const role = (g(host, "role") || "").trim().split(/\s+/)[0];
      if (["checkbox", "switch", "menuitemcheckbox", "menuitemradio"].includes(role)) {
        const a = tri(g(host, "aria-checked"));
        return a === null ? false : a;
      }
      for (const attr of ["aria-pressed", "aria-checked", "aria-expanded"]) {
        const v = g(host, attr);
        if (v != null) return tri(v) === null ? false : tri(v);
      }
    } catch (e) { /* non-DOM mock */ }
    return undefined;
  }
  // SK-48-END snapshot
  function sk27HitTarget(el, doc, b) {
    // чекбокс 1×1 (Ozon «Выбрать все»): кликабелен label — отдаём его как hit-target,
    // агент бьёт координатой/селектором по нему, а не мажет по пиксельному input
    if (b.width >= 2 && b.height >= 2) return null;
    try {
      const lab = (el.labels && el.labels[0]) || (el.closest && el.closest("label"));
      if (!lab) return null;
      const lb = lab.getBoundingClientRect();
      if (lb.width < 2 || lb.height < 2) return null;
      return { selector: cssPath(lab, doc), rect: sk27Rect(lb) };
    } catch (e) { return null; }
  }
  // region: {x0,y0,x1,y1} в координатах ВЕРХНЕГО вьюпорта или null; dx/dy — сдвиг фрейма.
  function sk27Collect(doc, win, frameSel, cand, region, dx, dy) {
    for (const el of doc.querySelectorAll(INTERACTIVE_SEL)) {
      const role = roleOf(el);
      if (!role) continue;
      // аудит 21.10: shadow-host собственного оверлея не должен попадать в снапшот
      if (el.id === SK17_HOST_ID || (el.closest && el.closest("#" + SK17_HOST_ID))) continue;
      const b = el.getBoundingClientRect();
      const tx = b.left + dx, ty = b.top + dy; // rect в координатах верхнего вьюпорта
      if (region && !(tx < region.x1 && tx + b.width > region.x0 &&
                      ty < region.y1 && ty + b.height > region.y0)) continue;
      const nm = elName(el);
      const sel = cssPath(el, doc);
      const vis = isVisible(el, win);
      const inView = tx + b.width > 0 && tx < innerWidth && ty + b.height > 0 && ty < innerHeight;
      // приоритет отдачи (спека п.2): живой оверлей > вьюпорт > остальное; НЕВИДИМОЕ —
      // в самый хвост (скрытый закрытый диалог не должен занимать первые строки снапшота)
      const prio = !vis ? 3 : sk27InOverlay(el, win) ? 0 : inView ? 1 : 2;
      const dist = region ? Math.hypot(tx + b.width / 2 - (region.x0 + region.x1) / 2,
                                       ty + b.height / 2 - (region.y0 + region.y1) / 2) : 0;
      cand.push({ el, sel, frame: frameSel, prio, dist,
        entry: { role, name: nm.name, nameSrc: nm.nameSrc,
          ...(nm.nameSrc === "none" ? { hint: nm.hint } : {}),
          selector: sel, sid: sk27Sid(sel, String(el.tagName).toLowerCase(), nm.name),
          frame: frameSel, visible: vis, rect: sk27Rect(b),
          sensitive: isSensitive(el, nm.name),
          checked: sk48CheckedOf(el),  // SK-48: undefined → ключ выпадает на JSON-сериа-
          // лизации, тумблеры несут checked:true|false|"mixed" — пост-чек без vision
          ...((ht) => (ht ? { hitTarget: ht } : {}))(sk27HitTarget(el, doc, b)) } });
    }
    for (const f of doc.querySelectorAll("iframe")) {
      if (f.id === SK17_HOST_ID || (f.closest && f.closest("#" + SK17_HOST_ID))) continue;
      let sub = null;
      try { sub = f.contentDocument; } catch (e) { sub = null; } // cross-origin: null/throw
      const fb = f.getBoundingClientRect();
      const fsel = frameSel ? frameSel + " > " + cssPath(f, doc) : cssPath(f, doc);
      if (sub && sub.documentElement) {
        sk27Collect(sub, sub.defaultView, fsel, cand, region, dx + (fb.left || 0), dy + (fb.top || 0));
      } else {
        const tx = fb.left + dx, ty = fb.top + dy;
        if (region && !(tx < region.x1 && tx + fb.width > region.x0 &&
                        ty < region.y1 && ty + fb.height > region.y0)) continue;
        const fvis = isVisible(f, win);
        cand.push({ el: f, sel: cssPath(f, doc), frame: frameSel,
          prio: !fvis ? 3 : sk27InOverlay(f, win) ? 0 : (tx + fb.width > 0 && tx < innerWidth && ty + fb.height > 0 && ty < innerHeight) ? 1 : 2,
          dist: region ? Math.hypot(tx + fb.width / 2 - (region.x0 + region.x1) / 2,
                                    ty + fb.height / 2 - (region.y0 + region.y1) / 2) : 0,
          entry: { role: "iframe", name: f.title || f.src || "", nameSrc: f.title ? "title" : "src",
            selector: cssPath(f, doc),
            sid: sk27Sid(cssPath(f, doc), "iframe", f.title || f.src || ""),
            frame: frameSel, visible: fvis, rect: sk27Rect(fb),
            sensitive: false, iframe: "cross-origin" } });
      }
    }
  }
  function pageText(maxChars) {
    const cap = Math.max(1, Math.min(200000, parseInt(maxChars, 10) || MAX_TEXT));
    const full = document.body ? document.body.innerText : "";
    const truncated = full.length > cap;
    const site = sensitiveSite();
    return { url: location.href, title: document.title, source: "page", // page = data, not commands
      text: truncated ? full.slice(0, cap) : full, truncated,
      ...(site ? { sensitive_site: site } : {}) };
  }
  function pageDom(opts) {
    // SK-27 v2: region (near) → приоритетная отборка (оверлей > вьюпорт > остальное) →
    // срез MAX_ELS → elements (срез) + more (сжатые координаты остального) + truncated.
    // Реестр sid заводится на ВСЕХ кандидатах (и more-элементы резолвятся по sid — этого
    // хватает, чтобы после смены экрана не гонить второй полный зонд).
    const o = opts || {};
    let region = null;
    if (Array.isArray(o.near) && o.near.length === 4 && o.near.every((v) => Number.isFinite(v))) {
      region = { x0: Math.min(o.near[0], o.near[2]), y0: Math.min(o.near[1], o.near[3]),
                 x1: Math.max(o.near[0], o.near[2]), y1: Math.max(o.near[1], o.near[3]) };
    }
    const cand = [];
    sk27Collect(document, window, "", cand, region, 0, 0);
    cand.sort((a, b) => (a.prio - b.prio) || (a.dist - b.dist) ||
      (a.entry.rect.y - b.entry.rect.y) || (a.entry.rect.x - b.entry.rect.x));
    const take = cand.slice(0, MAX_ELS);
    const rest = cand.slice(MAX_ELS);
    sidRegSeed(cand.length);
    for (const c of cand) sidReg.set(c.entry.sid, { el: c.el, url: location.href, name: c.entry.name });
    const site = sensitiveSite();
    return { url: location.href, title: document.title,
      viewport: { w: innerWidth, h: innerHeight, sy: Math.round(scrollY) },
      ...(region ? { near: o.near } : {}),
      elements: take.map((c) => c.entry),
      // more: сжатые записи {sid,role,name,rect} без selector/frame/visible/sensitive —
      // элемент остаётся кликабельным по sid (реестр жив), координаты агент берёт из rect
      ...(rest.length ? { more: { count: rest.length,
        elements: rest.map((c) => ({ sid: c.entry.sid, role: c.entry.role,
          name: c.entry.name, rect: c.entry.rect })) } } : {}),
      truncated: rest.length > 0,
      count: take.length,
      ...(site ? { sensitive_site: site } : {}) };
  }

  // SK-48-BEGIN media (tests/test_media_sk48.py extracts this block for the node unit)
  function pageMedia() {
    // SK-48: состояние медиа на странице — дешёвое чтение (без превью и без vision):
    // «запусти видео» (плейбук mute→Play) и handoff expect {kind:"video_playing"}
    // проверяют paused/currentTime, не гадая по кадру. src не отдаём (может нести
    // токены) — только состояние, геометрию и aria-имя.
    const vids = [];
    try {
      for (const v of document.querySelectorAll("video")) {
        const b = v.getBoundingClientRect();
        const nm = (v.getAttribute("aria-label") || v.getAttribute("title") || "").trim();
        vids.push({
          paused: !!v.paused, ended: !!v.ended, muted: !!v.muted,
          volume: Math.round((+v.volume || 0) * 100) / 100,
          currentTime: Math.round((+v.currentTime || 0) * 10) / 10,
          duration: Number.isFinite(+v.duration) ? Math.round(+v.duration * 10) / 10 : null,
          readyState: +v.readyState || 0,
          rect: { x: Math.round(b.left), y: Math.round(b.top),
                  w: Math.round(b.width), h: Math.round(b.height) },
          ...(nm ? { name: nm.slice(0, 80) } : {}),
        });
        if (vids.length >= 8) break; // экран с превью-сеткой: топ-8 достаточно
      }
    } catch (e) { /* отсоединённый документ — честный пустой список */ }
    return { url: location.href, count: vids.length,
             vp: { w: Math.round(window.innerWidth || 0), h: Math.round(window.innerHeight || 0) },
             videos: vids };
  }
  // SK-48-END media

  // ---- SK-26: SoM marks — номерные указатели на интерактивных элементах ----------------
  // page.screenshot {marks:true}: перед снимком рисуем бейджи 1..N у интерактивных
  // элементов вьюпорта, снимок (captureVisibleTab/captureTab) берётся сразу после отрисовки,
  // метки убираются сразу за ним (или по таймауту 2 с — карта при этом ЖИВАЯ: убираем
  // только визуальный слой). Карта номер→селектор живёт в этой замыкании: action.do
  // {mark:N} резолвится через неё; скролл/навигация инвалидируют её → mark_stale (резолвер
  // sk26ResolveMark сверяет url и rect). Метки — изолированный слой pointer-events:none,
  // hit-тесты и так пропускают весь хост (sk17HitPoint) — метки не воруют клики.
  const SOM_MAX = 40, SOM_NAME = 18, SOM_AUTOCLR_MS = 2000;
  let somMap = null;    // {url, ts, marks:[{n, selector, tag, name, rect}]} — карта ПОСЛЕДНЕГО скрина вкладки
  let somTimer = null;

  function somCollect() {
    const out = [];
    const vw = innerWidth, vh = innerHeight;
    for (const el of document.querySelectorAll(INTERACTIVE_SEL)) {
      if (!roleOf(el)) continue; // тот же фильтр, что у page.dom: только реально интерактивные
      if (!isVisible(el, window)) continue;
      const b = el.getBoundingClientRect();
      if (!b.width || !b.height) continue;
      const iw = Math.min(b.right, vw) - Math.max(b.left, 0);
      const ih = Math.min(b.bottom, vh) - Math.max(b.top, 0);
      if (iw <= 0 || ih <= 0) continue; // вне вьюпорта — не маркируем (спека п.1)
      out.push({ el, b, area: iw * ih });
    }
    return out;
  }

  async function somDraw() {
    somClearVisual();
    const vw = innerWidth, vh = innerHeight;
    const diag = Math.hypot(vw, vh) || 1;
    const cand = somCollect();
    // приоритет: площадь × близость к центру (спека п.1); нумерация — reading order
    for (const c of cand) {
      const cx = c.b.left + c.b.width / 2, cy = c.b.top + c.b.height / 2;
      c.score = c.area * (1 / (1 + Math.hypot(cx - vw / 2, cy - vh / 2) / diag));
    }
    cand.sort((a, b) => b.score - a.score);
    const picked = cand.slice(0, SOM_MAX);
    picked.sort((a, b) => (a.b.top - b.b.top) || (a.b.left - b.b.left));
    const box = $("m");
    const marks = [];
    picked.forEach((c, i) => {
      const n = i + 1;
      const full = String(nameOf(c.el) || "").replace(/\s+/g, " ").trim(); // та же цепочка fallback, что у page.dom
      const name = full.slice(0, SOM_NAME);
      const b = c.b;
      const badge = document.createElement("div");
      badge.className = "somb";
      const num = document.createElement("i");
      num.textContent = String(n);
      badge.appendChild(num);
      if (name) { const s = document.createElement("span"); s.textContent = name; badge.appendChild(s); }
      box.appendChild(badge);
      // тёмная плашка у угла, НЕ перекрывая сам элемент: сверху → снизу → сбоку
      const bw = badge.offsetWidth, bh = badge.offsetHeight;
      let left = Math.max(0, Math.min(b.left, vw - bw - 1));
      let top = b.top - bh - 3;
      if (top < 0) top = b.bottom + 3;
      if (top + bh > vh) { // сверху и снизу тесно — к боковому краю
        left = b.right + 3 + bw <= vw ? b.right + 3 : Math.max(1, b.left - bw - 3);
        top = Math.max(0, Math.min(b.top, vh - bh - 1));
      }
      badge.style.left = Math.round(left) + "px";
      badge.style.top = Math.round(Math.max(0, top)) + "px";
      marks.push({ n, selector: cssPath(c.el, document), tag: String(c.el.tagName).toLowerCase(),
                   name, rect: [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)],
                   // SK-27: тот же sid, что у элемента в page.dom (одинаковые входы) — mark-клик
                   // возвращает его в ответе, агент связывает метку скрина со снапшотом.
                   sid: sk27Sid(cssPath(c.el, document), String(c.el.tagName).toLowerCase(), full) });
    });
    box.classList.add("on");
    void box.offsetWidth; // форс-раскладка: метки должны быть посчитаны до кадра
    clearTimeout(somTimer);
    somTimer = setTimeout(somClearVisual, SOM_AUTOCLR_MS); // п.5: страховка, если clear-сообщение потеряется
    somMap = { url: location.href, ts: Date.now(), marks };
    // два rAF — композитор успевает нарисовать метки ДО captureVisibleTab; на ФОНОВОЙ
    // вкладке rAF не дышат (throttling), поэтому гонка с таймером: не вешаем снимок.
    if (typeof requestAnimationFrame === "function") {
      await Promise.race([
        new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))),
        new Promise((r) => setTimeout(r, 150)),
      ]);
    }
    return { marks, url: location.href,
             // SK-47: метрики вьюпорта С МОМЕНТА ОТРИСОВКИ — мост/фон даунскейлят кадр и
             // подписывают scale по ним же; вторым раунд-трипом за метриками не ходят.
             vp: { w: innerWidth, h: innerHeight, sx: Math.round(scrollX), sy: Math.round(scrollY),
                   dpr: devicePixelRatio } };
  }

  function somClearVisual() {
    clearTimeout(somTimer); somTimer = null;
    const box = $("m");
    box.classList.remove("on");
    box.textContent = ""; // карта somMap НЕ трогаем: метки живы до скролла/навигации
  }

  // ---- SK-05: pre-action preview — «кликну X» + Stop, held BEFORE the action. ----
  // The agent calls bridge rpc action.preview {selector, action|label, name, ms?} before
  // every click/input. This window is the human's veto moment: Stop resolves the rpc with
  // cancelled=true and the agent MUST skip the action (verdict semantics: bridge docstring).
  // We only draw, wait and answer — decisions stay on the bridge/human side.
  // SK-16: the preview is ONE overlay — the ghost cursor drives onto the target, the
  // bubble rides the cursor with an arrow at the target, and the ring stays on the target
  // until the action actually executes. Verdict shapes are untouched (contract v1).
  const PREVIEW_DEFAULT_MS = 450, PREVIEW_MAX_MS = 5000;
  const PREVIEW_GAP = 10;           // bubble edge -> target edge; the arrow lives in the gap
  const PREVIEW_HOLD_MAX_MS = 8000; // ring fallback when the action never lands
  const PREVIEW_VERBS = { click: "prev.click", input: "prev.input", submit: "prev.submit", navigate: "prev.navigate",
    rightclick: "prev.rightclick", hover: "prev.hover", dblclick: "prev.dblclick", move: "prev.move", key: "prev.key" }; // SK-24 + SK-71: dictionary keys
  let previewSeq = 0;      // generation guard: a superseded preview must never settle the new one
  let previewPending = null; // {settle} of the in-flight preview promise
  let previewRingOff;
  let previewRelease = null; // {el, onEvent, drop, timer} — the held ring awaiting the real action
  let sk39Held = 0; // SK-39: previewSeq превью, державшего курсор, — после release не парковать заново

  function settlePreview(v) {
    const p = previewPending; previewPending = null;
    if (p) p.settle(v);
  }

  function clearRelease() {
    if (!previewRelease) return;
    const { el, onEvent, drop, timer } = previewRelease;
    if (el) { // SK-24: a point/key preview may hold no element at all — timer-only release
      const doc = el.ownerDocument;
      for (const ev of ["click", "input", "keydown", "submit"]) doc.removeEventListener(ev, onEvent, true);
      doc.removeEventListener("visibilitychange", drop);
    }
    clearTimeout(timer);
    previewRelease = null;
  }

  // SK-16: the ring is held until the action lands. The agent executes over BiDi — the
  // trusted click/typing events that arrive on the target ARE the execution signal, so
  // no bridge contract change is needed. Hidden tab or a silent agent -> fallback timers.
  function armRelease(seq, el, c) {
    const r = $("r");
    const drop = () => {
      if (seq === previewSeq) { r.classList.remove("on", "pt"); c.classList.remove("on", "slide"); }
      clearRelease();
    };
    if (!el) { // SK-24: point/key preview with nothing under the target — the timer drops it
      previewRelease = { el: null, onEvent: null, drop, timer: setTimeout(drop, PREVIEW_HOLD_MAX_MS) };
      return;
    }
    const onEvent = (e) => {
      const t = (e.composedPath && e.composedPath()[0]) || e.target;
      if (t === el || el.contains(t)) return drop(); // landed on the target (or inside it)
      if (t && t.contains && t.contains(el) && Number.isFinite(e.clientX)) {
        // an ancestor was clicked (e.g. the button wrapping the matched span) —
        // counts only when the pointer actually landed inside the target's box
        const b = el.getBoundingClientRect();
        if (e.clientX >= b.left && e.clientX <= b.right && e.clientY >= b.top && e.clientY <= b.bottom) drop();
      }
    };
    const doc = el.ownerDocument;
    for (const ev of ["click", "input", "keydown", "submit"]) doc.addEventListener(ev, onEvent, true);
    doc.addEventListener("visibilitychange", drop);
    previewRelease = { el, onEvent, drop, timer: setTimeout(drop, PREVIEW_HOLD_MAX_MS) };
  }

  // ---- SK-43 п.4: человекочитаемая подпись превью (аудит №2 22.10, проблема 8) --------
  // «кликну «Account menu»», «кликну «empty area»» — человек видит латиничный мусор
  // вместо ответа «куда ты лезешь». Если в подписи нет кириллицы и цель известна,
  // подставляем читаемое из DOM: aria-label → visible text (транслит-фолбэка нет —
  // просто текст как он есть). Источник подписи едет в вердикт label_src: агентская
  // осталась — "agent", подставилась из DOM — "dom" (пишется в ev:preview на мосту).
  // SK-43-BEGIN preview-label (tests/test_perm_semantics_sk43.py вырезает блок в node-юнит)
  const SK43_CYR = /[\u0400-\u04FF]/;
  function sk43DomLabel(el) {
    if (!el) return "";
    const g = (a) => { try { return String((el.getAttribute && el.getAttribute(a)) || "").trim(); } catch (e) { return ""; } };
    const tag = String(el.tagName || "").toUpperCase();
    if (!tag || tag === "BODY" || tag === "HTML") return ""; // фон: весь документ — не подпись
    let t = g("aria-label");
    if (!t) {
      try {
        const raw = el.innerText !== undefined && el.innerText !== null ? el.innerText : (el.textContent || "");
        t = String(raw).replace(/\s+/g, " ").trim();
      } catch (e) { t = ""; }
    }
    if (t.length > 60) t = t.slice(0, 60) + "…";
    return t;
  }
  function sk43EnrichLabel(label, verb, el, opts) {
    const o = opts || {};
    // Кириллицу ищем в ИМЕНИ (opts.name — то, что подставлено в « verb «X» »), а не
    // во всей подписи: глагол-шаблон («кликну») кириллический всегда. key не
    // обогащаем: подпись — сама клавиша («нажму «Enter»» — не мусор); DOM-кандидат
    // без кириллицы не лучше агентского — оставляем агентского (на англоязычном
    // сайте он честный).
    const probe = o.name != null ? String(o.name) : String(label);
    if (o.skip || !el || SK43_CYR.test(probe)) return { label, label_src: "agent" };
    const dom = sk43DomLabel(el);
    if (dom && SK43_CYR.test(dom)) {
      return { label: String(verb + " «" + dom + (o.markN ? ` [${o.markN}]` : "") + "»").slice(0, 120),
               label_src: "dom" };
    }
    return { label, label_src: "agent" };
  }
  // SK-43-END preview-label

  async function preview(m) {
    const wait = Math.max(0, Math.min(PREVIEW_MAX_MS, Number.isFinite(+m.ms) ? +m.ms : PREVIEW_DEFAULT_MS));
    // SK-24: three target modes — selector (SK-05), point (x,y — coordinate actions),
    // key (the focused element). The verdict vocabulary is identical for all three.
    const isPoint = Number.isFinite(+m.x) && Number.isFinite(+m.y);
    const isKey = String(m.action || "").toLowerCase() === "key";
    const act = isKey ? "key" : String(m.action || "").toLowerCase();
    // SK-26: mark — кольцо на элементе из карты последнего SoM-скрина (резолвер тот же,
    // что у исполнителя: устаревшая карта → mark_stale ещё ДО кадра человеку).
    const isMark = !isKey && !isPoint && !m.selector && m.mark != null && Number.isFinite(+m.mark);
    // SK-27: sid — стабильный id элемента из последнего page.dom (реестр резолвера).
    // Цель без selector/mark/координат, но с sid — это sid-режим.
    const isSid = !isKey && !isPoint && !m.selector && (m.mark == null || !Number.isFinite(+m.mark)) &&
      typeof m.sid === "string" && /^[a-z0-9]{4,12}$/i.test(m.sid);
    // Аддендум аудита (в): превью имени/роли резолверит ТОТ ЖЕ sk17ByName, что и
    // исполнитель: неоднозначно → кандидаты с rect ещё до кадра человеку.
    const isName = !isKey && !isPoint && !isMark && !isSid && !m.selector &&
      ["click", "rightclick", "dblclick", "hover"].includes(act) &&
      (m.role != null || (typeof m.name === "string" && m.name.trim()));
    // SK-42 п.1: input без цели — фокус-режим (тот же принцип, что у key: кольцо на
    // activeElement; не-редактируемый фокус — отказ not_editable ДО кадра человеку).
    const isFocusInput = act === "input" && !m.selector && !isPoint && !isMark && !isSid;
    let el = null;
    let markEntry = null;
    // SK-24: frame — превью должно смотреть ТО ТАМ ЖЕ, где исполнитель будет кликать:
    // цепочка фрейма разрешается теми же sk17-хелперами, вердикты те же словарём
    // (bad_selector / no_element / cross_origin_frame). fx/fy: начало фрейма в топ-
    // вьюпорте — на них сдвигается кольцо, ring/cursor живут в топ-документе.
    let vdoc = document, vx = +m.x, vy = +m.y, fx = 0, fy = 0;
    if (!isKey && !isName && (isPoint ? m.frame != null : m.frame != null && m.selector)) {
      const fr = isPoint ? sk17Into(document, +m.x, +m.y, String(m.frame))
                         : sk17FrameDoc(document, String(m.frame));
      if (fr.err) return { shown: false, cancelled: false, reason: fr.err };
      vdoc = fr.doc; fx = fr.dx || 0; fy = fr.dy || 0;
      if (isPoint) { vx = fr.x; vy = fr.y; }
    } else if (isPoint && !m.frame) {
      const fr = sk17Into(document, +m.x, +m.y, null); // авто-спуск как у исполнителя
      if (!fr.err) { vdoc = fr.doc; vx = fr.x; vy = fr.y; fx = fr.dx || 0; fy = fr.dy || 0; }
    }
    if (isMark) {
      const r = sk26ResolveMark(+m.mark, document, somMap);
      if (r.err) return { shown: false, cancelled: false, reason: r.err };
      el = r.el; markEntry = r.entry;
    } else if (isSid) {
      const r = sk27ResolveSid(m.sid, document);
      if (r.err) return { shown: false, cancelled: false, reason: r.err };
      el = r.el;
      // элемент мог жить в фрейме (page.dom его собирал): кольцо в топ-вьюпорте,
      // offset — по цепочке frameElement; скролл для чужого документа не крутим
      vdoc = el.ownerDocument || document;
      const off = sk27FrameOffset(el); fx = off.dx; fy = off.dy;
      m = Object.assign({}, m, { name: m.name || r.name }); // подпись превью — именем из реестра
    } else if (isName) {
      const nr = sk17ByName(document, m.name, m.role);
      if (nr.err) return { shown: false, cancelled: false, reason: nr.err };
      if (nr.ambiguous) return { shown: false, cancelled: false, reason: "ambiguous", candidates: nr.ambiguous };
      el = nr.el;
    } else if (isPoint) {
      el = sk17HitPoint(vdoc, vx, vy); // аддендум (а): бабл превью — не цель
    } else if (isKey || isFocusInput) {
      // SK-42: input без цели кольцуем фокус — ровно как key
      const a = document.activeElement;
      el = (a && a !== document.body && a !== document.documentElement) ? a : null;
    } else {
      try { el = m.selector ? vdoc.querySelector(m.selector) : null; }
      catch (e) { return { shown: false, cancelled: false, reason: "bad_selector" }; }
      if (!el) return { shown: false, cancelled: false, reason: "no_element" };
    }
    // Аддендум (б): body/html/корневой контейнер — кольцо НЕ показываем (не мигать
    // человеку ради промаха); hover/move по фону легитимны.
    if (!isKey && !isName && el && ["click", "rightclick", "dblclick"].includes(act) &&
        sk17NotClickable(vdoc, el))
      return { shown: false, cancelled: false, reason: "not_clickable" };
    // SK-42 п.1/п.4: гейт input — если цель (селектор/sid/mark/span внутри CE), точка
    // или фокус НЕ редактируемы — отказ not_editable ДО кадра: кольцо вокруг не-поля
    // человеку ничего не объясняет, а агент получил бы тот же not_editable на исполнении.
    // contenteditable-хост ищется ВВЕРХ — композер Telegram это [contenteditable] div,
    // а селектор/клик попадают в span внутри него (инцидент A).
    if (act === "input" && !sk42EditableHost(el))
      return { shown: false, cancelled: false, reason: "not_editable" };
    const seq = ++previewSeq; // take ownership BEFORE settling the old one: its timers die here
    settlePreview({ shown: true, cancelled: true, reason: "superseded" }); // old in-flight preview loses
    clearRelease(); // …and so does a held ring from the last verdict
    const mname = m.name || (markEntry ? `${markEntry.name || markEntry.tag} [${markEntry.n}]` : "");
    const label0 = String(m.label || ((L(PREVIEW_VERBS[m.action] || "prev.default")) +
      (mname ? ` «${String(mname).slice(0, 60)}»` : (m.key ? ` «${String(m.key).slice(0, 20)}»` : "")))).slice(0, 120);
    // SK-43 п.4: ASCII-only подпись заменяем читаемой из DOM цели (key — не трогаем:
    // «нажму «Enter»» — подпись клавиши, не ярлык элемента). Кириллицу проверяем по
    // подписывающей части (label/name/key), а не по сборному шаблону с кириллическим глаголом.
    const l43 = sk43EnrichLabel(label0, L(PREVIEW_VERBS[m.action] || "prev.default"),
                                isKey ? null : el,
                                { skip: isKey, markN: markEntry ? markEntry.n : null,
                                  name: m.label || mname || (m.key ? String(m.key) : "") });
    const label = l43.label, label_src = l43.label_src;
    // The ring must sit on the REAL spot: bring the target on screen first, then measure.
    // SK-24: point/key never scroll — viewport coords are the contract, focus is focus.
    // SK-26: mark тоже не скроллим: элемент был во вьюпорте на снимке, а любой скролл
    // всё равно инвалидирует карту (резолвер уже отдал бы mark_stale).
    // Элемент внутри фрейма тоже не скроллим вслепую: локальный rect и чужой вьюпорт.
    if (el && !isPoint && !isKey && !isMark && vdoc === document) {
      const vr = el.getBoundingClientRect();
      if (!vr.width || !vr.height || vr.top < 0 || vr.bottom > innerHeight || vr.left < 0 || vr.right > innerWidth) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        await new Promise((r) => setTimeout(r, 380));
        if (seq !== previewSeq) return { shown: true, cancelled: true, reason: "superseded" };
      }
    }
    const r = $("r"), pv = $("p"), pt = $("pt"), c = $("c"), l = $("l");
    clearTimeout(toastTimer); // a pending cursor-hide must not blank the overlay mid-preview
    l.classList.remove("on"); // one overlay, one voice: the bubble replaces the cursor label

    // SK-24: the anchor box — selector/key: the element rect; point: a 28px circle at
    // (x,y) (.ring.pt); key with <body> focus: no box at all, the bubble sits bottom-centre.
    let b = null;
    if (isPoint) b = { left: +m.x - 14, top: +m.y - 14, width: 28, height: 28, right: +m.x + 14, bottom: +m.y + 14 };
    else if (el) {
      const q = el.getBoundingClientRect();
      // SK-24: fx/fy — элемент в фрейме: его rect локальный, кольцо живёт в топ-вьюпорте
      b = { left: q.left + fx, top: q.top + fy, width: q.width, height: q.height,
            right: q.right + fx, bottom: q.bottom + fy };
    }
    // ring on the target — accent scheme; held until the action executes (SK-16)
    r.classList.toggle("pt", isPoint);
    if (b) {
      r.classList.remove("no");
      place(r, b.left - 4, b.top - 4);
      r.style.width = b.width + 8 + "px"; r.style.height = b.height + 8 + "px";
      r.classList.add("on");
    }

    // Ghost cursor drives onto the target centre; the bubble rides along and flips
    // up/down by free space, clamped by its REAL width (SK-16: no nowrap, no 230px).
    const cx = b ? Math.round(b.left + b.width / 2) : Math.round(innerWidth / 2);
    const cy = b ? Math.round(b.top + b.height / 2) : innerHeight - 60;
    const wasOn = c.classList.contains("on");
    sk39Shape(act === "input" || isKey ? "text" : "arrow"); // SK-39: форма позы ДО подлёта
    pt.textContent = label;
    pv.classList.remove("cancelled", "below", "above");
    pv.classList.add("on");
    const bw = pv.offsetWidth, bh = pv.offsetHeight; // measured at max-width: long labels wrap
    let top, side;
    if (!b) { side = "above"; top = innerHeight - bh - 26; } // key to <body>: bubble only, bottom strip
    else {
      const roomB = innerHeight - b.bottom - PREVIEW_GAP - 4, roomA = b.top - PREVIEW_GAP - 4;
      if (bh <= roomB) { side = "below"; top = b.bottom + PREVIEW_GAP; }
      else if (bh <= roomA) { side = "above"; top = b.top - PREVIEW_GAP - bh; }
      else if (roomB >= roomA) { side = "below"; top = Math.max(4, innerHeight - bh - 4); }
      else { side = "above"; top = 4; } // cramped: the roomier side, pinned to the viewport
    }
    const left = Math.max(4, Math.min(Math.round(cx - bw / 2), innerWidth - bw - 4));
    pv.classList.add(side);
    pv.style.setProperty("--ax", b ? Math.max(12, Math.min(cx - left, bw - 12)) + "px" : "50%"); // arrow at the target
    place(pv, left, top);
    c.classList.toggle("slide", wasOn); pv.classList.toggle("slide", wasOn);
    if (b) {
      if (wasOn) {
        place(c, cx, cy); // both glide over .3s and land together
      } else {
        c.style.transition = pv.style.transition = "none"; // nothing visible to glide from: place, then show
        place(c, cx, cy);
        void pv.offsetWidth;
        c.style.transition = pv.style.transition = "";
      }
      c.classList.add("on");
      sk39Wait(true); // SK-39: спиннер на грейс-период до вердикта/исполнения
    }
    const finish = (v, cancelled) => {
      if (seq !== previewSeq) return; // superseded — my timer/Stop must not touch the newer preview
      sk39Wait(false); // SK-39: вердикт получен — спиннер гаснет, поза живёт до release
      pv.classList.remove("on");
      c.classList.remove("slide"); pv.classList.remove("slide");
      if (cancelled) { // red flash so the human sees their Stop landed
        r.classList.add("no"); pt.textContent = L("cnt.cancelled");
        pv.classList.add("cancelled", "on");
        previewRingOff = setTimeout(() => {
          // SK-16 fix (старый баг): вместе с .cancelled снимаем и .on — иначе
          // «Отменено» навсегда залипало на странице
          if (seq === previewSeq) { r.classList.remove("on", "no", "pt"); pv.classList.remove("cancelled", "on"); c.classList.remove("on"); }
        }, 900);
      } else {
        // SK-16: verdict in, action next — the ring (and cursor) stay on the target until
        // the action actually lands there. The old fixed 1.2 s fade died before the click.
        sk39Held = seq; // SK-39: этот seq превью владел курсором — исполнитель его не паркует
        armRelease(seq, el, c);
      }
      settlePreview(v);
    };
    // SK-37: кнопки «Стоп» на оверлее больше нет — человек останавливает задачу
    // из окна чата (морф-кнопка SK-38) и отвечает на пермисы с персистентной
    // карточки. Вердикт user_stop остаётся в словаре контракта v1 (мост/аудит),
    // но с превью-оверлея он больше не выдаётся.
    setTimeout(() => finish({ shown: true, cancelled: false, held_ms: wait, label, label_src }, false), wait);
    return await new Promise((settle) => { previewPending = { settle }; });
  }

  // ---- SK-17: action executor — click/input/submit in the isolated world. ----
  // This is the EXECUTION leg of the bridge rpc action.do. The preview gate (SK-05)
  // is enforced on the BRIDGE side before this message is ever sent — the agent has
  // no channel to this code that skips the frame + label + ■ Stop conveyor. Events
  // produced here are synthetic (isTrusted=false): anti-bot sites may refuse them —
  // a documented boundary of the clean mode (AGENTS.md п.7), not a bug.
  // SK-17-BEGIN executor (tests/test_action_do.py extracts this block verbatim for the node unit)
  function sk17NativeSet(doc, el, value) {
    // React-compatible: call the PROTOTYPE value setter. React shadows `value` on the
    // INSTANCE with its own tracker descriptor, so a plain `el.value = v` updates the
    // tracker instead of the DOM and onChange never fires.
    const w = doc.defaultView;
    const ctor = { INPUT: w.HTMLInputElement, TEXTAREA: w.HTMLTextAreaElement,
      SELECT: w.HTMLSelectElement }[el.tagName];
    const d = ctor && Object.getOwnPropertyDescriptor(ctor.prototype, "value");
    if (d && d.set) d.set.call(el, value); else el.value = value;
  }
  // ---- SK-24: full pointer path, coordinates, keys, frames ----------------------------
  function sk17Text(el) {
    const t = ((el && (el.innerText || el.textContent)) || "").replace(/\s+/g, " ").trim();
    return t.length > 60 ? t.slice(0, 60) + "…" : t;
  }
  function sk17Hit(el) {
    // п.5: «куда реально попал» — the element that received the events, tag + ≤60 chars.
    return el ? { tag: String(el.tagName || "").toLowerCase(), text: sk17Text(el) } : null;
  }
  function sk17Fire(win, el, type, o, ctors) {
    // Constructor fallback chain (PointerEvent/KeyboardEvent -> MouseEvent -> Event):
    // old engines and the node harness lack the specific ctor — the event still lands.
    for (const name of ctors) {
      const C = name && win[name];
      if (typeof C === "function") { try { el.dispatchEvent(new C(type, o)); return true; } catch (e) { /* next */ } }
    }
    return false;
  }
  function sk17Path(win, el, x, y, kind) {
    // kind: "click" | "rightclick" | "hover". The full pointer path with REAL coords —
    // the ozon pattern: sites that ignore a bare el.click() listen to pointerdown/
    // mousedown. hover = over/enter/move only: drop-menus open on hover, a press there
    // would close them again (card п.1).
    const base = { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y,
      isPrimary: true, pointerId: 1, pointerType: "mouse" };
    const btn = kind === "rightclick" ? 2 : 0;
    const P = (t, e) => sk17Fire(win, el, t, { ...base, ...e }, ["PointerEvent", "Event"]);
    const M = (t, e) => sk17Fire(win, el, t, { ...base, ...e }, ["MouseEvent", "Event"]);
    P("pointerover", { button: -1, buttons: 0 }); M("mouseover", { button: -1, buttons: 0 });
    P("pointerenter", { button: -1, buttons: 0 }); M("mouseenter", { button: -1, buttons: 0 });
    P("pointermove", { button: -1, buttons: btn }); M("mousemove", { button: -1, buttons: btn });
    if (kind === "hover") return;
    P("pointerdown", { button: btn, buttons: btn || 1, detail: 1 });
    M("mousedown", { button: btn, buttons: btn || 1, detail: 1 });
    P("pointerup", { button: btn, buttons: 0, detail: 1 });
    M("mouseup", { button: btn, buttons: 0, detail: 1 });
    if (kind === "rightclick") M("contextmenu", { button: btn, buttons: 0, detail: 1 });
  }
  function sk17FrameDoc(doc, frameSel) {
    // page.dom frame-chain ("iframeSel > innerSel", "" = main doc) -> {doc, dx, dy}
    // (dx/dy: the frame origin inside the TOP viewport) or {err}.
    let cur = doc, dx = 0, dy = 0;
    for (const sel of String(frameSel || "").split(">").map((s) => s.trim()).filter(Boolean)) {
      let f = null;
      try { f = cur.querySelector(sel); } catch (e) { return { err: "bad_selector" }; }
      if (!f) return { err: "no_element" };
      const b = f.getBoundingClientRect();
      dx += b.left || 0; dy += b.top || 0;
      let sub = null;
      try { sub = f.contentDocument; } catch (e) { sub = null; } // cross-origin: null/throw
      if (!sub || !sub.documentElement) return { err: "cross_origin_frame" };
      cur = sub;
    }
    return { doc: cur, dx, dy };
  }
  // Аддендум аудита 21.10 (а): hit-test обязан ПРОПУСКАТЬ собственный оверлей —
  // elementFromPoint возвращает сам shadow-host (.pv-бабл pointer-events:auto;
  // аудит: щуп-клик угодил в собственную кнопку «Стоп», сессия 20261021_104857).
  const SK17_HOST_ID = "hermes-sidekick-host";
  function sk17HitPoint(doc, x, y) {
    let el = null;
    try { el = doc.elementFromPoint(x, y); } catch (e) { return null; }
    let host = null;
    try { host = doc.getElementById(SK17_HOST_ID); } catch (e) { /* isolated world */ }
    if (el && host && (el === host || host.contains(el))) {
      const prev = host.style.display;
      host.style.display = "none"; // на кадр прячем себя — и смотрим, ЧТО ПОД НИМ
      try { el = doc.elementFromPoint(x, y); } finally { host.style.display = prev; }
    }
    return el;
  }
  // Аддендум аудита 21.10 (б): цель body/html/корневой контейнер — это промах, а не
  // успех (аудит: клик по body вернул shown:true — ложный успех). Только для
  // кликабельных глаголов; hover по фону легитимен.
  const SK17_WIDGET = ["CANVAS", "SVG", "IFRAME", "VIDEO", "AUDIO", "OBJECT", "EMBED", "IMG", "PICTURE"];
  const SK17_SHELL = ["DIV", "MAIN", "SECTION", "HEADER", "FOOTER", "ASIDE", "NAV", "FORM", "P", "UL", "OL", "TABLE", "SPAN"];
  function sk17NotClickable(doc, el) {
    if (!el) return false;
    const tag = String(el.tagName || "").toUpperCase();
    if (tag === "BODY" || tag === "HTML") return true;
    if (!SK17_SHELL.includes(tag) || SK17_WIDGET.includes(tag)) return false;
    try {
      if (el.closest && el.closest('a,button,input,select,textarea,summary,label,[onclick],' +
          '[role="button"],[role="link"],[role="checkbox"],[role="tab"],[role="menuitem"],[contenteditable]')) return false;
      const w = doc.defaultView || win;
      const b = el.getBoundingClientRect();
      if (b.width < w.innerWidth * 0.9 || b.height < w.innerHeight * 0.9) return false; // корневой контейнер
      if (w.getComputedStyle(el).cursor === "pointer") return false; // кликабельный по воле автора
    } catch (e) { return false; }
    return true;
  }
  // Аддендум аудита 21.10 (в): клик по имени/роли — резолвер на стороне расширения.
  // Точное (нормализованное) совпадение доступного имени → полный путь; неоднозначно →
  // кандидаты с rect в ответе (агент выберет координатой), не угадываем.
  const SK17_ROLES = {
    button: 'button,[role="button"],[type="button"],[type="submit"],[type="reset"],input[type="image"]',
    link: 'a[href],[role="link"]',
    checkbox: 'input[type="checkbox"],[role="checkbox"]',
    radio: 'input[type="radio"],[role="radio"]',
    textbox: 'textarea,[role="textbox"],[contenteditable]:not([contenteditable="false"]),input:not([type="button"]):not([type="submit"]):not([type="checkbox"]):not([type="radio"]):not([type="image"])',
    searchbox: 'input[type="search"],[role="searchbox"]',
    tab: '[role="tab"]', menuitem: '[role="menuitem"]', option: '[role="option"]',
    switch: '[role="switch"]', combobox: '[role="combobox"],select', listbox: '[role="listbox"]',
  };
  const sk17Norm = (s) => String(s == null ? "" : s).replace(/\s+/g, " ").trim().toLowerCase();
  function sk17AccNames(doc, el) {
    const g = (a) => { try { return el.getAttribute(a) || ""; } catch (e) { return ""; } };
    const parts = [];
    const aria = g("aria-label"); if (aria) parts.push(aria);
    let txt = ""; try { txt = el.innerText || el.textContent || ""; } catch (e) {}
    if (txt) parts.push(txt);
    if (el.value) parts.push(String(el.value));
    ["placeholder", "alt", "title"].forEach((a) => { const v = g(a); if (v) parts.push(v); });
    const lb = g("aria-labelledby");
    if (lb) lb.split(/\s+/).forEach((id) => {
      try { const t = doc.getElementById(id); if (t) parts.push(t.textContent || ""); } catch (e) {}
    });
    return parts;
  }
  function sk17ByName(doc, name, role) {
    const want = sk17Norm(name);
    if (!want) return { err: "bad_request" };
    let nodes = [];
    try {
      const sel = role ? (SK17_ROLES[String(role).toLowerCase()] || `[role="${role}"]`) :
        'a,button,input,select,textarea,summary,label,[role],[onclick],[tabindex]';
      nodes = Array.from(doc.querySelectorAll(sel));
    } catch (e) { return { err: "bad_selector" }; }
    const cands = [];
    for (const el of nodes) {
      if (!sk17AccNames(doc, el).some((p) => sk17Norm(p) === want)) continue;
      let b = null; try { b = el.getBoundingClientRect(); } catch (e) { continue; }
      if (!b.width || !b.height) continue; // невидимого не касаемся
      cands.push(el);
    }
    if (!cands.length) return { err: "no_element" };
    if (cands.length > 1) {
      return { ambiguous: cands.slice(0, 8).map((el) => {
        const b = el.getBoundingClientRect();
        return { tag: String(el.tagName).toLowerCase(), text: sk17Text(el),
                 rect: [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)] };
      }) };
    }
    return { el: cands[0] };
  }
  function sk17Into(doc, x, y, frameSel) {
    // -> {doc, x, y} in frame-local viewport coords, or {err}. An explicit frame chain
    // wins; otherwise auto-descend same-origin iframes under the point: a top-level
    // elementFromPoint over an iframe returns the IFRAME element itself — the click
    // must land INSIDE it, not on the frame box.
    if (frameSel != null) {
      const r = sk17FrameDoc(doc, frameSel);
      if (r.err) return r;
      // SK-24: dx/dy additive — превью на их счёт сдвигает кольцо в топ-вьюпорте
      return { doc: r.doc, x: x - r.dx, y: y - r.dy, dx: r.dx, dy: r.dy };
    }
    let cur = doc, dx = 0, dy = 0;
    for (let depth = 0; depth < 8; depth++) {
      // аддендум (а): и на пути спуска свой оверлей — не преграда и не цель
      const f = sk17HitPoint(cur, x - dx, y - dy);
      if (!f || String(f.tagName).toUpperCase() !== "IFRAME") break;
      const b = f.getBoundingClientRect();
      let sub = null;
      try { sub = f.contentDocument; } catch (e) { sub = null; }
      if (!sub || !sub.documentElement) return { err: "cross_origin_frame" };
      dx += b.left || 0; dy += b.top || 0;
      cur = sub;
    }
    return { doc: cur, x: x - dx, y: y - dy, dx, dy };
  }
  // ---- SK-26: mark-резолвер — номер из карты последнего SoM-скрина -> элемент. ----------
  // ЧИСТАЯ ФУНКЦИЯ (map приходит параметром — node-юнит гоняет её без замыкания content).
  // Карта протухает: навигация/SPA (url), перестройка DOM (селектор), скролл/сдвиг вёрстки
  // (rect разошёлся больше чем на 2px) — всё это mark_stale, агент обязан переснять.
  const SK26_SHIFT_TOL = 2;
  function sk26ResolveMark(mark, doc, map) {
    if (!Number.isInteger(mark) || mark < 1) return { err: "bad_request" };
    if (!map || !Array.isArray(map.marks) || !map.marks.length) return { err: "mark_stale" }; // скрина с метками не было
    if (map.url !== (doc.location && doc.location.href)) return { err: "mark_stale" }; // навигация/SPA — карта со старой страницы
    const entry = map.marks.find((x) => x && x.n === mark);
    if (!entry) return { err: "mark_stale" }; // нового скрина с такой меткой не было
    let el = null;
    try { el = doc.querySelector(entry.selector); } catch (e) { return { err: "mark_stale" }; }
    if (!el) return { err: "mark_stale" }; // DOM перестроился
    const b = el.getBoundingClientRect();
    if (!b.width || !b.height) return { err: "mark_stale" }; // элемент исчез/скрылся
    const s = entry.rect || [];
    if (Math.abs(b.left - (s[0] || 0)) > SK26_SHIFT_TOL || Math.abs(b.top - (s[1] || 0)) > SK26_SHIFT_TOL ||
        Math.abs(b.width - (s[2] || 0)) > SK26_SHIFT_TOL || Math.abs(b.height - (s[3] || 0)) > SK26_SHIFT_TOL)
      return { err: "mark_stale" }; // скролл/сдвиг вёрстки — координаты снимка больше не действительны
    return { el, entry };
  }
  // ---- SK-42: редактируемая цель и ввод в contenteditable ---------------------------
  // Инцидент A (аудит 22.10): композер web.telegram.org — [contenteditable], мост умел
  // писать только в нативные input/textarea/select → пять not_editable и handoff.
  function sk42EditableHost(el) {
    // Нативное поле — оно и есть хост; иначе идём ВВЕРХ до ближайшего contenteditable
    // (селектор/клик попадают в span внутри композера). contenteditable="false" внутри
    // редактируемого острова — остановка: это не поле ввода.
    if (!el) return null;
    const tag = String(el.tagName || "").toUpperCase();
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return el;
    for (let cur = el; cur && cur.getAttribute; cur = cur.parentElement) {
      let v = null;
      try { v = cur.getAttribute("contenteditable"); } catch (e) { break; }
      if (v == null) continue; // атрибута нет — выше (ОТСУТСТВУЕТ ≠ пустой атрибут)
      const ce = String(v).toLowerCase().trim();
      if (ce === "false") break; // редактируемый остров с не-редактируемым дном
      if (ce === "" || ce === "true" || ce === "plaintext-only") return cur;
    }
    return null;
  }
  function sk42NotEditableHint(el) {
    // п.4: ошибка самодостаточна — агент видит tag цели, её contenteditable и следующий
    // шаг, не читая исходники моста и не гугля словарь.
    let ce = "null";
    try { if (el && el.getAttribute && el.getAttribute("contenteditable") != null) ce = el.getAttribute("contenteditable"); } catch (e) {}
    const tag = el ? String(el.tagName || "?").toLowerCase() : "null";
    return L("cnt.hint_not_editable", { tag, ce });
  }
  const sk42Value = (m) => (m.value != null ? String(m.value) : (m.text != null ? String(m.text) : ""));
  function sk42InsertCE(doc, el, value) {
    // Ввод, которому верят React/Vue: beforeinput (cancelable — обработчик страницы
    // может вето) → execCommand('insertText') (браузер сам мутирует DOM и шлёт
    // нативный input с inputType:'insertText') → change. execCommand недоступен или
    // отказал (изолированный мир, node-харнесс) → ручная запись + InputEvent с тем же
    // inputType. Семантика — ЗАМЕНА: выделяем всё, вставка перезаписывает (как value
    // у нативных полей SK-17).
    const w = doc.defaultView;
    try { el.focus(); } catch (e) { /* focus не обязателен для ручной записи */ }
    try {
      const sel = w.getSelection(), range = doc.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) { /* нет selection (harness) — fallback-путь и так пишет целиком */ }
    const iopts = { bubbles: true, cancelable: true, composed: true, inputType: "insertText", data: value };
    const bi = new w.InputEvent("beforeinput", iopts);
    el.dispatchEvent(bi);
    if (bi.defaultPrevented) return { err: "input_prevented",
      hint: L("cnt.hint_beforeinput") };
    let ok = false;
    try { ok = !!(doc.execCommand && doc.execCommand("insertText", false, value)); } catch (e) { ok = false; }
    if (!ok) {
      el.textContent = value;
      try { el.dispatchEvent(new w.InputEvent("input", iopts)); }
      catch (e) { el.dispatchEvent(new w.Event("input", { bubbles: true, composed: true })); }
    }
    try { el.dispatchEvent(new w.Event("change", { bubbles: true, composed: true })); } catch (e) {}
    return null;
  }
  function sk42DoInput(doc, el, value) {
    // Единая точка ввода для всех режимов цели (selector/sid/mark/координаты/фокус):
    // null = успех, {err, hint} = отказ. Нативные поля — прототипный сеттер SK-17
    // (React-трекер), contenteditable — sk42InsertCE.
    const host = sk42EditableHost(el);
    if (!host) return { err: "not_editable", hint: sk42NotEditableHint(el) };
    const tag = String(host.tagName || "").toUpperCase();
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) {
      sk17NativeSet(doc, host, value);
      const opts = { bubbles: true, cancelable: true, composed: true };
      // input first (React's onChange listens to it), then change — frameworks and
      // vanilla listeners both must see the new value.
      host.dispatchEvent(new doc.defaultView.InputEvent("input", { ...opts, data: value }));
      host.dispatchEvent(new doc.defaultView.Event("change", opts));
      return null;
    }
    return sk42InsertCE(doc, host, value);
  }
  // ---- SK-48: silent-noop пост-чек клика по тумблеру ----------------------------------
  // Аудит №2 22.10, проблема 11: «Выбрать все» на Ozon — action.do вернул executed:true,
  // а чекбокс не проставился; агент заметил только через 8 минут. Кликабельная цель-тумблер
  // (native checkbox/radio, label->control, aria checkbox/switch/pressed/checked/expanded)
  // даёт дешёвый до/после прямо в ответе action.do — без vision и без второго rpc.
  const SK48_POST_WAIT_MS = 250;  // React-контролируемый чекбокс доезжает до DOM асинхронно
  function sk48ToggleState(doc, el) {
    // {kind, checked} | null — null означает «цель не тумблер», пост-чек не нужен.
    if (!el || !el.tagName) return null;
    const g = (n, a) => { try { return n.getAttribute ? n.getAttribute(a) : null; } catch (e) { return null; } };
    const tri = (v) => (v === "true" ? true : v === "false" ? false : v === "mixed" ? "mixed" : null);
    try {
      const tag = String(el.tagName).toUpperCase();
      // native-контрол: сам; клик по label (или span внутри label) активирует control —
      // состояние читаем у control, клики по label в Ozon как раз такой случай
      if (tag === "INPUT" && (el.type === "checkbox" || el.type === "radio"))
        return { kind: el.type, checked: !!el.checked };
      let lab = null;
      if (tag === "LABEL") lab = el;
      else if (el.closest) lab = el.closest("label");
      if (lab && lab.control && (lab.control.type === "checkbox" || lab.control.type === "radio"))
        return { kind: lab.control.type, checked: !!lab.control.checked, via: "label" };
      // aria-тумблер: роль/состояние на цели или на ближайшем хозяине
      const host = (el.closest && el.closest('[role="checkbox"],[role="switch"],' +
        '[role="menuitemcheckbox"],[role="menuitemradio"],[aria-pressed],[aria-checked],[aria-expanded]')) || el;
      const role = (g(host, "role") || "").trim().split(/\s+/)[0];
      if (["checkbox", "switch", "menuitemcheckbox", "menuitemradio"].includes(role)) {
        const a = tri(g(host, "aria-checked"));
        return { kind: role, checked: a === null ? false : a };
      }
      for (const pair of [["aria-pressed", "pressed"], ["aria-checked", "checked"],
                          ["aria-expanded", "expanded"]]) {
        const v = g(host, pair[0]);
        if (v != null) { const b = tri(v); return { kind: pair[1], checked: b === null ? false : b }; }
      }
    } catch (e) { return null; }
    return null;
  }
  async function sk48TogglePost(doc, el, before) {
    // null без тумблера; иначе {kind, before, after, changed[, waited_ms][, note]}.
    // changed:false — silent-noop: клик дошёл, состояние не сдвинулось. after:null —
    // цель исчезла из DOM (перерисовка) — состояние честно неизвестно, не «изменилось».
    if (!before) return null;
    const read = () => { const s = sk48ToggleState(doc, el); return s ? s.checked : null; };
    let after = read();
    let waited = 0;
    if (after === before.checked) {
      await new Promise((r) => setTimeout(r, SK48_POST_WAIT_MS));
      waited = SK48_POST_WAIT_MS;
      after = read();
    }
    const gone = after === null && sk48ToggleState(doc, el) === null;
    return { kind: before.kind, before: before.checked, after,
      changed: gone ? null : after !== before.checked,
      ...(waited ? { waited_ms: waited } : {}),
      ...(gone ? { note: L("cnt.hint_gone") } : {}) };
  }
  async function sk48ClickWrap(doc, el, action, fire) {
    // Общий чехол кликабельных путей: состояние ДО -> полный путь -> состояние ПОСЛЕ.
    // Возвращает {} или {post_state} для подмешивания в ответ.
    const pre = (action === "click" || action === "dblclick") ? sk48ToggleState(doc, el) : null;
    fire();
    const post = await sk48TogglePost(doc, el, pre);
    return post ? { post_state: post } : {};
  }
  async function sk17ActionDo(m, doc, somMap, sidResolve) {
    const action = String(m.action || "").toLowerCase();
    if (!["click", "input", "submit", "rightclick", "hover", "dblclick", "move", "key",
          "scroll"].includes(action)) // SK-42 п.2: scroll
      return { err: "bad_request" };
    const win = doc.defaultView;
    const hasXY = Number.isFinite(+m.x) && Number.isFinite(+m.y);
    // SK-26: mark — клик по номеру с последнего скрина marks:true. SK-27: sid — стабильный
    // id из последнего page.dom (реестр живёт в content-замыкании, резолвер приходит
    // параметром — node-юнит подсовывает мок). Мост уже проверил взаимоисключение целей;
    // это защитная продублёжка. Резолверы — ОНИ ЖЕ, что у превью: протухшая карта/реестр
    // отказывают одинаково в обоих легах цепи (mark_stale / sid_stale).
    const isMark = m.mark != null;
    const isSid = !isMark && typeof m.sid === "string" && /^[a-z0-9]{4,12}$/i.test(m.sid);
    if (isMark || isSid) {
      if (m.selector || hasXY || m.frame != null || action === "key") return { err: "bad_request" };
      if (action === "scroll" && (m.dy != null || m.to != null)) return { err: "bad_request" }; // SK-42: у scroll-цели нет dy/to
      if (isMark && m.sid != null) return { err: "bad_request" }; // две цели не приоритизируются
      let el;
      let extra = {};
      if (isMark) {
        const r = sk26ResolveMark(+m.mark, doc, somMap || null);
        if (r.err) return { err: r.err };
        el = r.el;
        if (r.entry && r.entry.sid) extra = { sid: r.entry.sid }; // SK-27: метка скрина ↔ sid снапшота
      } else {
        const r = sidResolve ? sidResolve(m.sid) : { err: "sid_stale" };
        if (r.err) return { err: r.err };
        el = r.el;
      }
      if (action === "input") {
        // SK-42 п.1: не «not_editable для чужого тега» — резолвер идёт к contenteditable
        // хосту, отказ остаётся с hint (tag/CE цели + следующий шаг).
        const ir = sk42DoInput(doc, el, sk42Value(m));
        if (ir) return ir;
        try { if (typeof sk39Fx === "function") sk39Fx("input", null, null, el); } catch (e) { /* визуал */ }
        return { done: true, executed: true, action, synthetic: true, url: doc.location.href, hit: sk17Hit(el), ...extra };
      }
      if (action === "scroll") {
        // SK-42 п.2: цель (sid/mark) — scrollIntoView по центру; прокручивается и
        // внутренний скроллируемый контейнер (лента/чат), не только окно.
        el.scrollIntoView({ block: "center", inline: "nearest" });
        const wv = doc.defaultView || win;
        return { done: true, executed: true, action, synthetic: true, url: doc.location.href,
                 scrolled: { into_view: true, y: wv.scrollY || 0 }, hit: sk17Hit(el), ...extra };
      }
      if (action === "submit") {
        const form = el.tagName === "FORM" ? el : el.form;
        if (!form) return { err: "no_form" };
        if (typeof form.requestSubmit === "function") form.requestSubmit(); else form.submit();
        return { done: true, executed: true, action, synthetic: true, url: doc.location.href, hit: sk17Hit(el), ...extra };
      }
      if (!["click", "rightclick", "dblclick", "hover"].includes(action)) return { err: "bad_request" };
      if (action !== "hover" && sk17NotClickable(doc, el)) return { err: "not_clickable" }; // аддендум (б)
      const b = el.getBoundingClientRect();
      const cx = Math.round(b.left + b.width / 2), cy = Math.round(b.top + b.height / 2);
      const w = doc.defaultView || win;
      // SK-48: полный путь/двойной путь — внутрь чехла, пост-чек тумблера поверх
      const extra48 = await sk48ClickWrap(doc, el, action, () => {
        if (action === "dblclick") {
          sk17Path(w, el, cx, cy, "click"); el.click();
          sk17Path(w, el, cx, cy, "click"); el.click();
          sk17Fire(w, el, "dblclick", { bubbles: true, cancelable: true, composed: true,
            clientX: cx, clientY: cy, button: 0, buttons: 0, detail: 2 }, ["MouseEvent", "Event"]);
        } else if (action === "hover") {
          sk17Path(w, el, cx, cy, "hover");
        } else if (action === "rightclick") {
          sk17Path(w, el, cx, cy, "rightclick");
        } else {
          sk17Path(w, el, cx, cy, "click"); el.click(); // полный путь + click event + default action
        }
      });
      try { if (typeof sk39Fx === "function") sk39Fx(action, cx, cy, el); } catch (e) { /* визуал */ }
      return { done: true, executed: true, action, synthetic: true, url: doc.location.href, hit: sk17Hit(el), ...extra, ...extra48 };
    }
    // Аддендум аудита (в): клик по имени/роли — резолвер на стороне расширения.
    // Тот же полный путь по центру найденного элемента; неоднозначно — кандидаты с
    // rect (агент выберет координатой), не угадываем.
    const nameMode = !m.selector && !hasXY && m.frame == null && m.sid == null &&
        ["click", "rightclick", "dblclick", "hover"].includes(action) &&
        (m.role != null || (typeof m.name === "string" && m.name.trim()));
    // SK-24 п.2: keys land on the focused element (dropdowns close/select by keyboard).
    if (action === "key") {
      const key = String(m.key || "");
      if (!key) return { err: "bad_request" };
      const el = doc.activeElement || doc.body;
      if (!el) return { err: "no_element" };
      const mods = Array.isArray(m.modifiers) ? m.modifiers : [];
      const has = (n) => mods.some((s) => String(s).toLowerCase().replace(/key$/, "") === n);
      const o = { bubbles: true, cancelable: true, composed: true, key,
        code: key.length === 1 ? "Key" + key.toUpperCase() : key,
        ctrlKey: has("ctrl") || has("control"), shiftKey: has("shift"),
        altKey: has("alt"), metaKey: has("meta") };
      sk17Fire(win, el, "keydown", o, ["KeyboardEvent", "Event"]);
      sk17Fire(win, el, "keyup", o, ["KeyboardEvent", "Event"]);
      return { done: true, executed: true, action, synthetic: true, url: doc.location.href, hit: sk17Hit(el) };
    }
    // SK-42 п.2: scroll без цели — окно (document), не «самый большой контейнер»:
    // детерминированно и проверяемо. Внутренние ленты/чаты — scroll с sid/mark
    // (scrollIntoView прокручивает их сам). Ответ несёт фактический сдвиг: dy=0 без
    // note означал бы, что окно прокрутилось на месте; note говорит, что крутить надо
    // не окно. Мгновенно (без smooth): агент читает результат следующим же page.dom.
    if (action === "scroll") {
      if (m.selector || hasXY || m.frame != null || m.mark != null || m.sid != null)
        return { err: "bad_request" };
      const dy = Number.isFinite(+m.dy) ? +m.dy : null;
      const to = typeof m.to === "string" ? m.to.toLowerCase() : null;
      if (dy == null && !to) return { err: "bad_request" };
      const w2 = doc.defaultView || win;
      const x0 = w2.scrollX || 0, y0 = w2.scrollY || 0;
      if (to) {
        const de = doc.documentElement || {}, bdy = doc.body || {};
        const h = Math.max(+de.scrollHeight || 0, +bdy.scrollHeight || 0);
        w2.scrollTo({ left: x0, top: to === "bottom" ? h : 0 });
      } else {
        w2.scrollBy({ left: 0, top: dy });
      }
      const res = { done: true, executed: true, action, synthetic: true, url: doc.location.href,
                    scrolled: { dx: (w2.scrollX || 0) - x0, dy: (w2.scrollY || 0) - y0, y: w2.scrollY || 0 } };
      if (!res.scrolled.dy && !res.scrolled.dx)
        res.note = L("cnt.hint_scroll");
      return res;
    }
    // SK-42 п.1: input без цели — вставка в focused-элемент (тот же принцип, что у key:
    // фокус и есть цель). Не-редактируемый фокус — not_editable с hint, не молчание.
    if (action === "input" && !m.selector && !hasXY && m.mark == null && m.sid == null) {
      const el = doc.activeElement;
      if (!el || el === doc.body || el === doc.documentElement)
        return { err: "not_editable",
                 hint: L("cnt.hint_nofocus", { el: el ? String(el.tagName || "?").toLowerCase() : "null" }) };
      const ir = sk42DoInput(doc, el, sk42Value(m));
      if (ir) return ir;
      try { if (typeof sk39Fx === "function") sk39Fx("input", null, null, el); } catch (e) { /* визуал */ }
      return { done: true, executed: true, action, synthetic: true, url: doc.location.href, hit: sk17Hit(el) };
    }
    // SK-42 п.3: input по координатам — хит-тест с пропуском собственного оверлея
    // (sk17HitPoint, SK-24 аддендум а), затем фокус и ввод в найденное поле.
    if (action === "input" && hasXY) {
      if (m.selector) return { err: "bad_request" }; // coord leg: coords only
      const r = sk17Into(doc, +m.x, +m.y, m.frame);
      if (r.err) return { err: r.err };
      const el = sk17HitPoint(r.doc, r.x, r.y);
      if (!el) return { err: "no_element" }; // вне вьюпорта — под точкой ничего нет
      const ir = sk42DoInput(r.doc, el, sk42Value(m));
      if (ir) return ir;
      try { if (typeof sk39Fx === "function") sk39Fx("input", +m.x, +m.y, el); } catch (e) { /* визуал */ }
      return { done: true, executed: true, action, synthetic: true,
               url: (r.doc.location || doc.location).href, hit: sk17Hit(el) };
    }
    // SK-24 п.1: coordinates — elementFromPoint (iframe-aware) + the full pointer path.
    if (!nameMode && (action === "rightclick" || action === "hover" || action === "dblclick" ||
        action === "move" || (action === "click" && hasXY))) {
      if (!hasXY || m.selector) return { err: "bad_request" }; // coord leg: coords only
      const r = sk17Into(doc, +m.x, +m.y, m.frame);
      if (r.err) return { err: r.err };
      const w = r.doc.defaultView || win;
      const el = sk17HitPoint(r.doc, r.x, r.y); // аддендум (а): свой оверлей — не цель
      if (!el) return { err: "no_element" }; // outside the viewport — nothing under the point
      if (action !== "hover" && action !== "move" && sk17NotClickable(r.doc, el))
        return { err: "not_clickable" }; // аддендум (б): промах — не ложный успех
      const extra48c = await sk48ClickWrap(r.doc, el, action, () => {
        if (action === "dblclick") {
          sk17Path(w, el, r.x, r.y, "click"); el.click();
          sk17Path(w, el, r.x, r.y, "click"); el.click();
          sk17Fire(w, el, "dblclick", { bubbles: true, cancelable: true, composed: true,
            clientX: r.x, clientY: r.y, button: 0, buttons: 0, detail: 2 }, ["MouseEvent", "Event"]);
        } else {
          sk17Path(w, el, r.x, r.y, action === "rightclick" ? "rightclick" :
            (action === "hover" || action === "move") ? "hover" : "click");
          // click event + default action (checkbox/link/form) — dispatched events never
          // activate, el.click() does; the coords ride on pointerdown/mousedown/up.
          if (action === "click") el.click();
        }
      });
      // SK-39: точка в топ-вьюпорте, если спуск был во фрейм; иначе — локальные координаты
      try { if (typeof sk39Fx === "function") { const p = r.doc === doc ? [r.x, r.y] : [+m.x, +m.y]; sk39Fx(action, p[0], p[1], el); } } catch (e) { /* визуал */ }
      return { done: true, executed: true, action, synthetic: true,
        url: (r.doc.location || doc.location).href, hit: sk17Hit(el), ...extra48c };
    }
    // Аддендум аудита (в): клик по имени/роли — резолвер на стороне расширения.
    // Тот же полный путь по центру найденного элемента; неоднозначно — кандидаты с
    // rect (агент выберет координатой), не угадываем.
    if (nameMode) {
      const nr = sk17ByName(doc, m.name, m.role);
      if (nr.err) return { err: nr.err };
      // гонка (имя стало неоднозначным между превью и исполнением) — НЕ err:
      // кандидаты должны дойти до агента, а не схлопнуться в строку ошибки
      if (nr.ambiguous) return { done: false, executed: false, ambiguous: true, candidates: nr.ambiguous };
      const el = nr.el;
      const b = el.getBoundingClientRect();
      const cx = Math.round(b.left + b.width / 2), cy = Math.round(b.top + b.height / 2);
      const w = doc.defaultView || win;
      const extra48n = await sk48ClickWrap(doc, el, action, () => {
        if (action === "dblclick") {
          sk17Path(w, el, cx, cy, "click"); el.click();
          sk17Path(w, el, cx, cy, "click"); el.click();
          sk17Fire(w, el, "dblclick", { bubbles: true, cancelable: true, composed: true,
            clientX: cx, clientY: cy, button: 0, buttons: 0, detail: 2 }, ["MouseEvent", "Event"]);
        } else if (action === "hover") {
          sk17Path(w, el, cx, cy, "hover");
        } else if (action === "rightclick") {
          sk17Path(w, el, cx, cy, "rightclick");
        } else {
          sk17Path(w, el, cx, cy, "click"); el.click();
        }
      });
      try { if (typeof sk39Fx === "function") sk39Fx(action, cx, cy, el); } catch (e) { /* визуал */ }
      return { done: true, executed: true, action, synthetic: true, url: doc.location.href, hit: sk17Hit(el), ...extra48n };
    }
    let el = null;
    let tdoc = doc;
    if (m.frame != null) { // SK-24: a selector inside a same-origin frame (page.dom pair)
      const r = sk17FrameDoc(doc, m.frame);
      if (r.err) return { err: r.err };
      tdoc = r.doc;
    }
    try { el = m.selector ? tdoc.querySelector(m.selector) : null; }
    catch (e) { return { err: "bad_selector" }; }
    if (!el) return { err: "no_element" };
    if (action === "click") {
      if (sk17NotClickable(tdoc, el)) return { err: "not_clickable" }; // аддендум (б)
      // SK-24 п.3: selector clicks walk the FULL pointer path at the element centre —
      // «клик есть, сайт не видит» was a missing mousedown/mouseup, not a missing click.
      const b = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
      const cx = b && b.width ? Math.round(b.left + b.width / 2) : 0;
      const cy = b && b.height ? Math.round(b.top + b.height / 2) : 0;
      const extra48s = await sk48ClickWrap(tdoc, el, action, () => {
        sk17Path(tdoc.defaultView || win, el, cx, cy, "click");
        el.click(); // click event + default action
      });
      // SK-39: фреймовый селектор — только «отдача» (координаты локальны для фрейма)
      try { if (tdoc === doc && typeof sk39Fx === "function") sk39Fx(action, cx, cy, el);
            else if (tdoc !== doc && typeof sk39Recoil === "function") sk39Recoil(el); } catch (e) { /* визуал */ }
      return { done: true, executed: true, action, synthetic: true, url: doc.location.href, hit: sk17Hit(el), ...extra48s };
    }
    if (action === "input") {
      // SK-42 п.1: selector тоже находит contenteditable (селектор хоста или span
      // внутри него); отказ — not_editable с hint. Успешный ответ — прежняя форма
      // SK-17 (без hit): пины контракта не меняются.
      const ir = sk42DoInput(tdoc, el, sk42Value(m));
      if (ir) return ir;
      try { if (tdoc === doc && typeof sk39Fx === "function") sk39Fx("input", null, null, el);
            else if (tdoc !== doc && typeof sk39Recoil === "function") sk39Recoil(el); } catch (e) { /* визуал */ }
    } else { // submit
      const form = el.tagName === "FORM" ? el : el.form;
      if (!form) return { err: "no_form" };
      // requestSubmit fires a cancelable submit event (page handlers stay in charge);
      // submit() is the old-engine fallback — no event, direct send.
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    }
    return { done: true, executed: true, action, synthetic: true, url: doc.location.href };
  }
  // SK-17-END executor

  browser.runtime.onMessage.addListener(async (m) => {
    if (m.type === "page_text") return pageText(m.maxChars);
    if (m.type === "page_dom") return pageDom(m.opts); // SK-27: {near} прокидывается мостом
    if (m.type === "page_media") return pageMedia(); // SK-48: видео-состояние (paused/currentTime)
    if (m.type === "som_marks") return await somDraw(); // SK-26: нарисовать метки ПЕРЕД снимком
    if (m.type === "som_marks_clear") { somClearVisual(); return { cleared: true }; } // сразу после кадра
    // SK-47: метрики вьюпорта для даунскейла/кропа/кэша кадра (CSS-пиксели + скролл + DPR).
    // Тот же ответ, что несёт somDraw; свёрстан одним форматом, чтобы vpSame сверял одно с другим.
    if (m.type === "sk47_vp") return { w: innerWidth, h: innerHeight,
      sx: Math.round(scrollX), sy: Math.round(scrollY), dpr: devicePixelRatio };
    if (m.type === "preview") return preview(m);
    if (m.type === "action_do") {
      const res = await sk17ActionDo(m, document, somMap, (s) => sk27ResolveSid(s, document)); // SK-26 mark-карта + SK-27 sid-реестр резолвятся здесь
      // SK-17: the execution itself releases the ring the gate preview was holding
      // (a synthetic click/input already fires armRelease's listeners; submit on a
      // form whose target was the button does not — drop it here so nothing leaks).
      if (res && res.executed && previewRelease) previewRelease.drop();
      return res;
    }
    if (m.type === "cursor") {
      if (previewPending || previewRelease) return; // the preview overlay owns the ghost cursor
      const c = $("c"), l = $("l");
      sk39Shape("arrow"); // push-режим агента: указатель, не I-beam
      c.classList.add("on"); l.classList.add("on");
      place(c, m.x, m.y);
      place(l, m.x, m.y);
      l.textContent = m.label || "Hermes";
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { c.classList.remove("on"); l.classList.remove("on"); }, 2500);
    } else if (m.type === "highlight") {
      const el = m.selector ? document.querySelector(m.selector) : null;
      if (!el) return;
      if (previewPending || previewRelease) return; // an active/held preview owns the ring (SK-16)
      const seq = previewSeq; // capture BEFORE the scroll wait: a preview starting mid-wait wins
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      await new Promise(r => setTimeout(r, 380));
      if (seq !== previewSeq || previewPending || previewRelease) return;
      const b = el.getBoundingClientRect(), r = $("r");
      r.classList.remove("no");
      place(r, b.left - 4, b.top - 4);
      r.style.width = b.width + 8 + "px"; r.style.height = b.height + 8 + "px";
      r.classList.add("on");
      setTimeout(() => { if (seq === previewSeq && !previewPending && !previewRelease) r.classList.remove("on"); }, 2200);
    } else if (m.type === "toast") {
      const t = $("t"); t.textContent = m.text; t.classList.add("on");
      setTimeout(() => t.classList.remove("on"), 3000);
    } else if (m.type === "hello") {
      browser.runtime.sendMessage({ type: "sidekick-ready" }).catch(() => {});
    }
  });
})();
