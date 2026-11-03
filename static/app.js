"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  docs: [],
  busy: false,
  connected: false,
  turns: [],
  sequence: 0,
  maxBytes: 10 * 1024 * 1024,
  maxFiles: 10,
};
const initialWelcome = $("answer-area").innerHTML;
let toastTimer;

const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const escapeHtml = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const formatBytes = (bytes) =>
  bytes < 1024
    ? `${bytes} B`
    : bytes < 1024 * 1024
      ? `${(bytes / 1024).toFixed(1)} KB`
      : `${(bytes / 1024 / 1024).toFixed(1)} MB`;

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      signal: AbortSignal.timeout(90000),
    });
  } catch (error) {
    throw new Error(
      error.name === "TimeoutError"
        ? "This request took too long. Please try again."
        : "Could not reach the server. Check your connection and try again.",
    );
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail
          .map((item) => `${item.loc?.at(-1) || "Input"}: ${item.msg}`)
          .join("; ")
      : data.detail;
    throw new Error(
      detail || `The request failed (${response.status}). Please try again.`,
    );
  }
  return data;
}

function showError(id, message = "") {
  $(id).textContent = message;
  $(id).hidden = !message;
}
function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 4500);
}
function syncControls() {
  $("ask-btn").disabled = state.busy || !state.connected || !state.docs.length;
  $("ask-btn").innerHTML = state.busy
    ? '<span class="spinner"></span> Working…'
    : `Ask question ${icon("arrow")}`;
  $("dropzone").disabled = state.busy;
  $("file-input").disabled = state.busy;
  $("reset-btn").disabled =
    state.busy || !state.docs.length || !state.connected;
  $("export-btn").disabled = !state.turns.length || state.busy;
  $("new-session").disabled = !state.turns.length || state.busy;
  const sample = $("sample-btn");
  if (sample) sample.disabled = state.busy || !state.connected;
  document.querySelectorAll(".doc-remove").forEach((button) => {
    button.disabled = state.busy;
  });
  $("question-hint").textContent = state.docs.length
    ? "Enter to ask · Shift + Enter for a new line"
    : "Add a document to start exploring";
}
function setBusy(busy) {
  state.busy = busy;
  $("ask-form").setAttribute("aria-busy", String(busy));
  syncControls();
}

function renderDocuments() {
  const filter = $("file-search").value.toLowerCase();
  const docs = state.docs.filter((doc) =>
    doc.name.toLowerCase().includes(filter),
  );
  $("doc-list").innerHTML = docs
    .map((doc) => {
      const type = doc.name.split(".").pop().toLowerCase();
      return `<li class="doc-item"><span class="file-type ${["pdf", "docx"].includes(type) ? type : ""}">${escapeHtml(type.toUpperCase())}</span><div class="doc-info"><span class="doc-name" title="${escapeHtml(doc.name)}">${escapeHtml(doc.name)}</span><small>${doc.chunks} passage${doc.chunks === 1 ? "" : "s"} · ${formatBytes(doc.size_bytes || 0)}</small></div><button type="button" class="icon-button doc-remove" data-name="${escapeHtml(doc.name)}" aria-label="Remove ${escapeHtml(doc.name)}" title="Remove document">${icon("trash")}</button></li>`;
    })
    .join("");
  $("library-empty").hidden = docs.length > 0;
  $("library-empty").innerHTML = state.docs.length
    ? "<strong>No matching documents.</strong><p>Try a different filename.</p>"
    : `<span class="empty-file">${icon("logo")}</span><strong>A fresh start.</strong><p>Add your first document to turn<br>information into answers.</p>`;
  document
    .querySelectorAll(".doc-remove")
    .forEach((button) =>
      button.addEventListener("click", () =>
        removeDocument(button.dataset.name),
      ),
    );
  syncControls();
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    state.connected = true;
    state.docs = status.docs;
    state.maxBytes = status.limits?.max_file_bytes || state.maxBytes;
    state.maxFiles = status.limits?.max_files || state.maxFiles;
    $("stat-docs").textContent = status.num_docs.toLocaleString();
    $("stat-chunks").textContent = status.num_chunks.toLocaleString();
    $("stat-vocab").textContent = status.vocab_size.toLocaleString();
    $("nav-count").textContent = status.num_docs;
    $("library-count").textContent = status.num_docs;
    $("storage-note").textContent = status.size_bytes
      ? `${formatBytes(status.size_bytes)} · Saved on server`
      : "Saved on this server";
    $("connection").classList.remove("offline");
    $("connection").innerHTML =
      '<span class="status-dot"></span>Workspace connected';
    $("llm-badge").textContent = status.llm.available
      ? "AI provider configured"
      : "Document answers";
    $("llm-badge").title = status.llm.available
      ? `Model: ${status.llm.model}`
      : "Local retrieval. No API key needed.";
    const selected = $("document-scope").value;
    $("document-scope").innerHTML =
      '<option value="">All documents</option>' +
      state.docs
        .map(
          (doc) =>
            `<option value="${escapeHtml(doc.name)}">${escapeHtml(doc.name)}</option>`,
        )
        .join("");
    $("document-scope").value = state.docs.some((doc) => doc.name === selected)
      ? selected
      : "";
    renderDocuments();
  } catch (error) {
    state.connected = false;
    $("connection").classList.add("offline");
    $("connection").innerHTML =
      '<span class="status-dot"></span>Server unavailable';
    syncControls();
    throw error;
  }
}

async function uploadFiles(fileList) {
  if (state.busy) return false;
  showError("ingest-error");
  const files = Array.from(fileList);
  if (!files.length) return false;
  if (files.length > state.maxFiles) {
    showError(
      "ingest-error",
      `Choose at most ${state.maxFiles} files at a time.`,
    );
    return false;
  }
  const invalid = files.find(
    (file) =>
      !/\.(pdf|docx|txt|md)$/i.test(file.name) ||
      !file.size ||
      file.size > state.maxBytes,
  );
  if (invalid) {
    showError(
      "ingest-error",
      `${invalid.name}: choose a nonempty PDF, DOCX, TXT or MD file under ${formatBytes(state.maxBytes)}.`,
    );
    return false;
  }
  setBusy(true);
  $("upload-progress").hidden = false;
  $("dropzone").setAttribute("aria-busy", "true");
  try {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    const result = await api("/api/ingest", { method: "POST", body: form });
    await refreshStatus();
    if (result.errors?.length)
      showError(
        "ingest-error",
        result.errors.map((error) => `${error.name}: ${error.error}`).join(" "),
      );
    const replaced = result.added.filter((doc) => doc.replaced).length;
    toast(
      `${result.added.length} document${result.added.length === 1 ? "" : "s"} indexed${replaced ? ` (${replaced} replaced)` : ""}. Ready to explore.`,
    );
    return true;
  } catch (error) {
    showError("ingest-error", error.message);
    return false;
  } finally {
    $("upload-progress").hidden = true;
    $("dropzone").removeAttribute("aria-busy");
    $("file-input").value = "";
    setBusy(false);
  }
}

function confirmAction(title, description, action) {
  const dialog = $("confirm-dialog");
  $("confirm-title").textContent = title;
  $("confirm-description").textContent = description;
  $("confirm-action").textContent = action;
  dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      () => resolve(dialog.returnValue === "confirm"),
      { once: true },
    );
    dialog.showModal();
  });
}
async function removeDocument(name) {
  if (
    state.busy ||
    !(await confirmAction(
      "Remove document?",
      `“${name}” and its passages will be removed from this server’s shared library.`,
      "Remove document",
    ))
  )
    return;
  setBusy(true);
  showError("ingest-error");
  try {
    const form = new FormData();
    form.append("name", name);
    await api("/api/remove", { method: "POST", body: form });
    await refreshStatus();
    toast("Document removed. Previous answers remain in this session.");
  } catch (error) {
    showError("ingest-error", error.message);
  } finally {
    setBusy(false);
  }
}

function clearSession() {
  state.turns = [];
  $("answer-area").innerHTML = initialWelcome;
  $("session-label").textContent = "New research session";
  $("sample-btn").addEventListener("click", loadSample);
  showError("ask-error");
  syncControls();
}
async function resetLibrary() {
  if (
    state.busy ||
    !(await confirmAction(
      "Clear document library?",
      "This deletes every indexed document from this server’s shared library and clears this research session. This cannot be undone.",
      "Clear library",
    ))
  )
    return;
  setBusy(true);
  showError("ingest-error");
  try {
    await api("/api/reset", { method: "POST" });
    await refreshStatus();
    clearSession();
    toast("Library cleared. A fresh start.");
  } catch (error) {
    showError("ingest-error", error.message);
  } finally {
    setBusy(false);
  }
}

function highlight(text, keywords = []) {
  // Match the plain text first. Never run replacements over HTML or attributes.
  const words = [...new Set(keywords)].filter((word) =>
    /^[a-z0-9']{2,}$/i.test(word),
  );
  if (!words.length) return escapeHtml(text);
  const pattern = new RegExp(`\\b(${words.join("|")})\\b`, "gi");
  let previous = 0,
    html = "";
  for (const match of text.matchAll(pattern)) {
    html +=
      escapeHtml(text.slice(previous, match.index)) +
      `<mark>${escapeHtml(match[0])}</mark>`;
    previous = match.index + match[0].length;
  }
  return html + escapeHtml(text.slice(previous));
}
function answerMarkup(result, id) {
  const sources = new Set(result.sources.map((source) => source.n));
  return result.answer
    .split(/(\[\d+\])/g)
    .map((part) => {
      const match = part.match(/^\[(\d+)\]$/);
      return match && sources.has(Number(match[1]))
        ? `<button class="citation" type="button" data-target="source-${id}-${match[1]}" aria-label="View source ${match[1]}">${part}</button>`
        : highlight(part, result.keywords);
    })
    .join("");
}

function renderTurn(turn) {
  const result = turn.result;
  const node = document.createElement("article");
  node.className = "turn";
  const mode =
    result.mode === "generative"
      ? "AI-generated answer"
      : result.mode === "extractive"
        ? "From your documents"
        : "No matching evidence";
  node.innerHTML = `<div class="turn-meta">Your question <span class="turn-scope">${escapeHtml(turn.scope || "All documents")}</span></div><h3 class="turn-question">${escapeHtml(turn.question)}</h3><div class="answer-meta">${icon("logo")} ${mode}<button class="icon-button answer-copy" type="button" aria-label="Copy answer" title="Copy answer">${icon("copy")}</button></div><div class="answer-text">${answerMarkup(result, turn.id)}</div>${result.llm_error ? `<p class="notice">${escapeHtml(result.llm_error)}</p>` : ""}${result.sources.length ? `<div class="source-heading">EVIDENCE · ${result.sources.length} SOURCE${result.sources.length === 1 ? "" : "S"}<span></span></div>` : ""}${result.sources.map((source) => `<details class="source" id="source-${turn.id}-${source.n}"><summary><span class="snum">${source.n}</span><span class="sdoc">${escapeHtml(source.doc)}${source.page ? ` · p. ${source.page}` : ""}</span><span class="sscore" title="Cosine similarity, not confidence">relevance ${Number(source.score).toFixed(2)}</span></summary><div class="sbody">${highlight(source.text, result.keywords)}</div></details>`).join("")}`;
  node.querySelector(".answer-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(result.answer);
      toast("Answer copied.");
    } catch {
      toast(
        "Clipboard unavailable. Select the answer to copy it, or export the session.",
      );
    }
  });
  node.querySelectorAll(".citation").forEach((button) =>
    button.addEventListener("click", () => {
      const source = $(button.dataset.target);
      source.open = true;
      source.querySelector("summary").focus({ preventScroll: true });
      source.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }),
  );
  $("answer-area").append(node);
  node.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

async function ask(event) {
  event?.preventDefault();
  if (state.busy) return;
  showError("ask-error");
  const question = $("question").value.trim();
  if (!state.docs.length) {
    showError("ask-error", "Add a document first, or try the sample document.");
    return;
  }
  if (!question) {
    showError("ask-error", "Enter a question about your documents.");
    $("question").focus();
    return;
  }
  if (!$("topk").checkValidity()) {
    showError(
      "ask-error",
      "Choose between 1 and 10 sources in Answer settings.",
    );
    return;
  }
  const scope = $("document-scope").value;
  setBusy(true);
  const loading = document.createElement("div");
  loading.className = "loading-turn";
  loading.setAttribute("role", "status");
  loading.innerHTML = '<span class="spinner"></span>Finding the evidence…';
  $("answer-area").append(loading);
  loading.scrollIntoView({ block: "nearest", behavior: "smooth" });
  try {
    const form = new FormData();
    form.append("question", question);
    form.append("top_k", $("topk").value);
    form.append("mode", $("mode").value);
    if (scope) form.append("document", scope);
    const result = await api("/api/ask", { method: "POST", body: form });
    $("welcome")?.remove();
    const turn = { id: ++state.sequence, question, scope, result };
    state.turns.push(turn);
    renderTurn(turn);
    $("session-label").textContent =
      `${state.turns.length} question${state.turns.length === 1 ? "" : "s"} in this session`;
    if ($("question").value.trim() === question) $("question").value = "";
  } catch (error) {
    showError("ask-error", error.message);
  } finally {
    loading.remove();
    setBusy(false);
    $("question").focus({ preventScroll: true });
  }
}

async function loadSample() {
  const name = "Observatory visitor guide.txt";
  if (!state.docs.some((doc) => doc.name === name)) {
    const text =
      "The Meridian Observatory is located in Jaipur, India. The observatory opens at 9 AM and closes at 6 PM from Tuesday to Sunday. It is closed on Mondays. General admission costs 200 rupees per adult. Children under twelve enter for free. Guided telescope sessions begin at 4 PM and last forty-five minutes. Visitors must book telescope sessions at least one day in advance. Photography is permitted in the gardens, but flash photography is prohibited near the telescopes. The observatory was established in 1982 to make astronomy accessible to the public.";
    if (!(await uploadFiles([new File([text], name, { type: "text/plain" })])))
      return;
  }
  $("document-scope").value = name;
  $("question").value =
    "When can I visit the observatory and how much does admission cost?";
  $("question").focus();
  toast("Sample ready. Ask the suggested question or write your own.");
}

function exportSession() {
  if (!state.turns.length) return;
  const markdown =
    "# DocumentAnalyser · Research session\n\n" +
    state.turns
      .map(
        (turn) =>
          `## ${turn.question}\n\nScope: ${turn.scope || "All documents"}\n\n${turn.result.answer}\n\n${turn.result.sources.length ? "### Sources\n\n" : ""}${turn.result.sources.map((source) => `[${source.n}] ${source.doc}${source.page ? `, page ${source.page}` : ""} (relevance ${source.score})\n\n${source.text}\n`).join("\n")}`,
      )
      .join("\n---\n\n");
  const url = URL.createObjectURL(
    new Blob([markdown], { type: "text/markdown;charset=utf-8" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = "research-session.md";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("Session exported with source passages.");
}

$("dropzone").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (event) =>
  uploadFiles(event.target.files),
);
$("dropzone").addEventListener("dragover", (event) => {
  event.preventDefault();
  if (!state.busy) $("dropzone").classList.add("drag");
});
$("dropzone").addEventListener("dragleave", () =>
  $("dropzone").classList.remove("drag"),
);
$("dropzone").addEventListener("drop", (event) => {
  event.preventDefault();
  $("dropzone").classList.remove("drag");
  uploadFiles(event.dataTransfer.files);
});
// Stop dropping a document outside the dropzone from replacing the page.
window.addEventListener("dragover", (event) => {
  if (event.dataTransfer.types.includes("Files")) event.preventDefault();
});
window.addEventListener("drop", (event) => {
  if (event.dataTransfer.types.includes("Files")) event.preventDefault();
});
$("file-search").addEventListener("input", renderDocuments);
$("reset-btn").addEventListener("click", resetLibrary);
$("sample-btn").addEventListener("click", loadSample);
$("new-session").addEventListener("click", () => {
  clearSession();
  $("question").focus();
});
$("export-btn").addEventListener("click", exportSession);
$("ask-form").addEventListener("submit", ask);
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    ask();
  }
});
window.addEventListener("online", () =>
  refreshStatus().catch((error) => showError("ingest-error", error.message)),
);
window.addEventListener("focus", () => {
  if (!state.busy)
    refreshStatus().catch((error) => showError("ingest-error", error.message));
});
refreshStatus().catch((error) => showError("ingest-error", error.message));
