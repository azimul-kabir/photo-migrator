"use strict";

const ACTION_LABELS = {
  "library-index": "Indexing library",
  "library-resume": "Finishing library fingerprints",
  "import-scan": "Scanning sources",
  "import-plan": "Creating import plan",
  "import-dry-run": "Dry run",
  "import-run": "Importing",
};
const PAGE_SIZE = 50;

const ui = {
  token: readToken(),
  state: null,
  planId: null,
  group: "new",
  offset: 0,
  lastJobKey: null,
  timer: null,
};

function $(id) {
  return document.getElementById(id);
}

function readToken() {
  const match = /(?:^#|&)token=([^&]+)/.exec(window.location.hash);
  if (match) {
    const token = decodeURIComponent(match[1]);
    try { sessionStorage.setItem("photo-migrator-token", token); } catch (_) { /* optional */ }
    history.replaceState(null, "", window.location.pathname);
    return token;
  }
  try { return sessionStorage.getItem("photo-migrator-token"); } catch (_) { return null; }
}

async function api(path, body) {
  const options = { headers: { Authorization: `Bearer ${ui.token || ""}` } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    $("auth-error").hidden = false;
    throw new Error("unauthorized");
  }
  if (!response.ok) throw new Error(payload.error || `request failed (${response.status})`);
  return payload;
}

function showError(error) {
  const banner = $("request-error");
  if (!error || error.message === "unauthorized") {
    banner.hidden = true;
    return;
  }
  banner.textContent = error.message;
  banner.hidden = false;
}

function formatBytes(value) {
  if (value === null || value === undefined) return "–";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let amount = Number(value);
  let unit = 0;
  while (Math.abs(amount) >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${amount} B` : `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[unit]}`;
}

function formatCount(value) {
  return Number(value || 0).toLocaleString();
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "calculating";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m`;
  return "<1m";
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function renderStats(target, entries) {
  target.replaceChildren(
    ...entries.map(([label, value, tone]) => {
      const wrapper = element("div");
      wrapper.append(element("dt", label), element("dd", value, tone));
      return wrapper;
    }),
  );
}

function render(state) {
  ui.state = state;
  $("connection").textContent = "connected";
  $("connection").classList.add("online");
  $("paths").textContent = `Database ${state.database} · Config ${state.config_path}`;
  renderConfig(state);
  renderLibrary(state);
  renderJob(state.job);
  renderPlans(state);
  const busy = Boolean(state.job && state.job.status === "running");
  const configured = Boolean(state.config);
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.disabled = busy || !configured;
  });
  renderImport(state, busy);
}

function renderConfig(state) {
  const summary = $("config-summary");
  const error = $("config-error");
  $("config-form").hidden = state.config_exists;
  $("config-path").textContent = state.config_path;
  error.hidden = !state.config_error;
  error.textContent = state.config_error || "";
  summary.replaceChildren();
  $("step-setup").classList.toggle("done", Boolean(state.config));
  if (!state.config) return;
  const config = state.config;
  const list = element("dl", null, "facts");
  [
    ["Clean library", config.library_root],
    ["New imports go to", config.default_directory + (config.preserve_source_subdirectories ? "/<source folders>" : "")],
  ].forEach(([label, value]) => list.append(element("dt", label), element("dd", value, "mono")));
  const sources = element("ul", null, "sources");
  config.sources.forEach((source) => {
    const item = element("li");
    item.append(element("strong", source.name), ` (priority ${source.priority}) `, element("span", source.path, "mono muted"));
    sources.append(item);
  });
  summary.append(list, sources);
}

function renderLibrary(state) {
  const library = state.library;
  const remaining = library.assets - library.hashed;
  renderStats($("library-stats"), [
    ["Files in library", formatCount(library.assets)],
    ["Size", formatBytes(library.bytes)],
    ["Fingerprinted", formatCount(library.hashed), remaining ? "warn" : "ok"],
    ["Unreadable", formatCount(library.failed), library.failed ? "bad" : ""],
  ]);
  $("step-index").classList.toggle("done", library.assets > 0 && remaining === 0);
  const candidates = state.candidates;
  renderStats($("candidate-stats"), [
    ["Files in sources", formatCount(candidates.assets)],
    ["Size", formatBytes(candidates.bytes)],
    ["Sources scanned", formatCount(candidates.sources)],
  ]);
  $("step-scan").classList.toggle("done", candidates.assets > 0);
}

function renderJob(job) {
  const card = $("job");
  if (!job) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const label = ACTION_LABELS[job.action] || job.action;
  const running = job.status === "running";
  const titles = {
    running: job.cancel_requested ? `${label}: stopping at a safe point…` : `${label}…`,
    succeeded: `${label}: finished`,
    failed: `${label}: failed`,
    cancelled: `${label}: stopped (completed work is saved; run it again to resume)`,
  };
  $("job-title").textContent = titles[job.status] || label;
  $("job-cancel").hidden = !running;
  $("job-cancel").disabled = job.cancel_requested;
  const progress = job.progress;
  const bar = $("job-progress");
  if (progress && progress.total_bytes) {
    bar.max = progress.total_bytes;
    bar.value = Math.min(progress.completed_bytes, progress.total_bytes);
  } else if (progress && progress.total_items) {
    bar.max = progress.total_items;
    bar.value = Math.min(progress.completed_items, progress.total_items);
  } else if (running) {
    bar.removeAttribute("value");
  } else {
    bar.max = 1;
    bar.value = 1;
  }
  bar.hidden = !running && !progress;
  if (progress) {
    const parts = [
      progress.phase_name,
      `${formatCount(progress.completed_items)} of ${formatCount(progress.total_items)} files`,
    ];
    if (progress.total_bytes) {
      parts.push(`${formatBytes(progress.completed_bytes)} of ${formatBytes(progress.total_bytes)}`);
      parts.push(`${formatBytes(Math.round(progress.bytes_per_second))}/s`);
      if (running) parts.push(`about ${formatDuration(progress.eta_seconds)} left`);
    }
    if (progress.failed) parts.push(`${formatCount(progress.failed)} failed`);
    $("job-message").textContent = parts.join(" · ");
    $("job-current").textContent = running && progress.current_item ? progress.current_item : "";
  } else {
    $("job-message").textContent = running ? "Starting…" : "";
    $("job-current").textContent = "";
  }
  const result = $("job-result");
  result.className = "";
  if (job.status === "failed") {
    result.textContent = job.error;
    result.className = "bad";
  } else if (job.status === "succeeded") {
    result.textContent = describeResult(job);
    result.className = job.result && job.result.errors ? "warn" : "ok";
  } else {
    result.textContent = "";
  }
  const log = $("job-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = job.logs.join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;

  const key = `${job.id}:${job.status}`;
  if (ui.lastJobKey && ui.lastJobKey !== key && !running) onJobFinished(job);
  ui.lastJobKey = key;
}

function describeResult(job) {
  const result = job.result || {};
  if (result.plan_id) return `Plan ${result.plan_id} is ready to review below.`;
  if (result.run_id && job.action === "import-dry-run") return "Dry run complete. Review it, then import.";
  if (result.run_id) return "Import complete. See the summary below.";
  if (result.errors) return `Finished with ${formatCount(result.errors)} unreadable files; see the log.`;
  if (result.indexed !== undefined) return `${formatCount(result.indexed)} files found.`;
  return "Done.";
}

function onJobFinished(job) {
  if (job.status === "succeeded" && job.result && job.result.plan_id) {
    ui.planId = job.result.plan_id;
    ui.group = "new";
    ui.offset = 0;
  }
  loadItems();
}

function renderPlans(state) {
  const select = $("plan-select");
  const plans = state.plans;
  if (ui.planId === null && plans.length) ui.planId = plans[0].id;
  select.replaceChildren(
    ...plans.map((plan) => {
      const option = element("option", `#${plan.id} · ${plan.created_at.slice(0, 16).replace("T", " ")} · ${plan.status}`);
      option.value = String(plan.id);
      option.selected = plan.id === ui.planId;
      return option;
    }),
  );
  select.disabled = !plans.length;
  const plan = currentPlan();
  $("plan-detail").hidden = !plan;
  $("step-plan").classList.toggle("done", Boolean(plan && plan.status === "ready"));
  if (!plan) return;
  renderStats($("plan-stats"), [
    ["New files", formatCount(plan.new)],
    ["Data to copy", formatBytes(plan.new_bytes)],
    ["Already in library", formatCount(plan.existing)],
    ["Duplicates in sources", formatCount(plan.internal_duplicates)],
    ["Needs review", formatCount(plan.review), plan.review ? "warn" : ""],
    ["Space saved", formatBytes(plan.bytes_avoided)],
  ]);
  const counts = { new: plan.new, existing: plan.existing, internal: plan.internal_duplicates, review: plan.review };
  document.querySelectorAll("#plan-tabs button").forEach((tab) => {
    const group = tab.dataset.group;
    tab.setAttribute("aria-selected", String(group === ui.group));
    if (!tab.dataset.label) tab.dataset.label = tab.textContent;
    tab.textContent = `${tab.dataset.label} (${formatCount(counts[group])})`;
  });
}

function currentPlan() {
  if (!ui.state) return null;
  return ui.state.plans.find((plan) => plan.id === ui.planId) || null;
}

function renderImport(state, busy) {
  const plan = currentPlan();
  const status = $("import-status");
  const dryRunDone = Boolean(plan && ["completed", "completed_with_errors"].includes(plan.dry_run_status));
  const imported = Boolean(plan && plan.import_status === "completed");
  $("dry-run").disabled = busy || !plan || plan.status !== "ready";
  $("import").disabled = busy || !plan || plan.status !== "ready" || !dryRunDone || imported;
  status.className = "";
  if (!plan) {
    status.textContent = "Create a plan first.";
  } else if (plan.status === "planning" && busy) {
    status.textContent = `Plan ${plan.id} is being created…`;
  } else if (plan.status !== "ready") {
    status.textContent = `Plan ${plan.id} is ${plan.status}. Create a new plan to continue.`;
    status.className = "warn";
  } else if (imported) {
    const run = plan.import_run || {};
    status.textContent = `Imported: ${formatCount(run.copied_count)} copied, ${formatCount(run.reused_count)} already present, ${formatCount(run.failed_count)} failed (${formatBytes(run.bytes_written)} written).`;
    status.className = "ok";
  } else if (plan.import_status) {
    const run = plan.import_run || {};
    status.textContent = `Import ${plan.import_status.replaceAll("_", " ")}: ${formatCount(run.copied_count)} copied, ${formatCount(run.failed_count)} failed. Import again to retry or resume.`;
    status.className = "warn";
  } else if (dryRunDone) {
    status.textContent = `Dry run finished for plan ${plan.id}. Ready to import ${formatCount(plan.new)} files (${formatBytes(plan.new_bytes)}).`;
  } else {
    status.textContent = `Run a dry run of plan ${plan.id} before importing.`;
  }
  $("step-import").classList.toggle("done", imported);
}

async function loadItems() {
  const plan = currentPlan();
  if (!plan) return;
  try {
    const page = await api(`/api/plans/${plan.id}/items?group=${ui.group}&offset=${ui.offset}&limit=${PAGE_SIZE}`);
    renderItems(page);
    showError(null);
  } catch (error) {
    showError(error);
  }
}

function renderItems(page) {
  $("target-heading").textContent = { new: "Goes to", existing: "Already at", internal: "Same as", review: "Why" }[page.group];
  const rows = page.items.map((item) => {
    const row = element("tr");
    const source = element("td");
    source.append(element("span", item.candidate_path, "mono"), element("small", item.source));
    let target;
    if (page.group === "new") target = element("td", item.destination, "mono");
    else if (page.group === "review") target = element("td", item.reason);
    else {
      target = element("td");
      target.append(element("span", item.matching_path, "mono"), element("small", item.reason));
    }
    row.append(source, target, element("td", formatBytes(item.size_bytes), "num"));
    return row;
  });
  if (!rows.length) {
    const empty = element("tr");
    const cell = element("td", "Nothing in this group.", "muted");
    cell.colSpan = 3;
    empty.append(cell);
    rows.push(empty);
  }
  $("plan-items").replaceChildren(...rows);
  const last = Math.min(page.offset + page.limit, page.total);
  $("page-info").textContent = page.total ? `${formatCount(page.offset + 1)}–${formatCount(last)} of ${formatCount(page.total)}` : "";
  $("page-prev").disabled = page.offset === 0;
  $("page-next").disabled = last >= page.total;
  document.querySelector(".pager").hidden = page.total <= page.limit;
}

async function refresh() {
  try {
    render(await api("/api/state"));
    showError(null);
  } catch (error) {
    $("connection").textContent = "offline";
    $("connection").classList.remove("online");
    showError(error);
  }
  clearTimeout(ui.timer);
  const running = ui.state && ui.state.job && ui.state.job.status === "running";
  ui.timer = setTimeout(refresh, running ? 1000 : 5000);
}

async function startJob(action, extra) {
  try {
    await api("/api/jobs", { action, ...extra });
    showError(null);
  } catch (error) {
    showError(error);
  }
  refresh();
}

function addSourceRow() {
  const row = $("source-row").content.firstElementChild.cloneNode(true);
  row.querySelector(".remove").addEventListener("click", () => {
    if ($("source-rows").children.length > 1) row.remove();
  });
  $("source-rows").append(row);
}

async function submitConfig(event) {
  event.preventDefault();
  const form = event.target;
  const sources = [...$("source-rows").children].map((row) => ({
    name: row.querySelector('[name="name"]').value,
    path: row.querySelector('[name="path"]').value,
    priority: Number.parseInt(row.querySelector('[name="priority"]').value, 10),
  }));
  try {
    await api("/api/config", {
      library_root: form.library_root.value,
      default_directory: form.default_directory.value,
      preserve_source_subdirectories: form.preserve_source_subdirectories.checked,
      sources,
    });
    showError(null);
  } catch (error) {
    showError(error);
  }
  refresh();
}

function openConfirm() {
  const plan = currentPlan();
  if (!plan) return;
  $("confirm-summary").textContent = `Copy ${formatCount(plan.new)} files (${formatBytes(plan.new_bytes)}) into ${ui.state.config.library_root}. Existing library files and your sources are not modified.`;
  $("confirm-check").checked = false;
  $("confirm-ok").disabled = true;
  $("confirm").showModal();
}

function bind() {
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => startJob(button.dataset.action));
  });
  $("job-cancel").addEventListener("click", async () => {
    try { await api("/api/jobs/cancel", {}); } catch (error) { showError(error); }
    refresh();
  });
  $("plan-select").addEventListener("change", (event) => {
    ui.planId = Number(event.target.value);
    ui.offset = 0;
    if (ui.state) render(ui.state);
    loadItems();
  });
  document.querySelectorAll("#plan-tabs button").forEach((tab) => {
    tab.addEventListener("click", () => {
      ui.group = tab.dataset.group;
      ui.offset = 0;
      if (ui.state) render(ui.state);
      loadItems();
    });
  });
  $("page-prev").addEventListener("click", () => {
    ui.offset = Math.max(0, ui.offset - PAGE_SIZE);
    loadItems();
  });
  $("page-next").addEventListener("click", () => {
    ui.offset += PAGE_SIZE;
    loadItems();
  });
  $("dry-run").addEventListener("click", () => startJob("import-dry-run", { plan_id: ui.planId }));
  $("import").addEventListener("click", openConfirm);
  $("confirm-check").addEventListener("change", (event) => {
    $("confirm-ok").disabled = !event.target.checked;
  });
  $("confirm").addEventListener("close", () => {
    if ($("confirm").returnValue === "ok" && $("confirm-check").checked) {
      startJob("import-run", { plan_id: ui.planId, confirm: true });
    }
  });
  $("add-source").addEventListener("click", addSourceRow);
  $("config-form").addEventListener("submit", submitConfig);
  addSourceRow();
}

bind();
if (!ui.token) $("auth-error").hidden = false;
refresh().then(loadItems);
