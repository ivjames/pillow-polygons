#!/bin/bash
set -e
BASE=/var/www/poly

cat > $BASE/templates/index.html << 'EOF_HTML'
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pillow Polygons</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/css/app.css">
</head>
<body>
  <div class="app">

    <!-- ── BANNER ── -->
    <header class="banner-header">
      <img src="/static/banner.png" alt="Pillow Polygons">
    </header>

    <!-- ── LEFT SIDEBAR: Controls ── -->
    <aside class="sidebar" id="sidebar">
      <div class="sidebar-header">
        <span class="sidebar-logo">⬡ Controls</span>
        <button class="icon-btn" id="toggle-sidebar" title="Collapse">‹</button>
      </div>

      <div class="sidebar-body">
        <div class="field">
          <label>Prompt</label>
          <textarea id="prompt" rows="5" placeholder="Describe your scene… e.g. 'a lighthouse at night in swamp preset'"></textarea>
        </div>

        <div class="field">
          <label>Model</label>
          <select id="model-select">
            <option value="claude-haiku-4-5">Haiku — fast &amp; cheap</option>
            <option value="claude-sonnet-4-6" selected>Sonnet — default</option>
            <option value="claude-opus-4-6">Opus — best</option>
          </select>
        </div>

                <div class="field">
          <label>Preset palette</label>
          <div class="preset-grid">
            <button class="preset-btn active" data-preset="">None</button>
            <button class="preset-btn" data-preset="night">Night</button>
            <button class="preset-btn" data-preset="golden">Golden</button>
            <button class="preset-btn" data-preset="swamp">Swamp</button>
            <button class="preset-btn" data-preset="bone">Bone</button>
          </div>
        </div>

        <div class="field row">
          <div class="half">
            <label>Width px</label>
            <input type="number" id="width" value="1024" min="256" max="2048" step="128">
          </div>
          <div class="half">
            <label>Height px</label>
            <input type="number" id="height" value="1024" min="256" max="2048" step="128">
          </div>
        </div>

        <div class="field">
          <label>Seed</label>
          <div class="seed-row">
            <input type="number" id="seed" value="42" min="0" max="99999">
            <button class="icon-btn dark" id="random-seed" title="Random seed">⟳</button>
          </div>
        </div>

        <div class="field">
          <label>Reference image <span class="muted">optional — sampled for color</span></label>
          <div class="upload-zone" id="upload-zone">
            <input type="file" id="ref-input" accept="image/*" hidden>
            <div class="upload-inner" id="upload-inner">
              <span class="upload-icon">⊕</span>
              <span>Drop or click to upload</span>
            </div>
            <img id="ref-preview" class="ref-preview hidden" alt="Reference">
            <button class="remove-ref hidden" id="remove-ref">✕</button>
          </div>
        </div>

        <button class="generate-btn" id="generate-btn">
          <span id="btn-label">Generate</span>
          <svg class="spinner hidden" id="spinner" width="20" height="20" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
          <polygon id="spin-hex" points="10,1 17,5 17,15 10,19 3,15 3,5" stroke="white" stroke-width="1.5" fill="none" stroke-linejoin="round"/>
          <polygon id="spin-tri" points="10,4 15,13 5,13" stroke="white" stroke-width="1" fill="white" opacity="0.4"/>
        </svg>
        </button>

        <div class="token-meter hidden" id="token-meter">
          <div class="token-row">
            <span class="token-label">IN</span>
            <span class="token-val in-val" id="t-in">—</span>
          </div>
          <div class="token-row">
            <span class="token-label">OUT</span>
            <span class="token-val out-val" id="t-out">—</span>
          </div>
          <div class="token-row">
            <span class="token-label">TOTAL</span>
            <span class="token-val tot-val" id="t-total">—</span>
          </div>
        </div>

        <div class="error-box hidden" id="error-box"></div>
      </div>
    </aside>

    <!-- ── CENTER: Image display ── -->
    <main class="canvas-area">
      <div class="image-frame" id="image-frame">

        <div class="empty-state" id="empty-state">
          <div class="empty-diamonds">
            <div class="empty-diamond" style="background:#ff3b7a"></div>
            <div class="empty-diamond" style="background:#1a90ff"></div>
            <div class="empty-diamond" style="background:#22dd88"></div>
            <div class="empty-diamond" style="background:#ffe000"></div>
          </div>
          <p>Enter a prompt and hit Generate<br><span style="font-size:11px;opacity:0.6">⌘↵ to generate</span></p>
        </div>

        <img id="result-img" class="result-img hidden" alt="Generated">

        <div class="img-meta hidden" id="img-meta">
          <div class="meta-tags" id="meta-tags"></div>
          <div class="meta-actions">
            <input type="text" id="tag-input" placeholder="Add tag…" class="inline-input" style="width:110px">
            <button class="small-btn" id="add-tag-btn">+ Tag</button>
            <select id="folder-select" class="inline-input">
              <option value="">Add to folder…</option>
            </select>
            <a id="download-btn" class="small-btn" download>↓ PNG</a>
            <a id="svg-download-btn" class="small-btn hidden" download>↓ SVG</a>
            <button class="small-btn danger" id="delete-btn">✕ Delete</button>
          </div>
        </div>

      </div>
    </main>

    <!-- ── RIGHT PANEL: Gallery ── -->
    <aside class="gallery-panel" id="gallery-panel">
      <div class="gallery-header">
        <input type="text" id="search-input" class="search-input" placeholder="Search prompts…">
        <button class="icon-btn" id="toggle-gallery" title="Collapse">›</button>
      </div>

      <div class="folder-bar" id="folder-bar">
        <button class="folder-chip active" data-folder="">All</button>
      </div>

      <div class="tag-bar" id="tag-bar"></div>

      <div class="gallery-grid" id="gallery-grid">
        <div class="gallery-empty">No images yet</div>
      </div>

      <div class="folder-controls">
        <input type="text" id="new-folder-input" class="inline-input" placeholder="New folder…">
        <button class="small-btn" id="create-folder-btn">+ Folder</button>
      </div>
    </aside>

  </div>

    <div class="lightbox-inner">
      <button class="lightbox-close" id="lightbox-close">✕</button>
      <img class="lightbox-img" id="lightbox-img" alt="">
      <div class="lightbox-caption" id="lightbox-caption"></div>
    </div>
  </div>
  <script src="/static/js/app.js"></script>
</body>
</html>
EOF_HTML

cat > $BASE/static/css/app.css << 'EOF_CSS'
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:        #fff8f0;
  --panel:     #ffffff;
  --card:      #f5f0ff;
  --border:    #e0d0ff;
  --pink:      #ff3b7a;
  --blue:      #1a90ff;
  --lime:      #22dd88;
  --orange:    #ff8c00;
  --purple:    #8b2be2;
  --yellow:    #ffe000;
  --text:      #1a0a2e;
  --muted:     #8870aa;
  --danger:    #ff3b3b;
  --mono:      'Space Mono', monospace;
  --sans:      'Space Grotesk', sans-serif;
  --sidebar-w: 290px;
  --gallery-w: 310px;
}

html, body {
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 14px;
}

/* ── Layout ── */
.app {
  display: grid;
  grid-template-columns: var(--sidebar-w) 1fr var(--gallery-w);
  grid-template-rows: auto 1fr;
  height: 100vh;
  overflow: hidden;
}

/* ── Banner header ── */
.banner-header {
  grid-column: 1 / -1;
  background: #fff0f5;
  border-bottom: 3px solid var(--pink);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  height: 90px;
  flex-shrink: 0;
}
.banner-header img {
  height: 100%;
  width: 100%;
  object-fit: cover;
  object-position: center top;
}

/* ── Sidebar ── */
.sidebar {
  background: var(--panel);
  border-right: 3px solid var(--purple);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.2s ease;
}
.sidebar.collapsed { width: 42px; }
.sidebar.collapsed .sidebar-body,
.sidebar.collapsed .sidebar-logo { display: none; }

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  background: var(--purple);
  flex-shrink: 0;
}
.sidebar-logo {
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: #fff;
  text-transform: uppercase;
}

.sidebar-body {
  padding: 14px;
  overflow-y: auto;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* ── Fields ── */
.field { display: flex; flex-direction: column; gap: 5px; }
.field.row { flex-direction: row; gap: 10px; }
.half { flex: 1; display: flex; flex-direction: column; gap: 5px; }

label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
  color: var(--purple);
  text-transform: uppercase;
}
.muted { color: var(--muted); font-weight: 400; text-transform: none; font-size: 10px; }

textarea, input[type="text"], input[type="number"], select {
  background: var(--card);
  border: 2px solid var(--border);
  color: var(--text);
  font-family: var(--sans);
  font-size: 13px;
  padding: 7px 10px;
  border-radius: 8px;
  width: 100%;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
textarea:focus, input:focus, select:focus {
  border-color: var(--blue);
  box-shadow: 0 0 0 3px rgba(26,144,255,0.15);
}
textarea { resize: vertical; min-height: 85px; }

/* ── Preset grid ── */
.preset-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px; }
.preset-btn {
  background: #fff;
  border: 2px solid var(--border);
  color: var(--muted);
  font-family: var(--sans);
  font-size: 10px;
  font-weight: 600;
  padding: 5px 2px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.12s;
  text-align: center;
}
.preset-btn:hover { border-color: var(--pink); color: var(--pink); transform: translateY(-1px); }
.preset-btn.active { border-color: var(--pink); color: #fff; background: var(--pink); }

/* ── Seed row ── */
.seed-row { display: flex; gap: 6px; }
.seed-row input { flex: 1; }

/* ── Upload zone ── */
.upload-zone {
  border: 2px dashed var(--border);
  border-radius: 8px;
  position: relative;
  min-height: 70px;
  cursor: pointer;
  transition: all 0.15s;
  overflow: hidden;
  background: var(--card);
}
.upload-zone:hover, .upload-zone.drag {
  border-color: var(--blue);
  background: #eef6ff;
}
.upload-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 14px;
  color: var(--muted);
  font-size: 12px;
}
.upload-icon { font-size: 22px; color: var(--blue); }
.ref-preview {
  width: 100%;
  height: 110px;
  object-fit: cover;
  display: block;
}
.remove-ref {
  position: absolute;
  top: 5px; right: 5px;
  background: var(--pink);
  border: none;
  color: #fff;
  border-radius: 50%;
  width: 22px; height: 22px;
  font-size: 11px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  font-weight: 700;
}

/* ── Generate button ── */
.generate-btn {
  background: linear-gradient(135deg, var(--pink), var(--purple));
  color: #fff;
  border: none;
  font-family: var(--sans);
  font-size: 14px;
  font-weight: 700;
  padding: 12px;
  border-radius: 10px;
  cursor: pointer;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  transition: all 0.15s;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  box-shadow: 0 4px 14px rgba(139,43,226,0.3);
}
.generate-btn:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(139,43,226,0.45);
}
.generate-btn:disabled {
  background: var(--border);
  color: var(--muted);
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
}
.spinner { display: inline-block; }
#spin-hex { animation: spin-hex 1.2s linear infinite; transform-origin: 10px 10px; }
#spin-tri { animation: spin-tri 1.2s ease-in-out infinite reverse; transform-origin: 10px 10px; }
@keyframes spin-hex { to { transform: rotate(360deg); } }
@keyframes spin-tri { 
  0%   { transform: rotate(0deg) scale(1); opacity: 0.4; }
  50%  { transform: rotate(180deg) scale(0.6); opacity: 0.9; }
  100% { transform: rotate(360deg) scale(1); opacity: 0.4; }
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Token meter ── */
.token-meter {
  background: #1a0a2e;
  border: 2px solid var(--purple);
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.token-row { display: flex; justify-content: space-between; align-items: center; }
.token-label {
  font-family: var(--mono);
  font-size: 9px;
  color: var(--muted);
  letter-spacing: 0.12em;
}
.token-val {
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 700;
}
.token-val.in-val   { color: var(--blue); }
.token-val.out-val  { color: var(--lime); }
.token-val.tot-val  { color: var(--yellow); }

/* ── Error ── */
.error-box {
  background: #fff0f0;
  border: 2px solid var(--danger);
  color: #cc0000;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.5;
}

/* ── Canvas area ── */
.canvas-area {
  display: flex;
  align-items: stretch;
  justify-content: center;
  background: var(--bg);
  overflow: hidden;
  padding: 20px;
  /* diagonal stripe bg — playful */
  background-image: repeating-linear-gradient(
    45deg,
    transparent,
    transparent 28px,
    rgba(139,43,226,0.04) 28px,
    rgba(139,43,226,0.04) 30px
  );
}
.image-frame {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  max-width: 900px;
}
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  color: var(--muted);
  text-align: center;
}
.empty-diamonds {
  display: flex;
  gap: 16px;
}
.empty-diamond {
  width: 32px; height: 32px;
  transform: rotate(45deg);
  border-radius: 4px;
}
.empty-state p { font-size: 13px; color: var(--muted); }

.result-img {
  max-width: 100%;
  max-height: calc(100vh - 240px);
  object-fit: contain;
  border-radius: 8px;
  box-shadow:
    0 0 0 3px var(--pink),
    0 0 0 6px var(--blue),
    0 12px 40px rgba(0,0,0,0.15);
}

/* ── Image meta strip ── */
.img-meta {
  width: 100%;
  max-width: 700px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.meta-tags { display: flex; flex-wrap: wrap; gap: 6px; min-height: 24px; }
.tag-chip {
  background: var(--purple);
  color: #fff;
  font-size: 11px;
  font-family: var(--mono);
  padding: 3px 9px;
  border-radius: 20px;
  display: flex;
  align-items: center;
  gap: 5px;
}
.tag-chip button {
  background: none;
  border: none;
  color: rgba(255,255,255,0.6);
  cursor: pointer;
  font-size: 10px;
  padding: 0;
  line-height: 1;
}
.tag-chip button:hover { color: var(--yellow); }

.meta-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.inline-input {
  background: #fff;
  border: 2px solid var(--border);
  color: var(--text);
  font-family: var(--sans);
  font-size: 12px;
  padding: 5px 8px;
  border-radius: 6px;
  outline: none;
  transition: border-color 0.15s;
}
.inline-input:focus { border-color: var(--blue); }

/* ── Buttons ── */
.small-btn {
  background: #fff;
  border: 2px solid var(--border);
  color: var(--text);
  font-family: var(--sans);
  font-size: 12px;
  font-weight: 600;
  padding: 5px 10px;
  border-radius: 6px;
  cursor: pointer;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  transition: all 0.12s;
  white-space: nowrap;
}
.small-btn:hover { border-color: var(--blue); color: var(--blue); transform: translateY(-1px); }
.small-btn.danger:hover { border-color: var(--danger); color: var(--danger); }

.icon-btn {
  background: rgba(255,255,255,0.2);
  border: none;
  color: #fff;
  cursor: pointer;
  font-size: 18px;
  padding: 2px 7px;
  border-radius: 4px;
  transition: background 0.15s;
  flex-shrink: 0;
  line-height: 1.4;
}
.icon-btn:hover { background: rgba(255,255,255,0.35); }
.icon-btn.dark {
  background: var(--card);
  color: var(--muted);
}
.icon-btn.dark:hover { background: var(--border); color: var(--text); }

/* ── Gallery panel ── */
.gallery-panel {
  background: var(--panel);
  border-left: 3px solid var(--blue);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.2s ease;
}
.gallery-panel.collapsed { width: 42px; }
.gallery-panel.collapsed .gallery-grid,
.gallery-panel.collapsed .folder-bar,
.gallery-panel.collapsed .tag-bar,
.gallery-panel.collapsed .folder-controls,
.gallery-panel.collapsed .search-input { display: none; }

.gallery-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background: var(--blue);
  flex-shrink: 0;
}
.search-input {
  flex: 1;
  background: rgba(255,255,255,0.9);
  border: 2px solid transparent;
  color: var(--text);
  font-family: var(--sans);
  font-size: 12px;
  padding: 5px 10px;
  border-radius: 6px;
  outline: none;
}
.search-input:focus { border-color: #fff; }

/* ── Folder bar ── */
.folder-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 8px 10px;
  border-bottom: 2px solid var(--border);
  flex-shrink: 0;
  background: #f0f8ff;
}
.folder-chip {
  background: #fff;
  border: 2px solid var(--border);
  color: var(--muted);
  font-family: var(--sans);
  font-size: 11px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.12s;
  display: flex;
  align-items: center;
  gap: 4px;
}
.folder-chip:hover { border-color: var(--blue); color: var(--blue); }
.folder-chip.active { border-color: var(--blue); color: #fff; background: var(--blue); }
.folder-chip .del-folder {
  font-size: 10px;
  background: none;
  border: none;
  cursor: pointer;
  color: rgba(255,255,255,0.6);
  padding: 0;
  line-height: 1;
}
.folder-chip:not(.active) .del-folder { color: var(--muted); }
.folder-chip .del-folder:hover { color: var(--danger); }

/* ── Tag bar ── */
.tag-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 6px 10px;
  border-bottom: 2px solid var(--border);
  flex-shrink: 0;
  min-height: 34px;
  background: #fdf5ff;
}
.tag-filter {
  background: #fff;
  border: 2px solid var(--border);
  color: var(--muted);
  font-family: var(--mono);
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 20px;
  cursor: pointer;
  transition: all 0.12s;
  font-weight: 700;
}
.tag-filter:hover { border-color: var(--purple); color: var(--purple); }
.tag-filter.active { border-color: var(--purple); color: #fff; background: var(--purple); }

/* ── Gallery grid ── */
.gallery-grid {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  align-content: start;
}
.gallery-empty {
  color: var(--muted);
  font-size: 12px;
  padding: 20px;
  grid-column: 1/-1;
  text-align: center;
}

.gallery-thumb {
  aspect-ratio: 1;
  border-radius: 6px;
  overflow: hidden;
  cursor: pointer;
  border: 3px solid transparent;
  transition: all 0.15s;
  position: relative;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
}
.gallery-thumb:hover { border-color: var(--pink); transform: scale(1.03); }
.gallery-thumb.active { border-color: var(--pink); box-shadow: 0 0 0 2px var(--purple); }
.gallery-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.gallery-thumb .thumb-prompt {
  position: absolute;
  bottom: 0; left: 0; right: 0;
  background: linear-gradient(transparent, rgba(26,10,46,0.9));
  color: #fff;
  font-size: 9px;
  padding: 16px 5px 4px;
  opacity: 0;
  transition: opacity 0.15s;
  line-height: 1.3;
}
.gallery-thumb:hover .thumb-prompt { opacity: 1; }

/* SVG badge */
.svg-badge {
  position: absolute;
  top: 5px; right: 5px;
  background: var(--lime);
  color: #1a0a2e;
  font-family: var(--mono);
  font-size: 8px;
  font-weight: 700;
  padding: 2px 5px;
  border-radius: 3px;
  opacity: 0;
  transition: opacity 0.15s;
}
.gallery-thumb:hover .svg-badge { opacity: 1; }

/* ── Folder controls ── */
.folder-controls {
  display: flex;
  gap: 6px;
  padding: 10px;
  border-top: 2px solid var(--border);
  flex-shrink: 0;
  background: #f0f8ff;
}
.folder-controls .inline-input { flex: 1; }

/* ── Utilities ── */
.hidden { display: none !important; }
EOF_CSS

cat > $BASE/static/js/app.js << 'EOF_JS'
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

  setLoading(true);
  hideError();

  const fd = new FormData();
  fd.append("prompt", p);
  fd.append("preset", selectedPreset);
  fd.append("model",  $("model-select").value);
  fd.append("seed",   seedInput.value);
  fd.append("width",  widthInput.value);
  fd.append("height", heightInput.value);
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
  // set prompt data attrs (safe for any chars)
  document.querySelectorAll('[data-id]').forEach(el => {
    const found = images.find(i => i.id === el.dataset.id);
    if (found) el.dataset.prompt = found.prompt || '';
  });
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

  galleryGrid.innerHTML = images.map(img => `
    <div class="gallery-thumb ${activeImage?.id === img.id ? 'active' : ''}"
         data-id="${img.id}" data-url="${img.url}" data-prompt=""
         onclick="loadImage(${JSON.stringify(JSON.stringify(img))})">
      <img src="${img.thumb_url}" alt="" loading="lazy">
      <div class="thumb-prompt">${img.prompt || ''}</div>
    </div>
  `).join("");

  await loadTagBar();
}

function loadImage(jsonStr) {
  const img = JSON.parse(jsonStr);
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


/* ── Lightbox ── */
const lightbox         = $("lightbox");
const lightboxImg      = $("lightbox-img");
const lightboxCaption  = $("lightbox-caption");
const lightboxClose    = $("lightbox-close");
const lightboxBackdrop = $("lightbox-backdrop");

function openLightbox(url, caption) {
  lightboxImg.src = url;
  lightboxCaption.textContent = caption || '';
  lightbox.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}
function closeLightbox() {
  lightbox.classList.add('hidden');
  lightboxImg.src = '';
  document.body.style.overflow = '';
}

lightboxClose.addEventListener('click', closeLightbox);
lightboxBackdrop.addEventListener('click', closeLightbox);
document.addEventListener('keydown', e => e.key === 'Escape' && closeLightbox());

/* ── Init ── */
loadGallery();
loadFolders();
EOF_JS

pm2 restart poly-app
echo 'Done'
