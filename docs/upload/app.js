// Council Tracking PWA — UI + Pyodide bootstrap.

const bootEl = document.getElementById("boot");
const bootStatus = document.getElementById("boot-status");
const workspaceEl = document.getElementById("workspace");
const dropzone = document.getElementById("dropzone");
const filepicker = document.getElementById("filepicker");
const toolbar = document.getElementById("toolbar");
const filterInput = document.getElementById("filter");
const downloadBtn = document.getElementById("download-xlsx");
const clearBtn = document.getElementById("clear-all");
const resultsEl = document.getElementById("results");
const installBtn = document.getElementById("install-btn");
const cardTpl = document.getElementById("card-template");
const motionRowTpl = document.getElementById("motion-row-template");

const PYODIDE_VERSION = "v0.26.4";
const PYODIDE_INDEX_URL = `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`;

// Map of file content sha-256 hex -> { key, result, filename }
const parsed = new Map();
let pyodide = null;
let parseDocument = null;
let exportXlsx = null;
let deferredInstall = null;

function setBootStatus(msg) {
  bootStatus.textContent = msg;
}

async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  try {
    await navigator.serviceWorker.register("./sw.js");
  } catch (err) {
    console.warn("Service worker registration failed:", err);
  }
}

async function bootPyodide() {
  setBootStatus("Downloading Pyodide runtime…");
  pyodide = await loadPyodide({ indexURL: PYODIDE_INDEX_URL });

  setBootStatus("Loading pandas + micropip…");
  await pyodide.loadPackage(["pandas", "micropip"]);

  setBootStatus("Installing pdfplumber + openpyxl…");
  // pdfplumber pins pdfminer.six to a specific build, but Pyodide ships a
  // newer pdfminer.six in its repodata. Skipping pdfplumber's dep resolution
  // sidesteps the strict-pin conflict; pdfplumber works against the newer
  // pdfminer.six just fine at runtime.
  await pyodide.runPythonAsync(`
import micropip
await micropip.install(["openpyxl", "pillow", "pdfminer.six"])
await micropip.install("pdfplumber", deps=False)
`);

  setBootStatus("Loading parser…");
  // parser.py lives at /parser.py (one level up from /upload/)
  const code = await (await fetch("../parser.py", { cache: "no-cache" })).text();
  await pyodide.runPythonAsync(code);

  parseDocument = pyodide.globals.get("parse_document");
  exportXlsx = pyodide.globals.get("export_xlsx");
}

async function sha256Hex(buf) {
  const digest = await crypto.subtle.digest("SHA-256", buf);
  const bytes = new Uint8Array(digest);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function fileSupported(name) {
  const n = name.toLowerCase();
  return n.endsWith(".pdf") || n.endsWith(".txt");
}

async function handleFiles(fileList) {
  const files = Array.from(fileList || []).filter((f) => fileSupported(f.name));
  if (!files.length) return;

  for (const file of files) {
    const buf = await file.arrayBuffer();
    const key = await sha256Hex(buf);
    if (parsed.has(key)) continue;

    const placeholder = renderPlaceholderCard(key, file.name);
    try {
      const bytes = new Uint8Array(buf);
      const proxy = parseDocument(file.name, bytes);
      const result = proxy.toJs({ dict_converter: Object.fromEntries });
      proxy.destroy();
      parsed.set(key, { key, filename: file.name, result });
      renderCard(placeholder, result, key);
    } catch (err) {
      console.error(err);
      placeholder.querySelector(".card-sub").textContent = `Failed to parse: ${err.message || err}`;
      placeholder.classList.add("card-error");
    }
  }
  updateToolbarVisibility();
  applyFilter();
}

function renderPlaceholderCard(key, name) {
  const node = cardTpl.content.firstElementChild.cloneNode(true);
  node.dataset.key = key;
  node.querySelector(".card-title").textContent = name;
  node.querySelector(".card-date").textContent = "Parsing…";
  node.querySelector(".card-counts").textContent = "";
  node.querySelector(".card-remove").addEventListener("click", () => {
    parsed.delete(key);
    node.remove();
    updateToolbarVisibility();
  });
  resultsEl.appendChild(node);
  return node;
}

function renderCard(node, result, key) {
  const dateStr = result.meeting_date || "Date unknown";
  const motions = result.motions || [];
  const ords = result.ordinances || [];
  const ress = result.resolutions || [];

  node.querySelector(".card-date").textContent = dateStr;
  node.querySelector(".card-counts").textContent =
    `${motions.length} motion${motions.length === 1 ? "" : "s"} · ` +
    `${ords.length} ordinance line${ords.length === 1 ? "" : "s"} · ` +
    `${ress.length} resolution line${ress.length === 1 ? "" : "s"}`;

  const tbody = node.querySelector(".motions-body");
  tbody.innerHTML = "";
  const emptyMotionsMsg = node.querySelector(".motions-empty");
  emptyMotionsMsg.hidden = motions.length > 0;

  const memberRowsByMotion = groupMemberRows(result.votes_by_member || []);

  motions.forEach((m, idx) => {
    const frag = motionRowTpl.content.cloneNode(true);
    const mainRow = frag.querySelector(".motion-row");
    const detailRow = frag.querySelector(".motion-detail-row");

    mainRow.querySelector(".m-page").textContent = m.page ?? "";
    mainRow.querySelector(".m-ref").textContent = m.agenda_ref || "";
    mainRow.querySelector(".m-type").textContent = m.business_type || "";
    mainRow.querySelector(".m-title").textContent = m.item_title || "";
    mainRow.querySelector(".m-outcome").textContent = m.outcome || "";

    detailRow.querySelector(".m-text").textContent = m.motion || "(no motion text)";

    const rollUl = detailRow.querySelector(".rollcall-list");
    const rollEmpty = detailRow.querySelector(".rollcall-empty");
    const members = memberRowsByMotion[idx] || parseRollCallFromSummary(m.roll_call);
    if (members.length === 0) {
      rollEmpty.hidden = false;
    } else {
      rollEmpty.hidden = true;
      for (const { member, vote } of members) {
        const li = document.createElement("li");
        const v = String(vote || "").toLowerCase();
        li.innerHTML = `${escapeHtml(member)} <span class="vote ${v}">${escapeHtml(vote)}</span>`;
        rollUl.appendChild(li);
      }
    }

    mainRow.dataset.search = [
      m.agenda_ref,
      m.business_type,
      m.item_title,
      m.motion,
      m.outcome,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();

    mainRow.addEventListener("click", () => {
      const hidden = detailRow.hasAttribute("hidden");
      if (hidden) {
        detailRow.removeAttribute("hidden");
        mainRow.classList.add("expanded");
      } else {
        detailRow.setAttribute("hidden", "");
        mainRow.classList.remove("expanded");
      }
    });

    tbody.appendChild(frag);
  });

  // Ordinance / resolution line lists
  populateLineList(node.querySelector(".ord-list"), ords);
  populateLineList(node.querySelector(".res-list"), ress);
  node.querySelector(".ord-count").textContent = ords.length;
  node.querySelector(".res-count").textContent = ress.length;
}

function groupMemberRows(rows) {
  // votes_by_member is a flat list; group consecutive rows that share the same
  // (page, motion_excerpt, agenda_ref, outcome) tuple as belonging to the same
  // motion in the order motions were emitted.
  const groups = [];
  let cur = null;
  let curKey = null;
  for (const r of rows) {
    const key = [r.page, r.agenda_ref, r.motion_excerpt, r.outcome].join("|");
    if (key !== curKey) {
      cur = [];
      curKey = key;
      groups.push(cur);
    }
    cur.push({ member: r.member, vote: r.vote });
  }
  const byIndex = {};
  groups.forEach((g, i) => (byIndex[i] = g));
  return byIndex;
}

function parseRollCallFromSummary(s) {
  if (!s) return [];
  return s
    .split(";")
    .map((seg) => seg.trim())
    .filter(Boolean)
    .map((seg) => {
      const idx = seg.lastIndexOf(":");
      if (idx === -1) return { member: seg, vote: "" };
      return { member: seg.slice(0, idx).trim(), vote: seg.slice(idx + 1).trim() };
    });
}

function populateLineList(ul, items) {
  ul.innerHTML = "";
  for (const it of items) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="page-tag">p.${it.page}</span>${escapeHtml(it.line_text)}`;
    ul.appendChild(li);
  }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function updateToolbarVisibility() {
  const hasResults = parsed.size > 0;
  toolbar.hidden = !hasResults;
}

function applyFilter() {
  const q = filterInput.value.trim().toLowerCase();
  for (const row of resultsEl.querySelectorAll(".motion-row")) {
    const hay = row.dataset.search || "";
    const match = !q || hay.includes(q);
    row.classList.toggle("hidden", !match);
    const detail = row.nextElementSibling;
    if (detail && detail.classList.contains("motion-detail-row")) {
      if (!match) {
        detail.setAttribute("hidden", "");
        row.classList.remove("expanded");
      }
    }
  }
}

function clearAll() {
  parsed.clear();
  resultsEl.innerHTML = "";
  filterInput.value = "";
  updateToolbarVisibility();
}

async function downloadXlsx() {
  if (parsed.size === 0 || !exportXlsx) return;
  downloadBtn.disabled = true;
  const prev = downloadBtn.textContent;
  downloadBtn.textContent = "Building XLSX…";
  try {
    const resultsList = Array.from(parsed.values()).map((p) => p.result);
    const pyResults = pyodide.toPy(resultsList);
    const proxy = exportXlsx(pyResults);
    const bytes = proxy.toJs();
    proxy.destroy();
    pyResults.destroy();
    const blob = new Blob([bytes], {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "eagle_mountain_extract.xlsx";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (err) {
    console.error(err);
    alert(`Failed to build XLSX: ${err.message || err}`);
  } finally {
    downloadBtn.textContent = prev;
    downloadBtn.disabled = false;
  }
}

function wireDropzone() {
  const open = () => filepicker.click();
  dropzone.addEventListener("click", open);
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      open();
    }
  });

  filepicker.addEventListener("change", () => {
    handleFiles(filepicker.files);
    filepicker.value = "";
  });

  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    })
  );
  dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer?.files?.length) handleFiles(e.dataTransfer.files);
  });

  // Also accept drops anywhere on the page once the workspace is visible.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    if (dropzone.contains(e.target)) return; // already handled
    e.preventDefault();
    if (e.dataTransfer?.files?.length) handleFiles(e.dataTransfer.files);
  });
}

function wireToolbar() {
  filterInput.addEventListener("input", applyFilter);
  clearBtn.addEventListener("click", clearAll);
  downloadBtn.addEventListener("click", downloadXlsx);
}

function wireInstallPrompt() {
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferredInstall = e;
    installBtn.hidden = false;
  });
  installBtn.addEventListener("click", async () => {
    if (!deferredInstall) return;
    deferredInstall.prompt();
    await deferredInstall.userChoice;
    deferredInstall = null;
    installBtn.hidden = true;
  });
  window.addEventListener("appinstalled", () => {
    installBtn.hidden = true;
  });
}

async function main() {
  wireInstallPrompt();
  registerServiceWorker(); // fire-and-forget; don't block boot

  try {
    await bootPyodide();
  } catch (err) {
    console.error(err);
    bootStatus.textContent = `Failed to start parser: ${err.message || err}`;
    return;
  }

  bootEl.hidden = true;
  workspaceEl.hidden = false;
  wireDropzone();
  wireToolbar();
}

main();
