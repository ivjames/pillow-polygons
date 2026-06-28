/* ── State ── */
let activeImage  = null;   // current image object
let activeFolder = "";
let activeTag    = "";
let searchQ      = "";
let refFile      = null;
let selectedPreset = "";

/* ── DOM refs ── */
const $ = id => document.getElementById(id);
const prompt       = $("prompt");
const widthInput   = $("width");
const heightInput  = $("height");
const seedInput    = $("seed");
const generateBtn  = $("generate-btn");
const btnLabel     = $("btn-label");
const spinner      = $("spinner");
const tokenMeter   = $("token-meter");
const tIn          = $("t-in");
const tOut         = $("t-out");
const tTotal       = $("t-total");
const errorBox     = $("error-box");
const resultImg    = $("result-img");
const emptyState   = $("empty-state");
const imgMeta      = $("img-meta");
const metaTags     = $("meta-tags");
const tagInput     = $("tag-input");
const addTagBtn    = $("add-tag-btn");
const folderSelect = $("folder-select");
const downloadBtn  = $("download-btn");
const deleteBtn    = $("delete-btn");
const galleryGrid  = $("gallery-grid");
const searchInput  = $("search-input");
const folderBar    = $("folder-bar");
const tagBar       = $("tag-bar");
const uploadZone   = $("upload-zone");
const uploadInner  = $("upload-inner");
const refInput     = $("ref-input");
const refPreview   = $("ref-preview");
const removeRef    = $("remove-ref");
const newFolderInput = $("new-folder-input");
const createFolderBtn = $("create-folder-btn");

/* ── Preset picker ── */
document.querySelectorAll(".preset-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".preset-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    selectedPreset = btn.dataset.preset;
  });
});
document.querySelector('.preset-btn[data-preset=""]').classList.add("active");

/* ── Seed random ── */
$("random-seed").addEventListener("click", () => {
  seedInput.value = Math.floor(Math.random() * 99999);
});

/* ── Sidebar / gallery collapse ── */
$("toggle-sidebar").addEventListener("click", () => {
  const s = $("sidebar");
  s.classList.toggle("collapsed");
  $("toggle-sidebar").textContent = s.classList.contains("collapsed") ? "›" : "‹";
});
$("toggle-gallery").addEventListener("click", () => {
  const g = $("gallery-panel");
  g.classList.toggle("collapsed");
  $("toggle-gallery").textContent = g.classList.contains("collapsed") ? "‹" : "›";
});

/* ── Upload zone ── */
uploadZone.addEventListener("click", () => !refFile && refInput.click());
uploadZone.addEventListener("dragover", e => { e.preventDefault(); uploadZone.classList.add("drag"); });
uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("drag"));
uploadZone.addEventListener("drop", e => {
  e.preventDefault();
  uploadZone.classList.remove("drag");
  const f = e.dataTransfer.files[0];
  if (f && f.type.startsWith("image/")) setRef(f);
});
refInput.addEventListener("change", () => refInput.files[0] && setRef(refInput.files[0]));
removeRef.addEventListener("click", e => { e.stopPropagation(); clearRef(); });

function setRef(file) {
  refFile = file;
  const url = URL.createObjectURL(file);
  refPreview.src = url;
  refPreview.classList.remove("hidden");
  uploadInner.classList.add("hidden");
  removeRef.classList.remove("hidden");
}
function clearRef() {
  refFile = null;
  refPreview.classList.add("hidden");
  refPreview.src = "";
  uploadInner.classList.remove("hidden");
  removeRef.classList.add("hidden");
  refInput.value = "";
}

/* ── Generate ── */
generateBtn.addEventListener("click", generate);
prompt.addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) generate();
});

async function generate() {
  const p = prompt.value.trim();
  if (!p) { showError("Enter a prompt."); return; }
  if (!$("ack-checkbox").checked) {
    showError("Please accept the content policy before generating.");
    return;
  }

  setLoading(true);
  hideError();

  const fd = new FormData();
  fd.append("prompt", p);
  fd.append("preset", selectedPreset);
  fd.append("model",  $("model-select").value);
  fd.append("seed",   seedInput.value);
  fd.append("width",  widthInput.value);
  fd.append("height", heightInput.value);
  fd.append("ack",        $("ack-checkbox").checked ? "1" : "");
  fd.append("form_token", $("form-token").value);
  fd.append("website",    $("website").value);   // honeypot (stays empty for humans)
  // Turnstile token, only present when CAPTCHA_SITEKEY is configured server-side.
  const captcha = document.querySelector('[name="cf-turnstile-response"]');
  if (captcha) fd.append("captcha_token", captcha.value);
  if (refFile) fd.append("ref", refFile);

  try {
    const res  = await fetch("/api/generate", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Generation failed");

    showImage(data);
    setTokens(data.tokens_in, data.tokens_out);
    await loadGallery();
    await loadFolders();
    selectThumb(data.id);
  } catch (err) {
    showError(err.message);
  } finally {
    setLoading(false);
  }
}

/* ── Display image ── */
function showImage(img) {
  activeImage = img;
  resultImg.src = img.url + "?t=" + Date.now();
  resultImg.classList.remove("hidden");
  emptyState.classList.add("hidden");
  imgMeta.classList.remove("hidden");

  downloadBtn.href     = img.url;
  downloadBtn.download = img.filename || "poly.png";

  const svgBtn = $("svg-download-btn");
  if (img.svg_url) {
    svgBtn.href     = img.svg_url;
    svgBtn.download = (img.filename || "poly").replace(".png", ".svg");
    svgBtn.classList.remove("hidden");
  } else {
    svgBtn.classList.add("hidden");
  }

  renderMetaTags(img.tags || []);
  populateFolderSelect(img.folders || []);
}

function renderMetaTags(tags) {
  metaTags.innerHTML = tags.map(t => `
    <span class="tag-chip">
      ${t}
      <button onclick="removeTag('${t}')" title="Remove tag">✕</button>
    </span>
  `).join("");
}

async function populateFolderSelect(current) {
  const res  = await fetch("/api/folders");
  const all  = await res.json();
  folderSelect.innerHTML = '<option value="">Add to folder…</option>';
  all.forEach(f => {
    if (!current.includes(f.name)) {
      const o = document.createElement("option");
      o.value = o.textContent = f.name;
      folderSelect.appendChild(o);
    }
  });
}

/* ── Tokens ── */
function setTokens(inp, out) {
  tIn.textContent    = inp.toLocaleString();
  tOut.textContent   = out.toLocaleString();
  tTotal.textContent = (inp + out).toLocaleString();
  tokenMeter.classList.remove("hidden");
}

/* ── Tags ── */
addTagBtn.addEventListener("click", async () => {
  if (!activeImage) return;
  const name = tagInput.value.trim().toLowerCase();
  if (!name) return;
  await fetch(`/api/images/${activeImage.id}/tags`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name })
  });
  tagInput.value = "";
  activeImage.tags = [...(activeImage.tags || []), name];
  renderMetaTags(activeImage.tags);
  await loadTagBar();
});
tagInput.addEventListener("keydown", e => e.key === "Enter" && addTagBtn.click());

async function removeTag(name) {
  if (!activeImage) return;
  await fetch(`/api/images/${activeImage.id}/tags/${name}`, { method: "DELETE" });
  activeImage.tags = (activeImage.tags || []).filter(t => t !== name);
  renderMetaTags(activeImage.tags);
  await loadTagBar();
}

/* ── Folders ── */
folderSelect.addEventListener("change", async () => {
  if (!activeImage || !folderSelect.value) return;
  const name = folderSelect.value;
  await fetch(`/api/images/${activeImage.id}/folders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name })
  });
  activeImage.folders = [...(activeImage.folders || []), name];
  populateFolderSelect(activeImage.folders);
  await loadFolders();
});

createFolderBtn.addEventListener("click", async () => {
  const name = newFolderInput.value.trim();
  if (!name) return;
  await fetch("/api/folders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name })
  });
  newFolderInput.value = "";
  await loadFolders();
});

/* ── Delete ── */
deleteBtn.addEventListener("click", async () => {
  if (!activeImage || !confirm("Delete this image?")) return;
  await fetch(`/api/images/${activeImage.id}`, { method: "DELETE" });
  activeImage = null;
  resultImg.classList.add("hidden");
  emptyState.classList.remove("hidden");
  imgMeta.classList.add("hidden");
  tokenMeter.classList.add("hidden");
  await loadGallery();
  await loadFolders();
});

/* ── Gallery ── */
const imageCache = {};

async function loadGallery() {
  const params = new URLSearchParams();
  if (searchQ)      params.set("q",      searchQ);
  if (activeFolder) params.set("folder", activeFolder);
  if (activeTag)    params.set("tag",    activeTag);

  const res    = await fetch("/api/images?" + params);
  const images = await res.json();

  if (!images.length) {
    galleryGrid.innerHTML = '<div class="gallery-empty">No images yet</div>';
    return;
  }

  images.forEach(img => imageCache[img.id] = img);

  galleryGrid.innerHTML = images.map(img => `
    <div class="gallery-thumb ${activeImage?.id === img.id ? 'active' : ''}"
         data-id="${img.id}" onclick="loadImageById('${img.id}')">
      <img src="${img.thumb_url}" alt="" loading="lazy">
      <div class="thumb-prompt">${img.prompt || ''}</div>
    </div>
  `).join("");

  await loadTagBar();
}

function loadImageById(id) {
  const img = imageCache[id];
  if (!img) return;
  showImage(img);
  setTokens(img.tokens_in || 0, img.tokens_out || 0);
  selectThumb(img.id);
}

function selectThumb(id) {
  document.querySelectorAll(".gallery-thumb").forEach(t => {
    t.classList.toggle("active", t.dataset.id === id);
  });
}

/* ── Folders sidebar ── */
async function loadFolders() {
  const res     = await fetch("/api/folders");
  const folders = await res.json();

  // rebuild folder bar (keep "All")
  const existing = folderBar.querySelector('[data-folder=""]');
  folderBar.innerHTML = "";
  folderBar.appendChild(existing);

  folders.forEach(f => {
    const chip = document.createElement("button");
    chip.className = "folder-chip" + (activeFolder === f.name ? " active" : "");
    chip.dataset.folder = f.name;
    chip.innerHTML = `${f.name} <span style="color:var(--muted);font-size:10px">${f.count}</span>
      <button class="del-folder" onclick="deleteFolder(event,'${f.name}')">✕</button>`;
    chip.addEventListener("click", () => setFolder(f.name));
    folderBar.appendChild(chip);
  });
}

function setFolder(name) {
  activeFolder = name;
  document.querySelectorAll(".folder-chip").forEach(c => {
    c.classList.toggle("active", c.dataset.folder === name);
  });
  loadGallery();
}

async function deleteFolder(e, name) {
  e.stopPropagation();
  if (!confirm(`Delete folder "${name}"?`)) return;
  await fetch(`/api/folders/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (activeFolder === name) activeFolder = "";
  await loadFolders();
  await loadGallery();
}

/* ── Tag bar ── */
async function loadTagBar() {
  const res  = await fetch("/api/tags");
  const tags = await res.json();
  tagBar.innerHTML = tags.map(t => `
    <button class="tag-filter ${activeTag === t.name ? 'active' : ''}"
            onclick="setTag('${t.name}')">
      ${t.name} <span style="opacity:0.5">${t.count}</span>
    </button>
  `).join("");
}

function setTag(name) {
  activeTag = activeTag === name ? "" : name;
  loadTagBar();
  loadGallery();
}

/* ── Search ── */
let searchTimer;
searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchQ = searchInput.value.trim();
    loadGallery();
  }, 300);
});

/* ── Utils ── */
function setLoading(on) {
  generateBtn.disabled = on;
  spinner.classList.toggle("hidden", !on);
  btnLabel.textContent = on ? "Generating…" : "Generate";
}
function showError(msg) { errorBox.textContent = msg; errorBox.classList.remove("hidden"); }
function hideError()    { errorBox.classList.add("hidden"); }

/* ── Init ── */
loadGallery();
loadFolders();
