// SK-10: saved prompts («skills») — pure logic, no DOM, no storage.
// The sidebar owns storage.local (key sk10Skills) and rendering; this module is the
// contract: slash-trigger parsing, filtering/ranking, upsert caps, export/import json.
// Loaded as a plain script in the sidebar (window.Skills) and require()'d by node tests.
(function (root) {
  const NAME_MAX = 60, TEXT_MAX = 4000, LIST_MAX = 200, SHOWN_MAX = 12;

  // single-line normalisation: the composer is an <input>, so \n never survives
  function clean(s, max) {
    return String(s == null ? "" : s).replace(/[\r\n]+/g, " ").replace(/\s{2,}/g, " ").trim().slice(0, max);
  }
  // the trigger is a leading «/» — the whole rest of the line is the query
  function slashQuery(value) {
    const v = String(value == null ? "" : value);
    return v.startsWith("/") ? v.slice(1) : null;
  }
  function rank(s, q) {
    const n = s.name.toLowerCase(), t = (s.text || "").toLowerCase();
    if (n.startsWith(q)) return 0;
    if (n.includes(q)) return 1;
    if (t.includes(q)) return 2;
    return -1;
  }
  function byName(a, b) { return a.name.localeCompare(b.name, "ru"); }
  function filter(list, q) {
    const all = (Array.isArray(list) ? list : []).filter((s) => s && s.name);
    const query = String(q == null ? "" : q).trim().toLowerCase();
    if (!query) return all.slice().sort(byName).slice(0, SHOWN_MAX);
    return all
      .map((s) => ({ s, r: rank(s, query) }))
      .filter((x) => x.r >= 0)
      .sort((a, b) => a.r - b.r || byName(a.s, b.s))
      .slice(0, SHOWN_MAX)
      .map((x) => x.s);
  }
  // upsert by name (case-insensitive) — re-saving a name updates its text, no dupes
  function upsert(list, name, text, now) {
    const t = clean(text, TEXT_MAX);
    if (!t) return { ok: false, err: "empty" };
    const n = clean(name, NAME_MAX) || t.slice(0, 40);
    const out = (Array.isArray(list) ? list : []).slice();
    const i = out.findIndex((s) => s && s.name && s.name.toLowerCase() === n.toLowerCase());
    if (i >= 0) {
      out[i] = { ...out[i], name: n, text: t };
      return { ok: true, action: "updated", name: n, list: out };
    }
    out.push({
      id: "sk" + Math.random().toString(36).slice(2, 10),
      name: n, text: t,
      created: new Date(now == null ? Date.now() : now).toISOString(),
    });
    if (out.length > LIST_MAX) out.splice(0, out.length - LIST_MAX);
    return { ok: true, action: "added", name: n, list: out };
  }
  function exportJson(list, iso) {
    return JSON.stringify({
      app: "hermes-sidekick", kind: "skills", version: 1,
      exported: iso || new Date().toISOString(),
      skills: (Array.isArray(list) ? list : []).map((s) => ({ id: s.id, name: s.name, text: s.text, created: s.created })),
    }, null, 2);
  }
  // merge by name (imported wins), keep the rest; accepts a bare array or {skills:[...]}
  function importJson(text, existing) {
    let data;
    try { data = JSON.parse(String(text)); } catch (e) { return { ok: false, err: "bad_json" }; }
    const raw = Array.isArray(data) ? data : (data && Array.isArray(data.skills) ? data.skills : null);
    if (!raw) return { ok: false, err: "no_skills" };
    const out = (Array.isArray(existing) ? existing : []).slice();
    let added = 0, updated = 0, skipped = 0;
    for (const s of raw) {
      if (!s || typeof s !== "object") { skipped++; continue; }
      const name = clean(s.name, NAME_MAX), t = clean(s.text, TEXT_MAX);
      if (!name || !t) { skipped++; continue; }
      const i = out.findIndex((x) => x && x.name && x.name.toLowerCase() === name.toLowerCase());
      if (i >= 0) { out[i] = { ...out[i], name, text: t }; updated++; }
      else {
        out.push({
          id: "sk" + Math.random().toString(36).slice(2, 10),
          name, text: t,
          created: typeof s.created === "string" ? s.created : new Date().toISOString(),
        });
        added++;
      }
    }
    if (out.length > LIST_MAX) out.splice(0, out.length - LIST_MAX);
    return { ok: true, list: out, added, updated, skipped };
  }

  const api = { NAME_MAX, TEXT_MAX, LIST_MAX, SHOWN_MAX, clean, slashQuery, filter, upsert, exportJson, importJson };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Skills = api;
})(typeof window !== "undefined" ? window : globalThis);
