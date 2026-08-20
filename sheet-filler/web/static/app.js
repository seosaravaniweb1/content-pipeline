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

const state = { logNext: 0, jobStatus: "idle", fields: [], kinds: {} };

/* -------------------------------------------------------------------- تب */
for (const button of document.querySelectorAll("#tabs button")) {
  button.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    button.classList.add("active");
    $(`#tab-${button.dataset.tab}`).classList.add("active");
    if (button.dataset.tab === "rows") loadRows();
  });
}

/* ------------------------------------------------------------ فرم تنظیمات */
// فرم از فهرستی که سرور می‌دهد ساخته می‌شود (core/settings.py). افزودن یک
// تنظیم تازه یعنی یک خط پایتون، نه دست زدن به این فایل.
function renderFields(fields, values) {
  state.fields = fields;
  const build = (field) => {
    const id = `f_${field.key}`;
    let control;
    if (field.kind === "bool") {
      control = el("input", { type: "checkbox", id, checked: Boolean(values[field.key]) });
    } else if (field.kind === "number") {
      control = el("input", { type: "number", id, min: 0, value: values[field.key] ?? 0 });
    } else if (field.kind === "choice") {
      control = el("select", { id }, field.choices.map((choice) =>
        el("option", { value: choice.value, textContent: choice.label,
                       selected: values[field.key] === choice.value })));
    } else if (field.kind === "lines") {
      control = el("textarea", {
        id, dir: "ltr", spellcheck: false,
        value: (values[field.key] || []).join("\n"),
        placeholder: "https://shop1.ir\nhttps://shop2.ir",
      });
    } else {
      control = el("input", { type: "text", id, value: values[field.key] ?? "" });
    }
    return el("tr", {}, [
      el("td", {}, [el("label", { htmlFor: id, textContent: field.label })]),
      el("td", {}, field.hint
        ? [control, el("div", { className: "muted hint", textContent: field.hint })]
        : [control]),
    ]);
  };

  $("#basicFields").replaceChildren(...fields.filter((f) => !f.advanced).map(build));
  $("#advancedFields").replaceChildren(...fields.filter((f) => f.advanced).map(build));
}

function formValues() {
  const body = {};
  for (const field of state.fields) {
    const node = $(`#f_${field.key}`);
    if (!node) continue;
    body[field.key] = field.kind === "bool" ? node.checked : node.value;
  }
  return body;
}

/* ------------------------------------------------------------------ حالت */
function applyState(data) {
  if (data.fields) renderFields(data.fields, data.values || {});
  state.kinds = data.kinds || {};

  renderPlan((data.plan || []).map((item) => ({
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

  const values = data.values || {};
  $("#sheetLabel").textContent = values.sheet_url
    ? (values.tab ? `شیت / ${values.tab}` : "گوگل‌شیت")
    : (values.file || "هنوز شیتی تنظیم نشده");
  $("#autoNote").textContent = values.auto
    ? `حالت خودکار روشن است: هر ${values.auto_every_minutes} دقیقه یک‌بار شیت دوباره خوانده می‌شود.`
    : "حالت خودکار خاموش است — هر بار خودتان «اجرا» را می‌زنید.";
  $("#linksNote").textContent = data.links ? `${data.links} آدرس ذخیره شده` : "";

  if (!data.ready) $("#headStatus").textContent = "شیت تنظیم نشده";
  else if (values.sheet_url && !data.gspread) {
    $("#headStatus").textContent = "gspread نصب نیست: pip install gspread";
  }

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

async function save() {
  const data = await api("/api/settings", { method: "POST", body: formValues() });
  applyState(data);
  return data;
}

$("#saveBtn").addEventListener("click", async () => {
  try { await save(); toast("ذخیره شد."); }
  catch (error) { toast(error.message, true); }
});

$("#inspectBtn").addEventListener("click", async () => {
  $("#inspectNote").textContent = "در حال خواندن شیت…";
  try {
    await save();
    const data = await api("/api/inspect", { method: "POST", body: formValues() });
    const lists = Object.entries(data.lists || {})
      .map(([name, count]) => `${name} (${count})`).join("، ");
    $("#inspectNote").textContent =
      `${data.rows} ردیف — ${data.plan.length} ستون` + (lists ? ` — لیست‌ها: ${lists}` : "");
    renderPlan(data.plan);
  } catch (error) {
    $("#inspectNote").textContent = "";
    toast(error.message, true);
  }
});

/* ------------------------------------------------------------------ منابع */
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
    toast(`${data.cleared} صفحه از کش پاک شد.`);
  } catch (error) { toast(error.message, true); }
});

/* --------------------------------------------------------------- اجرا */
function applyJob(job) {
  if (!job) return;
  state.jobStatus = job.status;
  const labels = {
    idle: "بی‌کار", running: job.auto ? "در حال اجرا (خودکار)" : "در حال اجرا",
    done: "تمام شد", failed: "شکست خورد", cancelled: "متوقف شد",
  };
  const elapsed = job.elapsed ? ` — ${job.elapsed} ثانیه` : "";
  $("#headStatus").textContent =
    (labels[job.status] || job.status) + elapsed + (job.error ? ` — ${job.error}` : "");
  for (const id of ["#runBtn", "#runBtn2", "#tryBtn"]) $(id).disabled = job.status === "running";
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

async function startRun(extra = {}) {
  try {
    await save();
    $("#log").textContent = "";
    state.logNext = 0;
    applyJob(await api("/api/start", { method: "POST", body: extra }));
    toast("اجرا شروع شد.");
    document.querySelector('#tabs button[data-tab="run"]').click();
  } catch (error) { toast(error.message, true); }
}

for (const id of ["#runBtn", "#runBtn2"]) $(id).addEventListener("click", () => startRun());
$("#tryBtn").addEventListener("click", () => startRun({ once: true, limit: 20 }));
for (const id of ["#cancelBtn", "#cancelBtn2"]) {
  $(id).addEventListener("click", async () => {
    try {
      await api("/api/cancel", { method: "POST", body: {} });
      toast("توقف ثبت شد؛ بعد از ردیف جاری متوقف می‌شود.");
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
