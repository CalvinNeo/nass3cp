"use strict";

(() => {
  const form = document.getElementById("search-form");
  if (!form) return;
  const start = document.getElementById("search-start");
  const cancel = document.getElementById("search-cancel");
  const close = document.getElementById("search-close");
  const progress = document.getElementById("search-progress");
  const bar = document.getElementById("search-bar");
  const status = document.getElementById("search-status");
  const counts = document.getElementById("search-counts");
  const percent = document.getElementById("search-percent");
  const scope = document.getElementById("search-scope");
  const traversal = document.getElementById("search-traversal");
  const estimate = document.getElementById("search-estimate");
  const current = document.getElementById("search-current");
  const error = document.getElementById("search-error");
  const results = document.getElementById("search-results");
  const rows = document.getElementById("search-rows");
  const empty = document.getElementById("search-empty");
  const directory = document.getElementById("directory-view");
  const active = new Set(["starting", "running", "cancelling"]);
  let job = null;
  let cursor = 0;
  let timer = null;
  let generation = 0;
  const storageKey = "nass3cp-file-search";

  function remember(value) {
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
    } catch (_) { /* Searching also works when session storage is disabled. */ }
  }

  async function request(url, body) {
    const response = await fetch(url, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: body === undefined ? {} : {"Content-Type": "application/json", "X-Nass3cp-Request": "search"},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const raw = await response.text();
    let data;
    try { data = JSON.parse(raw); } catch (_) { data = {error: raw || "Unable to reach the local browser service."}; }
    if (!response.ok) {
      const problem = new Error(typeof data.error === "string" ? data.error : "Search request failed.");
      problem.status = response.status;
      throw problem;
    }
    if (!Array.isArray(data.results)) throw new Error("Invalid search response from NAS.");
    return data;
  }

  function showError(message) {
    error.textContent = message;
    error.hidden = !message;
  }

  function size(bytes) {
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let value = bytes, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return (unit ? value.toFixed(1) : value) + " " + units[unit];
  }

  function date(value) {
    if (value === null || value === undefined) return "N/A";
    const parsed = new Date(value / 1000000);
    return Number.isNaN(parsed.getTime()) ? "N/A" : parsed.toLocaleString("en-GB", {hour12: false});
  }

  function addResults(items) {
    for (const item of items) {
      const row = document.createElement("tr");
      const name = row.insertCell();
      name.className = "name";
      const title = document.createElement("bdi");
      title.textContent = item.name;
      name.appendChild(title);
      const link = document.createElement("a");
      link.href = "/?" + new URLSearchParams({path: item.parent_path, cursor: "0"});
      link.className = "result-location";
      link.textContent = item.relative_path + " · Open folder";
      name.appendChild(link);
      const bytes = row.insertCell();
      bytes.className = "size";
      bytes.textContent = size(item.size);
      bytes.title = item.size.toLocaleString("en-US") + " bytes";
      row.insertCell().textContent = date(item.birthtime_ns);
      row.insertCell().textContent = date(item.mtime_ns);
      rows.appendChild(row);
    }
    cursor += items.length;
    empty.hidden = cursor > 0;
  }

  function render(data) {
    const labels = {starting: "Starting search…", running: "Searching…", cancelling: "Cancelling…",
      completed: "Search completed", cancelled: "Search cancelled", limited: "Result limit reached", failed: "Search failed"};
    status.textContent = labels[data.status] || data.status;
    const running = active.has(data.status);
    counts.textContent = data.scanned_entries.toLocaleString("en-US") + " scanned · " +
      data.results_count + " matches · " + data.directories_completed + "/" + data.directories_discovered +
      " folders done · " + data.elapsed_seconds + " s · Limit " + data.rate_limit + "/s";
    if (data.skipped_entries) counts.textContent += " · " + data.skipped_entries + " skipped";
    const excluded = data.exclude_dirs || [];
    scope.textContent = excluded.length ? "Excluded folders: " + excluded.join(", ") +
      " · " + (data.excluded_directories || 0) + " skipped" : "";
    scope.hidden = !excluded.length;
    traversal.textContent = (data.queued_directories || 0) + " folders queued. " +
      (data.skipped_entries || 0) + " entries skipped (unreadable, links, too deep or unsupported).";
    if (data.depth_first_directories) traversal.textContent += " " + data.depth_first_directories +
      " folders used depth-first scanning to keep the queue bounded; no folders were dropped because of the queue limit.";
    if (data.estimated_percent === null) {
      bar.removeAttribute("value");
      percent.textContent = "—";
      estimate.textContent = running ? "Discovering folders. The total size of the search is not known yet." :
        "Search stopped before enough folders were scanned to estimate progress.";
    } else {
      bar.value = data.estimated_percent;
      percent.textContent = (data.status === "completed" ? "" : "~") + data.estimated_percent + "%";
      estimate.textContent = data.status === "completed" ? "Finished scanning accessible files." :
        "Estimated progress: " + data.estimated_percent + "%. Based on observed folder sizes; may change as more folders are discovered.";
    }
    current.textContent = data.current_path ? (running ? "Scanning: " : "Last scanned: ") + data.current_path : "";
    showError(data.error || "");
    start.disabled = running;
    cancel.disabled = !running || data.status === "cancelling";
    if (!running && data.estimated_percent === null) bar.value = 0;
    empty.textContent = running ? "Waiting for results…" : "No matching files found.";
  }

  async function poll(version) {
    if (!job || version !== generation) return;
    try {
      const data = await request("/api/searches/" + job + "?cursor=" + cursor);
      if (version !== generation) return;
      addResults(data.results);
      render(data);
      if (data.next_cursor !== null || active.has(data.status)) {
        timer = setTimeout(() => poll(version), data.next_cursor !== null ? 100 : 1000);
      }
    } catch (problem) {
      if (version !== generation) return;
      if (problem.status >= 400 && problem.status < 500) {
        showError(problem.message);
        status.textContent = "Search is no longer available";
        start.disabled = false;
        cancel.disabled = true;
        bar.value = 0;
        job = null;
        remember(null);
        return;
      }
      showError(problem.message + " Retrying progress in 3 seconds…");
      timer = setTimeout(() => poll(version), 3000);
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (start.disabled) return;
    clearTimeout(timer);
    const version = ++generation;
    job = null;
    remember(null);
    cursor = 0;
    rows.textContent = "";
    empty.hidden = false;
    empty.textContent = "Waiting for results…";
    progress.hidden = results.hidden = false;
    directory.hidden = true;
    start.disabled = true;
    cancel.disabled = true;
    close.disabled = true;
    status.textContent = "Starting search…";
    counts.textContent = estimate.textContent = current.textContent = "";
    percent.textContent = "—";
    scope.hidden = true;
    scope.textContent = traversal.textContent = "";
    bar.removeAttribute("value");
    showError("");
    try {
      const data = await request("/api/searches", {
        path: form.dataset.path.replace(/^nas:/, ""),
        pattern: document.getElementById("search-pattern").value,
        regex: document.getElementById("search-regex").checked,
        case_sensitive: document.getElementById("search-case").checked,
      });
      job = data.id;
      remember({id: job, path: form.dataset.path, pattern: data.pattern,
        regex: data.regex, case_sensitive: data.case_sensitive});
      addResults(data.results);
      render(data);
      timer = setTimeout(() => poll(version), 500);
    } catch (problem) {
      status.textContent = "Unable to start search";
      showError(problem.message);
      start.disabled = false;
      bar.value = 0;
      empty.textContent = "No search results.";
    } finally {
      close.disabled = false;
    }
  });

  cancel.addEventListener("click", async () => {
    if (!job) return;
    const version = generation;
    cancel.disabled = true;
    try {
      const data = await request("/api/searches/" + job + "/cancel", {});
      if (version === generation) {
        // Invalidate an in-flight poll before scheduling the final refresh.
        const nextVersion = ++generation;
        render(data);
        clearTimeout(timer);
        timer = setTimeout(() => poll(nextVersion), 100);
      }
    } catch (problem) {
      if (version === generation) { showError(problem.message); cancel.disabled = false; }
    }
  });

  close.addEventListener("click", async () => {
    close.disabled = true;
    try {
      if (job && start.disabled) await request("/api/searches/" + job + "/cancel", {});
      ++generation;
      clearTimeout(timer);
      job = null;
      remember(null);
      progress.hidden = results.hidden = true;
      directory.hidden = false;
      start.disabled = false;
      cancel.disabled = true;
    } catch (problem) {
      showError(problem.message);
    } finally {
      close.disabled = false;
    }
  });

  // Restore a same-folder search after refresh without starting a second disk scan.
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey));
    if (saved && saved.path === form.dataset.path && /^[0-9a-f]{32}$/.test(saved.id)) {
      job = saved.id;
      document.getElementById("search-pattern").value = saved.pattern;
      document.getElementById("search-regex").checked = saved.regex;
      document.getElementById("search-case").checked = saved.case_sensitive;
      progress.hidden = results.hidden = false;
      directory.hidden = true;
      start.disabled = true;
      cancel.disabled = false;
      status.textContent = "Restoring search…";
      poll(++generation);
    }
  } catch (_) { /* A missing or expired saved search can be started again. */ }
})();
