/**
 * ComfyUI Assistant — 前端主逻辑
 * =================================
 * 负责页面导航、提示词管理、LoRA 面板、生成队列、
 * 资产/收藏/历史视图渲染、与后端 API 通信。
 *
 * 关键机制：
 * - 提示词实时同步到 ComfyUI 画布（Debounce 150ms）
 * - 生成前通过 sync_check 确认前端已同步（最多重试3次）
 * - 生成后轮询资产 API 直到图片出现在输出目录
 * - ComfyUI WebSocket 接收实时进度百分比
 */

// ---- 页面配置 ----------------------------------------------------------
const pages = {
  generate: { title: "生成", sub: "工作流控制台" },
  assets: { title: "我的资产", sub: "生成的图片与提示词" },
  library: { title: "收藏", sub: "提示词与图片收藏" },
  history: { title: "历史记录", sub: "最近的生成记录" },
  settings: { title: "设置", sub: "连接与保存目录" },
};

// ---- 预设参数 ----------------------------------------------------------
const RATIO_PRESETS = {
  "1:1": [1024, 1024],
  "2:3": [832, 1248],
  "3:4": [896, 1152],
  "16:9": [1344, 768],
  "9:16": [768, 1344],
};

const TYPE_STYLES = {
  "写实": "realistic photo, highly detailed",
  "动漫": "anime style, clean lines",
  "3D渲染": "3D render, octane, high quality",
  "插画": "illustration, painterly",
  "电影感": "cinematic lighting, film grain",
  "赛博朋克": "cyberpunk, neon lights, futuristic city",
  "水墨": "ink wash painting, traditional chinese art",
  "像素风": "pixel art, retro game",
  "哥特": "gothic, dark fantasy",
  "未来主义": "futuristic, sci-fi",
};
const TYPE_STYLES_RE = new RegExp(
  Object.values(TYPE_STYLES).map((text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"),
  "gi"
);

// ---- 全局状态 ----------------------------------------------------------
let dictFiles = [];
let nsfwTotal = 0;
let includeNsfw = false;
let activeCat = "";
let allLoras = [];
let loraList = [];
let currentModel = "Z-Image Turbo";
let assets = [];
let historyItems = [];
let favorites = [];
const favoritedPrompts = new Set();
const favoritedImages = new Set();
const favoritePromptIds = new Map();
const favoriteImageIds = new Map();
const favoritePromptPairIds = new Map();
let assetPollTimer = null;
let queueTasks = [];
let loraNotes = {};
let favTab = "prompt";
let assetTab = "unsaved";
let assetSearchQuery = "";
let historySearchQuery = "";
let assetModelFilter = "";
let historyModelFilter = "";
let progressWs = null;
let promptSyncTimer = null;
const syncedQueueIds = new Set();
const modelAvailability = new Map();

function promptFavoriteKey(prompt, negative) {
  return `${prompt || ""}\u0000${negative || ""}`;
}

function currentPromptFavoriteKey() {
  return promptFavoriteKey(
    document.getElementById("positivePrompt").value,
    document.getElementById("negativePrompt").value,
  );
}

function randomSeed() {
  const cryptoValue = globalThis.crypto?.getRandomValues
    ? globalThis.crypto.getRandomValues(new Uint32Array(1))[0]
    : Math.floor(Math.random() * 4294967296);
  return cryptoValue;
}

function itemModel(item) {
  return item?.model || parseParams(item || {}).model || "";
}

function matchesSearch(item, query) {
  if (!query) return true;
  const params = parseParams(item);
  const haystack = [
    item.path, item.image_path, item.title, item.prompt, item.negative,
    item.model, params.prompt, params.negative, params.model,
  ].filter(Boolean).join(" ").toLowerCase();
  return haystack.includes(query);
}

// ---- 通用工具：API 请求封装 ---------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

// ---- 提示词字典 ----------------------------------------------------------
function currentDictFile() {
  const select = document.getElementById("dictFileSelect");
  return dictFiles.find((file) => file.id === select.value) || dictFiles[0] || null;
}

function renderDictFileOptions() {
  const select = document.getElementById("dictFileSelect");
  select.innerHTML = "";
  for (const file of dictFiles) {
    const option = document.createElement("option");
    option.value = file.id;
    option.textContent = file.label + (file.nsfw ? "（NSFW）" : "");
    select.appendChild(option);
  }
}

function renderDict() {
  const file = currentDictFile();
  const catsEl = document.querySelector(".dict-cats");
  const list = document.getElementById("dictList");
  const status = document.querySelector(".dict-count");
  if (!file) {
    catsEl.innerHTML = "";
    list.innerHTML = `<div class="dict-item en">暂无提示词，请检查 Markdown 文件目录</div>`;
    status.textContent = "0 条";
    return;
  }

  catsEl.innerHTML = "";
  const allBtn = document.createElement("button");
  allBtn.className = "cat" + (activeCat ? "" : " active");
  allBtn.textContent = "全部";
  allBtn.addEventListener("click", () => {
    activeCat = "";
    renderDict();
  });
  catsEl.appendChild(allBtn);
  for (const category of file.categories || []) {
    const btn = document.createElement("button");
    btn.className = "cat" + (activeCat === category.label ? " active" : "");
    btn.textContent = category.label;
    btn.addEventListener("click", () => {
      activeCat = category.label;
      renderDict();
    });
    catsEl.appendChild(btn);
  }

  const query = document.getElementById("dictSearch").value.trim().toLowerCase();
  list.innerHTML = "";
  let count = 0;
  for (const category of file.categories || []) {
    if (activeCat && category.label !== activeCat) {
      continue;
    }
    for (const item of category.items || []) {
      const text = `${item.prompt} ${item.meaning} ${item.note || ""}`.toLowerCase();
      if (query && !text.includes(query)) {
        continue;
      }
      const el = document.createElement("div");
      el.className = "dict-item" + (file.target === "negative" ? " neg" : "");
      el.draggable = true;
      el.innerHTML = `<div class="en">${escapeHtml(item.prompt)}</div><div class="cn">${escapeHtml(item.meaning)}</div>`;
      el.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", item.prompt);
      });
      el.addEventListener("click", () => {
        const target = file.target === "negative"
          ? document.getElementById("negativePrompt")
          : document.getElementById("positivePrompt");
        target.value += (target.value && !target.value.endsWith(", ") ? ", " : "") + item.prompt;
        target.focus();
        updateFavoriteButtons();
      });
      el.addEventListener("dblclick", () => {
        const target = file.target === "negative"
          ? document.getElementById("negativePrompt")
          : document.getElementById("positivePrompt");
        target.value += (target.value && !target.value.endsWith(", ") ? ", " : "") + item.prompt;
        target.focus();
        updateFavoriteButtons();
      });
      list.appendChild(el);
      count += 1;
    }
  }
  status.textContent = `${count} 条`;
}

// ---- XSS 防护与内联 SVG 图标-----------------------------------------------
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

const ICON_VIEW = `<svg viewBox="0 0 24 24"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>`;
const ICON_SAVE = `<svg viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>`;
const ICON_STAR = `<svg viewBox="0 0 24 24"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>`;
const ICON_TRASH = `<svg viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>`;
const ICON_FILL = `<svg viewBox="0 0 24 24"><path d="M4 20h16"/><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 4h6"/></svg>`;

// ---- 模态框：确认对话框 ---------------------------------------------------
function showConfirm(title, message, onConfirm) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal">
      <h3>${escapeHtml(title)}</h3>
      <p>${escapeHtml(message)}</p>
      <div class="modal-actions">
        <button class="btn-secondary" data-action="cancel">取消</button>
        <button class="btn-danger" data-action="ok">确认</button>
      </div>
    </div>`;
  overlay.querySelector('[data-action="cancel"]').addEventListener("click", () => overlay.remove());
  overlay.querySelector('[data-action="ok"]').addEventListener("click", () => {
    overlay.remove();
    onConfirm();
  });
  document.body.appendChild(overlay);
}

// ---- 模态框：图片预览（带缩放/拖拽）-------------------------------------
function showImagePreview(path, title = "图片预览") {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal preview-window" role="dialog" aria-modal="true">
      <div class="preview-titlebar">
        <span>${escapeHtml(title)}</span>
        <div class="preview-title-actions">
          <button class="icon-btn" data-action="maximize" title="最大化"><svg viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="1"/></svg></button>
          <button class="icon-btn" data-action="close" title="关闭"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
        </div>
      </div>
      <div class="preview-stage">
        <img src="/api/file?path=${encodeURIComponent(path)}" alt="${escapeHtml(title)}">
      </div>
      <div class="preview-tools">
        <button class="btn-secondary" data-action="zoom-out">缩小</button>
        <button class="btn-secondary" data-action="zoom-in">放大</button>
        <button class="btn-secondary" data-action="reset">重置</button>
        <button class="btn-secondary" data-action="system">系统打开</button>
      </div>
    </div>`;
  const win = overlay.querySelector(".preview-window");
  const img = overlay.querySelector("img");
  const titlebar = overlay.querySelector(".preview-titlebar");
  let zoom = 1;
  let tx = 0;
  let ty = 0;
  let dragging = false;
  let startX = 0;
  let startY = 0;
  let startTx = 0;
  let startTy = 0;
  let movingWindow = false;
  let windowStartX = 0;
  let windowStartY = 0;
  let windowLeft = 0;
  let windowTop = 0;
  let restoreBounds = null;
  const update = () => {
    img.style.transform = `scale(${zoom}) translate(${tx}px, ${ty}px)`;
  };
  img.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoom = Math.min(8, Math.max(0.2, zoom + (e.deltaY < 0 ? 0.15 : -0.15)));
    update();
  }, { passive: false });
  img.addEventListener("pointerdown", (e) => {
    dragging = true;
    startX = e.clientX;
    startY = e.clientY;
    startTx = tx;
    startTy = ty;
    img.setPointerCapture(e.pointerId);
  });
  img.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    tx = startTx + (e.clientX - startX);
    ty = startTy + (e.clientY - startY);
    update();
  });
  img.addEventListener("pointerup", () => {
    dragging = false;
  });
  overlay.querySelector('[data-action="zoom-in"]').addEventListener("click", () => {
    zoom = Math.min(8, zoom + 0.25);
    update();
  });
  overlay.querySelector('[data-action="zoom-out"]').addEventListener("click", () => {
    zoom = Math.max(0.2, zoom - 0.25);
    update();
  });
  overlay.querySelector('[data-action="reset"]').addEventListener("click", () => {
    zoom = 1;
    tx = 0;
    ty = 0;
    update();
  });
  overlay.querySelector('[data-action="system"]').addEventListener("click", async () => {
    await api("/api/open_file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
  });
  overlay.querySelector('[data-action="maximize"]').addEventListener("click", () => {
    if (win.classList.contains("maximized")) {
      win.classList.remove("maximized");
      if (restoreBounds) {
        win.style.left = restoreBounds.left;
        win.style.top = restoreBounds.top;
        win.style.width = restoreBounds.width;
        win.style.height = restoreBounds.height;
      }
    } else {
      const rect = win.getBoundingClientRect();
      restoreBounds = {
        left: `${rect.left}px`, top: `${rect.top}px`,
        width: `${rect.width}px`, height: `${rect.height}px`,
      };
      win.style.transform = "none";
      win.classList.add("maximized");
    }
  });
  titlebar.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button") || win.classList.contains("maximized")) return;
    const rect = win.getBoundingClientRect();
    movingWindow = true;
    windowStartX = e.clientX;
    windowStartY = e.clientY;
    windowLeft = rect.left;
    windowTop = rect.top;
    win.style.transform = "none";
    win.style.left = `${rect.left}px`;
    win.style.top = `${rect.top}px`;
    titlebar.setPointerCapture(e.pointerId);
  });
  titlebar.addEventListener("pointermove", (e) => {
    if (!movingWindow) return;
    win.style.left = `${Math.max(0, windowLeft + e.clientX - windowStartX)}px`;
    win.style.top = `${Math.max(0, windowTop + e.clientY - windowStartY)}px`;
  });
  titlebar.addEventListener("pointerup", () => { movingWindow = false; });
  overlay.querySelector('[data-action="close"]').addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });
  document.body.appendChild(overlay);
}

// ---- 提示词同步到 ComfyUI（Debounce 150ms）-------------------------------
function syncPromptToComfyUI(prompt, negative) {
  prompt = prompt != null ? prompt : document.getElementById("positivePrompt").value;
  negative = negative != null ? negative : document.getElementById("negativePrompt").value;
  clearTimeout(promptSyncTimer);
  promptSyncTimer = setTimeout(async () => {
    try {
      await api("/api/sync_prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          negative,
        }),
      });
    } catch (err) {
      console.error(err);
    }
  }, 150);
}

// ---- LoRA 面板渲染 ------------------------------------------------------
function renderLoras() {
  const list = document.getElementById("loraList");
  list.innerHTML = "";
  if (!loraList.length) {
    list.innerHTML = `<div class="dict-item en">当前模型没有检测到兼容 LoRA</div>`;
    return;
  }
  for (const lora of loraList) {
    const recommended = lora.recommended || {};
    const strength = recommended.strength || 0.7;
    const clipStrength = recommended.strength_clip || strength;
    const note = (loraNotes[currentModel] && loraNotes[currentModel][lora.name]) || "";
    const recParts = [];
    if (strength) recParts.push(`模型 ${Number(strength).toFixed(2)}`);
    if (clipStrength) recParts.push(`CLIP ${Number(clipStrength).toFixed(2)}`);
    if (recommended.steps) recParts.push(`Steps ${recommended.steps}`);
    if (recommended.cfg) recParts.push(`CFG ${recommended.cfg}`);
    if (recommended.scheduler) recParts.push(recommended.scheduler);
    const row = document.createElement("div");
    row.className = "lora-row";
    row.dataset.name = lora.name;
    row.innerHTML = `
      <div class="lora-top">
        <label style="display:flex;align-items:center;gap:7px;min-width:0">
          <input type="checkbox">
          <span class="lora-name">${escapeHtml(lora.name)}</span>
        </label>
        <span class="badge-soft">${Number(strength).toFixed(2)}</span>
      </div>
      <div class="lora-tags">${(lora.triggers || []).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>
      ${lora.description_cn ? `<div class="lora-recommend">${escapeHtml(lora.description_cn)}</div>` : ""}
      <div class="lora-recommend"><b>推荐参数：</b>${recParts.join(" · ") || "未提供"}</div>
      <div class="lora-strength">
        <input type="range" min="0" max="1.5" step="0.05" value="${strength}">
        <output>${Number(strength).toFixed(2)}</output>
      </div>
      <textarea class="lora-note" placeholder="手动备注，可留空">${escapeHtml(note)}</textarea>`;
    row.querySelector("input[type='range']").addEventListener("input", (e) => {
      row.querySelector("output").textContent = Number(e.target.value).toFixed(2);
    });
    row.querySelector(".lora-note").addEventListener("change", async (e) => {
      await api("/api/lora_notes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: currentModel, lora_name: lora.name, note: e.target.value }),
      });
      if (!loraNotes[currentModel]) loraNotes[currentModel] = {};
      loraNotes[currentModel][lora.name] = e.target.value;
    });
    list.appendChild(row);
  }
}

function imageSrc(item) {
  if (item.image_path) {
    return "/api/file?path=" + encodeURIComponent(item.image_path);
  }
  if (item.thumb) {
    return "/api/file?path=" + encodeURIComponent(item.thumb);
  }
  if (item.path) {
    return "/api/file?path=" + encodeURIComponent(item.path);
  }
  return "assets/thumb-1.png";
}

// ---- 资产视图 -----------------------------------------------------------
function renderAssets() {
  const grid = document.getElementById("assetGrid");
  grid.innerHTML = "";
  const data = assets.filter((asset) => {
    const tabMatches = assetTab === "saved" ? Boolean(asset.saved) : !asset.saved;
    const modelMatches = !assetModelFilter || itemModel(asset) === assetModelFilter;
    return tabMatches && modelMatches && matchesSearch(asset, assetSearchQuery);
  });
  if (!data.length) {
    grid.innerHTML = `<div class="dict-item en">${assetTab === "saved" ? "还没有已保存的图片" : "还没有未保存的图片"}</div>`;
    return;
  }
  for (const asset of data) {
    const params = parseParams(asset);
    const card = document.createElement("div");
    card.className = "asset-card";
    card.innerHTML = `
      <img src="${imageSrc(asset)}" alt="">
      <div class="asset-body">
        ${asset.saved ? `<div class="saved-badge">已保存</div>` : ""}
        <div class="asset-title">${escapeHtml(asset.title || asset.path || "未命名")}</div>
        <div class="asset-meta">${escapeHtml(new Date((asset.created_at || Date.now()) * 1000).toLocaleString())}</div>
        <div class="asset-meta asset-model">模型：${escapeHtml(asset.model || params.model || "未知")}</div>
        <div class="asset-meta asset-prompt" title="可滚动查看完整提示词">${escapeHtml(asset.prompt || params.prompt || "")}</div>
        <div class="asset-actions">
          <button class="icon-btn" title="查看">${ICON_VIEW}</button>
          <button class="icon-btn" title="保存">${ICON_SAVE}</button>
          <button class="icon-btn" title="收藏">${ICON_STAR}</button>
          <button class="icon-btn" title="一键填写">${ICON_FILL}</button>
          <button class="icon-btn" title="删除">${ICON_TRASH}</button>
        </div>
      </div>`;
    card.querySelector("img").addEventListener("dblclick", () => showImagePreview(asset.path, asset.model || params.model || "图片预览"));
    card.querySelectorAll(".icon-btn")[0].addEventListener("click", () => showImagePreview(asset.path, asset.model || params.model || "图片预览"));
    card.querySelectorAll(".icon-btn")[1].addEventListener("click", () => {
      showConfirm("保存图片", "确认把这张图片保存到你的输出目录吗？", async () => {
        await api("/api/assets/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ asset_id: asset.id, path: asset.path }),
        });
        await loadAssets();
      });
    });
    const favBtn = card.querySelectorAll(".icon-btn")[2];
    favBtn.classList.toggle("active", favoritedImages.has(asset.path));
    favBtn.addEventListener("click", async () => {
      if (favBtn.classList.contains("active")) {
        const favoriteId = favoriteImageIds.get(asset.path);
        if (favoriteId) {
          await api(`/api/favorites?id=${favoriteId}`, { method: "DELETE" });
        }
        favBtn.classList.remove("active");
        favoritedImages.delete(asset.path);
        favoriteImageIds.delete(asset.path);
      } else {
        await api("/api/favorites", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "image",
            image_path: asset.path,
            prompt: asset.prompt,
            negative: params.negative || "",
            model: asset.model,
            params: params,
            category: "图片",
            tags: "",
          }),
        });
        favBtn.classList.add("active");
      }
      await loadFavorites();
    });
    card.querySelectorAll(".icon-btn")[3].addEventListener("click", () => {
      showConfirm("一键填写", "确认把这张图片的提示词和参数恢复到生成页吗？", () => applyParams(params));
    });
    card.querySelectorAll(".icon-btn")[4].addEventListener("click", () => {
      showConfirm("删除图片", "确认删除这条图片记录吗？", async () => {
        await api(`/api/assets?id=${asset.id}`, { method: "DELETE" });
        await loadAssets();
      });
    });
    grid.appendChild(card);
  }
}

// 安全解析 JSON 参数（容错）----------------------------------------------
function parseParams(item) {
  try {
    return JSON.parse(item.params_json || "{}");
  } catch {
    return {};
  }
}

// 一键回填：将历史/收藏的参数恢复到生成页 ---------------------------------
function applyParams(params) {
  params = params || {};
  if (Object.hasOwn(params, "prompt")) document.getElementById("positivePrompt").value = params.prompt || "";
  if (Object.hasOwn(params, "negative")) document.getElementById("negativePrompt").value = params.negative || "";
  if (params.steps) {
    document.getElementById("steps").value = params.steps;
    document.getElementById("stepsOut").textContent = params.steps;
  }
  if (params.cfg) {
    document.getElementById("cfg").value = params.cfg;
    document.getElementById("cfgOut").textContent = Number(params.cfg).toFixed(1);
  }
  if (params.sampler) document.getElementById("samplerSelect").value = params.sampler;
  if (params.scheduler) document.getElementById("schedulerSelect").value = params.scheduler;
  if (params.seed != null) document.getElementById("seedInput").value = params.seed;
  if (params.random_seed != null) document.getElementById("randomSeed").checked = Boolean(params.random_seed);
  if (params.width) document.getElementById("widthInput").value = params.width;
  if (params.height) document.getElementById("heightInput").value = params.height;
  if (params.model) {
    currentModel = params.model;
    const select = document.getElementById("modelSelect");
    if ([...select.options].some((o) => o.value === params.model)) {
      select.value = params.model;
    }
    document.querySelector(".page-subtitle").textContent = currentModel + " 工作流";
    applyModelLoras();
    updateCurrentModelUi();
  }
  updateFavoriteButtons();
  document.querySelector(".nav-item[data-page='generate']").click();
}

function resolutionTag(width, height) {
  width = Number(width) || 0;
  height = Number(height) || 0;
  if (width === 3840 && height === 2160) return "4k";
  if (width === 2560 && height === 1440) return "2k";
  if (width === 1920 && height === 1080) return "1080p";
  if (width === 1280 && height === 720) return "720p";
  return `${width}x${height}`;
}

function applyResolutionToPrompt(width, height) {
  const tag = resolutionTag(width, height);
  const promptEl = document.getElementById("positivePrompt");
  const cleaned = (promptEl.value || "")
    .replace(/\b(720p|1080p|2k|4k|8k)\b/gi, "")
    .replace(/\b\d{3,5}x\d{3,5}\b/gi, "")
    .replace(/\s*,\s*,+/g, ",")
    .replace(/^,|,\s*$/g, "")
    .trim();
  promptEl.value = cleaned ? `${cleaned}, ${tag}` : tag;
  updateFavoriteButtons();
}

function updateResolutionFromInputs() {
  const width = Number(document.getElementById("widthInput").value) || 0;
  const height = Number(document.getElementById("heightInput").value) || 0;
  if (width && height) {
    applyResolutionToPrompt(width, height);
    const chips = document.querySelectorAll("#resolutionChips .chip");
    chips.forEach((chip) => {
      chip.classList.toggle("active", Number(chip.dataset.w) === width && Number(chip.dataset.h) === height);
    });
  }
}

// ---- 收藏视图（提示词 + 图片双标签）-------------------------------------
function bindFavoriteNoteEditor(noteBox, item) {
  const showNote = () => {
    noteBox.className = "favorite-note-display";
    noteBox.tabIndex = 0;
    noteBox.textContent = item.note?.trim() || "无备注";
    noteBox.title = "双击编辑备注";
    noteBox.ondblclick = startEdit;
    noteBox.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        startEdit();
      }
    };
  };
  const startEdit = () => {
    const input = document.createElement("input");
    const saveButton = document.createElement("button");
    input.className = "favorite-note-input";
    input.value = item.note || "";
    input.placeholder = "输入备注";
    saveButton.className = "favorite-note-save";
    saveButton.type = "button";
    saveButton.title = "保存备注";
    saveButton.textContent = "✓";
    noteBox.className = "favorite-note-editor";
    noteBox.replaceChildren(input, saveButton);
    const save = async () => {
      const note = input.value.trim();
      saveButton.disabled = true;
      try {
        await api("/api/favorites/note", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: item.id, note }),
        });
        item.note = note;
        showNote();
      } catch (err) {
        console.error(err);
        saveButton.disabled = false;
      }
    };
    saveButton.addEventListener("click", save);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        save();
      }
      if (event.key === "Escape") showNote();
    });
    input.focus();
  };
  showNote();
}

function renderFavorites() {
  const promptList = document.getElementById("favList");
  const imageGrid = document.getElementById("favImageGrid");
  const promptFavs = favorites.filter((item) => item.type !== "image");
  const imageFavs = favorites.filter((item) => item.type === "image");
  const countEl = document.getElementById("favCount");
  countEl.textContent = `${favorites.length} 条`;
  promptList.style.display = favTab === "prompt" ? "" : "none";
  imageGrid.style.display = favTab === "image" ? "" : "none";

  promptList.innerHTML = "";
  if (favTab === "prompt") {
    if (!promptFavs.length) {
      promptList.innerHTML = `<div class="dict-item en">还没有收藏提示词，在生成页点击星标即可收藏</div>`;
    } else {
      for (const item of promptFavs) {
        const el = document.createElement("div");
        el.className = "fav-item";
        el.innerHTML = `
          <div class="en">${escapeHtml(item.prompt)}</div>
          <div class="favorite-note-display" title="双击编辑备注"></div>
          <div class="fav-foot">
            <div class="fav-actions">
              <button class="icon-btn" title="一键填写">${ICON_FILL}</button>
              <button class="icon-btn" title="删除">${ICON_TRASH}</button>
            </div>
          </div>`;
        const params = parseParams(item);
        el.querySelectorAll(".icon-btn")[0].addEventListener("click", () => {
          showConfirm("一键填写", "确认把这条收藏恢复到生成页吗？", () => applyParams({ ...params, prompt: item.prompt, negative: item.negative, model: item.model }));
        });
        el.querySelectorAll(".icon-btn")[1].addEventListener("click", () => {
          showConfirm("删除收藏", "确认删除这条收藏提示词吗？", async () => {
            await api(`/api/favorites?id=${item.id}`, { method: "DELETE" });
            await loadFavorites();
          });
        });
        bindFavoriteNoteEditor(el.querySelector(".favorite-note-display"), item);
        promptList.appendChild(el);
      }
    }
  }

  imageGrid.innerHTML = "";
  if (favTab === "image") {
    if (!imageFavs.length) {
      imageGrid.innerHTML = `<div class="dict-item en">还没有收藏图片，在“我的资产”里点击星标即可收藏</div>`;
    } else {
      for (const item of imageFavs) {
        const card = document.createElement("div");
        card.className = "asset-card";
        card.innerHTML = `
          <img src="${item.image_path ? "/api/file?path=" + encodeURIComponent(item.image_path) : "assets/thumb-1.png"}" alt="">
          <div class="asset-body">
            <div class="asset-title">${escapeHtml(itemModel(item) || "图片收藏")}</div>
            <div class="asset-meta asset-model">模型：${escapeHtml(itemModel(item) || "未知")}</div>
            <div class="asset-meta asset-prompt" title="可滚动查看完整提示词">${escapeHtml(item.prompt || parseParams(item).prompt || "")}</div>
            <div class="asset-actions">
              <button class="icon-btn" title="查看">${ICON_VIEW}</button>
              <button class="icon-btn" title="一键填写">${ICON_FILL}</button>
              <button class="icon-btn" title="删除">${ICON_TRASH}</button>
            </div>
          </div>`;
        const params = parseParams(item);
        card.querySelector("img").addEventListener("dblclick", () => item.image_path && showImagePreview(item.image_path, item.model || "图片收藏"));
        card.querySelectorAll(".icon-btn")[0].addEventListener("click", () => item.image_path && showImagePreview(item.image_path, item.model || "图片收藏"));
        card.querySelectorAll(".icon-btn")[1].addEventListener("click", () => {
          showConfirm("一键填写", "确认把这张图片的提示词和参数恢复到生成页吗？", () => applyParams({ ...params, prompt: item.prompt, negative: item.negative || params.negative || "", model: item.model }));
        });
        card.querySelectorAll(".icon-btn")[2].addEventListener("click", () => {
          showConfirm("删除收藏", "确认删除这张图片收藏吗？", async () => {
            await api(`/api/favorites?id=${item.id}`, { method: "DELETE" });
            await loadFavorites();
          });
        });
        imageGrid.appendChild(card);
      }
    }
  }
}

// ---- 历史记录视图 --------------------------------------------------------
function renderHistory() {
  const list = document.getElementById("historyList");
  list.innerHTML = "";
  const data = historyItems.filter((item) => {
    const modelMatches = !historyModelFilter || itemModel(item) === historyModelFilter;
    return modelMatches && matchesSearch(item, historySearchQuery);
  });
  if (!data.length) {
    list.innerHTML = `<div class="dict-item en">没有符合筛选条件的历史记录</div>`;
    return;
  }
  for (const item of data) {
    const params = parseParams(item);
    const el = document.createElement("div");
    el.className = "history-item";
    el.innerHTML = `
      <img src="${imageSrc(item)}" alt="">
      <div>
        <div class="history-prompt">${escapeHtml(item.prompt)}</div>
        <div class="history-sub">${escapeHtml(item.model || "")}</div>
      </div>
      <div class="history-actions">
        <div class="history-time">${escapeHtml(new Date((item.created_at || Date.now()) * 1000).toLocaleString())}</div>
        <div class="fav-actions">
          <button class="icon-btn" title="查看">${ICON_VIEW}</button>
          <button class="icon-btn" title="一键填写">${ICON_FILL}</button>
          <button class="icon-btn" title="收藏">${ICON_STAR}</button>
          <button class="icon-btn" title="删除">${ICON_TRASH}</button>
        </div>
      </div>`;
    if (item.image_path) {
      el.querySelector("img").addEventListener("dblclick", () => showImagePreview(item.image_path, item.model || "历史图片"));
      el.querySelectorAll(".icon-btn")[0].addEventListener("click", () => showImagePreview(item.image_path, item.model || "历史图片"));
    }
    const fill = () => {
      showConfirm("一键填写", "确认把这条历史记录的提示词和参数恢复到生成页吗？", () => applyParams({ ...params, prompt: item.prompt, negative: item.negative, model: item.model }));
    };
    el.querySelector(".history-prompt").addEventListener("click", fill);
    el.querySelectorAll(".icon-btn")[1].addEventListener("click", fill);
    const favBtn = el.querySelectorAll(".icon-btn")[2];
    const favoriteKey = promptFavoriteKey(item.prompt, item.negative);
    favBtn.classList.toggle("active", favoritePromptPairIds.has(favoriteKey));
    favBtn.addEventListener("click", async () => {
      if (favBtn.classList.contains("active")) {
        const favoriteId = favoritePromptPairIds.get(favoriteKey);
        if (favoriteId) {
          await api(`/api/favorites?id=${favoriteId}`, { method: "DELETE" });
        }
        favBtn.classList.remove("active");
        favoritePromptPairIds.delete(favoriteKey);
      } else {
        await api("/api/favorites", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "prompt",
            prompt: item.prompt,
            negative: item.negative,
            model: item.model,
            params: params,
            category: "历史收藏",
            tags: "",
          }),
        });
        favBtn.classList.add("active");
      }
      await loadFavorites();
    });
    el.querySelectorAll(".icon-btn")[3].addEventListener("click", () => {
      showConfirm("删除记录", "确认删除这条历史记录吗？", async () => {
        await api(`/api/history?id=${item.id}`, { method: "DELETE" });
        await loadHistory();
      });
    });
    list.appendChild(el);
  }
}

// ---- 生成队列视图 --------------------------------------------------------
function renderQueue() {
  const list = document.getElementById("queueList");
  const countEl = document.getElementById("queueCount");
  const cancelBtn = document.getElementById("cancelBtn");
  countEl.textContent = queueTasks.length;
  const activeCount = queueTasks.filter((task) => task.status === "queued" || task.status === "running").length;
  cancelBtn.disabled = activeCount === 0;
  list.innerHTML = "";
  if (!queueTasks.length) {
    list.innerHTML = `<div class="dict-item en">当前没有生成任务</div>`;
    return;
  }
  for (const task of queueTasks) {
    const statusLabel = {
      queued: "排队中",
      running: "生成中",
      success: "已完成",
      cancelled: "已取消",
      failed: "失败",
    }[task.status] || task.status;
    const el = document.createElement("div");
    el.className = "queue-item";
    el.innerHTML = `
      <div class="queue-top">
        <span class="queue-prompt">${escapeHtml(task.prompt || "")}</span>
        <span class="queue-status ${escapeHtml(task.status)}">${escapeHtml(statusLabel)}</span>
      </div>
      <div class="queue-meta">
        <span>${escapeHtml(task.model || "")}</span>
        <span>${escapeHtml(new Date((task.created_at || Date.now()) * 1000).toLocaleTimeString())}</span>
        ${task.status === "queued" || task.status === "running"
          ? '<button class="btn-secondary queue-cancel-btn" type="button">取消生成</button>'
          : `<button class="icon-btn" title="删除任务">${ICON_TRASH}</button>`}
      </div>`;
    const actionButton = el.querySelector(".queue-cancel-btn, .icon-btn");
    actionButton.addEventListener("click", () => {
      const active = task.status === "queued" || task.status === "running";
      showConfirm(active ? "取消生成" : "删除任务", active ? "确认取消这个生成任务吗？" : "确认从队列中删除这个任务吗？", async () => {
        await api(`/api/queue?prompt_id=${encodeURIComponent(task.prompt_id)}`, { method: "DELETE" });
        await loadQueue();
      });
    });
    list.appendChild(el);
  }
}

// ---- 后台数据加载函数 ----------------------------------------------------
async function loadQueue() {
  try {
    queueTasks = (await api("/api/queue")).queue || [];
    for (const task of queueTasks) {
      if ((task.status === "running" || task.status === "success") && !syncedQueueIds.has(task.prompt_id)) {
        syncedQueueIds.add(task.prompt_id);
        syncPromptToComfyUI(task.prompt, task.negative);
      }
    }
    renderQueue();
  } catch (err) {
    console.error(err);
  }
}

function renderRecentThumbs() {
  const container = document.getElementById("recentThumbs");
  container.innerHTML = "";
  const recent = assets.slice(0, 6);
  if (!recent.length) {
    container.innerHTML = `<div class="dict-item en">还没有生成图片</div>`;
    return;
  }
  for (const asset of recent) {
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    thumb.innerHTML = `<img src="${imageSrc(asset)}" alt=""><span>${escapeHtml(new Date((asset.created_at || Date.now()) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</span><button class="thumb-view" type="button">查看</button>`;
    const open = () => showImagePreview(asset.path, asset.model || parseParams(asset).model || "最近生成");
    thumb.querySelector("img").addEventListener("dblclick", open);
    thumb.querySelector(".thumb-view").addEventListener("click", open);
    container.appendChild(thumb);
  }
}

function updateResultPreview() {
  const image = document.getElementById("resultImage");
  if (assets.length && assets[0].path) {
    image.src = "/api/file?path=" + encodeURIComponent(assets[0].path);
    document.querySelector(".model-badge").textContent = assets[0].model || parseParams(assets[0]).model || currentModel;
  }
}

function updateCurrentModelUi() {
  const badge = document.querySelector(".model-badge");
  if (badge) badge.textContent = currentModel;
}

function refreshModelSelectOptions(models) {
  const select = document.getElementById("modelSelect");
  if (!select) return;
  for (const item of models || []) {
    if (!item?.model) continue;
    modelAvailability.set(item.model, Boolean(item.ready));
    const option = Array.from(select.options).find((entry) => entry.value === item.model);
    if (!option) continue;
    option.dataset.ready = item.ready ? "true" : "false";
    option.textContent = `${item.model} ${item.ready ? "● 已连接" : "○ 未连接"}`;
  }
}

// ---- 生成进度 ----------------------------------------------------------
function connectProgressWs() {
  // ComfyUI 会拒绝来自助手端口的跨源 WebSocket。队列状态由同源的
  // /api/queue 轮询提供，避免 403 和不断累积的失败连接。
  progressWs = null;
}

// ---- 提示词库加载（含 NSFW 开关）----------------------------------------
async function loadDictionary() {
  try {
    const data = await api(`/api/dictionary?nsfw=${includeNsfw ? "1" : "0"}`);
    dictFiles = data.files || [];
    nsfwTotal = data.nsfw_total || 0;
    const wrap = document.getElementById("nsfwToggleWrap");
    if (wrap) {
      wrap.hidden = nsfwTotal === 0;
      wrap.style.display = nsfwTotal > 0 ? "" : "none";
    }
    renderDictFileOptions();
    renderDict();
  } catch (err) {
    document.getElementById("dictList").innerHTML = `<div class="dict-item en">提示词库加载失败：${escapeHtml(err.message)}</div>`;
  }
}

async function loadLoras() {
  try {
    allLoras = await api("/api/loras");
    await loadLoraNotes();
    applyModelLoras();
    renderLoras();
  } catch (err) {
    console.error(err);
  }
}

async function loadLoraNotes() {
  try {
    const data = await api("/api/lora_notes");
    loraNotes = {};
    for (const note of data.notes || []) {
      if (!loraNotes[note.model]) loraNotes[note.model] = {};
      loraNotes[note.model][note.lora_name] = note.note;
    }
  } catch (err) {
    console.error(err);
  }
}

// 按当前模型筛选兼容 LoRA -------------------------------------------------
function applyModelLoras() {
  const flux = currentModel.toLowerCase().includes("flux");
  loraList = allLoras.filter((lora) => {
    const base = String(lora.base || "").toLowerCase();
    if (!base) {
      return true;
    }
    if (flux) {
      return base.includes("flux") || base.includes("sd3") || base.includes("flux1");
    }
    return base.includes("zimage") || base.includes("z_image");
  });
  renderLoras();
}

async function loadAssets() {
  try {
    assets = (await api("/api/assets")).assets || [];
    renderAssets();
    renderRecentThumbs();
    updateResultPreview();
  } catch (err) {
    console.error(err);
  }
}

async function loadHistory() {
  try {
    historyItems = (await api("/api/history")).history || [];
    renderHistory();
  } catch (err) {
    console.error(err);
  }
}

async function loadFavorites() {
  try {
    favorites = (await api("/api/favorites")).favorites || [];
    favoritedPrompts.clear();
    favoritedImages.clear();
    favoritePromptIds.clear();
    favoriteImageIds.clear();
    favoritePromptPairIds.clear();
    for (const item of favorites) {
      if (item.type === "image") {
        if (item.image_path) {
          favoritedImages.add(item.image_path);
          favoriteImageIds.set(item.image_path, item.id);
        }
        } else {
          favoritedPrompts.add(item.prompt);
          favoritePromptIds.set(item.prompt, item.id);
          favoritePromptPairIds.set(promptFavoriteKey(item.prompt, item.negative), item.id);
      }
    }
    renderFavorites();
    updateFavoriteButtons();
  } catch (err) {
    console.error(err);
  }
}

function updateFavoriteButtons() {
  const active = favoritePromptPairIds.has(currentPromptFavoriteKey());
  document.querySelectorAll(".favorite-btn").forEach((btn) => {
    btn.classList.toggle("active", active);
  });
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    const connected = Boolean(status.connected && modelAvailability.get(currentModel) === true);
    const label = connected ? `已连接到${currentModel}模型` : "未连接";
    const pill = document.getElementById("connectionStatus");
    if (pill) {
      pill.innerHTML = `<span class="dot"></span><span class="status-text">${label}</span>`;
      pill.style.color = connected ? "#7fe0b0" : "#e0a84b";
      pill.title = label;
      const dot = pill.querySelector(".dot");
      if (dot) dot.style.background = connected ? "#43d18f" : "#e0a84b";
    }
    const sidebarStatus = document.getElementById("sidebarConnectionStatus");
    if (sidebarStatus) {
      sidebarStatus.innerHTML = `<span class="dot"></span> ${label}`;
      sidebarStatus.style.color = connected ? "#7fe0b0" : "#e0a84b";
      const dot = sidebarStatus.querySelector(".dot");
      if (dot) dot.style.background = connected ? "#43d18f" : "#e0a84b";
    }
  } catch (err) {
    console.error(err);
  }
}

async function scanModels(autoSelect = false) {
  const scan = await api("/api/models_status");
  refreshModelSelectOptions(scan.models || []);
  const readyModels = (scan.models || []).filter((item) => item.ready);
  if (autoSelect && readyModels.length === 1 && readyModels[0].model !== currentModel) {
    currentModel = readyModels[0].model;
    document.getElementById("modelSelect").value = currentModel;
    document.querySelector(".page-subtitle").textContent = currentModel + " 工作流";
    applyModelLoras();
  }
  updateCurrentModelUi();
  return scan;
}

async function checkCurrentModel() {
  const button = document.getElementById("modelRefreshBtn");
  const result = document.getElementById("modelCheck");
  if (!button || !result) return;
  button.disabled = true;
  button.classList.add("is-checking");
  result.className = "model-check";
  result.textContent = "检查中…";
  try {
    const status = await api(`/api/model_status?model=${encodeURIComponent(currentModel)}`);
    refreshModelSelectOptions([{ model: currentModel, ready: status.ready }]);
    if (status.ready) {
      result.classList.add("ready");
      result.textContent = "模型可用";
    } else {
      result.classList.add("error");
      result.textContent = status.missing?.length
        ? `缺少：${status.missing.join(", ")}`
        : (status.error || "模型不可用");
    }
  } catch (err) {
    refreshModelSelectOptions([{ model: currentModel, ready: false }]);
    result.classList.add("error");
    result.textContent = "检查失败：ComfyUI 未连接";
  } finally {
    await loadStatus();
    updateCurrentModelUi();
    button.disabled = false;
    button.classList.remove("is-checking");
  }
}

function fileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("无法读取图片文件"));
    reader.onload = () => resolve(reader.result);
    reader.readAsDataURL(file);
  });
}

async function importSelectedImage(file) {
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    alert("请选择 PNG、JPG、WEBP 或 GIF 图片");
    return;
  }
  if (file.size > 25 * 1024 * 1024) {
    alert("图片不能超过 25 MB");
    return;
  }
  const button = document.getElementById("importImageBtn");
  button.disabled = true;
  button.textContent = "导入中…";
  try {
    const data = await fileAsDataUrl(file);
    await api("/api/import_image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, data }),
    });
    assetTab = "unsaved";
    document.querySelectorAll("[data-asset-tab]").forEach((item) => {
      item.classList.toggle("active", item.dataset.assetTab === assetTab);
    });
    await loadAssets();
  } catch (err) {
    alert(`导入失败：${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "导入图片";
  }
}

async function showModelManager() {
  let scan;
  try {
    scan = await scanModels(false);
  } catch (err) {
    alert("无法扫描模型：请确认 ComfyUI 已启动");
    return;
  }
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  const rows = (scan.models || []).map((item) => `
    <div class="model-manager-row">
      <div><strong>${escapeHtml(item.model)}</strong><small>${item.ready ? "已连接、可生成" : (item.error || "未连接或缺少所需模型文件")}</small></div>
      <div class="model-manager-actions">
        <span class="model-state ${item.ready ? "ready" : "offline"}">${item.ready ? "● 已连接" : "○ 未连接"}</span>
        <button class="btn-secondary use-model-btn" data-model="${escapeHtml(item.model)}" ${item.ready ? "" : "disabled"}>使用</button>
      </div>
    </div>`).join("");
  overlay.innerHTML = `
    <div class="modal model-manager-modal" role="dialog" aria-modal="true" aria-label="管理模型档案">
      <h3>管理模型档案</h3>
      <p>这里显示可用模型及其连接状态。选择“使用”会切换到该模型。</p>
      <div class="model-manager-list">${rows || "<p>没有扫描到模型。</p>"}</div>
      <div class="modal-actions"><button class="btn-secondary close-model-manager">关闭</button></div>
    </div>`;
  const close = () => overlay.remove();
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
  overlay.querySelector(".close-model-manager").addEventListener("click", close);
  overlay.querySelectorAll(".use-model-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const select = document.getElementById("modelSelect");
      select.value = button.dataset.model;
      select.dispatchEvent(new Event("change"));
      close();
    });
  });
  document.body.appendChild(overlay);
}

// ---- 事件绑定：导航切换 --------------------------------------------------
document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const page = btn.dataset.page;
    document.getElementById("page-" + page).classList.add("active");
    document.getElementById("pageTitle").textContent = pages[page].title;
    document.querySelector(".page-subtitle").textContent = pages[page].sub;
  });
});

document.querySelectorAll("[data-asset-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-asset-tab]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    assetTab = btn.dataset.assetTab;
    renderAssets();
  });
});

document.getElementById("assetSearch").addEventListener("input", (event) => {
  assetSearchQuery = event.target.value.trim().toLowerCase();
  renderAssets();
});
document.getElementById("assetModelFilter").addEventListener("change", (event) => {
  assetModelFilter = event.target.value;
  renderAssets();
});
document.getElementById("historySearch").addEventListener("input", (event) => {
  historySearchQuery = event.target.value.trim().toLowerCase();
  renderHistory();
});
document.getElementById("historyModelFilter").addEventListener("change", (event) => {
  historyModelFilter = event.target.value;
  renderHistory();
});
document.getElementById("importImageBtn").addEventListener("click", () => {
  document.getElementById("importImageInput").click();
});
document.getElementById("importImageInput").addEventListener("change", async (event) => {
  await importSelectedImage(event.target.files?.[0]);
  event.target.value = "";
});
document.getElementById("manageModelsBtn").addEventListener("click", showModelManager);

document.querySelectorAll("[data-fav-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-fav-tab]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    favTab = btn.dataset.favTab;
    renderFavorites();
  });
});

document.getElementById("steps").addEventListener("input", (e) => {
  document.getElementById("stepsOut").textContent = e.target.value;
});
document.getElementById("cfg").addEventListener("input", (e) => {
  document.getElementById("cfgOut").textContent = Number(e.target.value).toFixed(1);
});
document.getElementById("dictSearch").addEventListener("input", renderDict);
document.getElementById("dictFileSelect").addEventListener("change", () => {
  activeCat = "";
  renderDict();
});
document.getElementById("nsfwToggle").addEventListener("change", (e) => {
  includeNsfw = e.target.checked;
  activeCat = "";
  loadDictionary();
});

document.getElementById("modelSelect").addEventListener("change", (e) => {
  currentModel = e.target.value;
  document.querySelector(".page-subtitle").textContent = currentModel + " 工作流";
  applyModelLoras();
  updateCurrentModelUi();
  checkCurrentModel();
});
document.getElementById("modelRefreshBtn").addEventListener("click", async () => {
  try {
    await scanModels(true);
  } catch (err) {
    console.error(err);
  }
  await checkCurrentModel();
});

document.querySelectorAll(".favorite-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const prompt = document.getElementById("positivePrompt").value;
    const negative = document.getElementById("negativePrompt").value;
    const key = promptFavoriteKey(prompt, negative);
    if (favoritePromptPairIds.has(key)) {
      const favoriteId = favoritePromptPairIds.get(key);
      if (favoriteId) {
        await api(`/api/favorites?id=${favoriteId}`, { method: "DELETE" });
      }
    } else {
      await api("/api/favorites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, negative, model: currentModel, category: "未分类", tags: "" }),
      });
    }
    await loadFavorites();
  });
});

document.querySelectorAll("#ratioGroup button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#ratioGroup button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const dims = RATIO_PRESETS[btn.textContent.trim()];
    if (dims) {
      document.getElementById("widthInput").value = dims[0];
      document.getElementById("heightInput").value = dims[1];
      updateResolutionFromInputs();
    }
  });
});
document.getElementById("widthInput").addEventListener("change", updateResolutionFromInputs);
document.getElementById("heightInput").addEventListener("change", updateResolutionFromInputs);
document.querySelectorAll("#resolutionChips .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.getElementById("widthInput").value = chip.dataset.w;
    document.getElementById("heightInput").value = chip.dataset.h;
    updateResolutionFromInputs();
  });
});
document.querySelectorAll("#styleChips .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll("#styleChips .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    const styleText = TYPE_STYLES[chip.dataset.style] || "";
    const promptEl = document.getElementById("positivePrompt");
    const cleaned = (promptEl.value || "")
      .replace(TYPE_STYLES_RE, "")
      .replace(/\s*,\s*,+/g, ",")
      .replace(/^,|,\s*$/g, "")
      .trim();
    promptEl.value = cleaned ? `${cleaned}, ${styleText}` : styleText;
    updateFavoriteButtons();
  });
});
document.querySelectorAll(".quick-tags .chip").forEach((chip) => {
  chip.addEventListener("click", () => chip.classList.toggle("active"));
});

["positivePrompt", "negativePrompt"].forEach((id) => {
  const el = document.getElementById(id);
  el.addEventListener("dragover", (e) => e.preventDefault());
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    const text = e.dataTransfer.getData("text/plain");
    if (text) {
      el.value += (el.value && !el.value.endsWith(", ") ? ", " : "") + text;
      updateFavoriteButtons();
    }
  });
  el.addEventListener("input", () => {
    updateFavoriteButtons();
    syncPromptToComfyUI();
  });
});

const dictionaryCard = document.querySelector(".dictionary-card");
if (dictionaryCard) {
  dictionaryCard.addEventListener("dragover", (e) => {
    if (Array.from(e.dataTransfer.types || []).includes("Files")) {
      e.preventDefault();
    }
  });
  dictionaryCard.addEventListener("drop", async (e) => {
    e.preventDefault();
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!file || !file.name.toLowerCase().endsWith(".md")) {
      return;
    }
    const content = await file.text();
    const result = await api("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, content }),
    });
    alert(`已解析 ${result.count || 0} 条提示词：${file.name}`);
  });
}

// ---- 核心：生成按钮逻辑（含同步检查 → 提交队列 → 轮询结果）---------------
async function generate() {
  if (document.getElementById("randomSeed").checked) {
    document.getElementById("seedInput").value = randomSeed();
  }
  const loras = [];
  document.querySelectorAll(".lora-row").forEach((row) => {
    const checkbox = row.querySelector("input[type='checkbox']");
    if (!checkbox || !checkbox.checked) {
      return;
    }
    const slider = row.querySelector(".lora-strength input[type='range']");
    const strength = slider ? Number(slider.value) : 0.7;
    loras.push({
      name: row.dataset.name,
      enabled: true,
      strength_model: strength,
      strength_clip: strength,
    });
  });

  const params = {
    model: currentModel,
    prompt: document.getElementById("positivePrompt").value,
    negative: document.getElementById("negativePrompt").value,
    steps: Number(document.getElementById("steps").value) || 8,
    cfg: Number(document.getElementById("cfg").value) || 1,
    sampler: document.getElementById("samplerSelect").value,
    scheduler: document.getElementById("schedulerSelect").value,
    seed: Number(document.getElementById("seedInput").value) || Math.floor(Math.random() * 4294967296),
    width: Number(document.getElementById("widthInput").value) || 1024,
    height: Number(document.getElementById("heightInput").value) || 1024,
    batch_count: Number(document.getElementById("batchCount").value) || 1,
    random_seed: document.getElementById("randomSeed").checked,
    loras,
  };

  const statusEl = document.getElementById("generateStatus");
  const bar = document.querySelector(".progress-bar");
  const progressText = document.getElementById("progressText");
  statusEl.textContent = "正在同步提示词到 ComfyUI...";
  progressText.textContent = "正在同步";
  bar.style.transition = "width .8s ease";
  bar.style.width = "12%";

  let synced = false;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    progressText.textContent = `正在同步（第 ${attempt}/3 次）`;
    try {
      const check = await api("/api/sync_check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: params.prompt,
          negative: params.negative,
          model: params.model,
        }),
      });
      if (check.ok) {
        synced = true;
        break;
      }
    } catch (err) {
      console.error(err);
    }
    if (attempt < 3) {
      statusEl.textContent = "ComfyUI 未确认同步，正在重试...";
      bar.style.width = `${12 + attempt * 10}%`;
    }
  }

  if (!synced) {
    statusEl.textContent = "同步失败：ComfyUI 未确认提示词一致，已停止生成。";
    progressText.textContent = "同步失败";
    bar.style.width = "0%";
    return;
  }

  statusEl.textContent = "同步完成，正在提交生成任务...";
  progressText.textContent = "提交中";
  bar.style.width = "50%";
  try {
    const result = await api("/api/queue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    if (result.ok) {
      if (result.seeds?.length) {
        document.getElementById("seedInput").value = result.seeds[0];
      }
      statusEl.textContent = `已提交 ${result.prompt_ids ? result.prompt_ids.length : 1} 个任务到生成队列。`;
      progressText.textContent = "已入队，等待生成";
      bar.style.width = "60%";
      await loadQueue();
      await loadAssets();
      await loadHistory();
    } else {
      statusEl.textContent = result.reason || result.error || "生成失败，请查看服务日志。";
      bar.style.width = "0%";
    }
  } catch (err) {
    statusEl.textContent = "提交失败：" + err.message;
    bar.style.width = "0%";
  }
}

document.getElementById("generateBtn").addEventListener("click", generate);

document.getElementById("randomSeedBtn").addEventListener("click", () => {
  document.getElementById("seedInput").value = randomSeed();
});

document.querySelectorAll("[data-prompt-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.target);
    if (!target) return;
    if (button.dataset.promptAction === "clear") {
      target.value = "";
    } else {
      try {
        target.value = await navigator.clipboard.readText();
      } catch {
        alert("无法读取剪贴板，请确认应用拥有剪贴板权限。");
        return;
      }
    }
    target.focus();
    target.dispatchEvent(new Event("input", { bubbles: true }));
  });
});

document.getElementById("resultViewBtn").addEventListener("click", () => {
  if (assets.length && assets[0].path) {
    showImagePreview(assets[0].path, assets[0].model || parseParams(assets[0]).model || "生成结果");
  }
});

document.getElementById("viewAllRecent").addEventListener("click", () => {
  assetTab = "unsaved";
  document.querySelectorAll("[data-asset-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.assetTab === "unsaved");
  });
  renderAssets();
  document.querySelector(".nav-item[data-page='assets']").click();
});

async function loadSettings() {
  try {
    const settings = await api("/api/settings");
    document.getElementById("comfyuiUrlInput").value = settings.comfyui_url || "";
    document.getElementById("outputDirInput").value = settings.output_dir || "";
    document.getElementById("workflowDirInput").value = (settings.workflow_dirs || [""])[0] || "";
  } catch (err) {
    console.error(err);
  }
}

document.querySelectorAll("[data-browse-directory]").forEach((button) => {
  button.addEventListener("click", async () => {
    const input = document.getElementById(button.dataset.browseDirectory);
    const result = await api("/api/select_directory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initial_dir: input.value }),
    });
    if (result.path) input.value = result.path;
  });
});

document.getElementById("saveSettingsBtn").addEventListener("click", async () => {
  await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      comfyui_url: document.getElementById("comfyuiUrlInput").value.trim(),
      output_dir: document.getElementById("outputDirInput").value.trim(),
      workflow_dirs: [document.getElementById("workflowDirInput").value.trim()].filter(Boolean),
    }),
  });
  await loadStatus();
  alert("设置已保存。");
});

document.getElementById("testConnectionBtn").addEventListener("click", async () => {
  await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ comfyui_url: document.getElementById("comfyuiUrlInput").value.trim() }),
  });
  await loadStatus();
  await checkCurrentModel();
});

// ---- UI 增强：数字输入框增减按钮 -----------------------------------------
function decorateNumberInputs() {
  document.querySelectorAll("input[type='number']").forEach((input) => {
    if (input.parentElement && input.parentElement.classList.contains("number-field")) {
      return;
    }
    const wrap = document.createElement("div");
    wrap.className = "number-field";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const step = parseFloat(input.step) || 1;
    const makeButton = (delta, label) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "num-btn";
      button.textContent = label;
      button.title = delta > 0 ? "增加" : "减少";
      button.addEventListener("click", () => {
        input.value = (parseFloat(input.value) || 0) + delta * step;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      return button;
    };
    wrap.appendChild(makeButton(-1, "−"));
    wrap.appendChild(makeButton(1, "+"));
  });
}

document.getElementById("cancelBtn").addEventListener("click", () => {
  showConfirm("取消生成", "确认取消当前所有生成任务吗？", async () => {
    for (const task of queueTasks) {
      await api(`/api/queue?prompt_id=${encodeURIComponent(task.prompt_id)}`, { method: "DELETE" });
    }
    await loadQueue();
    document.getElementById("generateStatus").textContent = "已取消当前任务。";
    document.getElementById("progressText").textContent = "已取消";
    document.querySelector(".progress-bar").style.width = "0%";
  });
});

const resultImage = document.getElementById("resultImage");
if (resultImage) {
  resultImage.addEventListener("dblclick", () => {
    if (assets.length && assets[0].path) {
      showImagePreview(assets[0].path);
    }
  });
}

// ---- 应用初始化 ----------------------------------------------------------
setInterval(loadQueue, 3000);   // 每3秒轮询队列状态
setInterval(async () => {
  await loadAssets();
  await loadHistory();
}, 4000);
connectProgressWs();            // WebSocket 接收实时生成进度
decorateNumberInputs();         // 为数字输入框添加 +/- 按钮

// 并行加载初始数据
loadStatus();
scanModels(true).then(checkCurrentModel).catch(checkCurrentModel);
loadDictionary();
loadLoras();
loadAssets();
loadHistory();
loadFavorites();
loadQueue();
loadSettings();
updateCurrentModelUi();
