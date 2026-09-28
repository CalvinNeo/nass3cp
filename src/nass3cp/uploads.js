"use strict";

(() => {
  const form = document.getElementById("upload-form");
  if (!form) return;
  const chooser = document.getElementById("upload-files");
  const start = document.getElementById("upload-start");
  const selection = document.getElementById("upload-selection");
  const error = document.getElementById("upload-error");
  const panel = document.getElementById("uploads");
  const summary = document.getElementById("upload-summary");
  const rows = document.getElementById("upload-rows");
  const folder = form.dataset.path.replace(/^nas:/, "") || ".";
  const terminal = new Set(["complete", "failed", "cancelled"]);
  const views = new Map();
  const localFiles = new Map();
  const cancelling = new Set();
  let state = {items: [], busy: false};
  let sending = null;
  let submitting = false;
  let timer;
  let generation = 0;
  let pollError = "";
  document.getElementById("upload-refresh").href = "/?path=" + encodeURIComponent(folder);

  function showError(message, reveal = true) {
    error.textContent = message;
    error.hidden = !message;
    document.dispatchEvent(new Event("nass3cp:transfers-changed"));
    if (message && reveal) document.dispatchEvent(new Event("nass3cp:show-transfers"));
  }
  function bytes(value) {
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
    return (index ? value.toFixed(1) : value) + " " + units[index];
  }
  function updateSelection() {
    const files = [...chooser.files];
    start.disabled = chooser.disabled || submitting || !files.length;
    selection.textContent = files.length ? files.length + " local file(s) · " +
      bytes(files.reduce((sum, file) => sum + file.size, 0)) : "No local files selected";
  }
  async function request(url, body) {
    const response = await fetch(url, {
      method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
      headers: body === undefined ? {} : {"Content-Type": "application/json", "X-Nass3cp-Request": "upload"},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error("Unable to reach the local browser service."); }
    if (!response.ok) throw new Error(data.error || "Upload request failed.");
    if (!Array.isArray(data.items)) throw new Error("Invalid upload response.");
    return data;
  }
  function element(tag, className, parent) {
    const node = document.createElement(tag);
    node.className = className;
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
    bar.setAttribute("aria-label", "Upload progress for " + item.name);
    const detail = element("span", "download-bytes", meter);
    const actions = element("td", "download-actions", row);
    const cancel = element("button", "secondary", actions);
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.setAttribute("aria-label", "Cancel upload of " + item.name);
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      cancelling.add(item.id);
      try {
        await request("/api/uploads/" + item.id + "/cancel", {});
        if (sending && sending.id === item.id) sending.xhr.abort();
        localFiles.delete(item.id);
        refresh();
      } catch (problem) { showError(problem.message); cancel.disabled = false; }
      finally { cancelling.delete(item.id); }
    });
    const retry = element("button", "secondary", actions);
    retry.type = "button";
    retry.textContent = "Retry";
    retry.addEventListener("click", async () => {
      const file = localFiles.get(item.id);
      if (!file) {
        document.getElementById("transfers-dialog").close();
        document.getElementById("upload-tools").open = true;
        chooser.click();
        return;
      }
      retry.disabled = true;
      try {
        await queue([file], item.path.slice(0, item.path.lastIndexOf("/")) || "/", item.transport);
        localFiles.delete(item.id);
      } catch (problem) { showError(problem.message); }
      finally { retry.disabled = false; }
    });
    return {row, name, path, phase, problem, bar, detail, cancel, retry};
  }
  function render(data) {
    state = data;
    panel.hidden = !data.items.length;
    const pending = data.items.filter(item => !terminal.has(item.status)).length;
    const completed = data.items.filter(item => item.status === "complete").length;
    panel.dataset.pending = pending;
    panel.dataset.attention = data.items.some(item => item.status === "failed");
    document.dispatchEvent(new Event("nass3cp:transfers-changed"));
    summary.textContent = pending + " pending · " + completed + " uploaded · One file at a time";
    const ids = new Set(data.items.map(item => item.id));
    for (const [id, view] of views) {
      if (!ids.has(id)) { view.row.remove(); views.delete(id); localFiles.delete(id); }
    }
    for (const item of data.items) {
      if (!/^[0-9a-f]{32}$/.test(item.id)) continue;
      let view = views.get(item.id);
      if (!view) { view = createRow(item); views.set(item.id, view); }
      view.name.textContent = item.name;
      view.path.textContent = item.path;
      view.phase.textContent = item.phase + (item.transport === "nathole" ? " · nathole" : "");
      view.problem.textContent = item.error || (item.status === "waiting" && !localFiles.has(item.id) ?
        "Choose this local file again to upload it." : "");
      view.problem.hidden = !view.problem.textContent;
      const local = item.status === "receiving";
      const amount = local ? item.received : item.sent;
      const percent = item.size ? Math.min(100, Math.floor(amount / item.size * 100)) :
        (item.status === "complete" ? 100 : 0);
      view.bar.value = percent;
      view.detail.textContent = percent + "% · " + bytes(amount) + " / " + bytes(item.size);
      if (local) view.detail.textContent += " · Local copy";
      if (item.status === "uploading" && item.bytes_per_second > 0) {
        view.detail.textContent += " · " + bytes(item.bytes_per_second) + "/s";
      }
      if (item.status === "verifying") view.bar.removeAttribute("value");
      view.cancel.hidden = terminal.has(item.status);
      view.cancel.disabled = item.status === "cancelling" || cancelling.has(item.id);
      view.retry.hidden = !["failed", "cancelled"].includes(item.status);
      view.retry.textContent = localFiles.has(item.id) ? "Retry" : "Choose again";
      if (item.status === "complete") localFiles.delete(item.id);
    }
    pump();
  }
  async function queue(files, destination, selectedTransport) {
    const choice = document.getElementById("upload-transport");
    const transport = selectedTransport || (choice ? choice.value : "s3");
    const data = await request("/api/uploads", {path: destination, transport, files: files.map(file => ({
      name: file.name, size: file.size, mtime_ms: Math.max(0, file.lastModified),
    }))});
    if (!Array.isArray(data.enqueued_ids) || data.enqueued_ids.length !== files.length) {
      throw new Error("Invalid upload queue response.");
    }
    data.enqueued_ids.forEach((id, index) => localFiles.set(id, files[index]));
    showError("");
    refresh();
  }
  function pump() {
    if (sending || state.busy) return;
    const item = state.items.find(value => value.status === "waiting" && localFiles.has(value.id) && !cancelling.has(value.id));
    if (!item) return;
    const xhr = new XMLHttpRequest();
    sending = {id: item.id, xhr};
    xhr.open("PUT", "/api/uploads/" + item.id + "/file");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-Nass3cp-Request", "upload");
    // Sending a File streams it through the browser without reading it into JS memory.
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return;
      if (xhr.status === 409) return; // Another tab may have claimed the single slot.
      let message = "Local file transfer failed. Select the file again to retry.";
      try { message = JSON.parse(xhr.responseText).error || message; } catch (_) { /* Plain HTTP error. */ }
      showError(message);
      localFiles.delete(item.id);
    };
    xhr.onerror = () => {
      if (!cancelling.has(item.id)) showError("Local file transfer was interrupted. Select the file again to retry.");
      localFiles.delete(item.id);
    };
    xhr.onloadend = () => { sending = null; refresh(); };
    xhr.send(localFiles.get(item.id));
  }
  async function poll(version) {
    try {
      const data = await request("/api/uploads");
      if (version !== generation) return;
      if (pollError && error.textContent === pollError) showError("");
      pollError = "";
      render(data);
    } catch (problem) {
      if (version === generation) {
        pollError = problem.message + " Retrying progress…";
        showError(pollError, false);
      }
    } finally {
      if (version === generation) timer = setTimeout(() => poll(version), document.hidden ? 3000 : 1000);
    }
  }
  function refresh() {
    clearTimeout(timer);
    const version = ++generation;
    timer = setTimeout(() => poll(version), 100);
  }
  chooser.addEventListener("change", updateSelection);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (submitting || !chooser.files.length) return;
    submitting = true;
    updateSelection();
    try {
      await queue([...chooser.files], folder);
      chooser.value = "";
      document.getElementById("upload-tools").open = false;
      document.dispatchEvent(new Event("nass3cp:show-transfers"));
    } catch (problem) { showError(problem.message); }
    finally { submitting = false; updateSelection(); }
  });
  window.addEventListener("beforeunload", event => {
    if (sending || state.items.some(item => item.status === "waiting" && localFiles.has(item.id))) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  updateSelection();
  poll(generation);
})();
