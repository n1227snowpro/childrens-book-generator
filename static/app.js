const form = document.getElementById("book-form");
const submitBtn = document.getElementById("submit-btn");
const progressPanel = document.getElementById("progress-panel");
const progressStep = document.getElementById("progress-step");
const progressFill = document.getElementById("progress-fill");
const progressPct = document.getElementById("progress-pct");
const progressError = document.getElementById("progress-error");
const resultPanel = document.getElementById("result-panel");
const resultWarning = document.getElementById("result-warning");
const pdfLink = document.getElementById("pdf-link");
const coverLink = document.getElementById("cover-link");
const coverDimensionsEl = document.getElementById("cover-dimensions");
const thumbnails = document.getElementById("thumbnails");
const imageModelSelect = document.getElementById("image_model");
const imageModelNote = document.getElementById("image_model_note");
const pageCountInput = document.getElementById("page_count");
const costEstimate = document.getElementById("cost-estimate");

let imageModels = {};

async function loadImageModels() {
  try {
    const res = await fetch("/api/image-models");
    const data = await res.json();
    imageModels = data.models || {};

    imageModelSelect.innerHTML = "";
    for (const [id, info] of Object.entries(imageModels)) {
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = `${info.label} (${info.provider}) — $${info.price_per_image.toFixed(3)}/image`;
      if (id === data.default) opt.selected = true;
      imageModelSelect.appendChild(opt);
    }
    updateImageModelNote();
  } catch (err) {
    imageModelNote.textContent = "Could not load model list.";
  }
}

function updateImageModelNote() {
  const info = imageModels[imageModelSelect.value];
  imageModelNote.textContent = info ? info.note : "";
  updateCostEstimate();
}

function updateCostEstimate() {
  const info = imageModels[imageModelSelect.value];
  const pages = parseInt(pageCountInput.value, 10) || 0;
  if (!info || !pages) {
    costEstimate.textContent = "";
    return;
  }
  const estimatedImages = pages + 2; // pages plus ~2 character references
  const cost = (estimatedImages * info.price_per_image).toFixed(2);
  costEstimate.textContent = `Estimated illustration cost: ~$${cost} (${pages} pages + character refs)`;
}

imageModelSelect.addEventListener("change", updateImageModelNote);
pageCountInput.addEventListener("input", updateCostEstimate);
loadImageModels();

const autoIdeaInput = document.getElementById("auto_idea");
const autoGenerateBtn = document.getElementById("auto-generate-btn");
const autoGenerateStatus = document.getElementById("auto-generate-status");
const targetAgeSelect = document.getElementById("target_age");
const contentInstructionInput = document.getElementById("content_instruction");
const mainCharactersInput = document.getElementById("main_characters");
const artStylePreferenceInput = document.getElementById("art_style_preference");
const bookTitleInput = document.getElementById("book_title");
const themeInput = document.getElementById("theme");

autoGenerateBtn.addEventListener("click", async () => {
  const idea = autoIdeaInput.value.trim();
  if (!idea) {
    autoGenerateStatus.textContent = "Enter a book idea first.";
    autoGenerateStatus.classList.add("error");
    return;
  }

  autoGenerateBtn.disabled = true;
  autoGenerateStatus.classList.remove("error");
  autoGenerateStatus.textContent = "Generating with Gemini…";

  try {
    const res = await fetch("/api/books/auto-generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea, target_age: targetAgeSelect.value }),
    });
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.error || "Auto generation failed");
    }

    bookTitleInput.value = data.book_title || "";
    themeInput.value = data.theme || "";
    contentInstructionInput.value = data.content_instruction || "";
    mainCharactersInput.value = data.main_characters || "";
    artStylePreferenceInput.value = data.art_style_preference || "";

    autoGenerateStatus.textContent = "Fields filled in below — review and adjust as needed.";
  } catch (err) {
    autoGenerateStatus.textContent = err.message;
    autoGenerateStatus.classList.add("error");
  } finally {
    autoGenerateBtn.disabled = false;
  }
});

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
        onComplete(data.pdf_url, data.book_id, data.cover_url, data.warning);
      }
    }
  };

  source.onerror = () => {
    source.close();
    submitBtn.disabled = false;
  };
}

async function onComplete(pdfUrl, bookId, coverUrl, warning) {
  progressFill.style.width = "100%";
  progressPct.textContent = "100%";
  resultPanel.classList.remove("hidden");
  pdfLink.href = pdfUrl;
  thumbnails.innerHTML = "";

  if (warning) {
    resultWarning.textContent = warning;
    resultWarning.classList.remove("hidden");
  } else {
    resultWarning.classList.add("hidden");
  }

  if (coverUrl) {
    coverLink.href = coverUrl;
    coverLink.classList.remove("hidden");
  } else {
    coverLink.classList.add("hidden");
  }

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

      const dims = book.cover_dimensions;
      if (dims) {
        const compliance = dims.kdp_hardcover_compliant
          ? "meets KDP's 76-550 page hardcover requirement"
          : "below KDP's 76-page hardcover minimum — for preview only";
        coverDimensionsEl.textContent =
          `Cover size: ${dims.full_width_in}" × ${dims.full_height_in}" (spine ${dims.spine_width_in}") — ${compliance}`;
      }
    } catch (err) {
      // thumbnails/dimensions are a nice-to-have; ignore failures
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
    if (btn.dataset.tab === "history") {
      loadHistory();
    }
  });
});

const historyList = document.getElementById("history-list");
const historyEmpty = document.getElementById("history-empty");

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

async function loadHistory() {
  try {
    const res = await fetch("/api/books");
    const books = await res.json();
    historyList.innerHTML = "";
    historyEmpty.classList.toggle("hidden", books.length > 0);
    for (const book of books) {
      historyList.appendChild(renderHistoryCard(book));
    }
  } catch (err) {
    historyList.innerHTML = `<p class="error">Could not load history: ${escapeHtml(err.message)}</p>`;
  }
}

// Generation always runs as a background job on the server, independent of any browser
// connection — closing the tab or the whole browser does not stop it. This just lets a fresh
// page load (History tab, possibly reopened much later) find whatever job is/was running for a
// book still marked "running" and reconnect to its live progress, without needing the original
// in-memory job_id from the tab that started it.
function watchRunningBook(bookId, statusEl) {
  statusEl.textContent = "Checking status…";
  fetch(`/api/books/${bookId}/job`)
    .then((res) => res.json())
    .then((job) => {
      if (job.error && !job.job_id) {
        statusEl.textContent = "";
        statusEl.classList.add("hidden");
        return;
      }
      if (job.status === "done" || job.status === "error") {
        loadHistory();
        return;
      }
      statusEl.textContent = job.step ? `Generating… ${job.step}` : "Generating…";

      const source = new EventSource(`/api/books/stream/${job.job_id}`);
      source.onmessage = (event) => {
        const evt = JSON.parse(event.data);
        if (evt.error) {
          statusEl.textContent = evt.error;
          statusEl.classList.add("error");
          source.close();
          return;
        }
        if (evt.step) {
          statusEl.textContent =
            typeof evt.pct === "number" ? `Generating… ${evt.step} (${evt.pct}%)` : `Generating… ${evt.step}`;
        }
        if (evt.done) {
          source.close();
          loadHistory();
        }
      };
      source.onerror = () => source.close();
    })
    .catch(() => {
      statusEl.textContent = "";
      statusEl.classList.add("hidden");
    });
}

function renderHistoryCard(book) {
  const card = document.createElement("div");
  card.className = "history-card";

  const created = book.created_at ? new Date(book.created_at).toLocaleString() : "";
  card.innerHTML = `
    <div class="history-card-header">
      <div class="history-card-title">
        <h3 class="title-display">${escapeHtml(book.title)}</h3>
        <p class="hint subtitle-display ${book.subtitle ? "" : "hidden"}">${escapeHtml(book.subtitle || "")}</p>
        <div class="title-edit-form hidden">
          <input type="text" class="title-input" value="${escapeHtml(book.title)}" placeholder="Title" />
          <input type="text" class="subtitle-input" value="${escapeHtml(book.subtitle || "")}" placeholder="Subtitle (optional)" />
          <p class="hint title-edit-status hidden"></p>
          <div class="title-edit-actions">
            <button type="button" class="btn-primary btn-small" data-action="save-title">Save</button>
            <button type="button" class="btn-secondary btn-small" data-action="cancel-title">Cancel</button>
            <button type="button" class="btn-secondary btn-small" data-action="suggest-titles">✨ Suggest Titles</button>
          </div>
          <div class="title-suggestions hidden"></div>
        </div>
      </div>
      <div class="history-card-header-right">
        <button type="button" class="btn-secondary btn-small" data-action="edit-title">Edit Title</button>
        <span class="status-badge status-${escapeHtml(book.status)}">${escapeHtml(book.status)}</span>
      </div>
    </div>
    <p class="hint">${book.page_count} pages · ${escapeHtml(book.image_model || "")} · ${escapeHtml(created)}</p>
    <div class="result-actions">
      ${book.pdf_url ? `<a class="btn-primary" href="${book.pdf_url}" target="_blank">Download PDF</a>` : ""}
      ${book.cover_url ? `<a class="btn-primary" href="${book.cover_url}" target="_blank">Download Cover</a>` : ""}
      ${book.can_continue ? `<button type="button" class="btn-primary" data-action="continue">Continue</button>` : ""}
      ${book.can_regenerate_cover ? `<button type="button" class="btn-secondary" data-action="regen-cover">Regenerate Cover</button>` : ""}
      ${book.pdf_url ? `<button type="button" class="btn-secondary" data-action="rebuild-pdf">Rebuild PDF</button>` : ""}
      <button type="button" class="btn-secondary" data-action="toggle-pages">View Pages</button>
      <button type="button" class="btn-secondary" data-action="delete">Delete</button>
    </div>
    <p class="hint continue-status hidden"></p>
    <p class="hint rebuild-status hidden"></p>
    <p class="hint generation-status ${book.status === "running" ? "" : "hidden"}"></p>
    ${book.pdf_url ? `<p class="hint">Regenerating page images below updates the images only — click "Rebuild PDF" once you're happy with them to fold the changes into the PDF.</p>` : ""}
    <div class="history-pages hidden"></div>
  `;

  if (book.status === "running") {
    watchRunningBook(book.book_id, card.querySelector(".generation-status"));
  }

  const titleDisplay = card.querySelector(".title-display");
  const subtitleDisplay = card.querySelector(".subtitle-display");
  const titleEditForm = card.querySelector(".title-edit-form");
  const titleInput = card.querySelector(".title-input");
  const subtitleInput = card.querySelector(".subtitle-input");
  const titleEditStatus = card.querySelector(".title-edit-status");
  const editTitleBtn = card.querySelector('[data-action="edit-title"]');

  const closeTitleEdit = () => {
    titleEditForm.classList.add("hidden");
    titleDisplay.classList.remove("hidden");
    subtitleDisplay.classList.toggle("hidden", !subtitleDisplay.textContent);
    editTitleBtn.classList.remove("hidden");
    titleEditStatus.classList.add("hidden", "error");
    card.querySelector(".title-suggestions").classList.add("hidden");
    card.querySelector(".title-suggestions").innerHTML = "";
  };

  editTitleBtn.addEventListener("click", () => {
    titleInput.value = book.title;
    subtitleInput.value = book.subtitle || "";
    titleDisplay.classList.add("hidden");
    subtitleDisplay.classList.add("hidden");
    editTitleBtn.classList.add("hidden");
    titleEditForm.classList.remove("hidden");
    titleInput.focus();
  });

  card.querySelector('[data-action="cancel-title"]').addEventListener("click", closeTitleEdit);

  card.querySelector('[data-action="save-title"]').addEventListener("click", () => {
    const newTitle = titleInput.value.trim();
    if (!newTitle) {
      titleEditStatus.textContent = "Title can't be empty.";
      titleEditStatus.classList.remove("hidden");
      titleEditStatus.classList.add("error");
      return;
    }
    const newSubtitle = subtitleInput.value.trim();

    fetch(`/api/books/${book.book_id}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: newTitle, subtitle: newSubtitle }),
    })
      .then((res) => res.json().then((data) => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        if (!ok) throw new Error(data.error || "Failed to save");
        book.title = data.title;
        book.subtitle = data.subtitle;
        titleDisplay.textContent = book.title;
        subtitleDisplay.textContent = book.subtitle || "";
        closeTitleEdit();
      })
      .catch((err) => {
        titleEditStatus.textContent = err.message;
        titleEditStatus.classList.remove("hidden");
        titleEditStatus.classList.add("error");
      });
  });

  const suggestTitlesBtn = card.querySelector('[data-action="suggest-titles"]');
  const titleSuggestions = card.querySelector(".title-suggestions");
  suggestTitlesBtn.addEventListener("click", () => {
    suggestTitlesBtn.disabled = true;
    titleEditStatus.classList.remove("hidden", "error");
    titleEditStatus.textContent = "Reading the story and brainstorming titles…";
    titleSuggestions.classList.add("hidden");
    titleSuggestions.innerHTML = "";

    fetch(`/api/books/${book.book_id}/title-ideas`, { method: "POST" })
      .then((res) => res.json().then((data) => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        if (!ok) throw new Error(data.error || "Failed to get title ideas");
        if (!data.titles || !data.titles.length) throw new Error("No title ideas came back — try again.");

        titleEditStatus.textContent = "Pick one, or keep editing above:";
        titleSuggestions.classList.remove("hidden");
        for (const idea of data.titles) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "title-suggestion";
          btn.innerHTML = `<strong>${escapeHtml(idea.title)}</strong><span class="hint">${escapeHtml(idea.subtitle || "")}</span>`;
          btn.addEventListener("click", () => {
            titleInput.value = idea.title;
            subtitleInput.value = idea.subtitle || "";
          });
          titleSuggestions.appendChild(btn);
        }
      })
      .catch((err) => {
        titleEditStatus.textContent = err.message;
        titleEditStatus.classList.add("error");
      })
      .finally(() => {
        suggestTitlesBtn.disabled = false;
      });
  });

  const regenCoverBtn = card.querySelector('[data-action="regen-cover"]');
  if (regenCoverBtn) {
    const continueStatus = card.querySelector(".continue-status");
    regenCoverBtn.addEventListener("click", () => {
      regenCoverBtn.disabled = true;
      continueStatus.classList.remove("hidden", "error");
      continueStatus.textContent = "Starting…";

      fetch(`/api/books/${book.book_id}/cover/regenerate`, { method: "POST" })
        .then((res) => res.json().then((data) => ({ ok: res.ok, data })))
        .then(({ ok, data }) => {
          if (!ok) throw new Error(data.error || "Failed to regenerate cover");

          const source = new EventSource(`/api/books/stream/${data.job_id}`);
          source.onmessage = (event) => {
            const evt = JSON.parse(event.data);
            if (evt.error) {
              continueStatus.textContent = evt.error;
              continueStatus.classList.add("error");
              source.close();
              regenCoverBtn.disabled = false;
              return;
            }
            if (evt.step) {
              continueStatus.textContent =
                typeof evt.pct === "number" ? `${evt.step} (${evt.pct}%)` : evt.step;
            }
            if (evt.done) {
              source.close();
              continueStatus.textContent = "Cover updated.";
              regenCoverBtn.disabled = false;
              loadHistory();
            }
          };
          source.onerror = () => {
            source.close();
            regenCoverBtn.disabled = false;
          };
        })
        .catch((err) => {
          continueStatus.textContent = err.message;
          continueStatus.classList.add("error");
          regenCoverBtn.disabled = false;
        });
    });
  }

  const rebuildPdfBtn = card.querySelector('[data-action="rebuild-pdf"]');
  if (rebuildPdfBtn) {
    const rebuildStatus = card.querySelector(".rebuild-status");
    rebuildPdfBtn.addEventListener("click", () => {
      rebuildPdfBtn.disabled = true;
      rebuildStatus.classList.remove("hidden", "error");
      rebuildStatus.textContent = "Starting…";

      fetch(`/api/books/${book.book_id}/rebuild-pdf`, { method: "POST" })
        .then((res) => res.json().then((data) => ({ ok: res.ok, data })))
        .then(({ ok, data }) => {
          if (!ok) throw new Error(data.error || "Failed to rebuild PDF");

          const source = new EventSource(`/api/books/stream/${data.job_id}`);
          source.onmessage = (event) => {
            const evt = JSON.parse(event.data);
            if (evt.error) {
              rebuildStatus.textContent = evt.error;
              rebuildStatus.classList.add("error");
              source.close();
              rebuildPdfBtn.disabled = false;
              return;
            }
            if (evt.step) {
              rebuildStatus.textContent =
                typeof evt.pct === "number" ? `${evt.step} (${evt.pct}%)` : evt.step;
            }
            if (evt.done) {
              source.close();
              rebuildStatus.textContent = "PDF rebuilt.";
              rebuildPdfBtn.disabled = false;
            }
          };
          source.onerror = () => {
            source.close();
            rebuildPdfBtn.disabled = false;
          };
        })
        .catch((err) => {
          rebuildStatus.textContent = err.message;
          rebuildStatus.classList.add("error");
          rebuildPdfBtn.disabled = false;
        });
    });
  }

  const continueBtn = card.querySelector('[data-action="continue"]');
  if (continueBtn) {
    const continueStatus = card.querySelector(".continue-status");
    continueBtn.addEventListener("click", () => {
      continueBtn.disabled = true;
      continueStatus.classList.remove("hidden", "error");
      continueStatus.textContent = "Starting…";

      fetch(`/api/books/${book.book_id}/continue`, { method: "POST" })
        .then((res) => res.json().then((data) => ({ ok: res.ok, data })))
        .then(({ ok, data }) => {
          if (!ok) throw new Error(data.error || "Failed to continue");

          const source = new EventSource(`/api/books/stream/${data.job_id}`);
          source.onmessage = (event) => {
            const evt = JSON.parse(event.data);
            if (evt.error) {
              continueStatus.textContent = evt.error;
              continueStatus.classList.add("error");
              source.close();
              continueBtn.disabled = false;
              return;
            }
            if (evt.step) {
              continueStatus.textContent =
                typeof evt.pct === "number" ? `${evt.step} (${evt.pct}%)` : evt.step;
            }
            if (evt.done) {
              source.close();
              continueStatus.textContent = evt.warning || "Done!";
              loadHistory();
            }
          };
          source.onerror = () => {
            source.close();
            continueBtn.disabled = false;
          };
        })
        .catch((err) => {
          continueStatus.textContent = err.message;
          continueStatus.classList.add("error");
          continueBtn.disabled = false;
        });
    });
  }

  const pagesEl = card.querySelector(".history-pages");
  card.querySelector('[data-action="toggle-pages"]').addEventListener("click", async () => {
    if (!pagesEl.classList.contains("hidden")) {
      pagesEl.classList.add("hidden");
      return;
    }
    pagesEl.classList.remove("hidden");
    if (pagesEl.dataset.loaded) return;

    pagesEl.textContent = "Loading pages…";
    try {
      const res = await fetch(`/api/books/${book.book_id}`);
      const detail = await res.json();
      pagesEl.innerHTML = "";
      for (const page of detail.pages || []) {
        pagesEl.appendChild(renderPageThumb(book.book_id, page));
      }
      pagesEl.dataset.loaded = "1";
    } catch (err) {
      pagesEl.textContent = `Could not load pages: ${err.message}`;
    }
  });

  card.querySelector('[data-action="delete"]').addEventListener("click", async () => {
    if (!confirm(`Delete "${book.title}"? This cannot be undone.`)) return;
    try {
      await fetch(`/api/books/${book.book_id}`, { method: "DELETE" });
      card.remove();
      historyEmpty.classList.toggle("hidden", historyList.children.length > 0);
    } catch (err) {
      alert(`Failed to delete: ${err.message}`);
    }
  });

  return card;
}

function renderPageThumb(bookId, page) {
  const wrap = document.createElement("div");
  wrap.className = "page-thumb" + (page.is_placeholder ? " needs-fix" : "");
  const editPlaceholder = page.is_placeholder
    ? "Original prompt for this page — leave as-is to retry, or edit it first"
    : "Describe an edit to this image, e.g. \"make the sky orange\"";
  // Placeholder pages have no image to edit, so regenerate falls back to a fresh generation
  // from the stored scene prompt — pre-fill it so the user can see and optionally tweak what
  // will actually be sent, instead of an empty box that looks like nothing will happen.
  const prefill = page.is_placeholder ? (page.image_prompt || "") : "";
  wrap.innerHTML = `
    <img src="${page.s3_url}" alt="Page ${page.page_num}" />
    <div class="page-thumb-footer">
      <span>Page ${page.page_num}${page.is_placeholder ? " ⚠" : ""}</span>
      <button type="button" class="btn-secondary btn-small" data-action="regen">Regenerate</button>
    </div>
    <textarea class="page-prompt-input hint" rows="2" placeholder="${escapeHtml(editPlaceholder)}">${escapeHtml(prefill)}</textarea>
    <p class="page-thumb-status hint"></p>
  `;

  const btn = wrap.querySelector('[data-action="regen"]');
  const statusEl = wrap.querySelector(".page-thumb-status");
  const img = wrap.querySelector("img");
  const promptInput = wrap.querySelector(".page-prompt-input");

  btn.addEventListener("click", () => {
    btn.disabled = true;
    statusEl.textContent = "Starting…";

    fetch(`/api/books/${bookId}/pages/${page.page_num}/regenerate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: promptInput.value.trim() }),
    })
      .then((res) => res.json().then((data) => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        if (!ok) throw new Error(data.error || "Failed to start regeneration");

        const source = new EventSource(`/api/books/stream/${data.job_id}`);
        source.onmessage = (event) => {
          const evt = JSON.parse(event.data);
          if (evt.error) {
            statusEl.textContent = evt.error;
            source.close();
            btn.disabled = false;
            return;
          }
          if (evt.step) {
            statusEl.textContent =
              typeof evt.pct === "number" ? `${evt.step} (${evt.pct}%)` : evt.step;
          }
          if (evt.done) {
            source.close();
            img.src = `${page.s3_url}?t=${Date.now()}`;
            wrap.classList.remove("needs-fix");
            statusEl.textContent = "Updated.";
            btn.disabled = false;
          }
        };
        source.onerror = () => {
          source.close();
          btn.disabled = false;
        };
      })
      .catch((err) => {
        statusEl.textContent = err.message;
        btn.disabled = false;
      });
  });

  return wrap;
}

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
