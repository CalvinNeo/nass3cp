"use strict";

(() => {
  const start = document.getElementById("download-selected");
  if (!start) return;
  const clear = document.getElementById("selection-clear");
  const count = document.getElementById("selection-count");
  const error = document.getElementById("download-error");
  const panel = document.getElementById("downloads");
  const summary = document.getElementById("download-summary");
  const rows = document.getElementById("download-rows");
  const terminal = new Set(["complete", "cancelled", "failed", "expired"]);
  const rowViews = new Map();
  const selectionKey = "nass3cp-file-selection";
  const requestedKey = "nass3cp-requested-downloads";
  const automaticKey = "nass3cp-automatic-downloads";
  let selected = new Set();
  let requested = new Set();
  let automatic = new Set();
  let timer;
  let generation = 0;
  let submitting = false;
  let pollError = "";

  function restore(key, valid) {
    try {
      const value = JSON.parse(sessionStorage.getItem(key));
      return new Set(Array.isArray(value) ? value.filter(valid).slice(0, 1000) : []);
    } catch (_) { return new Set(); }
  }
  function persist(key, value) {
    try { sessionStorage.setItem(key, JSON.stringify([...value])); } catch (_) { /* Optional persistence. */ }
  }
  function showError(message, reveal = true) {
    error.textContent = message;
    error.hidden = !message;
    document.dispatchEvent(new Event("nass3cp:transfers-changed"));
    if (message && reveal) document.dispatchEvent(new Event("nass3cp:show-transfers"));
  }
  function pathOf(box) {
    try { return JSON.parse(box.dataset.filePath); } catch (_) { return null; }
  }
  function visibleBoxes() {
    const container = document.getElementById("directory-view").hidden ?
      document.getElementById("search-results") : document.getElementById("directory-view");
    return [...container.querySelectorAll(".file-select")];
  }
  function refreshSelection() {
    for (const box of document.querySelectorAll(".file-select")) {
      box.checked = selected.has(pathOf(box));
      box.closest("tr").classList.toggle("is-selected", box.checked);
    }
    const visible = visibleBoxes();
    const checked = visible.filter(box => box.checked).length;
    for (const box of document.querySelectorAll(".select-all")) {
      box.checked = visible.length > 0 && checked === visible.length;
      box.indeterminate = checked > 0 && checked < visible.length;
      box.disabled = !visible.length;
    }
    count.textContent = selected.size + (selected.size === 1 ? " file selected" : " files selected");
    document.getElementById("selection-bar").hidden = !selected.size;
    start.disabled = submitting || !selected.size;
    clear.disabled = submitting || !selected.size;
    persist(selectionKey, selected);
  }
  function select(path, checked) {
    if (typeof path !== "string" || !path) return;
    if (!checked) selected.delete(path);
    else if (selected.size < 1000) selected.add(path);
    else showError("Select up to 1,000 files at a time.");
  }
  document.addEventListener("change", event => {
    const box = event.target;
    if (box.matches(".file-select")) select(pathOf(box), box.checked);
    else if (box.matches(".select-all")) {
      for (const file of visibleBoxes()) select(pathOf(file), box.checked);
    } else return;
    refreshSelection();
  });
  document.addEventListener("nass3cp:files-changed", refreshSelection);
  clear.addEventListener("click", () => { selected.clear(); refreshSelection(); showError(""); });

  async function request(url, body) {
    const response = await fetch(url, {
      method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
      headers: body === undefined ? {} : {"Content-Type": "application/json", "X-Nass3cp-Request": "download"},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("Unable to reach the local browser service."); }
    if (!response.ok) throw new Error(data.error || "Download request failed.");
    if (!Array.isArray(data.items)) throw new Error("Invalid download response.");
    return data;
  }
  function bytes(value) {
    if (value === null || value === undefined) return "—";
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return (unit ? value.toFixed(1) : value) + " " + units[unit];
  }
  function element(tag, className, parent) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    parent.appendChild(node);
    return node;
  }
  function createRow(item) {
    const row = element("tr", "", rows);
    const file = element("td", "name", row);
    const name = element("bdi", "", file);
    const path = element("div", "download-path", file);
    const status = element("td", "download-status", row);
    const phase = element("span", "", status);
    const problem = element("div", "download-problem", status);
    const meter = element("td", "download-meter", row);
    const bar = element("progress", "", meter);
    bar.max = 100;
    bar.setAttribute("aria-label", "Download progress for " + item.name);
    const detail = element("span", "download-bytes", meter);
    const actions = element("td", "download-actions", row);
    const save = element("a", "button", actions);
    save.textContent = "Save file";
    save.href = "/api/downloads/" + item.id + "/file";
    save.download = item.name;
    save.addEventListener("click", () => { requested.add(item.id); persist(requestedKey, requested); });
    const cancel = element("button", "secondary", actions);
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.setAttribute("aria-label", "Cancel download of " + item.name);
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try { await mutate("/api/downloads/" + item.id + "/cancel", {}); }
      catch (problem) { showError(problem.message); cancel.disabled = false; }
    });
    const retry = element("button", "secondary", actions);
    retry.type = "button";
    retry.textContent = "Retry";
    retry.setAttribute("aria-label", "Retry download of " + item.name);
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      try { await mutate("/api/downloads", {paths: [item.path]}); }
      catch (problem) { showError(problem.message); }
      finally { retry.disabled = false; }
    });
    return {row, name, path, phase, problem, bar, detail, save, cancel, retry};
  }
  function render(data) {
    panel.hidden = !data.items.length;
    const live = data.items.filter(item => !terminal.has(item.status));
    const queued = live.filter(item => item.status === "queued").length;
    panel.dataset.pending = live.length;
    panel.dataset.attention = data.items.some(item => item.status === "failed" || item.status === "expired");
    document.dispatchEvent(new Event("nass3cp:transfers-changed"));
    summary.textContent = live.length + " pending · " + queued + " queued · File concurrency: " + data.concurrency;
    const ids = new Set(data.items.map(item => item.id));
    for (const [id, view] of rowViews) {
      if (!ids.has(id)) { view.row.remove(); rowViews.delete(id); }
    }
    requested = new Set([...requested].filter(id => ids.has(id)));
    automatic = new Set([...automatic].filter(id => ids.has(id)));
    for (const item of data.items) {
      if (!/^[0-9a-f]{32}$/.test(item.id)) continue;
      let view = rowViews.get(item.id);
      if (!view) { view = createRow(item); rowViews.set(item.id, view); }
      view.name.textContent = item.name;
      view.path.textContent = item.path;
      view.phase.textContent = item.phase;
      view.problem.textContent = item.error || "";
      view.problem.hidden = !item.error;
      const delivered = item.status === "sending" || item.status === "complete";
      const amount = delivered ? item.sent : item.received;
      const measured = item.size !== null && item.status !== "queued" && item.status !== "preparing";
      const percent = item.size ? Math.min(100, Math.floor(amount / item.size * 100)) :
        (["ready", "sending", "complete"].includes(item.status) ? 100 : 0);
      if (measured || terminal.has(item.status)) view.bar.value = percent;
      else view.bar.removeAttribute("value");
      view.detail.textContent = measured ? percent + "% · " + bytes(amount) + " / " + bytes(item.size) : bytes(item.size);
      if (item.status === "downloading" && item.bytes_per_second > 0) view.detail.textContent += " · " + bytes(item.bytes_per_second) + "/s";
      view.save.hidden = item.status !== "ready";
      view.cancel.hidden = terminal.has(item.status);
      view.cancel.disabled = item.status === "cancelling";
      view.retry.hidden = !["failed", "cancelled", "expired"].includes(item.status);
      // The native browser handles saving and its own partial file. Never buffer
      // large downloads in a JavaScript Blob. A blocked automatic save can be retried manually.
      if (item.status === "ready" && !item.error && automatic.has(item.id) && !requested.has(item.id)) {
        requested.add(item.id);
        persist(requestedKey, requested);
        view.save.click();
      }
    }
    persist(requestedKey, requested);
    persist(automaticKey, automatic);
  }
  async function poll(version) {
    try {
      const data = await request("/api/downloads");
      if (version !== generation) return;
      render(data);
      if (pollError && error.textContent === pollError) showError("");
      pollError = "";
    } catch (problem) {
      if (version === generation) {
        pollError = problem.message + " Retrying progress…";
        showError(pollError, false);
      }
    } finally {
      if (version === generation) timer = setTimeout(() => poll(version), document.hidden ? 3000 : 1000);
    }
  }
  async function mutate(url, body) {
    clearTimeout(timer);
    const version = ++generation;
    try {
      const data = await request(url, body);
      // Only the tab that queued a file automatically saves it. Other tabs can
      // still show progress or save manually without racing duplicate downloads.
      for (const id of data.enqueued_ids || []) if (/^[0-9a-f]{32}$/.test(id)) automatic.add(id);
      persist(automaticKey, automatic);
      if (version === generation) { showError(""); render(data); }
      return data;
    } finally {
      if (version === generation) timer = setTimeout(() => poll(version), 300);
    }
  }
  start.addEventListener("click", async () => {
    if (!selected.size || submitting) return;
    submitting = true;
    const paths = [...selected];
    refreshSelection();
    try {
      await mutate("/api/downloads", {paths});
      for (const path of paths) selected.delete(path);
      document.dispatchEvent(new Event("nass3cp:show-transfers"));
      panel.scrollIntoView({block: "nearest", behavior: "smooth"});
    } catch (problem) { showError(problem.message); }
    finally { submitting = false; refreshSelection(); }
  });

  selected = restore(selectionKey, value => typeof value === "string" && value.length > 0 && value.length <= 8192);
  requested = restore(requestedKey, value => typeof value === "string" && /^[0-9a-f]{32}$/.test(value));
  automatic = restore(automaticKey, value => typeof value === "string" && /^[0-9a-f]{32}$/.test(value));
  refreshSelection();
  poll(generation);
})();
