"use strict";

/* ------------------------------------------------------------------ توکن */
// توکن یک بار از URL خوانده و در sessionStorage نگه داشته می‌شود تا با رفرش
// از بین نرود و در نوار آدرس هم نماند.
const token = (() => {
  const fromUrl = new URLSearchParams(location.search).get("t");
  if (fromUrl) {
    sessionStorage.setItem("fillerToken", fromUrl);
    history.replaceState({}, "", location.pathname);
    return fromUrl;
  }
  return sessionStorage.getItem("fillerToken") || "";
})();

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, children = []) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of [].concat(children)) {
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
};

let toastTimer = 0;
function toast(message, bad = false) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.toggle("bad", bad);
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, bad ? 8000 : 3500);
}

async function api(path, { method = "GET", body = null, params = {} } = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  }
  const response = await fetch(url, {
    method,
    headers: { "X-Panel-Token": token, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : null,
  });
  let data = {};
  try { data = await response.json(); } catch { /* بدنه‌ی غیر JSON */ }
  if (!response.ok) throw new Error(data.error || `خطای ${response.status}`);
  return data;
}

const state = { logNext: 0, jobStatus: "idle", plan: [], kinds: {} };

/* -------------------------------------------------------------------- تب */
for (const button of document.querySelectorAll("#tabs button")) {
  button.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    button.classList.add("active");
    $(`#tab-${button.dataset.tab}`).classList.add("active");
    if (button.dataset.tab === "rows") loadRows();
    if (button.dataset.tab === "config") loadConfig();
  });
}

/* -------------------------------------------------------------- تنظیمات */
const FIELDS = [
  ["sheetId", "sheet_id"],
  ["serviceAccount", "service_account_json"],
  ["tab", "tab"],
  ["listsTab", "lists_tab"],
  ["reportTab", "report_tab"],
  ["headerRow", "header_row"],
  ["titleColumn", "title_column"],
  ["sourcesColumn", "sources_column"],
  ["file", "file"],
  ["overwrite", "overwrite"],
  ["limit", "limit"],
  ["maxTitles", "max_titles_per_session"],
  ["maxPages", "max_pages_per_session"],
  ["maxSources", "max_sources_per_title"],
  ["batchRows", "batch_rows"],
  ["maxCats", "max_categories"],
  ["maxTags", "max_tags"],
  ["separator", "multi_select_separator"],
];

function settingsBody() {
  const body = {};
  for (const [id, key] of FIELDS) body[key] = $(`#${id}`).value.trim();
  body.retry_partial = $("#retryPartial").checked;
  body.search_enabled = $("#searchEnabled").checked;
  body.image_enabled = $("#imageEnabled").checked;
  body.image_min_side = Number($("#imageMinSide").value) || 0;
  return body;
}

function applyState(data) {
  const s = data.settings || {};
  for (const [id, key] of FIELDS) $(`#${id}`).value = s[key] ?? "";
  $("#retryPartial").checked = Boolean(s.retry_partial);
  $("#searchEnabled").checked = s.search_enabled !== false;
  $("#imageEnabled").checked = s.image_enabled !== false;
  $("#imageMinSide").value = s.image_min_side ?? 400;
  if (document.activeElement !== $("#sitesText")) {
    $("#sitesText").value = (data.sites || []).join("\n");
  }

  state.kinds = data.kinds || {};
  state.plan = data.plan || [];
  renderPlan(state.plan.map((item) => ({
    ...item,
    kind_label: state.kinds[item.kind] || item.kind,
    labels: item.labels || [],
    writable: item.kind !== "skip" && item.kind !== "title",
  })));

  const counts = data.counts || {};
  $("#counterCards").replaceChildren(...[
    ["ردیف شیت", counts.total], ["کامل", counts.done], ["ناقص", counts.partial],
    ["در صف", counts.pending], ["نوشته‌شده در شیت", counts.pushed],
    ["آدرس ذخیره‌شده", data.links],
  ].map(([label, value]) => el("div", { className: "card" }, [
    el("b", { textContent: value ?? "—" }), el("span", { textContent: label }),
  ])));

  $("#sheetLabel").textContent = s.sheet_id
    ? `شیت ${s.sheet_id.slice(0, 8)}…${s.tab ? ` / ${s.tab}` : ""}`
    : (s.file || "هنوز شیتی تنظیم نشده");
  const notes = [];
  if (!data.ready) notes.push("شیت تنظیم نشده");
  if (s.sheet_id && !data.gspread) notes.push("gspread نصب نیست: pip install gspread");
  $("#headStatus").textContent = notes.join(" — ");
  $("#linksNote").textContent = data.links ? `${data.links} آدرس ذخیره شده` : "";
  $("#configPath").textContent = data.config_path || "(بدون فایل config)";

  const link = (format) => `/download?format=${format}&t=${encodeURIComponent(token)}`;
  $("#dlCsv").href = link("csv");
  $("#dlXlsx").href = link("xlsx");

  applyJob(data.job);
}

function renderPlan(plan) {
  $("#planTable tbody").replaceChildren(...plan.map((item) => el("tr", {}, [
    el("td", { textContent: item.column }),
    el("td", {
      className: item.writable ? "ok" : "muted",
      textContent: item.writable ? item.kind_label : `${item.kind_label} (نوشته نمی‌شود)`,
    }),
    el("td", { textContent: item.options ? String(item.options) : "—" }),
    el("td", { className: "muted", textContent: (item.labels || []).join("، ") }),
  ])));
}

async function refresh() {
  try { applyState(await api("/api/state", { params: { since: state.logNext } })); }
  catch (error) { toast(error.message, true); }
}

async function save(extra = {}) {
  const data = await api("/api/settings", { method: "POST", body: { ...settingsBody(), ...extra } });
  applyState(data);
  return data;
}

$("#saveSheetBtn").addEventListener("click", async () => {
  try { await save(); toast("تنظیمات شیت ذخیره شد."); }
  catch (error) { toast(error.message, true); }
});

$("#saveRulesBtn").addEventListener("click", async () => {
  try { await save(); toast("قواعد ذخیره شد."); }
  catch (error) { toast(error.message, true); }
});

$("#saveSitesBtn").addEventListener("click", async () => {
  try {
    const data = await save({ sites: $("#sitesText").value });
    toast(`${(data.sites || []).length} سایت ذخیره شد.`);
  } catch (error) { toast(error.message, true); }
});

/* --------------------------------------------------------- بررسی شیت */
$("#inspectBtn").addEventListener("click", async () => {
  $("#inspectNote").textContent = "در حال خواندن شیت…";
  try {
    await save();
    const data = await api("/api/inspect", { method: "POST", body: settingsBody() });
    const lists = Object.entries(data.lists || {})
      .map(([name, count]) => `${name} (${count})`).join("، ");
    $("#inspectNote").textContent =
      `${data.rows} ردیف — ${data.plan.length} ستون` + (lists ? ` — لیست‌ها: ${lists}` : "");
    renderPlan(data.plan);
    if (data.sample?.length) toast(`نمونه‌ی عنوان: ${data.sample[0]}`);
  } catch (error) {
    $("#inspectNote").textContent = "";
    toast(error.message, true);
  }
});

/* ------------------------------------------------------------- منابع */
$("#importLinksBtn").addEventListener("click", async () => {
  const file = $("#linksFile").value.trim();
  if (!file) return toast("مسیر فایل را بدهید.", true);
  try {
    const data = await api("/api/links", { method: "POST", body: { file } });
    toast(`${data.titles} عنوان و ${data.links} آدرس ایمپورت شد.`);
    await refresh();
  } catch (error) { toast(error.message, true); }
});

$("#clearCacheBtn").addEventListener("click", async () => {
  if (!confirm("کش صفحه‌های منبع خالی شود؟ اجرای بعدی دوباره دانلود می‌کند.")) return;
  try {
    const data = await api("/api/cache/clear", { method: "POST", body: {} });
    $("#cacheNote").textContent = `${data.cleared} صفحه پاک شد.`;
  } catch (error) { toast(error.message, true); }
});

/* --------------------------------------------------------------- اجرا */
function applyJob(job) {
  if (!job) return;
  state.jobStatus = job.status;
  const labels = {
    idle: "بی‌کار", running: "در حال اجرا", done: "تمام شد",
    failed: "شکست خورد", cancelled: "لغو شد",
  };
  const elapsed = job.elapsed ? ` — ${job.elapsed} ثانیه` : "";
  const text = (labels[job.status] || job.status) + elapsed + (job.error ? ` — ${job.error}` : "");
  $("#headStatus").textContent = text;
  for (const id of ["#runBtn", "#runBtn2"]) $(id).disabled = job.status === "running";
  for (const id of ["#cancelBtn", "#cancelBtn2"]) $(id).disabled = job.status !== "running";

  if (job.next !== undefined && job.next < state.logNext) {
    $("#log").textContent = "";
    state.logNext = 0;
  }
  if (job.lines?.length) {
    const box = $("#log");
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
    box.append(job.lines.join("\n") + "\n");
    state.logNext = job.next;
    if (atBottom) box.scrollTop = box.scrollHeight;
  } else if (job.next !== undefined) {
    state.logNext = job.next;
  }
}

async function startRun() {
  try {
    await save();
    $("#log").textContent = "";
    state.logNext = 0;
    applyJob(await api("/api/start", { method: "POST", body: {} }));
    toast("اجرا شروع شد.");
    document.querySelector('#tabs button[data-tab="run"]').click();
  } catch (error) { toast(error.message, true); }
}

for (const id of ["#runBtn", "#runBtn2"]) $(id).addEventListener("click", startRun);
for (const id of ["#cancelBtn", "#cancelBtn2"]) {
  $(id).addEventListener("click", async () => {
    try {
      await api("/api/cancel", { method: "POST", body: {} });
      toast("لغو ثبت شد؛ بعد از ردیف جاری متوقف می‌شود.");
    } catch (error) { toast(error.message, true); }
  });
}

$("#resetBtn").addEventListener("click", async () => {
  if (!confirm("همه‌ی ردیف‌ها دوباره در صف قرار بگیرند؟")) return;
  try {
    const data = await api("/api/reset", { method: "POST", body: {} });
    toast(`${data.reset} ردیف به صف برگشت.`);
    await refresh();
  } catch (error) { toast(error.message, true); }
});

/* ----------------------------------------------------------- ردیف‌ها */
const STATUS_LABELS = { done: "کامل", partial: "ناقص", pending: "در صف" };

async function loadRows() {
  try {
    const data = await api("/api/rows", { params: { status: $("#rowFilter").value } });
    const plan = data.plan || [];
    $("#rowsTable thead tr").replaceChildren(
      el("th", { textContent: "ردیف", style: "width:3rem" }),
      el("th", { textContent: "عنوان" }),
      ...plan.map((item) => el("th", { textContent: item.column })),
      el("th", { textContent: "وضعیت", style: "width:6rem" }),
      el("th", { textContent: "یادداشت" }),
    );
    $("#rowsTable tbody").replaceChildren(...(data.rows || []).map((row) => el("tr", {}, [
      el("td", { textContent: row.row_number || "—" }),
      el("td", { textContent: row.title }),
      ...plan.map((item) => {
        const value = row.values[item.key] || "";
        if (item.kind === "image" && value) {
          return el("td", {}, [el("a", {
            href: value, target: "_blank", rel: "noreferrer", textContent: "کاور",
          })]);
        }
        return el("td", { textContent: value.length > 90 ? `${value.slice(0, 90)}…` : value });
      }),
      el("td", {
        className: row.status === "done" ? "ok" : (row.status === "partial" ? "warnCell" : ""),
        textContent: STATUS_LABELS[row.status] || row.status,
      }),
      el("td", { className: "muted", textContent: row.note || "" }),
    ])));
    const counts = data.counts || {};
    $("#rowsNote").textContent =
      `${(data.rows || []).length} ردیف نمایش داده شد از ${counts.total ?? 0} ردیف`;
  } catch (error) { toast(error.message, true); }
}

$("#refreshRowsBtn").addEventListener("click", loadRows);
$("#rowFilter").addEventListener("change", loadRows);

/* --------------------------------------------------------- config.yaml */
async function loadConfig() {
  try {
    const data = await api("/api/config");
    $("#configText").value = data.text;
    $("#configText").disabled = !data.editable;
    $("#configSave").disabled = !data.editable;
  } catch (error) { toast(error.message, true); }
}

$("#configReload").addEventListener("click", loadConfig);
$("#configSave").addEventListener("click", async () => {
  try {
    const result = await api("/api/config", { method: "POST", body: { text: $("#configText").value } });
    toast(`ذخیره شد. پشتیبان: ${result.backup}`);
    await refresh();
  } catch (error) { toast(error.message, true); }
});

/* ------------------------------------------------------ کلمه‌ی کلیدی */
async function runKeyword() {
  try {
    const data = await api("/api/keyword", { params: { text: $("#keywordInput").value } });
    $("#keywordOut").replaceChildren(...[
      ["کلمه کلیدی", data.keyword],
      ["کلید تطبیق", data.match_key],
    ].map(([k, v]) => el("tr", {}, [el("td", { textContent: k }), el("td", { textContent: v })])));
  } catch (error) { toast(error.message, true); }
}
$("#keywordBtn").addEventListener("click", runKeyword);
$("#keywordInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") runKeyword();
});

/* --------------------------------------------------------------- پولینگ */
async function poll() {
  try {
    const job = await api("/api/job", { params: { since: state.logNext } });
    const wasRunning = state.jobStatus === "running";
    applyJob(job);
    if (wasRunning && job.status !== "running") {
      await refresh();
      if ($("#tab-rows").classList.contains("active")) loadRows();
      toast(job.status === "failed" ? `اجرا شکست خورد: ${job.error}` : "اجرا تمام شد.",
            job.status === "failed");
    }
  } catch { /* سرور بسته شده یا شبکه قطع است؛ پولینگ بعدی دوباره امتحان می‌کند */ }
  setTimeout(poll, state.jobStatus === "running" ? 1000 : 5000);
}

refresh().catch((error) => toast(error.message, true));
poll();
