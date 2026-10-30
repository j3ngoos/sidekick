// SK-15: логика options-страницы. Значение токена на странице НЕ показывается —
// только состояние (сохранён/не сохранён + отметка времени, storage.local).
// «Проверить подключение» спрашивает реальный WS-статус у background runtime-
// сообщением (никаких новых rpc-методов моста; ответ токена не содержит).
const $ = (id) => document.getElementById(id);
const inp = $("tok"), st = $("st"), chip = $("tokState"), chipTxt = $("tokStateTxt"),
      meta = $("tokMeta");

function setSt(txt, cls) { st.textContent = txt; st.className = "st" + (cls ? " " + cls : ""); }
function fmtTime(ts) {
  if (!Number.isFinite(ts)) return "";
  return new Date(ts).toLocaleString("ru-RU",
    { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
async function load() {
  const k = await browser.storage.local.get(["bridgeToken", "bridgeTokenAt"]);
  inp.value = ""; // секрет не предзаполняем — поле только для нового значения
  if (k.bridgeToken) {
    chip.className = "chip ok"; chipTxt.textContent = "сохранён";
    meta.textContent = "хранится локально в расширении"
      + (k.bridgeTokenAt ? " · " + fmtTime(k.bridgeTokenAt) : "") + " · значение скрыто";
  } else {
    chip.className = "chip warn"; chipTxt.textContent = "не сохранён";
    meta.textContent = "без токена мост не пустит — вставь значение из hub/token";
  }
}
async function save() {
  const t = inp.value.trim();
  if (!/^[0-9a-f]{32,128}$/i.test(t)) { st.textContent = "похоже, скопировалось не то: нужен hex-токен 64+ символов"; st.className = "st err"; return; }
  await browser.storage.local.set({ bridgeToken: t, bridgeTokenAt: Date.now() });
  await load();
  setSt("сохранено, переподключаюсь…", "ok");
  browser.runtime.sendMessage({ type: "token-updated" }).catch(() => {});
}
async function clear() {
  await browser.storage.local.remove(["bridgeToken", "bridgeTokenAt"]);
  await load();
  setSt("стёрто", "");
  browser.runtime.sendMessage({ type: "token-updated" }).catch(() => {});
}
let checking = false;
async function check() {
  if (checking) return;
  checking = true; $("check").disabled = true;
  setSt("проверка…", "");
  let r = null;
  try { r = await browser.runtime.sendMessage({ type: "ws-check" }); } catch (e) {}
  checking = false; $("check").disabled = false;
  if (!r || !r.state) { setSt("не подключено — расширение не ответило на проверку", "err"); return; }
  if (r.state === "connected") setSt("подключено · " + (r.url || "мост"), "ok");
  else setSt("не подключено — " + (r.reason || "неизвестная причина"), "err");
}
$("save").onclick = save;
$("clear").onclick = clear;
$("check").onclick = check;
// SK-50: «Проверить звонок» — живой notifications.create через background
// (API живёт в event-page). Ответ = API-успех + уровень разрешения Firefox;
// если человек «ни слышал, ни видел» — под кнопкой разворачивается чек-лист
// (Firefox-разрешение, macOS «Позволить уведомления» + «Звуки», правило фокуса).
let bellChecking = false;
function setBellSt(txt, cls) { const b = $("bellSt"); b.textContent = txt; b.className = "st" + (cls ? " " + cls : ""); }
async function bellTest() {
  if (bellChecking) return;
  bellChecking = true; $("bellTest").disabled = true;
  setBellSt("выпускаю уведомление…", "");
  let r = null;
  try { r = await browser.runtime.sendMessage({ type: "bell-test" }); } catch (e) {}
  bellChecking = false; $("bellTest").disabled = false;
  $("bellHint").hidden = false; // чек-лист полезен и при шуме, и при тишине
  if (!r) { setBellSt("расширение не ответило — фоновая страница моргнула, повтори", "err"); return; }
  if (r.ok) {
    setBellSt("уведомление выпущено"
      + (r.level === "granted" ? " · Firefox разрешил уведомления"
         : r.level && r.level !== "unknown" ? " · внимание, разрешение Firefox: " + r.level : ""),
      r.level && r.level !== "granted" ? "err" : "ok");
  } else {
    setBellSt("не вышло: " + (r.error || "неизвестная причина"), "err");
  }
}
$("bellTest").onclick = bellTest;
// SK-28: поле «Сервер» — прямой LAN-коннект без ssh-туннеля.
// Пустое значение = ws://127.0.0.1:8791 (туннельная совместимость). background читает
// storage на каждой попытке подключения; «Сохранить» форсирует немедленный reconnect.
// Валидация — только ws:// или wss:// + host[:port][/path]; wss пока предупреждение:
// мост умеет только ws (wss вне объёма SK-28).
const srvInp = $("srv"), srvSt = $("srvSt"), srvChip = $("srvState"), srvChipTxt = $("srvStateTxt");
const SRV_RE = /^wss?:\/\/[A-Za-z0-9][A-Za-z0-9._\-]*(:\d{1,5})?(\/[^\s]*)?$/;
function setSrvSt(txt, cls) { srvSt.textContent = txt; srvSt.className = "st" + (cls ? " " + cls : ""); }
async function loadSrv() {
  const { bridgeServer } = await browser.storage.local.get("bridgeServer");
  const v = String(bridgeServer || "");
  srvInp.value = v;
  if (v.trim()) { srvChip.className = "chip ok"; srvChipTxt.textContent = "LAN-адрес"; }
  else { srvChip.className = "chip"; srvChipTxt.textContent = "localhost (туннель)"; }
}
async function saveSrv() {
  const v = srvInp.value.trim().replace(/\/+$/, "");
  if (!v) { await clearSrv(); return; }
  if (!SRV_RE.test(v)) {
    setSrvSt("нужен адрес вида ws://host-a.local:8791 (или wss://…)", "err"); return;
  }
  await browser.storage.local.set({ bridgeServer: v });
  await loadSrv();
  setSrvSt("сохранено, переподключаюсь…" + (v.startsWith("wss:") ? " (внимание: мост пока говорит только по ws://)" : ""), "ok");
  browser.runtime.sendMessage({ type: "server-updated" }).catch(() => {});
}
async function clearSrv() {
  await browser.storage.local.remove("bridgeServer");
  await loadSrv();
  setSrvSt("localhost (режим туннеля)", "");
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
  afterSt.textContent = "сохранено"; setTimeout(() => { afterSt.textContent = ""; }, 2000);
}
$("afterSave").onclick = saveAfter;
load();
loadSrv();
loadAfter();
