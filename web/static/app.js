"use strict";

/* ------------------------------------------------------------------ توکن */
// توکن یک بار از URL خوانده و در sessionStorage نگه داشته می‌شود تا با رفرش
// از بین نرود و در نوار آدرس هم نماند.
const token = (() => {
  const fromUrl = new URLSearchParams(location.search).get("t");
  if (fromUrl) {
    sessionStorage.setItem("panelToken", fromUrl);
    history.replaceState({}, "", location.pathname);
    return fromUrl;
  }
  return sessionStorage.getItem("panelToken") || "";
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
  toastTimer = setTimeout(() => { box.hidden = true; }, bad ? 7000 : 3500);
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

/* ------------------------------------------------------------------ حالت */
const state = {
  runId: sessionStorage.getItem("runId") || "",
  logNext: 0,
  jobStatus: "idle",
  products: { filter: "all", q: "", offset: 0, limit: 50, total: 0 },
  mergeTarget: null,
  presets: [],
  categories: new Set(),
  sheets: new Set(),
  maxSites: 300,
};

/* -------------------------------------------------------------------- تب */
for (const button of document.querySelectorAll("#tabs button")) {
  button.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    button.classList.add("active");
    $(`#tab-${button.dataset.tab}`).classList.add("active");
    if (button.dataset.tab === "setup") loadSetup();
    if (button.dataset.tab === "products") loadProducts();
    if (button.dataset.tab === "review") loadReview();
    if (button.dataset.tab === "config") loadConfig();
  });
}

/* ----------------------------------------------------------------- شروع */
async function loadSetup() {
  try {
    if (!state.presets.length) {
      const data = await api("/api/presets");
      state.presets = data.presets;
      state.maxSites = data.max_sites;
      $("#siteLimit").textContent = `حداکثر ${data.max_sites} سایت در هر اجرا.`;
    }
    const sheets = await api("/api/sheets");
    state.sheets = new Set(sheets.selected);
    renderSheets(sheets.sheets);

    if (state.runId) {
      const plan = await api("/api/plan", { params: { run_id: state.runId } });
      state.categories = new Set(plan.categories);
      $("#maxProducts").value = plan.options.max_products_per_site ?? 0;
      $("#requireProduct").checked = plan.options.require_product_signals !== false;
      $("#forceJs").checked = Boolean(plan.options.js);
      $("#sitesText").value = (plan.sites.length ? plan.sites : plan.config_sites).join("\n");
      $("#planSource").textContent = plan.sites.length
        ? "سایت‌های همین اجرا"
        : (plan.config_sites.length ? "برگرفته از config — با ذخیره، مال این اجرا می‌شود" : "");
    }
    renderPresets();
    updateSiteCount();
  } catch (error) { toast(error.message, true); }
}

function renderPresets() {
  const known = new Set(state.presets.map((p) => p.label));
  const extra = [...state.categories].filter((label) => !known.has(label))
    .map((label) => ({ key: label, label, hint: "دسته‌ی دلخواه شما", examples: [] }));

  $("#presetList").replaceChildren(...[...state.presets, ...extra].map((preset) => {
    const box = el("input", { type: "checkbox", checked: state.categories.has(preset.label) });
    box.addEventListener("change", () => {
      if (box.checked) state.categories.add(preset.label);
      else state.categories.delete(preset.label);
      renderPresets();
      renderSummary();
    });
    const card = el("label", { className: `preset${state.categories.has(preset.label) ? " on" : ""}` }, [
      box,
      el("span", {}, [
        el("b", { textContent: preset.label }),
        el("small", { textContent: preset.hint }),
      ]),
    ]);
    return card;
  }));
  renderSummary();
}

function renderSheets(sheets) {
  $("#sheetList").replaceChildren(...sheets.map((sheet) => {
    const box = el("input", { type: "checkbox", checked: state.sheets.has(sheet.key) });
    box.addEventListener("change", async () => {
      if (box.checked) state.sheets.add(sheet.key);
      else state.sheets.delete(sheet.key);
      try {
        await api("/api/sheets", { method: "POST", body: { sheets: [...state.sheets] } });
      } catch (error) {
        toast(error.message, true);
        box.checked = !box.checked;
        if (box.checked) state.sheets.add(sheet.key); else state.sheets.delete(sheet.key);
      }
      renderSummary();
    });
    return el("label", {}, [box, `${sheet.title} — `, el("span", { className: "muted", textContent: sheet.hint })]);
  }));
}

function siteLines() {
  return $("#sitesText").value.split("\n").map((line) => line.trim()).filter(Boolean);
}

function updateSiteCount() {
  const count = siteLines().length;
  $("#siteCount").textContent = count ? `${count} خط وارد شده` : "هنوز سایتی وارد نشده";
  renderSummary();
}

function renderSummary() {
  const parts = [];
  parts.push(state.categories.size ? `دسته‌ها: ${[...state.categories].join("، ")}` : "هیچ دسته‌ای انتخاب نشده");
  parts.push(`${siteLines().length} سایت`);
  parts.push(`خروجی: ${state.sheets.size} شیت`);
  const cap = Number($("#maxProducts").value) || 0;
  parts.push(cap ? `سقف ${cap} محصول از هر منبع` : "بدون سقف");
  $("#setupSummary").textContent = parts.join("  |  ");
}

function planOptions() {
  return {
    max_products_per_site: Math.max(0, Number($("#maxProducts").value) || 0),
    require_product_signals: $("#requireProduct").checked,
    js: $("#forceJs").checked,
  };
}

$("#sitesText").addEventListener("input", updateSiteCount);
$("#maxProducts").addEventListener("input", renderSummary);

$("#addCategory").addEventListener("click", () => {
  const value = $("#customCategory").value.trim();
  if (!value) return;
  state.categories.add(value);
  $("#customCategory").value = "";
  renderPresets();
});
$("#customCategory").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); $("#addCategory").click(); }
});

$("#createRunBtn").addEventListener("click", async () => {
  if (!state.categories.size) return toast("حداقل یک نوع محصول را تیک بزنید.", true);
  if (!siteLines().length) return toast("حداقل یک سایت وارد کنید.", true);
  try {
    const created = await api("/api/runs", {
      method: "POST",
      body: {
        categories: [...state.categories],
        sites: $("#sitesText").value,
        options: planOptions(),
      },
    });
    state.runId = created.run_id;
    sessionStorage.setItem("runId", state.runId);
    toast(`اجرای تازه ساخته شد — ${created.sites} سایت، ${created.categories.length} دسته.`);
    await refreshState();
    await loadSetup();
  } catch (error) { toast(error.message, true); }
});

$("#savePlanBtn").addEventListener("click", async () => {
  if (!state.runId) return toast("اول یک اجرا بسازید.", true);
  try {
    const plan = await api("/api/plan", {
      method: "POST",
      body: {
        run_id: state.runId,
        categories: [...state.categories],
        sites: $("#sitesText").value,
        options: planOptions(),
      },
    });
    $("#sitesText").value = plan.sites.join("\n");
    $("#planSource").textContent = "سایت‌های همین اجرا";
    updateSiteCount();
    toast(`ذخیره شد — ${plan.sites.length} سایت.`);
    await refreshState();
  } catch (error) { toast(error.message, true); }
});

$("#goRunBtn").addEventListener("click", () => {
  document.querySelector('#tabs button[data-tab="run"]').click();
});

/* -------------------------------------------------------------- داشبورد */
const COUNTER_LABELS = {
  raw: "عنوان خام",
  relevant: "مرتبط",
  borderline: "مرزی (شیت C)",
  canonical: "محصول یکپارچه",
  needs_review: "نیازمند بازبینی",
  has_suggest: "دارای ساجست",
  no_suggest: "بدون ساجست",
  pending_suggest: "در صف ساجست",
  keywords: "کلمه‌ی کلیدی",
};

const STATUS_LABELS = {
  pending: "آماده", running: "در حال اجرا", completed: "تمام‌شده", failed: "شکست‌خورده",
};

async function refreshState() {
  const data = await api("/api/state", { params: { run_id: state.runId } });
  $("#topicLabel").textContent = data.target_topic ? `موضوع: ${data.target_topic}` : "";

  const select = $("#runSelect");
  select.replaceChildren(...data.runs.map((run) =>
    el("option", { value: run.run_id, textContent: `${run.target_topic} — ${run.run_id}` })));
  if (data.current) {
    state.runId = data.current.run_id;
    sessionStorage.setItem("runId", state.runId);
    select.value = state.runId;
  } else {
    state.runId = "";
    select.replaceChildren(el("option", { value: "", textContent: "— اجرایی نیست —" }));
  }

  if (data.phases.length && !$("#phaseChecks").childElementCount) {
    $("#phaseChecks").replaceChildren(...data.phases.map((phase) =>
      el("label", {}, [
        el("input", { type: "checkbox", value: phase.n, checked: true, className: "phase" }),
        `فاز ${phase.n} — ${phase.name}`,
      ])));
    $("#phaseChecks").addEventListener("change", updateSuggestWarning);
    updateSuggestWarning();
  }

  const counters = data.current?.counters || {};
  $("#counterCards").replaceChildren(...Object.entries(COUNTER_LABELS).map(([key, label]) =>
    el("div", { className: "card" }, [
      el("b", { textContent: counters[key] ?? "—" }),
      el("span", { textContent: label }),
    ])));

  const info = $("#runInfo");
  if (data.current) {
    info.replaceChildren(...[
      ["شناسه‌ی اجرا", data.current.run_id],
      ["موضوع هدف", data.current.target_topic],
      ["وضعیت", STATUS_LABELS[data.current.status] || data.current.status],
      ["آخرین فاز", String(data.current.current_phase)],
      ["ساخته‌شده در", data.current.created_at || "—"],
      ["دیتابیس", data.db_path],
    ].map(([k, v]) => el("tr", {}, [el("td", { textContent: k }), el("td", { textContent: v })])));
  } else {
    info.replaceChildren(el("tr", {}, [el("td", { colSpan: 2, textContent: "هنوز اجرایی ساخته نشده است." })]));
  }

  applyJob(data.job);
  loadOutputInfo();
}

async function loadOutputInfo() {
  const box = $("#downloadList");
  const note = $("#outputInfo");
  if (!state.runId) { box.replaceChildren(); note.textContent = "—"; return; }
  try {
    const info = await api("/api/output", { params: { run_id: state.runId } });
    const link = (key, format) =>
      `/download?run_id=${encodeURIComponent(state.runId)}&key=${key}`
      + `&format=${format}&t=${encodeURIComponent(token)}`;

    const rows = info.downloads.map((item) => el("div", { className: "dl" }, [
      el("b", { textContent: item.title }),
      el("span", { className: "muted", textContent: `${item.rows} ردیف — ${item.hint}` }),
      ...(info.xlsx ? [el("a", { href: link(item.key, "xlsx"), textContent: "اکسل" })] : []),
      el("a", { href: link(item.key, "csv"), textContent: "CSV" }),
    ]));

    if (info.xlsx) {
      rows.push(el("div", { className: "dl" }, [
        el("b", { textContent: "همه در یک فایل" }),
        el("span", { className: "muted", textContent: "هر فهرست یک شیت جدا" }),
        el("a", { href: link("workbook", "xlsx"), textContent: "اکسل" }),
      ]));
    }
    box.replaceChildren(...rows);
    note.textContent = info.xlsx
      ? ""
      : "openpyxl نصب نیست، پس فقط CSV در دسترس است: pip install openpyxl";
  } catch (error) {
    box.replaceChildren();
    note.textContent = error.message;
  }
}

/* ------------------------------------------------------------------ اجرا */
function selectedPhases() {
  return [...document.querySelectorAll("#phaseChecks .phase:checked")].map((i) => Number(i.value));
}

function updateSuggestWarning() {
  $("#suggestWarn").hidden = !selectedPhases().includes(3);
}

function applyJob(job) {
  state.jobStatus = job.status;
  const labels = {
    idle: "بی‌کار", running: "در حال اجرا", done: "تمام شد",
    failed: "شکست خورد", cancelled: "لغو شد",
  };
  const phase = job.phase ? ` — فاز ${job.phase} (${job.phase_name})` : "";
  const elapsed = job.elapsed ? ` — ${job.elapsed} ثانیه` : "";
  $("#jobStatus").textContent = (labels[job.status] || job.status) + phase + elapsed +
    (job.error ? ` — ${job.error}` : "");
  $("#startBtn").disabled = job.status === "running";
  $("#cancelBtn").disabled = job.status !== "running";

  if (job.next !== undefined && job.next < state.logNext) {
    $("#log").textContent = "";   // کار تازه‌ای شروع شده
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

$("#startBtn").addEventListener("click", async () => {
  const phases = selectedPhases();
  if (!phases.length) return toast("هیچ فازی انتخاب نشده است.", true);
  if (!state.runId) return toast("اول یک اجرای جدید بسازید.", true);
  try {
    $("#log").textContent = "";
    state.logNext = 0;
    applyJob(await api("/api/job", { method: "POST", body: { run_id: state.runId, phases } }));
    toast("اجرا شروع شد.");
  } catch (error) { toast(error.message, true); }
});

$("#cancelBtn").addEventListener("click", async () => {
  try {
    await api("/api/job/cancel", { method: "POST", body: {} });
    toast("لغو ثبت شد؛ بعد از پایان فاز جاری متوقف می‌شود.");
  } catch (error) { toast(error.message, true); }
});

/* ---------------------------------------------------------------- اجراها */
$("#runSelect").addEventListener("change", (event) => {
  state.runId = event.target.value;
  sessionStorage.setItem("runId", state.runId);
  refreshState().catch((error) => toast(error.message, true));
});

$("#newRunBtn").addEventListener("click", () => {
  // ساخت اجرا از تب «شروع» انجام می‌شود تا دسته و سایت‌ها با هم تعیین شوند
  document.querySelector('#tabs button[data-tab="setup"]').click();
  toast("نوع محصول و سایت‌ها را انتخاب کنید، بعد دکمه‌ی ساخت اجرا را بزنید.");
});

$("#deleteRunBtn").addEventListener("click", async () => {
  if (!state.runId) return;
  if (!confirm(`کل داده‌های اجرای ${state.runId} پاک شود؟ این کار برگشت ندارد.`)) return;
  try {
    await api("/api/runs/delete", { method: "POST", body: { run_id: state.runId } });
    state.runId = "";
    sessionStorage.removeItem("runId");
    toast("اجرا حذف شد.");
    await refreshState();
  } catch (error) { toast(error.message, true); }
});

/* -------------------------------------------------------------- محصولات */
const SUGGEST_TAG = {
  has_suggest: ["دارد", "ok"],
  no_suggest: ["ندارد", "no"],
};
const PENDING_HINT = "هنوز از گوگل پرسیده نشده — فاز ۳ را اجرا کنید";

async function loadProducts() {
  if (!state.runId) return;
  try {
    const data = await api("/api/products", {
      params: {
        run_id: state.runId, filter: state.products.filter, q: state.products.q,
        limit: state.products.limit, offset: state.products.offset,
      },
    });
    state.products.total = data.total;
    const from = data.total ? data.offset + 1 : 0;
    $("#prodCount").textContent = `${from}–${data.offset + data.items.length} از ${data.total}`;
    $("#prodPrev").disabled = data.offset === 0;
    $("#prodNext").disabled = data.offset + data.limit >= data.total;
    renderPager(data);

    $("#prodTable tbody").replaceChildren(...data.items.map(productRow));
    if (!data.items.length) {
      $("#prodTable tbody").replaceChildren(
        el("tr", {}, [el("td", { colSpan: 7, className: "muted", textContent: "چیزی پیدا نشد." })]));
    }
  } catch (error) { toast(error.message, true); }
}

function renderPager(data) {
  const pages = Math.ceil(data.total / data.limit) || 1;
  const current = Math.floor(data.offset / data.limit);
  const pager = $("#pager");
  if (pages < 2) { pager.replaceChildren(); return; }

  const buttons = [];
  const add = (index) => {
    const button = el("button", {
      className: index === current ? "primary" : "ghost",
      textContent: String(index + 1),
    });
    button.addEventListener("click", () => {
      state.products.offset = index * data.limit;
      loadProducts();
      const box = $("#prodTable").closest(".scrollbox");
      if (box) box.scrollTop = 0;
    });
    buttons.push(button);
  };
  // اول، آخر و چند صفحه دور صفحه‌ی جاری — با ۳۰۰ منبع فهرست صفحه‌ها طولانی می‌شود
  const shown = new Set([0, pages - 1, current, current - 1, current + 1, current - 2, current + 2]);
  let previous = -1;
  for (let index = 0; index < pages; index += 1) {
    if (!shown.has(index)) continue;
    if (index - previous > 1) buttons.push(el("span", { className: "muted", textContent: "…" }));
    add(index);
    previous = index;
  }
  pager.replaceChildren(
    el("span", { className: "muted", textContent: `صفحه ${current + 1} از ${pages}` }),
    ...buttons,
  );
}

function productRow(product) {
  const [tagText, tagClass] = SUGGEST_TAG[product.suggest_status] || ["در صف", "no"];
  const tagTitle = product.suggest_status ? "" : PENDING_HINT;
  const radio = el("input", {
    type: "radio", name: "mergeTarget", value: product.id,
    checked: state.mergeTarget?.id === product.id,
    title: "انتخاب به‌عنوان مقصد ادغام",
  });
  radio.addEventListener("change", () => {
    state.mergeTarget = product;
    renderMergeBar();
  });

  const title = el("a", { href: "#", textContent: product.title });
  title.addEventListener("click", (event) => { event.preventDefault(); showDetail(product.id); });

  const actions = el("td");
  if (state.mergeTarget && state.mergeTarget.id !== product.id) {
    const button = el("button", { className: "ghost", textContent: "ادغام در مقصد" });
    button.addEventListener("click", () => doMerge(state.mergeTarget.id, product));
    actions.append(button);
  }
  const rename = el("button", { className: "ghost", textContent: "تغییر نام" });
  rename.addEventListener("click", () => doRename(product));
  actions.append(rename);

  const row = el("tr", { className: product.needs_review ? "flagged" : "" }, [
    el("td", {}, [radio]),
    el("td", {}, [title, product.needs_review ? el("span", { className: "tag flag", textContent: "بازبینی" }) : ""]),
    el("td", { textContent: String(product.member_count) }),
    el("td", { textContent: String(product.keyword_count) }),
    el("td", {}, [
      el("span", { className: `tag ${tagClass}`, textContent: tagText, title: tagTitle }),
    ]),
    el("td", { className: "muted", textContent: product.domains.join("، ") }),
    actions,
  ]);
  return row;
}

function renderMergeBar() {
  const bar = $("#mergeBar");
  if (!state.mergeTarget) { bar.hidden = true; return; }
  bar.hidden = false;
  const clear = el("button", { className: "ghost", textContent: "انصراف" });
  clear.addEventListener("click", () => {
    state.mergeTarget = null;
    document.querySelectorAll('input[name="mergeTarget"]').forEach((r) => { r.checked = false; });
    renderMergeBar();
  });
  bar.replaceChildren(
    el("span", { textContent: `مقصد ادغام: «${state.mergeTarget.title}» — حالا روی «ادغام در مقصد» ردیف دیگری بزنید.` }),
    clear,
  );
}

async function doMerge(targetId, source) {
  if (!confirm(`«${source.title}» در مقصد ادغام شود؟ برای برگشت باید عنوان‌ها را دستی جدا کنید.`)) return;
  try {
    const result = await api("/api/products/merge", {
      method: "POST", body: { target_id: targetId, source_id: source.id },
    });
    toast(`ادغام شد؛ ${result.moved} عنوان منتقل شد.`);
    state.mergeTarget = null;
    renderMergeBar();
    await loadProducts();
    await refreshState();
  } catch (error) { toast(error.message, true); }
}

async function doRename(product) {
  const title = prompt("عنوان تازه:", product.title);
  if (title === null) return;
  try {
    await api("/api/products/rename", { method: "POST", body: { id: product.id, title } });
    toast("عنوان عوض شد.");
    await loadProducts();
  } catch (error) { toast(error.message, true); }
}

async function showDetail(id) {
  try {
    const product = await api("/api/product", { params: { id } });
    $("#detailPanel").hidden = false;
    $("#detailTitle").textContent = product.title;
    const body = $("#detailBody");
    body.replaceChildren();

    const meta = el("div", { className: "muted" }, [
      `کوئری‌هایی که از گوگل پرسیده می‌شود: ${product.suggest_queries.map((q) => `«${q}»`).join("، ")}`,
      el("br"),
      `اطمینان ادغام: ${product.merge_confidence ?? "—"}`,
      product.entity_tokens.length ? ` | توکن‌های تمایزدهنده: ${product.entity_tokens.join("، ")}` : "",
    ]);
    const flagBtn = el("button", {
      className: "ghost",
      textContent: product.needs_review ? "برداشتن علامت بازبینی" : "علامت‌زدن برای بازبینی",
    });
    flagBtn.addEventListener("click", async () => {
      await api("/api/products/review", {
        method: "POST", body: { id: product.id, flag: !product.needs_review },
      });
      await showDetail(id);
      await loadProducts();
      await refreshState();
    });
    body.append(meta, el("div", { className: "row" }, [flagBtn]));

    body.append(el("h3", { textContent: `عنوان‌های خام (${product.members.length})` }));
    for (const member of product.members) {
      const detach = el("button", { className: "ghost", textContent: "جدا کن" });
      detach.addEventListener("click", async () => {
        try {
          await api("/api/products/detach", { method: "POST", body: { raw_id: member.raw_id } });
          toast("عنوان جدا شد و محصول مستقل گرفت.");
          await showDetail(id);
          await loadProducts();
          await refreshState();
        } catch (error) { toast(error.message, true); }
      });
      body.append(el("div", { className: "member" }, [
        el("div", { className: "grow" }, [
          el("div", { textContent: member.raw_title }),
          el("div", { className: "muted", textContent: `${member.domain} | امتیاز موضوعی: ${member.topic_score ?? "—"}` }),
          el("a", { href: member.url, target: "_blank", rel: "noopener", textContent: member.url }),
        ]),
        detach,
      ]));
    }

    body.append(el("h3", { textContent: `کلمات ساجست (${product.keywords.length})` }));
    if (!product.keywords.length) {
      body.append(el("div", { className: "muted", textContent: "—" }));
    }
    for (const keyword of product.keywords) {
      const source = product.keyword_sources[keyword];
      body.append(el("div", { className: "member" }, [
        el("div", { className: "grow" }, [
          el("div", { textContent: keyword }),
          source ? el("div", { className: "muted", textContent: `از کوئری: ${source}` }) : "",
        ]),
      ]));
    }
  } catch (error) { toast(error.message, true); }
}

$("#prodFilter").addEventListener("change", (event) => {
  state.products.filter = event.target.value;
  state.products.offset = 0;
  loadProducts();
});
let searchTimer = 0;
$("#prodSearch").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.products.q = event.target.value;
    state.products.offset = 0;
    loadProducts();
  }, 300);
});
$("#prodPrev").addEventListener("click", () => {
  state.products.offset = Math.max(0, state.products.offset - state.products.limit);
  loadProducts();
});
$("#prodNext").addEventListener("click", () => {
  state.products.offset += state.products.limit;
  loadProducts();
});

/* -------------------------------------------------------------- بازبینی */
async function loadReview() {
  if (!state.runId) return;
  try {
    const data = await api("/api/review", { params: { run_id: state.runId } });
    const list = $("#reviewList");
    list.replaceChildren();
    if (!data.products.length) {
      list.append(el("div", { className: "muted", textContent: "چیزی برای بازبینی نمانده است." }));
    }
    for (const product of data.products) {
      const open = el("button", { className: "ghost", textContent: "جزئیات و جدا کردن" });
      open.addEventListener("click", () => {
        document.querySelector('#tabs button[data-tab="products"]').click();
        showDetail(product.id);
      });
      const accept = el("button", { className: "ghost", textContent: "تأیید (برداشتن علامت)" });
      accept.addEventListener("click", async () => {
        await api("/api/products/review", { method: "POST", body: { id: product.id, flag: false } });
        toast("علامت برداشته شد.");
        await loadReview();
        await refreshState();
      });
      list.append(el("div", { className: "review-item" }, [
        el("div", {}, [el("strong", { textContent: product.title })]),
        el("div", { className: "muted", textContent: product.members.map((m) => m.raw_title).join("  |  ") }),
        el("div", { className: "row" }, [open, accept]),
      ]));
    }

    const borderline = $("#borderlineList");
    borderline.replaceChildren();
    if (!data.borderline.length) {
      borderline.append(el("div", { className: "muted", textContent: "عنوان مرزی‌ای ثبت نشده است." }));
    }
    for (const item of data.borderline.slice(0, 200)) {
      borderline.append(el("div", { className: "member" }, [
        el("div", { className: "grow" }, [
          el("div", { textContent: item.raw_title }),
          el("div", { className: "muted", textContent: `${item.domain} | امتیاز موضوعی: ${item.topic_score ?? "—"}` }),
        ]),
      ]));
    }
  } catch (error) { toast(error.message, true); }
}

/* -------------------------------------------------------------- تنظیمات */
async function loadConfig() {
  try {
    const data = await api("/api/config");
    $("#configPath").textContent = data.path || "پنل بدون فایل config اجرا شده است.";
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
    await refreshState();
  } catch (error) { toast(error.message, true); }
});

/* ----------------------------------------------------------------- ابزار */
async function runNormalize() {
  try {
    const data = await api("/api/normalize", { params: { text: $("#normInput").value } });
    $("#normOut").replaceChildren(...[
      ["نمایشی", data.display],
      ["تطبیق", data.match],
      ["توکن‌ها", data.tokens.join("، ")],
    ].map(([k, v]) => el("tr", {}, [el("td", { textContent: k }), el("td", { textContent: v })])));
  } catch (error) { toast(error.message, true); }
}
$("#normBtn").addEventListener("click", runNormalize);
$("#normInput").addEventListener("keydown", (event) => { if (event.key === "Enter") runNormalize(); });

/* --------------------------------------------------------------- پولینگ */
async function poll() {
  try {
    const job = await api("/api/job", { params: { since: state.logNext } });
    const wasRunning = state.jobStatus === "running";
    applyJob(job);
    if (wasRunning && job.status !== "running") {
      await refreshState();
      if ($("#tab-products").classList.contains("active")) loadProducts();
      toast(job.status === "failed" ? `اجرا شکست خورد: ${job.error}` : "اجرا تمام شد.",
            job.status === "failed");
    }
  } catch { /* سرور بسته شده یا شبکه قطع است؛ پولینگ بعدی دوباره امتحان می‌کند */ }
  setTimeout(poll, state.jobStatus === "running" ? 1000 : 4000);
}

refreshState()
  .then(loadSetup)
  .catch((error) => toast(error.message, true));
poll();
