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
let assetPollTimer = null;
let queueTasks = [];
let loraNotes = {};
let favTab = "prompt";
let assetTab = "unsaved";
let progressWs = null;
let promptSyncTimer = null;
const syncedQueueIds = new Set();

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
      });
      el.addEventListener("dblclick", () => {
        const target = file.target === "negative"
          ? document.getElementById("negativePrompt")
          : document.getElementById("positivePrompt");
        target.value += (target.value && !target.value.endsWith(", ") ? ", " : "") + item.prompt;
        target.focus();
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
function showImagePreview(path) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal preview-modal">
      <div class="preview-stage">
        <img src="/api/file?path=${encodeURIComponent(path)}" alt="图片预览">
      </div>
      <div class="preview-tools">
        <button class="btn-secondary" data-action="zoom-out">缩小</button>
        <button class="btn-secondary" data-action="zoom-in">放大</button>
        <button class="btn-secondary" data-action="reset">重置</button>
        <button class="btn-secondary" data-action="system">系统打开</button>
      </div>
      <div class="modal-actions">
        <button class="btn-secondary" data-action="close">关闭</button>
      </div>
    </div>`;
  const img = overlay.querySelector("img");
  let zoom = 1;
  let tx = 0;
  let ty = 0;
  let dragging = false;
  let startX = 0;
  let startY = 0;
  let startTx = 0;
  let startTy = 0;
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
  const data = assets.filter((asset) => assetTab === "saved" ? asset.saved : !asset.saved);
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
        <div class="asset-meta">${escapeHtml(new Date((asset.created_at || Date.now()) * 1000).toLocaleString())} · ${escapeHtml(asset.model || "")}</div>
        <div class="asset-meta">${escapeHtml(asset.prompt || "")}</div>
        <div class="asset-actions">
          <button class="icon-btn" title="查看">${ICON_VIEW}</button>
          <button class="icon-btn" title="保存">${ICON_SAVE}</button>
          <button class="icon-btn" title="收藏">${ICON_STAR}</button>
          <button class="icon-btn" title="一键填写">${ICON_FILL}</button>
          <button class="icon-btn" title="删除">${ICON_TRASH}</button>
        </div>
      </div>`;
    card.querySelector("img").addEventListener("dblclick", () => showImagePreview(asset.path));
    card.querySelectorAll(".icon-btn")[0].addEventListener("click", () => showImagePreview(asset.path));
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
  if (params.prompt) document.getElementById("positivePrompt").value = params.prompt;
  if (params.negative) document.getElementById("negativePrompt").value = params.negative;
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
  }
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
          <div class="cn">${escapeHtml(item.category || "未分类")}</div>
          <div class="fav-foot">
            <span class="badge-soft">${escapeHtml(item.tags || "无标签")}</span>
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
            <div class="asset-title">${escapeHtml(item.model || "图片收藏")}</div>
            <div class="asset-meta">${escapeHtml(item.prompt || "")}</div>
            <div class="asset-actions">
              <button class="icon-btn" title="查看">${ICON_VIEW}</button>
              <button class="icon-btn" title="一键填写">${ICON_FILL}</button>
              <button class="icon-btn" title="删除">${ICON_TRASH}</button>
            </div>
          </div>`;
        const params = parseParams(item);
        card.querySelector("img").addEventListener("dblclick", () => item.image_path && showImagePreview(item.image_path));
        card.querySelectorAll(".icon-btn")[0].addEventListener("click", () => item.image_path && showImagePreview(item.image_path));
        card.querySelectorAll(".icon-btn")[1].addEventListener("click", () => {
          showConfirm("一键填写", "确认把这张图片的提示词和参数恢复到生成页吗？", () => applyParams({ ...params, prompt: item.prompt, model: item.model }));
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
  if (!historyItems.length) {
    list.innerHTML = `<div class="dict-item en">暂无历史记录，生成后会显示在这里</div>`;
    return;
  }
  for (const item of historyItems) {
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
          <button class="icon-btn" title="一键填写">${ICON_FILL}</button>
          <button class="icon-btn" title="收藏">${ICON_STAR}</button>
          <button class="icon-btn" title="删除">${ICON_TRASH}</button>
        </div>
      </div>`;
    if (item.image_path) {
      el.querySelector("img").addEventListener("dblclick", () => showImagePreview(item.image_path));
    }
    const fill = () => {
      showConfirm("一键填写", "确认把这条历史记录的提示词和参数恢复到生成页吗？", () => applyParams({ ...params, prompt: item.prompt, negative: item.negative, model: item.model }));
    };
    el.querySelector(".history-prompt").addEventListener("click", fill);
    el.querySelectorAll(".icon-btn")[0].addEventListener("click", fill);
    const favBtn = el.querySelectorAll(".icon-btn")[1];
    favBtn.classList.toggle("active", favoritedPrompts.has(item.prompt));
    favBtn.addEventListener("click", async () => {
      if (favBtn.classList.contains("active")) {
        const favoriteId = favoritePromptIds.get(item.prompt);
        if (favoriteId) {
          await api(`/api/favorites?id=${favoriteId}`, { method: "DELETE" });
        }
        favBtn.classList.remove("active");
        favoritedPrompts.delete(item.prompt);
        favoritePromptIds.delete(item.prompt);
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
    el.querySelectorAll(".icon-btn")[2].addEventListener("click", () => {
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
        <button class="icon-btn" title="删除任务">${ICON_TRASH}</button>
      </div>`;
    el.querySelector(".icon-btn").addEventListener("click", () => {
      showConfirm("删除任务", "确认从队列中删除这个任务吗？", async () => {
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
    thumb.innerHTML = `<img src="${imageSrc(asset)}" alt=""><span>${escapeHtml(new Date((asset.created_at || Date.now()) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</span>`;
    thumb.querySelector("img").addEventListener("dblclick", () => showImagePreview(asset.path));
    container.appendChild(thumb);
  }
}

function updateResultPreview() {
  const image = document.getElementById("resultImage");
  if (assets.length && assets[0].path) {
    image.src = "/api/file?path=" + encodeURIComponent(assets[0].path);
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
    for (const item of favorites) {
      if (item.type === "image") {
        if (item.image_path) {
          favoritedImages.add(item.image_path);
          favoriteImageIds.set(item.image_path, item.id);
        }
      } else {
        favoritedPrompts.add(item.prompt);
        favoritePromptIds.set(item.prompt, item.id);
      }
    }
    renderFavorites();
    updateFavoriteButtons();
  } catch (err) {
    console.error(err);
  }
}

function updateFavoriteButtons() {
  document.querySelectorAll(".favorite-btn").forEach((btn) => {
    const card = btn.closest(".prompt-card");
    const textarea = card ? card.querySelector("textarea") : null;
    const prompt = textarea ? textarea.value : "";
    btn.classList.toggle("active", favoritedPrompts.has(prompt));
  });
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    const pill = document.querySelector(".status-pill");
    if (pill) {
      pill.innerHTML = `<span class="dot"></span> ${status.connected ? "已连接" : "未连接"}`;
      pill.style.color = status.connected ? "#7fe0b0" : "#e0a84b";
    }
  } catch (err) {
    console.error(err);
  }
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
    result.classList.add("error");
    result.textContent = "检查失败：ComfyUI 未连接";
  } finally {
    button.disabled = false;
    button.classList.remove("is-checking");
  }
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
  checkCurrentModel();
});
document.getElementById("modelRefreshBtn").addEventListener("click", checkCurrentModel);

document.querySelectorAll(".favorite-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const card = btn.closest(".prompt-card");
    const textarea = card ? card.querySelector("textarea") : null;
    const prompt = textarea ? textarea.value : document.getElementById("positivePrompt").value;
    if (btn.classList.contains("active")) {
      const favoriteId = favoritePromptIds.get(prompt);
      if (favoriteId) {
        await api(`/api/favorites?id=${favoriteId}`, { method: "DELETE" });
      }
      btn.classList.remove("active");
      favoritedPrompts.delete(prompt);
      favoritePromptIds.delete(prompt);
    } else {
      await api("/api/favorites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, negative: document.getElementById("negativePrompt").value, category: "未分类", tags: "" }),
      });
      btn.classList.add("active");
    }
    loadFavorites();
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
    }
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
      statusEl.textContent = `已提交 ${result.prompt_ids ? result.prompt_ids.length : 1} 个任务到生成队列。`;
      progressText.textContent = "已入队，等待生成";
      bar.style.width = "60%";
      await loadQueue();
      const before = assets.length;
      if (assetPollTimer) {
        clearInterval(assetPollTimer);
      }
      assetPollTimer = setInterval(async () => {
        await loadAssets();
        await loadHistory();
        if (assets.length > before) {
          clearInterval(assetPollTimer);
          assetPollTimer = null;
          statusEl.textContent = "图片已生成并保存到资产库。";
          document.getElementById("progressText").textContent = "完成";
        }
      }, 5000);
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
connectProgressWs();            // WebSocket 接收实时生成进度
decorateNumberInputs();         // 为数字输入框添加 +/- 按钮

// 并行加载初始数据
loadStatus();
checkCurrentModel();
loadDictionary();
loadLoras();
loadAssets();
loadHistory();
loadFavorites();
loadQueue();
