const state = {
  settingsLoaded: false,
};

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[index]}`;
}

function fileName(path) {
  if (!path) return "-";
  return path.split(/[\\/]/).pop();
}

function fillSettings(settings) {
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(settings)) {
    const input = form.elements[key];
    if (!input) continue;
    if (input.type === "checkbox") {
      input.checked = Boolean(value);
    } else {
      input.value = value ?? "";
    }
  }
  state.settingsLoaded = true;
}

function collectSettings() {
  const form = $("#settings-form");
  const patch = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.type === "checkbox") {
      patch[input.name] = input.checked;
    } else if (input.type === "number") {
      patch[input.name] = Number(input.value);
    } else {
      patch[input.name] = input.value;
    }
  }
  return patch;
}

function renderJobs(jobs) {
  const body = $("#jobs");
  body.innerHTML = jobs
    .map((job) => {
      const statusClass = `status-${job.status}`;
      const progress = Number(job.progress_percent || 0).toFixed(1);
      return `
        <tr>
          <td>${job.id}</td>
          <td title="${job.source_path || ""}">${fileName(job.source_path)}</td>
          <td class="${statusClass}">${job.status}</td>
          <td>${job.source_codec || "-"}</td>
          <td>${formatBytes(job.source_size_bytes)} -> ${formatBytes(job.output_size_bytes)}</td>
          <td>${progress}%</td>
          <td>${job.fps ?? "-"}</td>
          <td>${job.speed || "-"}</td>
          <td>${job.error_message || ""}</td>
          <td>
            <button data-job-action="retry" data-id="${job.id}">Retry</button>
            <button data-job-action="skip" data-id="${job.id}">Skip</button>
            <button data-job-action="move-to-failed" data-id="${job.id}">Quarantine</button>
          </td>
        </tr>
      `;
    })
    .join("");
}

function renderProgress(data) {
  const worker = data.worker;
  const stats = data.stats;
  const current = data.current;
  const running = worker.thread_alive && worker.worker_enabled;
  const paused = !worker.auto_convert_enabled;
  $("#service-status").textContent = running ? (paused ? "Paused" : "Running") : "Stopped";
  $("#current-file").textContent = current ? fileName(current.source_path) : "-";
  $("#current-progress").textContent = current ? `${Number(current.progress_percent || 0).toFixed(1)}%` : "0%";
  $("#queue-count").textContent = `${stats.processed_jobs} / ${stats.total_jobs}`;
  $("#saved").textContent = `${formatBytes(stats.saved_bytes)} (${stats.saved_percent}%)`;
  $("#errors").textContent = stats.failed_jobs;
  if (!state.settingsLoaded) fillSettings(data.settings);
}

async function refresh() {
  try {
    const [progress, jobs, logs] = await Promise.all([
      api("/api/progress"),
      api("/api/jobs?limit=100"),
      api("/api/logs?lines=120"),
    ]);
    renderProgress(progress);
    renderJobs(jobs);
    $("#logs").textContent = logs;
  } catch (error) {
    $("#service-status").textContent = "Error";
    console.error(error);
  }
}

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    await api(`/api/worker/${button.dataset.action}`, { method: "POST" });
    await refresh();
  });
});

$("#scan-now").addEventListener("click", async () => {
  await api("/api/scan", { method: "POST" });
  await refresh();
});

$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await api("/api/settings", {
    method: "PUT",
    body: JSON.stringify(collectSettings()),
  });
  state.settingsLoaded = false;
  await refresh();
});

$("#jobs").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-job-action]");
  if (!button) return;
  await api(`/api/jobs/${button.dataset.id}/${button.dataset.jobAction}`, { method: "POST" });
  await refresh();
});

refresh();
setInterval(refresh, 1500);
