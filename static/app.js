const form = document.getElementById("book-form");
const submitBtn = document.getElementById("submit-btn");
const progressPanel = document.getElementById("progress-panel");
const progressStep = document.getElementById("progress-step");
const progressFill = document.getElementById("progress-fill");
const progressPct = document.getElementById("progress-pct");
const progressError = document.getElementById("progress-error");
const resultPanel = document.getElementById("result-panel");
const pdfLink = document.getElementById("pdf-link");
const thumbnails = document.getElementById("thumbnails");

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  submitBtn.disabled = true;
  progressPanel.classList.remove("hidden");
  resultPanel.classList.add("hidden");
  progressError.classList.add("hidden");
  progressStep.textContent = "Starting…";
  progressFill.style.width = "0%";
  progressPct.textContent = "0%";

  const formData = new FormData(form);

  try {
    const res = await fetch("/api/books/generate", { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.error || "Failed to start generation");
    }

    listenToJob(data.job_id);
  } catch (err) {
    showError(err.message);
  }
});

function listenToJob(jobId) {
  const source = new EventSource(`/api/books/stream/${jobId}`);

  source.onmessage = (event) => {
    const data = JSON.parse(event.data);

    if (data.error) {
      showError(data.error);
      source.close();
      return;
    }

    if (data.step) {
      progressStep.textContent = data.step;
    }
    if (typeof data.pct === "number") {
      progressFill.style.width = `${data.pct}%`;
      progressPct.textContent = `${data.pct}%`;
    }

    if (data.done) {
      source.close();
      submitBtn.disabled = false;
      if (data.pdf_url) {
        onComplete(data.pdf_url, data.book_id);
      }
    }
  };

  source.onerror = () => {
    source.close();
    submitBtn.disabled = false;
  };
}

async function onComplete(pdfUrl, bookId) {
  progressFill.style.width = "100%";
  progressPct.textContent = "100%";
  resultPanel.classList.remove("hidden");
  pdfLink.href = pdfUrl;
  thumbnails.innerHTML = "";

  if (bookId) {
    try {
      const res = await fetch(`/api/books/${bookId}`);
      const book = await res.json();
      const preview = (book.pages || []).slice(0, 3);
      for (const page of preview) {
        const img = document.createElement("img");
        img.src = page.s3_url;
        img.alt = `Page ${page.page_num}`;
        thumbnails.appendChild(img);
      }
    } catch (err) {
      // thumbnails are a nice-to-have; ignore failures
    }
  }
}

function showError(message) {
  progressError.textContent = message;
  progressError.classList.remove("hidden");
  submitBtn.disabled = false;
}

const tabButtons = document.querySelectorAll(".tab-btn");
const tabPanels = document.querySelectorAll(".tab-panel");

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    tabButtons.forEach((b) => b.classList.remove("active"));
    tabPanels.forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "settings") {
      loadSettingsStatus();
    }
  });
});

const settingsForm = document.getElementById("settings-form");
const settingsSubmitBtn = document.getElementById("settings-submit-btn");
const settingsSavedMsg = document.getElementById("settings-saved-msg");

async function loadSettingsStatus() {
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    for (const [key, info] of Object.entries(data)) {
      const statusEl = document.querySelector(`.setting-status[data-key="${key}"]`);
      const inputEl = document.getElementById(key);
      if (!statusEl) continue;

      statusEl.classList.remove("configured", "env", "unconfigured");
      if (info.configured && info.source === "settings") {
        statusEl.textContent = `Configured (${info.masked})`;
        statusEl.classList.add("configured");
      } else if (info.configured && info.source === "env") {
        statusEl.textContent = `Configured via environment (${info.masked})`;
        statusEl.classList.add("env");
      } else {
        statusEl.textContent = "Not configured";
        statusEl.classList.add("unconfigured");
      }

      if (inputEl && info.masked) {
        inputEl.placeholder = info.masked;
      }
    }
  } catch (err) {
    // status display is best-effort
  }
}

settingsForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  settingsSubmitBtn.disabled = true;
  settingsSavedMsg.classList.add("hidden");

  const payload = {};
  for (const el of settingsForm.querySelectorAll("input[name]")) {
    if (el.value.trim()) {
      payload[el.name] = el.value.trim();
    }
  }

  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("Failed to save settings");

    for (const el of settingsForm.querySelectorAll("input[name]")) {
      el.value = "";
    }
    settingsSavedMsg.textContent = "Saved.";
    settingsSavedMsg.classList.remove("hidden", "error");
    await loadSettingsStatus();
  } catch (err) {
    settingsSavedMsg.textContent = err.message;
    settingsSavedMsg.classList.remove("hidden");
    settingsSavedMsg.classList.add("error");
  } finally {
    settingsSubmitBtn.disabled = false;
  }
});
