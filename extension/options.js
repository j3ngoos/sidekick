// SK-15: options-page logic. The token value is NEVER shown on the page —
// only the state (saved/not saved + timestamp mark, storage.local).
// "Check connection" asks the real WS status from the background via a runtime
// message (no new bridge rpc methods; the answer carries no token).
// SK-71: every string comes from the locale pack (locales/en.json|ru.json);
// the language switcher (opt "Language") writes storage.uiLang and the whole
// page repaints in place — no reload.
const $ = (id) => document.getElementById(id);
const L = (k, a) => I18N.t(k, a);
const inp = $("tok"), st = $("st"), chip = $("tokState"), chipTxt = $("tokStateTxt"),
      meta = $("tokMeta");

function setSt(txt, cls) { st.textContent = txt; st.className = "st" + (cls ? " " + cls : ""); }
function fmtTime(ts) {
  if (!Number.isFinite(ts)) return "";
  return new Date(ts).toLocaleString(I18N.locale(),
    { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
async function load() {
  const k = await browser.storage.local.get(["bridgeToken", "bridgeTokenAt"]);
  inp.value = ""; // we never prefill the secret — the field is for a new value only
  if (k.bridgeToken) {
    chip.className = "chip ok"; chipTxt.textContent = L("opt.saved");
    meta.textContent = L("opt.stored_local")
      + (k.bridgeTokenAt ? " · " + fmtTime(k.bridgeTokenAt) : "") + L("opt.value_hidden");
  } else {
    chip.className = "chip warn"; chipTxt.textContent = L("opt.not_saved");
    meta.textContent = L("opt.no_token_hint");
  }
}
async function save() {
  const t = inp.value.trim();
  if (!/^[0-9a-f]{32,128}$/i.test(t)) { setSt(L("opt.bad_hex"), "err"); return; }
  await browser.storage.local.set({ bridgeToken: t, bridgeTokenAt: Date.now() });
  await load();
  setSt(L("opt.saved_reconnect"), "ok");
  browser.runtime.sendMessage({ type: "token-updated" }).catch(() => {});
}
async function clear() {
  await browser.storage.local.remove(["bridgeToken", "bridgeTokenAt"]);
  await load();
  setSt(L("opt.erased"), "");
  browser.runtime.sendMessage({ type: "token-updated" }).catch(() => {});
}
let checking = false;
async function check() {
  if (checking) return;
  checking = true; $("check").disabled = true;
  setSt(L("opt.checking"), "");
  let r = null;
  try { r = await browser.runtime.sendMessage({ type: "ws-check" }); } catch (e) {}
  checking = false; $("check").disabled = false;
  if (!r || !r.state) { setSt(L("opt.check_noanswer"), "err"); return; }
  // SK-71: the reason arrives as a key (reason_no_answer/…); unknown keys render as-is
  if (r.state === "connected") setSt(L("opt.connected") + (r.url || L("opt.bridge_word")), "ok");
  else setSt(L("opt.not_connected") + (r.reasonKey ? L(r.reasonKey) : (r.reason || L("opt.unknown_reason"))), "err");
}
$("save").onclick = save;
$("clear").onclick = clear;
$("check").onclick = check;
// SK-50: "Check bell" — a live notifications.create through the background
// (the API lives in the event-page). The answer = API success + the Firefox
// permission level; when the human "heard and saw nothing", a checklist opens
// under the button (Firefox permission, macOS "Allow Notifications" + "Sounds",
// the focus rule).
let bellChecking = false;
function setBellSt(txt, cls) { const b = $("bellSt"); b.textContent = txt; b.className = "st" + (cls ? " " + cls : ""); }
async function bellTest() {
  if (bellChecking) return;
  bellChecking = true; $("bellTest").disabled = true;
  setBellSt(L("opt.bell_publishing"), "");
  let r = null;
  try { r = await browser.runtime.sendMessage({ type: "bell-test" }); } catch (e) {}
  bellChecking = false; $("bellTest").disabled = false;
  $("bellHint").hidden = false; // the checklist is useful both with noise and with silence
  if (!r) { setBellSt(L("opt.bell_noanswer"), "err"); return; }
  if (r.ok) {
    setBellSt(L("opt.bell_sent")
      + (r.level === "granted" ? L("opt.bell_allowed")
         : r.level && r.level !== "unknown" ? L("opt.bell_perm_warn") + r.level : ""),
      r.level && r.level !== "granted" ? "err" : "ok");
  } else {
    setBellSt(L("opt.bell_fail") + (r.errorKey ? L(r.errorKey) : (r.error || L("opt.unknown_reason"))), "err");
  }
}
$("bellTest").onclick = bellTest;
// SK-28: the "Server" field — a direct LAN connect without the ssh tunnel.
// An empty value = ws://127.0.0.1:8791 (tunnel compatibility). The background
// re-reads storage on every connect attempt; "Save" forces an immediate reconnect.
// Validation is ws:// or wss:// + host[:port][/path] only; wss stays a warning:
// the bridge speaks ws only (wss is out of SK-28 scope).
const srvInp = $("srv"), srvSt = $("srvSt"), srvChip = $("srvState"), srvChipTxt = $("srvStateTxt");
const SRV_RE = /^wss?:\/\/[A-Za-z0-9][A-Za-z0-9._\-]*(:\d{1,5})?(\/[^\s]*)?$/;
function setSrvSt(txt, cls) { srvSt.textContent = txt; srvSt.className = "st" + (cls ? " " + cls : ""); }
async function loadSrv() {
  const { bridgeServer } = await browser.storage.local.get("bridgeServer");
  const v = String(bridgeServer || "");
  srvInp.value = v;
  if (v.trim()) { srvChip.className = "chip ok"; srvChipTxt.textContent = L("opt.lan_addr"); }
  else { srvChip.className = "chip"; srvChipTxt.textContent = L("opt.tunnel_mode"); }
}
async function saveSrv() {
  const v = srvInp.value.trim().replace(/\/+$/, "");
  if (!v) { await clearSrv(); return; }
  if (!SRV_RE.test(v)) {
    setSrvSt(L("opt.server_bad"), "err"); return;
  }
  await browser.storage.local.set({ bridgeServer: v });
  await loadSrv();
  setSrvSt(L("opt.saved_reconnect") + (v.startsWith("wss:") ? L("opt.ws_only_warn") : ""), "ok");
  browser.runtime.sendMessage({ type: "server-updated" }).catch(() => {});
}
async function clearSrv() {
  await browser.storage.local.remove("bridgeServer");
  await loadSrv();
  setSrvSt(L("opt.tunnel"), "");
  browser.runtime.sendMessage({ type: "server-updated" }).catch(() => {});
}
$("srvSave").onclick = saveSrv;
$("srvClear").onclick = clearSrv;
// SK-07: parking mode for the agent's tab group after a task (background reads it on done/failed)
const afterSel = $("after"), afterSt = $("afterSt");
async function loadAfter() {
  const { sk07AfterTask } = await browser.storage.local.get("sk07AfterTask");
  afterSel.value = ["collapse", "ungroup", "none"].includes(sk07AfterTask) ? sk07AfterTask : "collapse";
}
async function saveAfter() {
  await browser.storage.local.set({ sk07AfterTask: afterSel.value });
  afterSt.textContent = L("opt.saved"); setTimeout(() => { afterSt.textContent = ""; }, 2000);
}
$("afterSave").onclick = saveAfter;
// ---- SK-71: language switcher. uiLang "en"|"ru"|"system" (default en) lives in
// the same storage as everything else; background relays the change to content
// scripts, this page repaints through watchStorage.
const langSel = $("lang"), langSt = $("langSt");
async function loadLang() {
  const { uiLang } = await browser.storage.local.get(I18N.KEY);
  langSel.value = ["en", "ru", "system"].includes(uiLang) ? uiLang : "en";
}
langSel.onchange = async () => {
  await browser.storage.local.set({ [I18N.KEY]: langSel.value });
  langSt.textContent = L("opt.saved"); setTimeout(() => { langSt.textContent = ""; }, 2000);
};
function relangAll() {
  I18N.apply(document);
  load(); loadSrv(); loadAfter();
}
// ---- boot: dictionary first, then the state pulls — first paint is localized.
await I18N.load();
I18N.apply(document);
I18N.watchStorage(relangAll);
await Promise.all([load(), loadSrv(), loadAfter(), loadLang()]);
