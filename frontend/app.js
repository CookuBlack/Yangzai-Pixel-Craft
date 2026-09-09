const state = {
  view: "image",          // 'image' | 'video' | 'templates'
  sub: "single",          // 'single' | 'batch'
  stagingId: null,
  boxes: [],              // [{id, x, y, w, h}] 归一化
  autoBoxes: [],
  taskId: null,
  activeBoxId: null,
  layer: null,
  batchId: null,
  batchImages: [],        // 图片批量素材，与视频分离存放
  batchVideos: [],        // 视频批量素材，与图片分离存放
  activeBatchIndex: null,
  boxMode: "single",      // 'single' | 'batch'：路由框绘制与右下角列表
  resultKind: null,       // 当前结果卡所属类型：null | 'image' | 'video' | 'special'
};

const $ = (id) => document.getElementById(String(id).replace(/^#/, ""));
const uid = () => Math.random().toString(36).slice(2, 9);
const stripId = (b) => ({ x: b.x, y: b.y, w: b.w, h: b.h });
const cloneBoxes = (arr) => arr.map((b) => ({ id: uid(), ...b }));

// 当前视图对应的批量素材数组（图片/视频分离）
const currentBatch = () => (state.view === "video" ? state.batchVideos : state.batchImages);

const IMG_EXT = /\.(jpe?g|png|webp|bmp|gif|tiff?|heic|avif)$/i;
const VID_EXT = /\.(mp4|mov|webm|m4v|avi|mkv|flv|wmv|3gp)$/i;

const fmtTime = (s) => { s = Math.max(0, s || 0); const m = Math.floor(s / 60), sec = Math.floor(s % 60); return `${m}:${String(sec).padStart(2, "0")}`; };
const fmtSize = (b) => b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB";

let enginesInfo = null;

/* ===================== Toast ===================== */
function toast(msg, type = "info", ms = 3200) {
  const box = $("toasts");
  if (!box) { alert(msg); return; }
  const el = document.createElement("div");
  el.className = "toast " + type;
  const icon = type === "success" ? ICO("check") : type === "error" ? ICO("x") : ICO("info");
  el.innerHTML = `<span class="toast-txt">${icon} ${String(msg).replace(/</g, "&lt;")}</span>`;
  box.appendChild(el);
  setTimeout(() => { el.classList.add("leaving"); setTimeout(() => el.remove(), 260); }, ms);
}
const notify = (m) => toast(m, "info");
const notifyOk = (m) => toast(m, "success");
const notifyErr = (m) => toast(m, "error", 4500);

/* ===================== 按钮 loading ===================== */
function setBusy(el, busy, label) {
  const txt = el.querySelector(".btn-txt");
  if (busy) {
    el.classList.add("loading"); el.disabled = true;
    if (label && txt) { if (!el.dataset.orig) el.dataset.orig = txt.textContent; txt.innerHTML = label; }
  } else {
    el.classList.remove("loading"); el.disabled = false;
    if (txt && el.dataset.orig) { txt.innerHTML = el.dataset.orig; el.dataset.orig = ""; }
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function setStep(n) { if (n === 2) $("step2").classList.add("active"); if (n === 3) $("step3").classList.add("active"); }

/* ===================== 导航 ===================== */
/* 结果卡片(图片/视频去水印共用)所属类型：image / video / special；
   切换视图时若目标类型不符则清空结果，防止串界面。 */
function clearResultIfTypeMismatch(view) {
  const resultKind = state.resultKind;
  const target = (view === "image" || view === "video") ? view : "special";
  if (!resultKind || resultKind === target) return; // 同类型切换：保留结果
  const rc = $("resultCard"); if (rc) { rc.classList.add("hidden"); rc.classList.remove("open"); }
  const ri = $("resultImg"); if (ri) { ri.removeAttribute("src"); ri.classList.add("hidden"); }
  const rv = $("resultVideo"); if (rv) { rv.removeAttribute("src"); rv.classList.remove("hidden"); rv.pause(); }
  // 批量结果区（图片/视频批量共用）一并清空，避免串界面
  const bc = $("batchCard"); if (bc) bc.classList.add("hidden");
  const bb = $("batchBar"); if (bb) bb.style.width = "0%";
  const zb = $("batchZipBtn"); if (zb) { zb.classList.add("hidden"); zb.removeAttribute("href"); }
  state.resultKind = null;
}
function gotoView(view) {
  clearResultIfTypeMismatch(view);
  state.view = view;
  // 切走时暂停视频。
  if (view !== "video" && !$("srcVideo").paused) $("srcVideo").pause();
  document.querySelectorAll(".snav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  // 完整视图（模板 / 盲水印 / DLSS）：隐藏二级导航、步骤条与去水印面板
  const special = view === "templates" || view === "wm" || view === "dlss";
  $("subbar").classList.toggle("hidden", special);
  // 「载入文件→框选水印→处理下载」步骤条仅在 图片/视频去水印 中显示
  const stepsEl = document.querySelector(".workspace > .steps");
  if (stepsEl) stepsEl.classList.toggle("hidden", special);
  $("view-templates").classList.toggle("hidden", view !== "templates");
  $("view-wm").classList.toggle("hidden", view !== "wm");
  $("view-dlss").classList.toggle("hidden", view !== "dlss");
  // 同一时间只显示一个面板：根据当前二级菜单（单个/批量）精确切换
  $("single-editor").classList.toggle("hidden", special || state.sub !== "single");
  $("batch-panel").classList.toggle("hidden", special || state.sub !== "batch");
  if (view === "templates") { refreshTemplates(); return; }
  if (view === "wm") { initWmView(); return; }
  if (view === "dlss") { initDlssView(); return; }
  syncMediaVisibility();
  updateModeUI();
  // 批量模式：切换图片/视频视图时显示对应分离的列表，并关闭上一视图的预览
  if (state.sub === "batch") {
    state.activeBatchIndex = null;
    $("batchPreviewCard").classList.add("hidden");
    renderBatchFiles();
  }
}

/* ===================== 盲水印（图片与视频统一面板） ===================== */
let wmBound = false;
/* 统一 SVG 图标：从 index.html 内联图标库取 symbol，自动继承文字颜色 */
function ICO(name) { return '<svg class="ico"><use href="#i-' + name + '"/></svg>'; }

/* 参数解释气泡：悬停 .hint（ⓘ）时，在 body 层生成一个 fixed 定位的气泡，
   从而不受任何滚动容器（如设置弹窗 / 参数面板）overflow 裁剪的影响，永不被遮挡。 */
function initHintTips() {
  if (initHintTips._done) return;
  initHintTips._done = true;
  const tip = document.createElement("div");
  tip.className = "hint-tip";
  document.body.appendChild(tip);
  let active = false, hideT = null;
  const hide = (immediate) => {
    if (immediate) { active = false; clearTimeout(hideT); tip.classList.remove("show"); return; }
    if (active) { clearTimeout(hideT); hideT = setTimeout(() => { active = false; tip.classList.remove("show"); }, 120); }
  };
  document.addEventListener("mouseover", (e) => {
    const h = e.target && e.target.closest ? e.target.closest(".hint") : null;
    if (!h || !h.dataset || !h.dataset.hint) return;
    clearTimeout(hideT);
    active = true;
    tip.textContent = h.dataset.hint;
    tip.style.left = "-9999px"; tip.style.top = "-9999px"; tip.style.visibility = "visible"; tip.style.opacity = "1";
    const r = h.getBoundingClientRect();
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = Math.round(r.left + r.width / 2 - tw / 2);
    left = Math.max(10, Math.min(left, window.innerWidth - tw - 10));
    let top = Math.round(r.top - th - 10);
    if (top < 10) top = Math.round(r.bottom + 10); // 上方空间不足则翻转到下方
    tip.style.left = left + "px"; tip.style.top = top + "px"; tip.style.visibility = ""; tip.style.opacity = "";
    tip.classList.add("show");
  }, true);
  document.addEventListener("mouseout", (e) => {
    if (e.target && e.target.closest && e.target.closest(".hint")) hide();
  }, true);
  window.addEventListener("scroll", () => hide(true), { passive: true, capture: true });
  window.addEventListener("resize", () => hide(true));
}
initHintTips();

/* 最近一次嵌入得到的识别码，便于自动带往提取页 */
let lastWmKey = "";
/* 把任意"口令"文本稳定地哈希成一个正整数，供后端使用（留空为 1） */
function wmPwdNum(s) {
  if (!s) return 1;
  let h = 0;
  for (const ch of String(s)) h = (h * 31 + (ch.codePointAt(0) || 0)) >>> 0;
  return h || 1;
}
function initWmView() {
  if (wmBound) return;
  wmBound = true;
  const t = (id) => document.getElementById(id);

  // 嵌入 / 提取 切换
  const sw = (showEmbed) => {
    t("wmTabEmbed").classList.toggle("active", showEmbed);
    t("wmTabExtract").classList.toggle("active", !showEmbed);
    t("wmEmbedPanel").classList.toggle("hidden", !showEmbed);
    t("wmExtractPanel").classList.toggle("hidden", showEmbed);
    // 切到提取页时，预填最近一次嵌入得到的识别码
    if (!showEmbed && lastWmKey && !t("wmExKey").value.trim()) t("wmExKey").value = lastWmKey;
  };
  t("wmTabEmbed").addEventListener("click", () => sw(true));
  t("wmTabExtract").addEventListener("click", () => sw(false));
  // 「去提取水印」：跳到提取页并带好识别码
  on("wmGoExtract", "click", () => { sw(false); t("wmExKey").value = lastWmKey || ""; });
  // 复制提取识别码
  t("wmKeyCopy").addEventListener("click", () => {
    const k = t("wmKeyText").textContent || "";
    if (!k) return;
    navigator.clipboard.writeText(k)
      .then(() => notifyOk("识别码已复制"))
      .catch(() => notifyErr("复制失败，请手动复制"));
  });
  // 「下载结果」：强制下载（blob），绝不跳转打开大图
  t("wmOutLink").addEventListener("click", (ev) => {
    ev.preventDefault();
    const url = t("wmOutLink").getAttribute("href") || "";
    if (!url || url === "#") return;
    const abs = new URL(url, location.href).href;
    const name = decodeURIComponent(url.split("/").pop() || "output") || "output";
    fetch(abs, { credentials: "same-origin" })
      .then((r) => { if (!r.ok) throw new Error("下载地址无效 (" + r.status + ")"); return r.blob(); })
      .then((blob) => {
        const tmp = document.createElement("a");
        tmp.href = URL.createObjectURL(blob); tmp.download = name;
        document.body.appendChild(tmp); tmp.click(); tmp.remove();
        setTimeout(() => URL.revokeObjectURL(tmp.href), 4000);
        notifyOk("已开始下载《" + name + "》，提取识别码请用上方「复制」按钮保存");
      })
      .catch((e) => notifyErr("下载失败：" + (e.message || String(e)) + "，请直接右键预览图/视频另存为"));
  });

  // 水印文字 / 图片 切换（嵌入）
  t("wmMode").addEventListener("change", () => {
    const img = t("wmMode").value === "img";
    t("wmTextWrap").classList.toggle("hidden", img);
    t("wmImgWrap").classList.toggle("hidden", !img);
  });
  // 提取：仅图片水印需要填宽高，文字水印自动推断长度
  t("wmExMode").addEventListener("change", () => {
    const isImg = t("wmExMode").value === "img";
    t("wmExShapeWrap").classList.toggle("hidden", !isImg);
  });
  // 文件选择后的即时预览：上传区与预览窗口共用同一位置（同 DLSS 增强）
  const setPreview = (file, boxId, imgId, vidId, dropId) => {
    const box = t(boxId), img = t(imgId), vid = t(vidId), drop = t(dropId);
    if (!file) { box.classList.add("hidden"); if (drop) drop.classList.remove("hidden"); return; }
    const isV = wmOutIsVid(file);
    const url = URL.createObjectURL(file);
    img.classList.toggle("hidden", isV);
    vid.classList.toggle("hidden", !isV);
    box.classList.remove("hidden");
    if (drop) drop.classList.add("hidden");
    if (isV) { vid.src = url; vid.load(); }
    else img.src = url;
    // 播放条随视频显隐联动：显示图片时隐藏视频进度条
    const pb = box.querySelector("[data-wm-playbar]");
    if (pb) pb.classList.toggle("hidden", !isV);
  };
  // 右上角「更换」按钮：重新打开文件选择器；也支持直接拖拽新文件到预览窗口替换
  const bindReplace = (btnId, inputId, stageId, boxId, imgId, vidId, dropId, lblId) => {
    const input = t(inputId);
    t(btnId).addEventListener("click", () => input.click());
    const stage = t(stageId);
    stage.addEventListener("dragover", (e) => { e.preventDefault(); });
    stage.addEventListener("drop", (e) => {
      e.preventDefault();
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      const dt = new DataTransfer(); dt.items.add(f); input.files = dt.files;
      if (lblId) t(lblId).textContent = f.name;
      setPreview(f, boxId, imgId, vidId, dropId);
    });
  };
  bindReplace("wmReplaceBtn", "wmInput", "wmPrevBox", "wmPrevBox", "wmPrevImg", "wmPrevVid", "wmDrop", "wmFileLbl");
  bindReplace("wmExReplaceBtn", "wmExInput", "wmExPrevBox", "wmExPrevBox", "wmExPrevImg", "wmExPrevVid", "wmExDrop", "wmExFileLbl");
  // 预览视频统一进度条（不内嵌原生控件，与 DLSS 一致）
  const wmPlaybar = (stageId, vidId) => {
    const stage = t(stageId), vid = t(vidId);
    if (!stage || !vid || stage.querySelector("[data-wm-playbar]")) return;
    const pb = makePlaybar(() => (vid.classList.contains("hidden") ? null : vid), [vid]);
    pb.el.setAttribute("data-wm-playbar", "1");
    stage.appendChild(pb.el);
  };
  wmPlaybar("wmPrevBox", "wmPrevVid");
  wmPlaybar("wmExPrevBox", "wmExPrevVid");
  // ---- 盲水印批量：可继续添加 / 移除 / 点击任意文件切换预览（共用大预览窗口） ----
  // 以内存数组维护所选文件（input.files 只读，用于“继续添加”的增量选择）
  if (!window.wmEmbBatch) window.wmEmbBatch = {};
  if (!window.wmExBatch) window.wmExBatch = {};
  wmEmbBatch.files = [];
  wmExBatch.files = [];
  const wmBatchUI = (cfg) => {
    const { filesArr, inputId, gridId, stageId, imgId, vidId, lblId } = cfg;
    const input = t(inputId), grid = t(gridId), stage = t(stageId);
    const img = t(imgId), vid = t(vidId), lbl = t(lblId);
    const drop = stage.closest(".medbox") ? stage.closest(".medbox").querySelector(".dropzone") : null;
    // 预览视频统一进度条
    if (!stage.querySelector("[data-wm-playbar]")) {
      const pb = makePlaybar(() => (vid.classList.contains("hidden") ? null : vid), [vid]);
      pb.el.setAttribute("data-wm-playbar", "1");
      stage.appendChild(pb.el);
    }
    const showFile = (f) => {
      if (!f) { stage.classList.add("hidden"); if (drop) drop.classList.remove("hidden"); if (img) img.removeAttribute("src"); if (vid) vid.removeAttribute("src"); return; }
      const isV = wmOutIsVid(f);
      const url = URL.createObjectURL(f);
      img.classList.toggle("hidden", isV);
      vid.classList.toggle("hidden", !isV);
      stage.classList.remove("hidden");
      if (drop) drop.classList.add("hidden");
      if (isV) { vid.src = url; vid.load(); } else img.src = url;
      // 播放条随视频显隐联动：显示图片时隐藏视频进度条
      const pb = stage.querySelector("[data-wm-playbar]");
      if (pb) pb.classList.toggle("hidden", !isV);
      if (filesArr.length) grid.querySelectorAll(".wm-b-thumb").forEach((x, xi) => x.classList.toggle("active", filesArr[xi] === f));
    };
    const syncInput = () => { try { const dt = new DataTransfer(); filesArr.forEach((f) => dt.items.add(f)); input.files = dt.files; } catch (e) {} };
    const render = () => {
      grid.classList.toggle("hidden", filesArr.length === 0);
      grid.innerHTML = "";
      filesArr.forEach((f, i) => {
        const cell = document.createElement("div");
        cell.className = "b-thumb wm-b-thumb" + (i === 0 ? " active" : ""); cell.title = f.name;
        const isV = wmOutIsVid(f);
        if (isV) { const ic = document.createElement("span"); ic.className = "wm-thumb-ic"; ic.innerHTML = ICO("film"); cell.appendChild(ic); }
        else { const ti = document.createElement("img"); ti.src = URL.createObjectURL(f); ti.alt = f.name; ti.loading = "lazy"; cell.appendChild(ti); }
        const nm = document.createElement("span"); nm.className = "b-thumb-name"; nm.textContent = f.name; cell.appendChild(nm);
        const del = document.createElement("button"); del.type = "button";
        del.className = "b-del"; del.innerHTML = ICO("x"); del.title = "移除";
        del.addEventListener("click", (e) => { e.stopPropagation(); filesArr.splice(i, 1); syncInput(); render(); showFile(filesArr[0]); });
        cell.appendChild(del);
        cell.addEventListener("click", () => { showFile(f); grid.querySelectorAll(".wm-b-thumb").forEach((x, xi) => x.classList.toggle("active", xi === i)); });
        grid.appendChild(cell);
      });
      // 末尾「继续添加」磁贴
      const add = document.createElement("div");
      add.className = "b-thumb wm-b-add"; add.innerHTML = ICO("plus") + "<span>继续添加</span>";
      add.addEventListener("click", () => input.click());
      grid.appendChild(add);
    };
    input.addEventListener("change", () => {
      const added = Array.from(input.files || []);
      if (!added.length) return;
      filesArr.push(...added);
      syncInput();
      lbl.textContent = "已选 " + filesArr.length + " 个文件";
      if (filesArr.length) showFile(filesArr[0]);
      render();
    });
  };
  wmBatchUI({ filesArr: wmEmbBatch.files, inputId: "wmBatchInput", gridId: "wmBatchGrid", stageId: "wmBatchStage", imgId: "wmBatchImg", vidId: "wmBatchVid", lblId: "wmBatchLbl" });
  wmBatchUI({ filesArr: wmExBatch.files, inputId: "wmExBatchInput", gridId: "wmExBatchGrid", stageId: "wmExBatchStage", imgId: "wmExBatchImg", vidId: "wmExBatchVid", lblId: "wmExBatchLbl" });
  // 单个 / 批量 切换（emb=嵌入，ex=提取）
  const wmModeBar = (prefix) => {
    document.querySelectorAll('.wm-card [data-wmsub="' + prefix + '"]').forEach((b) =>
      b.addEventListener("click", () => {
        document.querySelectorAll('.wm-card [data-wmsub="' + prefix + '"]').forEach((x) => x.classList.toggle("active", x === b));
        const opt = b.dataset.opt;
        t(prefix === "emb" ? "wmEmbSingle" : "wmExSingle").classList.toggle("hidden", opt !== "single");
        t(prefix === "emb" ? "wmEmbBatch" : "wmExBatch").classList.toggle("hidden", opt !== "batch");
      })
    );
  };
  wmModeBar("emb");
  wmModeBar("ex");

  // 文件名显示 + 即时预览
  t("wmInput").addEventListener("change", () => {
    const f = t("wmInput").files[0];
    t("wmFileLbl").textContent = f ? f.name : "未选择文件";
    setPreview(f, "wmPrevBox", "wmPrevImg", "wmPrevVid", "wmDrop");
  });
  t("wmExInput").addEventListener("change", () => {
    const f = t("wmExInput").files[0];
    t("wmExFileLbl").textContent = f ? f.name : "未选择文件";
    setPreview(f, "wmExPrevBox", "wmExPrevImg", "wmExPrevVid", "wmExDrop");
  });

  // 水印图片选择：自定义文件控件（按钮点击弹窗，文件名靠左显示）
  {
    const inp = document.getElementById("wmWmFile");
    const btn = document.getElementById("wmWmFileBtn");
    const name = document.getElementById("wmWmFileName");
    if (inp && btn && name) {
      btn.addEventListener("click", () => inp.click());
      inp.addEventListener("change", () => {
        const f = inp.files[0];
        name.textContent = f ? f.name : "未选择文件";
        name.style.color = f ? "var(--text)" : "";
      });
    }
  }

  // 嵌入：单个 / 批量
  t("wmEmbedBtn").addEventListener("click", () => {
  const el = (id) => document.getElementById(id);
  const file = el("wmInput").files[0];
  if (!file) return notifyErr("请先选择图片或视频");
  const mode = el("wmMode").value;
  if (mode === "str" && !el("wmText").value.trim()) return notifyErr("请填写水印文字");
  if (mode === "img" && !el("wmWmFile").files[0]) return notifyErr("请选择水印图片");
  qAdd({ kind: "file", title: "嵌入盲水印 · " + file.name, run: () => wmEmbedRun() });
});
t("wmEmbedBatchBtn").addEventListener("click", () => {
  const files = (wmEmbBatch.files || []).slice();
  if (!files.length) return notifyErr("请先选择要加水印的文件");
  const mode = t("wmMode").value;
  if (mode === "str" && !t("wmText").value.trim()) return notifyErr("请填写水印文字");
  if (mode === "img" && !t("wmWmFile").files[0]) return notifyErr("请选择水印图片");
  files.forEach((f) => qAdd({ kind: "file", title: "嵌入盲水印 · " + f.name, run: () => wmEmbOne(f, true) }));
  t("wmBatchResult").classList.remove("hidden");
  notifyOk("已加入队列，开始批量嵌入 " + files.length + " 个文件");
});
t("wmExBtn").addEventListener("click", () => {
  const el = (id) => document.getElementById(id);
  const file = el("wmExInput").files[0];
  if (!file) return notifyErr("请先选择含水印的图片或视频");
  if (!el("wmExKey").value.trim() && el("wmExMode").value === "img" && !el("wmExShape").value.trim())
    return notifyErr("图片水印请填写「提取识别码」（或展开高级设置填写水印图宽高）");
  qAdd({ kind: "file", title: "提取盲水印 · " + file.name, run: () => wmExtractRun() });
});
t("wmExBatchBtn").addEventListener("click", () => {
  const files = (wmExBatch.files || []).slice();
  if (!files.length) return notifyErr("请先选择含水印的文件");
  if (!t("wmExKey").value.trim() && t("wmExMode").value === "img" && !t("wmExShape").value.trim())
    return notifyErr("图片水印请填写「提取识别码」（或展开高级设置填写水印图宽高）");
  files.forEach((f) => qAdd({ kind: "file", title: "提取盲水印 · " + f.name, run: () => wmExtOne(f, true) }));
  t("wmExBatchResult").classList.remove("hidden");
  notifyOk("已加入队列，开始批量提取 " + files.length + " 个文件");
});
}

const wmOutIsVid = (file) => (file ? VID_EXT.test(file.name) : false);

function wmBatchAppend(file, url, wmKey) {
  const box = document.getElementById("wmBatchResult");
  box.classList.remove("hidden");
  const isV = wmOutIsVid(file);
  const card = document.createElement("div");
  card.className = "wm-bcard";
  card.innerHTML =
    '<div class="wm-bcard-media">' +
      (isV ? '<video src="' + url + '" controls playsinline></video>' : '<img src="' + url + '" alt="" />') +
    '</div>' +
    '<div class="wm-bcard-meta">' +
      '<span class="wm-bname"></span>' +
      '<span class="wm-bkey">识别码：<code></code></span>' +
      '<a class="btn small ghost" href="' + url + '">' + ICO("download") + ' 下载结果</a>' +
    '</div>';
  card.querySelector(".wm-bname").textContent = file.name;
  card.querySelector(".wm-bkey code").textContent = wmKey || "";
  card.querySelector(".wm-bcard-meta a").download = file.name;
  box.appendChild(card);
}

function wmExBatchAppend(file, d) {
  const box = document.getElementById("wmExBatchResult");
  box.classList.remove("hidden");
  const isImg = d.wm_mode !== "str";
  const card = document.createElement("div");
  card.className = "wm-bcard";
  let media = isImg
    ? '<img src="' + (d.wm_output_url || "") + '" alt="水印" />'
    : '<span class="wm-btext"></span>';
  card.innerHTML =
    '<div class="wm-bcard-media">' + media + '</div>' +
    '<div class="wm-bcard-meta">' +
      '<span class="wm-bname"></span>' +
      (isImg ? '<a class="btn small ghost" href="' + (d.wm_output_url || "") + '" download="wm_' + file.name + '">' + ICO("download") + ' 下载水印图</a>' : '') +
    '</div>';
  card.querySelector(".wm-bname").textContent = file.name;
  if (!isImg) card.querySelector(".wm-btext").textContent = "提取到的文字：" + (d.text ?? "") + (d.note || "");
  box.appendChild(card);
}

async function wmEmbOne(file, inBatch) {
  const t = (id) => document.getElementById(id);
  if (!file) return notifyErr("请先选择图片或视频");
  const mode = t("wmMode").value;
  if (mode === "str" && !t("wmText").value.trim()) return notifyErr("请填写水印文字");
  if (mode === "img" && !t("wmWmFile").files[0]) return notifyErr("请选择水印图片");
  const fd = new FormData();
  fd.append("file", file);
  fd.append("wm_mode", mode);
  fd.append("wm_text", mode === "str" ? t("wmText").value.trim() : "");
  if (mode === "img") fd.append("wm_file", t("wmWmFile").files[0]);
  const pw = wmPwdNum(t("wmPw").value);          // 单个口令 → 哈希成后端两个口令整数
  fd.append("password_wm", pw);
  fd.append("password_img", pw);
  fd.append("strength", t("wmStrength") ? t("wmStrength").value : "balanced");
  const btn = inBatch ? t("wmEmbedBatchBtn") : t("wmEmbedBtn");
  const res = t("wmResult");
  const prog = t("wmEmbedProg"), bar = t("wmEmbedBar"), msg = t("wmEmbedMsg");
  res.classList.add("hidden");
  prog.classList.remove("hidden");
  bar.style.width = "0%"; msg.textContent = "提交中…";
  const started = Date.now();
  setBusy(btn, true, "嵌入中…");
  try {
    const r = await fetch("/api/wm/embed", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "提交失败");
    const s = await awaitTask(j.task_id, {
      update: (st) => {
        bar.style.width = (st.progress || 0) + "%";
        msg.textContent = (st.message || "嵌入中…") + " · " + fmtTime((Date.now() - started) / 1000);
      },
      done: () => (bar.style.width = "100%")
    });
    const d = s.output || {};
    if (!d.output_url) throw new Error(d.detail || "嵌入失败");
    const isVid = wmOutIsVid(file);
    if (inBatch) {
      wmBatchAppend(file, d.output_url, d.wm_key || "");
    } else {
      // 结果直接替换原位预览窗口内容（同 DLSS：预览即结果，无重复窗口）
      t("wmPrevImg").classList.toggle("hidden", isVid);
      t("wmPrevVid").classList.toggle("hidden", !isVid);
      if (isVid) { t("wmPrevVid").src = d.output_url; t("wmPrevVid").load(); }
      else t("wmPrevImg").src = d.output_url;
      t("wmOutLink").href = d.output_url;
      t("wmKeyText").textContent = d.wm_key || "";
      lastWmKey = d.wm_key || "";
      // 图片水印：展示实际嵌入尺寸（大图会被自动缩放，提取手填宽高时以此为准）
      const shapeRow = t("wmShapeRow");
      if (shapeRow) {
        const isImgWm = d.wm_mode === "img" && Array.isArray(d.wm_shape) && d.wm_shape.length === 2;
        shapeRow.classList.toggle("hidden", !isImgWm);
        if (isImgWm) t("wmShapeText").textContent = d.wm_shape[0] + " × " + d.wm_shape[1] + "（高×宽）";
      }
      res.classList.remove("hidden");
    }
    prog.classList.add("hidden");
    notifyOk("嵌入完成");
    pushHistory({ kind: isVid ? "video" : "image", title: "嵌入盲水印 · " + file.name, url: d.output_url });
  } catch (e) {
    prog.classList.add("hidden");
    notifyErr(e.message || String(e));
  }
  finally { setBusy(btn, false); }
}

async function wmEmbedRun() { wmEmbOne(document.getElementById("wmInput").files[0], false); }

async function wmExtOne(file, inBatch) {
  const t = (id) => document.getElementById(id);
  if (!file) return notifyErr("请先选择含水印的图片或视频");
  const key = t("wmExKey").value.trim();
  const mode = t("wmExMode").value;
  const shape = t("wmExShape").value.trim();
  const fd = new FormData();
  fd.append("file", file);
  fd.append("wm_key", key);
  if (key) {
    // 有识别码：后端按保存的参数自动带入 类型/长度/口令
  } else if (mode === "img") {
    if (!shape) return notifyErr("请填写图片水印的 宽,高");
    fd.append("wm_mode", mode);
    fd.append("wm_shape", shape);
  }
  // 文字水印：不要求长度，后端自动推断；图片水印无识别码时才需要宽高
  fd.append("password_wm", wmPwdNum(t("wmExPw").value));
  fd.append("password_img", wmPwdNum(t("wmExPw").value));
  // 嵌入强度无需用户提供：有识别码自动带入；无识别码后端自动尝试三档匹配
  const btn = inBatch ? t("wmExBatchBtn") : t("wmExBtn");
  const res = t("wmExResult");
  const prog = t("wmExtractProg"), bar = t("wmExtractBar"), msg = t("wmExtractMsg");
  res.classList.add("hidden");
  prog.classList.remove("hidden");
  bar.style.width = "0%"; msg.textContent = "提交中…";
  const started = Date.now();
  setBusy(btn, true, "提取中…");
  try {
    const r = await fetch("/api/wm/extract", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "提交失败");
    const s = await awaitTask(j.task_id, {
      update: (st) => {
        bar.style.width = (st.progress || 0) + "%";
        msg.textContent = (st.message || "提取中…") + " · " + fmtTime((Date.now() - started) / 1000);
      },
      done: () => (bar.style.width = "100%")
    });
    const d = s.output || {};
    const finalMode = d.wm_mode || mode;
    const note = d.matched_strength
      ? "（已自动匹配嵌入强度：" + ({ quality: "高画质", balanced: "均衡", robust: "最强保护" }[d.matched_strength] || d.matched_strength) + (d.auto_length ? "、水印长度 " + d.auto_length : "") + "）"
      : (d.auto_length ? "（已自动匹配水印长度 " + d.auto_length + "）" : "");
    if (inBatch) {
      wmExBatchAppend(file, { ...d, wm_mode: finalMode, note });
    } else {
      if (finalMode === "str") {
        t("wmExTextLbl").textContent = `提取到的文字：${d.text ?? ""}${note}`;
        t("wmExImgWrap").classList.add("hidden");
        t("wmExImgLink").classList.add("hidden");
      } else {
        t("wmExTextLbl").textContent = "已提取图片水印：" + note;
        t("wmExImg").src = d.wm_output_url;
        t("wmExImgLink").href = d.wm_output_url;
        t("wmExImgLink").classList.remove("hidden");
        t("wmExImgWrap").classList.remove("hidden");
      }
      res.classList.remove("hidden");
    }
    prog.classList.add("hidden");
    if (finalMode === "str") pushHistory({ kind: "text", title: "提取盲水印文字 · " + file.name, text: d.text ?? "" });
    else pushHistory({ kind: "image", title: "提取盲水印图 · " + file.name, url: d.wm_output_url });
  } catch (e) {
    prog.classList.add("hidden");
    notifyErr(e.message || String(e));
  }
  finally { setBusy(btn, false); }
}

async function wmExtractRun() { wmExtOne(document.getElementById("wmExInput").files[0], false); }

/* ===================== DLSS5 视觉增强（原生面板，能力并入本项目后端） ===================== */
let enhStatusTimer = null;
let enhReady = false;
let enhanceBound = false;

async function initDlssView() {
  const bar = $("enhStatusBar"), txt = $("enhStatusTxt");
  if (!bar || !txt) return;
  if (!enhanceBound) {
    enhanceBound = true;
    bindEnhanceTabs();
    bindVideoMode();
    bindEnhanceRuns();
  }
  enhCheckOnce();
}
async function enhCheckOnce() {
  const bar = $("enhStatusBar"), txt = $("enhStatusTxt");
  if (!bar || !txt) return;
  if (enhReady) { bar.classList.add("hidden"); return; }
  let d;
  try { d = await (await fetch("/api/enhance/status")).json(); }
  catch (_) { bar.classList.add("hidden"); return; }
  if (d.ready) {
    // 运行时就绪但能力探测未完成：不急着禁用插帧标签，继续轮询直到探测结束
    if (!d.probe_done) {
      bar.classList.add("hidden");
      if (!enhStatusTimer) enhStatusTimer = setInterval(enhCheckOnce, 1500);
      return;
    }
    enhReady = true;
    if (enhStatusTimer) clearInterval(enhStatusTimer);
    bar.classList.remove("hidden", "err");
    bar.classList.add("hidden");
    applyEnhCapabilities(d.capabilities || {});
  } else if (d.done && d.error) {
    enhReady = false;
    if (enhStatusTimer) clearInterval(enhStatusTimer);
    bar.classList.remove("hidden");
    bar.classList.add("err");
    txt.innerHTML = ICO("x") + " DLSS 运行时初始化失败：" + escapeHtml(String(d.error));
  } else {
    // 仍在初始化：仅在真正初始化时才显示加载条，避免每次进入都闪一下
    bar.classList.remove("hidden", "err");
    txt.innerHTML = '<span class="spinner sm"></span> 正在初始化 DLSS 运行时（首次加载较慢）…';
    if (!enhStatusTimer) enhStatusTimer = setInterval(enhCheckOnce, 1500);
  }
}
function applyEnhCapabilities(caps) {
  const interp = caps.frame_interpolation || {};
  const ok = interp.available === true;
  // 插帧标签始终可用：原生 DLSS 帧生成不可用时自动回退「软件光流补帧」
  document.querySelectorAll('.wm-tabs [data-enhtab="interp"]').forEach((btn) => {
    btn.classList.remove("disabled");
    btn.removeAttribute("title");
  });
  // 标签下方的可见说明：原生不可用时告知将使用软件补帧（可重新检测）
  const hint = $("interpDisabledHint");
  if (hint) {
    if (!ok) {
      const why = interp.hags_enabled === false
        ? "未开启 Windows「硬件加速 GPU 调度（HAGS）」"
        : "原生 DLSS 帧生成仅支持 RTX 40 系列及以上显卡";
      hint.innerHTML = ICO("warn") + " <b>DLSS 帧生成不可用：</b>" + escapeHtml(why) +
        "。<span class='hint-sub'>插帧功能仍可使用：引擎已自动切换为「软件光流补帧」，速度较慢但画质流畅；开启 HAGS 并更换支持的显卡后可点重新检测恢复原生加速。</span>" +
        "<button class='btn small ghost' id='interpReprobe'><svg class='ico'><use href='#i-refresh'/></svg> 重新检测</button>";
      hint.classList.remove("hidden");
      $("#interpReprobe")?.addEventListener("click", interpReprobe);
    } else {
      hint.classList.add("hidden");
    }
  }
  // 引擎下拉：原生不可用时启用并选中「软件光流（兼容）」，禁用原生引擎；可用时反向
  const engineSel = $("ipEngine");
  if (engineSel) {
    const swOpt = engineSel.querySelector('option[value="Software"]');
    if (ok) {
      if (swOpt) { swOpt.disabled = true; swOpt.hidden = true; }
      if (engineSel.value === "Software") engineSel.value = "Auto";
      engineSel.querySelectorAll("option").forEach((o) => { if (o.value !== "Software") o.disabled = false; });
    } else {
      if (swOpt) { swOpt.disabled = false; swOpt.hidden = false; engineSel.value = "Software"; }
      engineSel.querySelectorAll("option").forEach((o) => { if (o.value !== "Software") o.disabled = true; });
    }
  }
  const pp = $("#enhInterp");
  if (pp) {
    const warn = $("#interpWarn");
    if (warn) warn.remove();
  }
}
/* 重新探测帧插值能力：用户开启 HAGS 并重启电脑后，无需重启软件即可重试 */
async function interpReprobe() {
  const btn = $("#interpReprobe");
  if (btn) setBusy(btn, true, "检测中…");
  try {
    const r = await fetch("/api/enhance/probe", { method: "POST" }).then((x) => x.json());
    const caps = r.capabilities || {};
    applyEnhCapabilities(caps);
    if (caps.frame_interpolation && caps.frame_interpolation.available) notifyOk("原生 DLSS 帧生成已可用");
    else notifyOk("仍使用软件光流补帧：原生 DLSS 帧生成需 RTX 40 系列及以上显卡");
  } catch (e) {
    notifyErr("检测失败：" + (e.message || e));
  } finally {
    // 若检测后标签仍禁用，按钮随提示条重建；可用则提示条已隐藏，无需恢复按钮
  }
}
function bindEnhanceTabs() {
  document.querySelectorAll(".wm-tabs [data-enhtab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.classList.contains("disabled")) return; // 能力不可用的标签不允许切换进入
      document.querySelectorAll(".wm-tabs [data-enhtab]").forEach((b) => b.classList.toggle("active", b === btn));
      ["enhImage", "enhVideo", "enhInterp"].forEach((id) => {
        $(id).classList.toggle("hidden", id !== "enh" + btn.dataset.enhtab.charAt(0).toUpperCase() + btn.dataset.enhtab.slice(1));
      });
    });
  });
}
/* 通用：收集文件 + 表单字段 → POST → 轮询进度（含耗时计时） */
function pickFiles(inputId, btn, progId, onDone) {
  const inp = $(inputId);
  const files = Array.from(inp.files || []);
  if (!files.length) { notifyErr("请先选择文件"); return; }
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  const data = onDone.fields || {};
  Object.keys(data).forEach((k) => fd.append(k, data[k]));
  const Prog = $(progId);
  const bar = Prog.querySelector(".bar");
  const msg = Prog.querySelector(".status-msg");
  const started = Date.now();
  Prog.classList.remove("hidden");
  bar.style.animation = "indeterm 1.2s ease-in-out infinite";
  bar.style.width = "60%";
  msg.textContent = "提交中，等待后端… 0s";
  const tick = () => msg.textContent = "处理中… " + fmtTime((Date.now() - started) / 1000);
  const clock = setInterval(tick, 1000);
  setBusy($(btn), true, "处理中…");
  fetch(onDone.endpoint, { method: "POST", body: fd })
    .then((r) => r.json().then((j) => ({ ok: r.ok, j })))
    .then(({ ok, j }) => {
      if (!ok || !j.task_id) throw new Error(j.detail || "提交失败");
      bar.style.animation = "none";
      bar.style.width = "0%";
      tick();
      const timer = setInterval(async () => {
        try {
          const s = await (await fetch("/api/status/" + j.task_id)).json();
          bar.style.width = (s.progress || 0) + "%";
          msg.textContent = (s.message || "") + " · " + fmtTime((Date.now() - started) / 1000);
          if (s.status === "done") {
            clearInterval(timer); clearInterval(clock); setBusy($(btn), false);
            msg.textContent = "完成 · 用时 " + fmtTime((Date.now() - started) / 1000);
            onDone.result(s, Prog, files);
          } else if (s.status === "failed") {
            clearInterval(timer); clearInterval(clock); setBusy($(btn), false);
            msg.textContent = "失败：" + (s.message || "未知错误");
            notifyErr(s.message || "处理失败");
          }
        } catch (_) {}
      }, 600);
    })
    .catch((e) => {
      clearInterval(clock); setBusy($(btn), false);
      msg.textContent = "提交失败：" + (e.message || "");
      notifyErr(e.message || "提交失败");
    });
}
function renderEnhOut(containerId, outputs, isVideo) {
  const box = $(containerId);
  box.innerHTML = "";
  (outputs || []).forEach((o) => {
    const cell = document.createElement("div");
    cell.className = "enh-cell";
    const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return u; } };
    if (isVideo) {
      const v = document.createElement("video");
      v.src = abs(o.url); v.controls = true; v.preload = "metadata"; v.className = "enh-video";
      cell.appendChild(v);
    } else {
      const i = document.createElement("img");
      i.src = abs(o.url); i.loading = "lazy"; i.alt = o.name; i.className = "enh-thumb";
      cell.appendChild(i);
    }
    const a = document.createElement("a");
    const fname = o.name && /\.\w+$/.test(o.name) ? o.name : "output" + (isVideo ? ".mp4" : ".png");
    a.href = abs(o.url); a.download = fname; a.className = "btn small enh-link";
    a.innerHTML = ICO("download") + " 下载 " + escapeHtml(o.name || fname);
    /* 用 fetch->blob 强制下载：兼容相对路径、跨源地址及缺失 download 文件名的情况 */
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      const target = abs(o.url); if (!target) return;
      fetch(target, { credentials: "same-origin" })
        .then((r) => { if (!r.ok) throw new Error(r.status); return r.blob(); })
        .then((blob) => {
          const tmp = document.createElement("a");
          tmp.href = URL.createObjectURL(blob); tmp.download = fname;
          document.body.appendChild(tmp); tmp.click(); tmp.remove();
          setTimeout(() => URL.revokeObjectURL(tmp.href), 4000);
        })
        .catch(() => { window.open(target, "_blank"); });
    });
    cell.appendChild(a);
    box.appendChild(cell);
  });
}
function dlssImageFields() {
  return {
    nr_preset: $("#imNrPreset").value, nr_style: $("#imNrStyle").value,
    nr_intensity: $("#imNrInt").value, local_tone_strength: $("#imLtone").value,
    local_structure_strength: $("#imLstr").value, skin_structure_strength: $("#imSkin").value,
    upscaling_factor: $("#imUp").value, automatic_mask: $("#imMask").value,
    dlss_model_preset: $("#imModel").value, output_format: $("#imFormat").value,
    quality: $("#imQuality").value, rename_mode: $("#imRename").value, custom_suffix: $("#imSuffix").value,
    ai_gpu_uuid: getDevice(),
  };
}
function bindDlssMode() {
  document.querySelectorAll("#enhImage [data-dlss-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#enhImage [data-dlss-mode]").forEach((b) => b.classList.toggle("active", b === btn));
      const single = btn.dataset.dlssMode === "single";
      $("dlssSingle").classList.toggle("hidden", !single);
      $("dlssBatch").classList.toggle("hidden", single);
    });
  });
}
/* 通用增强预览：原 / 增强后 / 前后对比，单按钮循环切换（互斥，同一时刻仅一个展示） */
function enhMode(ui, store, idx) {
  store.idx = idx;
  ui.modeBtn.textContent = ui.labels[idx];
  ui.origEl.hidden = idx !== 0;
  ui.enhEl.hidden = idx !== 1;
  ui.cmpEl.classList.toggle("hidden", idx !== 2);
  // 单视图进度条：进入「前后对比」时隐藏，避免与对比自带进度条重叠
  const _stage = ui.origEl && ui.origEl.closest ? ui.origEl.closest(".medstage") : null;
  const _stageBar = _stage && _stage.querySelector("[data-stage-playbar]");
  if (_stageBar) _stageBar.classList.toggle("hidden", idx === 2);
  if (idx !== 2) ui.cmpEl.querySelectorAll("video").forEach((v) => v.pause());
  if (idx === 2 && store.makeCmp && !store.cmpBuilt) {
    ui.cmpEl.innerHTML = "";
    ui.cmpEl.appendChild(store.makeCmp());
    store.cmpBuilt = true;
    ui.cmpEl.querySelectorAll("video").forEach((v) => { if (!v.dataset.follow) v.play().catch(() => {}); });
  }
}
function wireModeBtn(ui, store, getMax) {
  ui.modeBtn.addEventListener("click", () => {
    const max = getMax();
    enhMode(ui, store, (store.idx + 1) % (max + 1));
  });
}
/* ============ 图片：单个增强（上传区即预览窗口） ============ */
const sSingle = { before: "", after: "", idx: 0, cmpBuilt: false, makeCmp: null };
const sUI = { modeBtn: null, origEl: null, enhEl: null, cmpEl: null, labels: ["原图", "增强后", "前后对比"] };
function initImageSingleUI() {
  sUI.modeBtn = $("sModeBtn"); sUI.origEl = $("sOrig"); sUI.enhEl = $("sEnh"); sUI.cmpEl = $("sCmp");
  wireModeBtn(sUI, sSingle, () => (sSingle.after ? 2 : 0));
}
function bindSingleImage() {
  const inp = $("sFiles"), drop = $("sDrop"), stage = $("sStage");
  inp.addEventListener("change", () => {
    const f = inp.files && inp.files[0];
    sSingle.before = f ? URL.createObjectURL(f) : "";
    sSingle.after = ""; sSingle.cmpBuilt = false; sSingle.idx = 0;
    sUI.cmpEl.innerHTML = ""; sUI.cmpEl.classList.add("hidden");
    if (sSingle.prevUrl) { URL.revokeObjectURL(sSingle.prevUrl); sSingle.prevUrl = null; }
    $("sEnh").removeAttribute("src");
    $("sEnh").style.width = ""; $("sEnh").style.height = "";
    $("sResult").classList.add("hidden"); $("sProg").classList.add("hidden");
    sUI.modeBtn.disabled = true; sUI.modeBtn.textContent = "原图";
    if (f) { drop.classList.add("hidden"); $("sOrig").src = sSingle.before; stage.classList.remove("hidden"); enhMode(sUI, sSingle, 0); }
    else { drop.classList.remove("hidden"); stage.classList.add("hidden"); }
  });
}
async function likeSingleEnhance() {
  const inp = $("sFiles"), f = inp.files && inp.files[0];
  if (!f) return notifyErr("请先选择一张图片");
  const fd = new FormData(); fd.append("files", f);
  Object.entries(dlssImageFields()).forEach(([k, v]) => fd.append(k, v));
  const before = URL.createObjectURL(f), name = f.name;
  qAdd({
    kind: "image",
    title: "DLSS 增强 · " + name,
    run: async () => {
      const Prog = $("sProg"), bar = Prog.querySelector(".bar"), msg = Prog.querySelector(".status-msg");
      $("sResult").classList.add("hidden");
      Prog.classList.remove("hidden");
      bar.style.width = "0%"; msg.textContent = "提交中…";
      const started = Date.now();
      $("sResult").scrollIntoView({ behavior: "smooth" });
      try {
        const j = await fetch("/api/enhance/image", { method: "POST", body: fd }).then((r) => r.json());
        if (!j.task_id) throw new Error(j.detail || "提交失败");
        const s = await awaitTask(j.task_id, {
          update: (st) => { bar.style.width = (st.progress || 0) + "%"; msg.textContent = (st.message || "处理中…") + " · " + fmtTime((Date.now() - started) / 1000); },
          done: () => (bar.style.width = "100%")
        });
        const out = s.outputs && s.outputs[0];
        if (!out) throw new Error("无输出结果");
        sSingle.after = out.url; sSingle.before = before; sSingle.cmpBuilt = false; sSingle.idx = 1;
        sSingle.makeCmp = () => makeCompare(sSingle.before, sSingle.after);
        sUI.cmpEl.innerHTML = ""; sUI.cmpEl.classList.add("hidden");
        if (sSingle.prevUrl) { URL.revokeObjectURL(sSingle.prevUrl); sSingle.prevUrl = null; }
        $("sEnh").src = out.url;
        $("sEnh").style.width = ""; $("sEnh").style.height = "";  // 正式结果：还原自然尺寸显示
        enhMode(sUI, sSingle, 1);
        const a = document.createElement("a"); a.href = out.url; a.download = out.name; a.className = "btn small enh-link"; a.innerHTML = ICO("download") + " 下载结果";
        $("sDl").innerHTML = ""; $("sDl").appendChild(a);
        $("sResult").classList.remove("hidden");
        sUI.modeBtn.disabled = false;
        $("sResult").scrollIntoView({ behavior: "smooth" });
        return { kind: "image", title: "DLSS 增强 · " + name, url: out.url };
      } finally { Prog.classList.add("hidden"); }
    }
  });
}
function dlssVideoFields() {
  return {
    nr_preset: $("#vdNrPreset").value, nr_style: $("#vdNrStyle").value,
    nr_intensity: $("#vdNrInt").value, local_tone_strength: $("#vdLtone").value,
    local_structure_strength: $("#vdLstr").value, skin_structure_strength: $("#vdSkin").value,
    upscaling_factor: $("#vdUp").value, automatic_mask: $("#vdMask").value,
    dlss_model_preset: $("#vdModel").value, codec: $("#vdCodec").value,
    container: $("#vdContainer").value, quality: $("#vdQuality").value,
    hdr_mode: $("#vdHdr").value, rename_mode: $("#vdRename").value, custom_suffix: $("#vdSuffix").value,
    ai_gpu_uuid: getDevice(),
  };
}
function bindVideoMode() {
  document.querySelectorAll("#enhVideo [data-dlss-vmode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#enhVideo [data-dlss-vmode]").forEach((b) => b.classList.toggle("active", b === btn));
      const single = btn.dataset.dlssVmode === "single";
      $("vsSingle").classList.toggle("hidden", !single);
      $("vsBatch").classList.toggle("hidden", single);
    });
  });
}
/* ============ 视频：单个增强（上传区即预览窗口） ============ */
const vSingle = { before: "", after: "", idx: 0, cmpBuilt: false, makeCmp: null };
const vUI = { modeBtn: null, origEl: null, enhEl: null, cmpEl: null, labels: ["原视频", "增强后", "前后对比"] };
function initVideoSingleUI() {
  vUI.modeBtn = $("vsModeBtn"); vUI.origEl = $("vsOrig"); vUI.enhEl = $("vsEnh"); vUI.cmpEl = $("vsCmp");
  wireModeBtn(vUI, vSingle, () => (vSingle.after ? 2 : 0));
  // 原视频 / 增强后 共用一条与「前后对比」一致统一的进度条，跟随当前显示的 video
  const vsPB = makePlaybar(() => {
    const cmp = $("vsCmp"); if (cmp && !cmp.classList.contains("hidden")) return null;
    const orig = $("vsOrig"), enh = $("vsEnh");
    if (enh && !enh.hidden) return enh;
    if (orig && !orig.hidden) return orig;
    return null;
  }, [$("vsOrig"), $("vsEnh")]);
  vsPB.el.setAttribute("data-stage-playbar", "1"); // enhMode 据此在进入对比时隐藏该条
  $("vsStage").appendChild(vsPB.el);
}
function bindSingleVideo() {
  const inp = $("vsFiles"), drop = $("vsDrop"), stage = $("vsStage");
  inp.addEventListener("change", () => {
    const f = inp.files && inp.files[0];
    vSingle.before = f ? URL.createObjectURL(f) : "";
    vSingle.after = ""; vSingle.cmpBuilt = false; vSingle.idx = 0;
    vUI.cmpEl.innerHTML = ""; vUI.cmpEl.classList.add("hidden");
    $("vsEnh").removeAttribute("src"); $("vsEnh").load();
    $("vsResult").classList.add("hidden"); $("vsProg").classList.add("hidden");
    vUI.modeBtn.disabled = true; vUI.modeBtn.textContent = "原视频";
    if (f) {
      if (drop) drop.classList.add("hidden");
      $("vsOrig").src = vSingle.before;
      stage.classList.remove("hidden");
      enhMode(vUI, vSingle, 0); // 统一复位：只显示原视频，隐藏增强框与对比区，避免黑框/双视频残留
    } else { if (drop) drop.classList.remove("hidden"); stage.classList.add("hidden"); }
  });
}
async function likeVideoSingleEnhance() {
  const inp = $("vsFiles"), f = inp.files && inp.files[0];
  if (!f) return notifyErr("请先选择一个视频");
  const fd = new FormData(); fd.append("files", f);
  Object.entries(dlssVideoFields()).forEach(([k, v]) => fd.append(k, v));
  const before = URL.createObjectURL(f), name = f.name;
  qAdd({
    kind: "video",
    title: "DLSS 视频增强 · " + name,
    run: async () => {
      const Prog = $("vsProg"), bar = Prog.querySelector(".bar"), msg = Prog.querySelector(".status-msg");
      $("vsResult").classList.add("hidden");
      Prog.classList.remove("hidden");
      bar.style.width = "0%"; msg.textContent = "提交中…";
      const started = Date.now();
      try {
        const j = await fetch("/api/enhance/video", { method: "POST", body: fd }).then((r) => r.json());
        if (!j.task_id) throw new Error(j.detail || "提交失败");
        const s = await awaitTask(j.task_id, {
          update: (st) => { bar.style.width = (st.progress || 0) + "%"; msg.textContent = (st.message || "处理中…") + " · " + fmtTime((Date.now() - started) / 1000); },
          done: () => (bar.style.width = "100%")
        });
        const out = s.outputs && s.outputs[0];
        if (!out) throw new Error("无输出结果");
        vSingle.after = out.url; vSingle.before = before; vSingle.cmpBuilt = false; vSingle.idx = 1;
        vSingle.makeCmp = () => makeVideoCompare(vSingle.before, vSingle.after);
        vUI.cmpEl.innerHTML = ""; vUI.cmpEl.classList.add("hidden");
        $("vsEnh").src = out.url;
        const _liveWrap = $("vsPrevWrap"); if (_liveWrap) _liveWrap.classList.add("hidden");
        enhMode(vUI, vSingle, 1);
        $("vsDl").innerHTML = "";
        const a = document.createElement("a"); a.href = out.url; a.download = out.name; a.className = "btn small enh-link"; a.innerHTML = ICO("download") + " 下载结果";
        $("vsDl").appendChild(a);
        const _rt = $("vsResultTxt"); if (_rt) _rt.textContent = "增强完成 · 用上方按钮在原视频 / 增强后 / 前后对比间切换";
        $("vsResult").classList.remove("hidden");
        vUI.modeBtn.disabled = false;
        $("vsResult").scrollIntoView({ behavior: "smooth" });
        return { kind: "video", title: "DLSS 视频增强 · " + name, url: out.url };
      } finally { Prog.classList.add("hidden"); }
    }
  });
}
/* 3 秒预览：只渲染视频前 3 秒（走完整管线但仅处理 3 秒片段），
   让用户先确认实际效果、调好参数，再点「开始增强」处理完整视频。
   走任务队列（串行），因此不会与实时预览/真实增强抢 GPU。 */
function likeVideoPreview3s() {
  const inp = $("vsFiles"), f = inp.files && inp.files[0];
  if (!f) return notifyErr("请先选择一个视频");
  const fd = new FormData(); fd.append("files", f);
  fd.append("duration", "3.0");
  Object.entries(dlssVideoFields()).forEach(([k, v]) => fd.append(k, v));
  const before = URL.createObjectURL(f), name = f.name;
  qAdd({
    kind: "video",
    title: "3 秒预览 · " + name,
    run: async () => {
      const Prog = $("vsProg"), bar = Prog.querySelector(".bar"), msg = Prog.querySelector(".status-msg");
      $("vsResult").classList.add("hidden");
      Prog.classList.remove("hidden");
      bar.style.width = "0%"; msg.textContent = "提交 3 秒预览…";
      const started = Date.now();
      try {
        const resp = await fetch("/api/enhance/video_preview", { method: "POST", body: fd });
        if (resp.status === 404) throw new Error("当前运行的后端还没有「3 秒预览」功能，请完全退出并重新打开 YangZai.exe 后再试");
        const j = await resp.json();
        if (!j.task_id) throw new Error(j.detail || "提交失败");
        const s = await awaitTask(j.task_id, {
          update: (st) => { bar.style.width = (st.progress || 0) + "%"; msg.textContent = (st.message || "处理中…") + " · " + fmtTime((Date.now() - started) / 1000); },
          done: () => (bar.style.width = "100%")
        });
        const out = s.outputs && s.outputs[0];
        if (!out) throw new Error((s.failures && s.failures[0] && s.failures[0].error) || "无输出结果");
        vSingle.after = out.url; vSingle.before = before; vSingle.cmpBuilt = false; vSingle.idx = 1;
        vSingle.makeCmp = () => makeVideoCompare(vSingle.before, vSingle.after);
        vUI.cmpEl.innerHTML = ""; vUI.cmpEl.classList.add("hidden");
        $("vsEnh").src = out.url;
        const _lw = $("vsPrevWrap"); if (_lw) _lw.classList.add("hidden");
        enhMode(vUI, vSingle, 1);
        $("vsDl").innerHTML = "";   // 预览是临时产物，不提供下载，引导调参后开始增强
        const _rt = $("vsResultTxt");
        if (_rt) _rt.textContent = "前 3 秒预览完成 · 用上方按钮切换 原视频 / 增强后 / 前后对比；调好参数后点「开始增强」处理完整视频";
        $("vsResult").classList.remove("hidden");
        vUI.modeBtn.disabled = false;
        $("vsResult").scrollIntoView({ behavior: "smooth" });
        // 不返回结果：3 秒预览不进历史记录
      } finally { Prog.classList.add("hidden"); }
    }
  });
}
/* 视频前后对比：两张视频叠放 + 可拖动竖线。以【增强后】为主播放器，
   原视频通过 rAF 每帧强制对齐到主播放器 currentTime，保证左右两侧始终处于
   同一时间线的同一时刻，实现动态帧对比（而非各自独立播放导致的画面错位）。 */

/* ===== 统一播放进度条组件 =====
   用于「原视频」「增强后」「前后对比」等所有场景的播放控制，样式统一、不内嵌视频原生控件。
   - getActive(): 返回当前应显示的 video（对比场景为增强主视频；单视图为当前可见项）
   - sources:    需要监听进度/播放状态的 video 列表（事件来自任一 source，但只更新当前 active）
   返回 { el, toggle, dragging }。*/
function makePlaybar(getActive, sources) {
  const el = document.createElement("div"); el.className = "cmp-playbar";
  const pbtn = document.createElement("button"); pbtn.type = "button"; pbtn.className = "cmp-pb-btn"; pbtn.innerHTML = ICO("play");
  const track = document.createElement("div"); track.className = "cmp-pb-track";
  const fill = document.createElement("div"); fill.className = "cmp-pb-fill";
  const tm = document.createElement("span"); tm.className = "cmp-pb-time"; tm.textContent = "0:00 / 0:00";
  track.appendChild(fill); el.append(pbtn, track, tm);

  const fmtClock = (s) => { s = Math.floor(s || 0); const m = Math.floor(s / 60); const sec = s % 60; return m + ":" + String(sec).padStart(2, "0"); };
  const clamp01 = (x) => Math.max(0, Math.min(1, x));
  const ref = () => { const v = getActive(); return v && document.contains(v) ? v : null; };
  const updateUI = () => {
    const v = ref();
    const d = (v && v.duration) || 0, c = (v && v.currentTime) || 0;
    fill.style.width = (d ? clamp01(c / d) * 100 : 0) + "%";
    tm.textContent = fmtClock(c) + " / " + fmtClock(d);
    pbtn.innerHTML = (v && !v.paused) ? ICO("pause") : ICO("play");
  };
  const toggle = () => { const v = ref(); if (!v) return; if (v.paused) v.play().catch(() => {}); else v.pause(); };
  const seekTo = (e) => {
    const v = ref(); if (!v || !v.duration) return;
    const r = track.getBoundingClientRect();
    const p = clamp01((e.clientX - r.left) / (r.width || 1));
    v.currentTime = p * v.duration; updateUI();
  };
  pbtn.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
  // 拖动抑制：拖动进度条结束会派发一次 click，若不抑制会误触发“点击画面切换播放/暂停”
  let dragging = false;
  track.addEventListener("pointerdown", (e) => { e.stopPropagation(); e.preventDefault(); dragging = true; seekTo(e); });
  track.addEventListener("pointermove", (e) => { if (e.buttons & 1) seekTo(e); });
  track.addEventListener("pointerup", () => { setTimeout(() => { dragging = false; }, 0); });
  (sources || []).forEach((v) => { if (!v) return;
    v.addEventListener("play", () => { if (v === ref()) updateUI(); });
    v.addEventListener("pause", () => { if (v === ref()) updateUI(); });
    v.addEventListener("seeked", () => { if (v === ref()) updateUI(); });
    v.addEventListener("timeupdate", () => { if (v === ref()) updateUI(); });
    v.addEventListener("loadedmetadata", () => { if (v === ref()) updateUI(); });
  });

  return { el, toggle, dragging: () => dragging };
}
/* 视频批量：原视频 / 增强后 共用一条与「前后对比」一致统一的进度条（不内嵌原生控件） */
function initVideoBatchUI() {
  const vbPB = makePlaybar(() => {
    const cmp = $("vbCmp"); if (cmp && !cmp.classList.contains("hidden")) return null;
    const orig = $("vbOrig"), enh = $("vbEnh");
    if (enh && !enh.hidden) return enh;
    if (orig && !orig.hidden) return orig;
    return null;
  }, [$("vbOrig"), $("vbEnh")]);
  vbPB.el.setAttribute("data-stage-playbar", "1");
  $("vbStage").appendChild(vbPB.el);
}

function makeVideoCompare(beforeUrl, afterUrl) {
  const wrap = document.createElement("div"); wrap.className = "enh-cmp"; wrap.style.minHeight = "220px";
  const after = document.createElement("video"); after.className = "cmp-after"; after.src = afterUrl;
  const before = document.createElement("video"); before.className = "cmp-before"; before.src = beforeUrl;
  after.muted = before.muted = true; after.loop = before.loop = true; after.playsInline = before.playsInline = true;
  before.pause(); // 从视频不自主播放，仅作为「同步帧」跟随主播放器
  before.dataset.follow = "1"; // 标记为从属视频：进入对比时不被外层统一 play 自主播放

  const line = document.createElement("div"); line.className = "cmp-line";
  const tb = document.createElement("span"); tb.className = "cmp-tag before"; tb.textContent = "原视频";
  const ta = document.createElement("span"); ta.className = "cmp-tag after"; ta.textContent = "增强";
  const pb = makePlaybar(() => after, [after]);
  wrap.append(after, before, line, tb, ta, pb.el);

  const setPct = (p) => wrap.style.setProperty("--p", Math.max(0, Math.min(100, p)) + "%");
  setPct(50);
  const size = () => {
    const v = [before, after].find((x) => x.videoWidth) || after;
    const w = wrap.clientWidth || 600;
    const r = (v.videoHeight || 16) / (v.videoWidth || 9);
    const maxH = Math.max(140, Math.round((window.innerHeight || 800) * 0.58));
    wrap.style.height = Math.round(Math.min(w * r, maxH)) + "px";
  };
  before.addEventListener("loadedmetadata", size);
  after.addEventListener("loadedmetadata", size);
  // 拖动竖线也会因 setPointerCapture 在松手后派发 click，需与拖动进度条一样抑制播放切换
  let wrapDrag = false;
  wrap.addEventListener("pointerdown", (e) => { if (pb.el.contains(e.target)) return; e.preventDefault(); try { wrap.setPointerCapture(e.pointerId); } catch (_) {} setPct((e.clientX - wrap.getBoundingClientRect().left) / wrap.clientWidth * 100); });
  wrap.addEventListener("pointermove", (e) => { if (pb.el.contains(e.target)) return; if (e.buttons & 1) { wrapDrag = true; setPct((e.clientX - wrap.getBoundingClientRect().left) / wrap.clientWidth * 100); } });
  wrap.addEventListener("pointerup", () => { setTimeout(() => { wrapDrag = false; }, 0); });
  requestAnimationFrame(size);

  // ---- 同一时间线同步：before 完全跟随 after 的播放状态，保证两侧同一时刻画面对比 ----
  const playSync = () => { before.play().catch(() => {}); };
  const pauseSync = () => { before.pause(); };
  after.addEventListener("play", playSync);
  after.addEventListener("pause", pauseSync);
  after.addEventListener("seeked", () => { before.currentTime = after.currentTime; });
  // 仅在有明显漂移（>0.12s）时对齐，避免每帧强制 seek 把一侧拽停（那会造成“只有一边播放”）
  const drift = () => {
    if (!wrap.isConnected) return;
    requestAnimationFrame(drift);
    if (before.currentTime && Math.abs(before.currentTime - after.currentTime) > 0.12) {
      before.currentTime = after.currentTime;
    }
  };
  requestAnimationFrame(drift);

  // 播放/暂停控制（播放按钮，或点击画面任意处；拖动进度条/竖线不会误触发）
  wrap.addEventListener("click", (e) => {
    if (pb.dragging() || wrapDrag) return;
    if (e.target === line || e.target === tb || e.target === ta || pb.el.contains(e.target)) return;
    pb.toggle();
  });

  return wrap;
}
/* 前后对比：两张图叠放，clip-path 露出左侧原始，右侧增强；拖拽竖线左右滑动查看。 */
function makeCompare(beforeUrl, afterUrl) {
  const wrap = document.createElement("div");
  wrap.className = "enh-cmp";
  const after = document.createElement("img"); after.className = "cmp-after"; after.src = afterUrl;
  const before = document.createElement("img"); before.className = "cmp-before"; before.src = beforeUrl;
  const line = document.createElement("div"); line.className = "cmp-line";
  const tb = document.createElement("span"); tb.className = "cmp-tag before"; tb.textContent = "原图";
  const ta = document.createElement("span"); ta.className = "cmp-tag after"; ta.textContent = "增强";
  wrap.append(after, before, line, tb, ta);
  const setPct = (p) => wrap.style.setProperty("--p", Math.max(0, Math.min(100, p)) + "%");
  const size = () => {
    // 定高舞台（.medstage）内：撑满舞台，与「原图/增强后」显示尺寸完全一致
    if (wrap.closest && wrap.closest(".medstage")) {
      wrap.style.height = "100%";
      return;
    }
    const w = wrap.clientWidth || 600;
    const r = (after.naturalHeight || 1) / (after.naturalWidth || 1.5);
    const maxH = Math.max(140, Math.round((window.innerHeight || 800) * 0.58));
    wrap.style.height = Math.round(Math.min(w * r, maxH)) + "px";
  };
  before.onload = after.onload = size;
  wrap.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    try { wrap.setPointerCapture(e.pointerId); } catch (_) {}
    setPct((e.clientX - wrap.getBoundingClientRect().left) / wrap.clientWidth * 100);
  });
  wrap.addEventListener("pointermove", (e) => {
    if (e.buttons & 1) setPct((e.clientX - wrap.getBoundingClientRect().left) / wrap.clientWidth * 100);
  });
  setPct(50);
  return wrap;
}
/* 通用增强任务体：提交 + 轮询进度（队列内部调用） */
function enhanceRunner(endpoint, fields, filesList, progId, onDone) {
  return async function () {
    const Prog = $(progId), bar = Prog.querySelector(".bar"), msg = Prog.querySelector(".status-msg");
    Prog.classList.remove("hidden");
    bar.style.animation = "none"; bar.style.width = "0%"; msg.textContent = "提交中…";
    const started = Date.now();
    try {
      const fd = new FormData();
      filesList.forEach((f) => fd.append("files", f));
      Object.entries(fields).forEach(([k, v]) => fd.append(k, v));
      const j = await fetch(endpoint, { method: "POST", body: fd }).then((r) => r.json());
      if (!j.task_id) throw new Error(j.detail || "提交失败");
      const s = await awaitTask(j.task_id, {
        update: (st) => { bar.style.width = (st.progress || 0) + "%"; msg.textContent = (st.message || "处理中…") + " · " + fmtTime((Date.now() - started) / 1000); },
        done: () => (bar.style.width = "100%")
      });
      if (onDone) onDone({ outputs: s.outputs || [], message: s.message });
    } finally { Prog.classList.add("hidden"); }
  };
}

/* ---- DLSS 批量（图片/视频共用）：大预览窗口 + 管理条（继续添加 / 移除 / 点击预览）---- */
const bBatch = { items: [], cur: -1, idx: 0 };
const vbBatch = { items: [], cur: -1, idx: 0 };
function initDlssBatch(kind) {
  const isImg = kind === "image";
  const state = isImg ? bBatch : vbBatch;
  const E = {
    input: $(isImg ? "bFiles" : "vbFiles"), list: $(isImg ? "bList" : "vbList"),
    stage: $(isImg ? "bStage" : "vbStage"), modeBtn: $(isImg ? "bModeBtn" : "vbModeBtn"),
    orig: $(isImg ? "bOrig" : "vbOrig"), enh: $(isImg ? "bEnh" : "vbEnh"), cmp: $(isImg ? "bCmp" : "vbCmp"),
  };
  const drop = E.input.closest(".enh-drop");
  const MODES = isImg ? ["原图", "增强后", "前后对比"] : ["原视频", "增强后", "前后对比"];

  const setMode = (idx) => {
    const it = state.items[state.cur];
    if (!it || !it.outUrl) idx = 0;   // 尚无结果时只能看原图
    state.idx = idx;
    E.modeBtn.textContent = MODES[idx];
    E.orig.hidden = idx !== 0; E.enh.hidden = idx !== 1; E.cmp.classList.toggle("hidden", idx !== 2);
    // 视频批量：进入「前后对比」隐藏外层统一进度条，避免与对比自带进度条重叠
    const pb = E.stage.querySelector("[data-stage-playbar]");
    if (pb) pb.classList.toggle("hidden", idx === 2);
    if (idx === 2 && it && !it.cmpBuilt) {
      E.cmp.innerHTML = "";
      E.cmp.appendChild(isImg ? makeCompare(it.url, it.outUrl) : makeVideoCompare(it.url, it.outUrl));
      it.cmpBuilt = true;
      E.cmp.querySelectorAll("video").forEach((v) => { if (!v.dataset.follow) v.play().catch(() => {}); });
    }
  };

  const showItem = (i) => {
    const it = state.items[i]; if (!it) return;
    state.cur = i; it.cmpBuilt = false;
    E.orig.src = it.url || "";
    if (it.outUrl) E.enh.src = it.outUrl;
    E.stage.classList.remove("hidden");
    if (drop) drop.classList.add("hidden");   // 上传区被预览窗口原位替换
    E.modeBtn.disabled = !it.outUrl;
    setMode(0);
    E.list.querySelectorAll(".b-thumb").forEach((t, ti) => t.classList.toggle("active", ti === i));
    // 下载按钮统一放在下方结果列表（每个结果一行），此处不再重复放「当前项」下载
  };

  const resetEmpty = () => {
    state.cur = -1; state.idx = 0;
    E.stage.classList.add("hidden");
    if (drop) drop.classList.remove("hidden");
    E.modeBtn.disabled = true;
    $(isImg ? "bDl" : "vbDl").innerHTML = "";
  };

  const render = () => {
    E.list.classList.toggle("hidden", state.items.length === 0);
    E.list.innerHTML = "";
    state.items.forEach((it, i) => {
      const cell = document.createElement("div");
      cell.className = "b-thumb wm-b-thumb" + (i === state.cur ? " active" : ""); cell.title = it.name;
      if (isImg) {
        const img = document.createElement("img"); img.src = it.url; img.alt = it.name; img.loading = "lazy"; cell.appendChild(img);
      } else {
        const ic = document.createElement("span"); ic.className = "wm-thumb-ic"; ic.innerHTML = ICO("film"); cell.appendChild(ic);
      }
      const nm = document.createElement("span"); nm.className = "b-thumb-name"; nm.textContent = it.name; cell.appendChild(nm);
      const del = document.createElement("button"); del.type = "button";
      del.className = "b-del"; del.innerHTML = ICO("x"); del.title = "移除";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        try { URL.revokeObjectURL(it.url); } catch (err) {}
        state.items.splice(i, 1);
        syncInput();
        if (!state.items.length) { render(); resetEmpty(); return; }
        if (state.cur === i) showItem(Math.min(i, state.items.length - 1));
        else if (state.cur > i) state.cur--;
        render();
      });
      cell.appendChild(del);
      cell.addEventListener("click", () => { state.idx = 0; showItem(i); });
      E.list.appendChild(cell);
    });
    const add = document.createElement("div");
    add.className = "b-thumb wm-b-add"; add.innerHTML = ICO("plus") + "<span>继续添加</span>";
    add.addEventListener("click", () => E.input.click());
    E.list.appendChild(add);
  };

  const syncInput = () => { try { const dt = new DataTransfer(); state.items.forEach((it) => dt.items.add(it.file)); E.input.files = dt.files; } catch (e) {} };
  const addFiles = (fl) => {
    const added = Array.from(fl || []);
    if (!added.length) return;
    added.forEach((f) => state.items.push({ file: f, name: f.name, url: URL.createObjectURL(f), outUrl: "", cmpBuilt: false }));
    syncInput(); render();
    showItem(state.cur >= 0 ? state.cur : 0);
  };

  E.input.addEventListener("change", () => addFiles(E.input.files));
  if (drop) {
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.remove("dragover"); }));
    drop.addEventListener("drop", (e) => { const dt = e.dataTransfer; if (dt && dt.files && dt.files.length) addFiles(dt.files); });
  }
  // “原图/增强后/前后对比”循环切换（无结果时只有原图档）
  E.modeBtn.addEventListener("click", () => { if (state.cur >= 0) setMode((state.idx + 1) % (state.items[state.cur].outUrl ? 3 : 1)); });
  state.addFiles = addFiles; state.render = render; state.showItem = showItem;
}

function likeImageBatch() {
  const items = bBatch.items.slice();
  if (!items.length) return notifyErr("请先选择图片");
  const files = items.map((it) => it.file);
  qAdd({
    title: "DLSS 批量增强 · " + items.length + " 张",
    run: enhanceRunner("/api/enhance/image", dlssImageFields(), files, "bProg", (s) => {
      (s.outputs || []).forEach((o, i) => { if (bBatch.items[i]) bBatch.items[i].outUrl = o.url; });
      $("bResult").classList.remove("hidden");
      renderEnhOut("bOut", s.outputs, false);
      bBatch.showItem(bBatch.cur >= 0 ? bBatch.cur : 0);
      $("bResult").scrollIntoView({ behavior: "smooth" });
      (s.outputs || []).forEach((o, i) => pushHistory({ kind: "image", title: "DLSS 增强 · " + (items[i] ? items[i].name : o.name), url: o.url }));
    })
  });
}

function likeVideoBatch() {
  const items = vbBatch.items.slice();
  if (!items.length) return notifyErr("请先选择视频");
  const files = items.map((it) => it.file);
  qAdd({
    title: "DLSS 批量视频增强 · " + items.length + " 个",
    run: enhanceRunner("/api/enhance/video", dlssVideoFields(), files, "vbProg", (s) => {
      (s.outputs || []).forEach((o, i) => { if (vbBatch.items[i]) vbBatch.items[i].outUrl = o.url; });
      $("vbResult").classList.remove("hidden");
      renderEnhOut("vbOut", s.outputs, true);
      vbBatch.showItem(vbBatch.cur >= 0 ? vbBatch.cur : 0);
      $("vbResult").scrollIntoView({ behavior: "smooth" });
      (s.outputs || []).forEach((o, i) => pushHistory({ kind: "video", title: "DLSS 视频增强 · " + (items[i] ? items[i].name : o.name), url: o.url }));
    })
  });
}
function interpolationFields() {
  return {
    target_fps: $("#ipFps").value, engine: $("#ipEngine").value,
    codec: $("#ipCodec").value, container: $("#ipContainer").value,
    quality: $("#ipQuality").value, hdr_mode: $("#ipHdr").value,
    rename_mode: $("#ipRename").value, custom_suffix: $("#ipSuffix").value,
    ai_gpu_uuid: getDevice(),
  };
}
/* ---- 插帧批量：共用大预览窗口（可继续添加 / 移除 / 点击切换预览）---- */
const interpBatch = { items: [], cur: -1, mode: "orig" };
function initInterpBatchUI() {
  const input = $("ipFiles"), grid = $("ipGrid"), stage = $("ipStage");
  const vid = $("ipVid"), modeBtn = $("ipModeBtn");
  const drop = stage.closest(".medbox") ? stage.closest(".medbox").querySelector(".dropzone") : null;
  // 预览视频统一进度条
  if (!stage.querySelector("[data-stage-playbar]")) {
    const pb = makePlaybar(() => (vid.classList.contains("hidden") ? null : vid), [vid]);
    pb.el.setAttribute("data-stage-playbar", "1");
    stage.appendChild(pb.el);
  }
  const showItem = (i) => {
    const it = interpBatch.items[i]; if (!it) return;
    interpBatch.cur = i;
    vid.classList.remove("hidden");
    const useEnh = interpBatch.mode === "enh" && it.outUrl;
    vid.src = useEnh ? it.outUrl : it.url;
    vid.load();
    stage.classList.remove("hidden");
    if (drop) drop.classList.add("hidden");
    modeBtn.textContent = useEnh ? "增强后" : "原视频";
    grid.querySelectorAll(".wm-b-thumb").forEach((x, xi) => x.classList.toggle("active", xi === i));
  };
  const render = () => {
    grid.classList.toggle("hidden", interpBatch.items.length === 0);
    grid.innerHTML = "";
    interpBatch.items.forEach((it, i) => {
      const cell = document.createElement("div");
      cell.className = "b-thumb wm-b-thumb" + (i === interpBatch.cur ? " active" : ""); cell.title = it.name;
      const ic = document.createElement("span"); ic.className = "wm-thumb-ic"; ic.innerHTML = ICO("film"); cell.appendChild(ic);
      const nm = document.createElement("span"); nm.className = "b-thumb-name"; nm.textContent = it.name; cell.appendChild(nm);
      const del = document.createElement("button"); del.type = "button";
      del.className = "b-del"; del.innerHTML = ICO("x"); del.title = "移除";
      del.addEventListener("click", (e) => { e.stopPropagation(); interpBatch.items.splice(i, 1); render(); showItem(interpBatch.items[0] ? 0 : -1); });
      cell.appendChild(del);
      cell.addEventListener("click", () => { interpBatch.mode = "orig"; modeBtn.textContent = "原视频"; showItem(i); });
      grid.appendChild(cell);
    });
    const add = document.createElement("div");
    add.className = "b-thumb wm-b-add"; add.innerHTML = ICO("plus") + "<span>继续添加</span>";
    add.addEventListener("click", () => input.click());
    grid.appendChild(add);
  };
  const syncInput = () => { try { const dt = new DataTransfer(); interpBatch.items.forEach((it) => dt.items.add(it.file)); input.files = dt.files; } catch (e) {} };
  input.addEventListener("change", () => {
    const added = Array.from(input.files || []);
    if (!added.length) return;
    added.forEach((f) => interpBatch.items.push({ file: f, name: f.name, url: URL.createObjectURL(f), outUrl: "" }));
    syncInput();
    interpBatch.mode = "orig"; modeBtn.textContent = "原视频";
    showItem(interpBatch.cur >= 0 ? interpBatch.cur : (interpBatch.items.length ? 0 : -1));
    render();
  });
  // 原视频 / 增强后 切换
  modeBtn.addEventListener("click", () => {
    if (interpBatch.cur < 0) return;
    interpBatch.mode = (interpBatch.mode === "enh") ? "orig" : "enh";
    showItem(interpBatch.cur);
  });
}
function likeInterpEnhance() {
  const items = interpBatch.items.slice();
  if (!items.length) return notifyErr("请先选择视频");
  const files = items.map((it) => it.file);
  qAdd({
    title: "DLSS 插帧 · " + items.length + " 个",
    run: enhanceRunner("/api/enhance/interpolate", interpolationFields(), files, "ipProg", (s) => {
      // 把增强结果 URL 关联回对应文件，便于在预览中切换「增强后」
      (s.outputs || []).forEach((o, i) => { if (interpBatch.items[i]) interpBatch.items[i].outUrl = o.url; });
      if (interpBatch.cur >= 0) showItem(interpBatch.cur);
      $("ipResult").classList.remove("hidden");
      renderEnhOut("ipOut", s.outputs, true);
      $("ipResult").scrollIntoView({ behavior: "smooth" });
      (s.outputs || []).forEach((o, i) => pushHistory({ kind: "video", title: "DLSS 插帧 · " + (items[i] ? items[i].name : o.name), url: o.url }));
    })
  });
}
function bindEnhanceRuns() {
  // DLSS 批量（图片/视频）：管理条（继续添加 / 移除 / 点击预览）+ 大预览窗口
  initDlssBatch("image");
  initDlssBatch("video");
  initInterpBatchUI();
  bindDlssMode();
  bindSingleImage();
  bindSingleVideo();
  initVideoBatchUI();
  // 单个增强：把文件拖进上传区即选用该文件
  const attachSingleDrop = (inputId, dropId) => {
    const inp = $(inputId), drop = $(dropId);
    if (!inp || !drop) return;
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.remove("dragover"); }));
    drop.addEventListener("drop", (e) => {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length) {
        try { inp.files = dt.files; } catch (_) {}
        inp.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
  };
  attachSingleDrop("sFiles", "sDrop");
  attachSingleDrop("vsFiles", "vsDrop");
  initImageSingleUI(); initVideoSingleUI();
  // 单张预览区「更换文件」：重新触发文件选择（导入错误时可直接重选）
  const replacePick = (inputId) => { const i = $(inputId); i && i.click(); };
  $("sReplace")?.addEventListener("click", () => replacePick("sFiles"));
  $("vsReplace")?.addEventListener("click", () => replacePick("vsFiles"));
  // 所有“生成/增强”按钮统一走自动队列：有任务在跑时新任务自动排队
  $("#sBtn").addEventListener("click", () => likeSingleEnhance());
  $("#vsBtn").addEventListener("click", () => likeVideoSingleEnhance());
  const _p3 = $("#vsPrev3sBtn"); if (_p3) _p3.addEventListener("click", () => likeVideoPreview3s());
  $("#bBtn").addEventListener("click", () => likeImageBatch());
  $("#vbBtn").addEventListener("click", () => likeVideoBatch());
  // 插帧
  $("#ipBtn").addEventListener("click", () => likeInterpEnhance());
  // 滑块值显示
  [["imNrInt","imNrIntV"],["imLtone","imLtoneV"],["imLstr","imLstrV"],["imSkin","imSkinV"],
   ["vdNrInt","vdNrIntV"],["vdLtone","vdLtoneV"],["vdLstr","vdLstrV"],["vdSkin","vdSkinV"]]
    .forEach(([inp, lbl]) => {
      const el = $(inp); if (!el) return;
      const sync = () => $(lbl).textContent = parseFloat(el.value).toFixed(2);
      el.addEventListener("input", sync); sync();
    });
  // 画质输入钳制：number 输入框的 min/max 挡不住手动键入，失焦时把值纠正回 1~100
  const qInp = $("imQuality");
  if (qInp) qInp.addEventListener("change", () => {
    let v = Math.round(parseFloat(qInp.value));
    if (isNaN(v)) v = 100;
    qInp.value = Math.max(1, Math.min(100, v));
  });
}
/* 视图决定显示哪种媒体：图片视图只看图片、视频视图只看视频；
   已上传的数据(src/kind/文件名/stagingId)始终保留，切回匹配视图时能 100% 还原；
   若当前已载入类型与视图不符 → 把 stageCard 整个隐藏（露出第一步上传 dropzone，恢复"第一步"状态）。 */
function syncMediaVisibility() {
  const v = $("srcVideo"), img = $("srcImg");
  const dropzone = $("dropzone");
  const stageCard = $("stageCard");
  const timeline = $("timeline");
  const want = state.view === "video" ? "video" : "image";

  // 1) 单个媒体元素的显隐
  if (v)   v.classList.toggle("hidden",   want !== "video");
  if (img) img.classList.toggle("hidden", want !== "image");

  // 2) 类型不匹配：清空"错误视图的那个媒体元素"的 src，避免出现图片视图下黑色空图占位（用户截图里那种问题）
  if (want === "video" && img && img.parentElement) img.removeAttribute("src");
  if (want === "image" && v && v.parentElement)    v.removeAttribute("src");

  // 3) 决定显示 上传 dropzone 还是 stageCard 预览
  if (!stageCard || !dropzone) return;
  const showStage = !!state.stagingId && state.kind === want;
  stageCard.classList.toggle("hidden", !showStage);
  dropzone.classList.toggle("hidden",  showStage);

  // 4) 时间轴只在视频视图、且 stageCard 显示时出现（图片视图必隐藏）
  if (timeline) timeline.classList.toggle("hidden", want !== "video" || !showStage);
  const segBarEl = $("segBar");
  if (segBarEl) segBarEl.classList.toggle("hidden", want !== "video" || !showStage);

  // 5) 如果已上传 + 类型匹配，把真正的 src 回填回去（之前为了去黑图被我们清掉了）
  if (showStage && want === "video" && state._mediaVideoSrc) {
    v.src = state._mediaVideoSrc;
    if (state._mediaVideoPoster) v.poster = state._mediaVideoPoster;
  }
  if (showStage && want === "image" && state._mediaImgSrc)   img.src = state._mediaImgSrc;
}

function gotoSub(sub) {
  state.sub = sub;
  // 仅切换带 data-sub 的按钮（DLSS 的 .sub 按钮用 data-dlss-mode/-vmode，由各自处理器管理）
  document.querySelectorAll(".sub[data-sub]").forEach((b) => b.classList.toggle("active", b.dataset.sub === sub));
  $("single-editor").classList.toggle("hidden", sub !== "single");
  $("batch-panel").classList.toggle("hidden", sub !== "batch");
  if (sub === "single") { state.boxMode = "single"; state.layer = $("boxesLayer"); }
  updateModeUI();
}

document.querySelectorAll(".snav-item[data-view]").forEach((b) => b.addEventListener("click", () => gotoView(b.dataset.view)));
document.querySelectorAll(".sub[data-sub]").forEach((b) => b.addEventListener("click", () => gotoSub(b.dataset.sub)));

/* ===================== 主题切换：黑夜 / 白天 / 奶油（小羊配色） ===================== */
const THEMES = [
  { id: "dark", icon: "moon", label: "黑夜模式" },
  { id: "light", icon: "sun", label: "白天模式" },
  { id: "cream", icon: "sparkle", label: "奶油模式" },
];
function applyTheme(theme) {
  const i = THEMES.findIndex((x) => x.id === theme);
  const t = THEMES[i >= 0 ? i : 0];
  document.documentElement.setAttribute("data-theme", t.id);
  localStorage.setItem("wm-theme", t.id);
  const icon = $("themeIcon"); const label = $("themeBtn").querySelector("span:not(.snav-ico)");
  const next = THEMES[(THEMES.indexOf(t) + 1) % THEMES.length];
  if (icon) icon.innerHTML = ICO(t.icon);
  if (label) label.textContent = t.label;
  $("themeBtn").title = "切换到" + next.label;
}
(function initTheme() {
  const saved = localStorage.getItem("wm-theme");
  applyTheme(THEMES.some((x) => x.id === saved) ? saved : "dark");
  $("themeBtn").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") || "dark";
    const i = THEMES.findIndex((x) => x.id === cur);
    applyTheme(THEMES[(i + 1) % THEMES.length].id);
  });
})();

/* ===================== 设置：运行设备 + 实时预览 ===================== */
function getDevice() { return localStorage.getItem("wm-device") || "auto"; }
function saveDevice(v) { localStorage.setItem("wm-device", v); }
function getLivePreview() { return localStorage.getItem("wm-liveprev") !== "0"; }
function saveLivePreview(v) { localStorage.setItem("wm-liveprev", v ? "1" : "0"); }

/* 打开设置弹窗时，从后端拉取已检测显卡，填充「运行设备」下拉 */
async function refreshDeviceOptions() {
  const sel = $("deviceSel"); if (!sel) return;
  const prev = getDevice();
  sel.innerHTML = "";
  const auto = new Option("自动检测 GPU", "auto");
  sel.appendChild(auto);
  try {
    const r = await fetch("/api/enhance/gpus").then((x) => x.json());
    if (r && r.ok && r.gpus && r.gpus.length) {
      r.gpus.forEach((g) => {
        sel.appendChild(new Option(
          (g.name || g.display_name || "NVIDIA 显卡") + (g.ai_compatible ? "" : "（不可用）"),
          g.uuid || ""
        ));
      });
    } else {
      sel.appendChild(new Option("未检测到可用显卡", ""));
    }
  } catch (_) {
    sel.appendChild(new Option("未检测到可用显卡", ""));
  }
  const exists = Array.from(sel.options).some((o) => o.value === prev);
  sel.value = exists ? prev : "auto";
  if (!exists && prev !== "auto") saveDevice("auto");
}
function initSettingsUI() {
  const modal = $("settingsModal"); if (!modal || initSettingsUI._done) return;
  initSettingsUI._done = true;
  const open = () => {
    modal.hidden = false;
    requestAnimationFrame(() => modal.classList.add("open"));
    const toggle = $("livePrevToggle"); if (toggle) toggle.checked = getLivePreview();
    refreshDeviceOptions();
  };
  const close = () => {
    modal.classList.remove("open");
    setTimeout(() => { modal.hidden = true; }, 220);
  };
  $("settingsBtn").addEventListener("click", open);
  $("settingsClose").addEventListener("click", close);
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
  $("deviceSel").addEventListener("change", (e) => { if (e.target.value) saveDevice(e.target.value); });
  $("livePrevToggle").addEventListener("change", (e) => saveLivePreview(e.target.checked));
}

/* ===================== DLSS 实时预览：参数一变，立即渲染当前帧/图 ===================== */
let livePrevSeq = 0;       // 请求序号：只有最新一次预览的响应才生效
let livePrevTimer = null;  // 防抖定时器
let livePrevBusy = false;  // 是否有预览请求在途

/* 把 img/video 当前画面转成 JPEG Blob；超过 maxSide 才降采样（默认保持原尺寸，保证预览清晰） */
function previewToCanvas(source, maxSide) {
  return new Promise((resolve, reject) => {
    const isVid = source && source.tagName === "VIDEO";
    const w0 = isVid ? (source.videoWidth || 1) : (source.naturalWidth || 1);
    const h0 = isVid ? (source.videoHeight || 1) : (source.naturalHeight || 1);
    const k = Math.min(1, maxSide / Math.max(w0, h0));
    const w = Math.max(1, Math.round(w0 * k)), h = Math.max(1, Math.round(h0 * k));
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext("2d");
    const draw = () => {
      try {
        ctx.drawImage(source, 0, 0, w, h);
        canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("预览截图失败"))), "image/jpeg", 0.95);
      } catch (_) { reject(new Error("预览截图失败")); }
    };
    if (isVid) {
      if (source.readyState >= 2) draw();
      else source.addEventListener("loadeddata", draw, { once: true });
    } else {
      if (source.complete && source.naturalWidth) { draw(); return; }
      source.onload = draw;
      source.onerror = () => reject(new Error("预览截图失败"));
    }
  });
}

/* 把预览结果图应用到对应视图（图片槽 / 视频实时帧浮层） */
function applyPreviewUrl(view, url) {
  if (view === "image") {
    const enh = $("sEnh");
    if (!enh) return;
    if (sSingle.prevUrl && sSingle.prevUrl !== url) URL.revokeObjectURL(sSingle.prevUrl);
    sSingle.prevUrl = url;
    enh.src = url;
    // 预览输出与原图同尺寸（1×）：按原图当前的渲染尺寸显示，观感与正式结果一致
    const orig = $("sOrig");
    if (orig && orig.clientWidth) {
      enh.style.width = orig.clientWidth + "px";
      enh.style.height = orig.clientHeight + "px";
    }
    enh.hidden = false;
    sSingle.after = url; sSingle.cmpBuilt = false; sSingle.idx = 1;
    sSingle.makeCmp = () => makeCompare(sSingle.before, sSingle.after);
    if (sUI.modeBtn) sUI.modeBtn.disabled = false;
    enhMode(sUI, sSingle, 1);
  } else {
    const wrap = $("vsPrevWrap");
    if (!wrap) return;
    const pv = $("vsPrev");
    if (pv.dataset.prevUrl && pv.dataset.prevUrl !== url) URL.revokeObjectURL(pv.dataset.prevUrl);
    pv.dataset.prevUrl = url;
    pv.src = url;
    wrap.classList.remove("hidden");
  }
}

/* 走轻量 /api/enhance/preview 快速通道：直接返回渲染后的 JPEG 字节，无任务轮询 */
let livePrevBusyTimer = null;
/* 屏幕顶部固定提示条(无视父容器隐藏)，保证预览等待期一定有可见反馈 */
function _busyToast(show, txt, depth) {
  let t = $("#livePrevToast");
  if (!t) {
    t = document.createElement("div");
    t.id = "livePrevToast";
    document.body.appendChild(t);
  }
  if (show) {
    t.style.display = "flex";
    t.innerHTML = '<span class="spinner"></span><span>' + (txt || (depth > 0 ? "GPU 忙碌中，正在等待渲染… 0s" : "正在生成预览… 0s")) + "</span>";
  } else {
    t.style.display = "none";
  }
}
/* 图像预览显示在 sStage，视频预览显示在 vsPrevWrap：转圈跟着当前视图放 */
function _busyEl(view) { return view === "image" ? $("sPrevBusy") : $("vsPrevBusy"); }
function _busyTxt(view) { return view === "image" ? $("sPrevBusyTxt") : $("vsPrevBusyTxt"); }
function showLivePrevBusy(view, depth) {
  clearInterval(livePrevBusyTimer);
  if (view !== "image") {   // 视频：确保预览舞台可见（图像舞台由已加载的图片保证可见）
    const wrap = $("vsPrevWrap");
    if (wrap && wrap.classList.contains("hidden")) wrap.classList.remove("hidden");
  }
  const el = _busyEl(view);
  if (el) el.classList.remove("hidden");
  const txt = _busyTxt(view);
  _busyToast(true, null, depth);
  const t0 = Date.now();
  livePrevBusyTimer = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    const label = depth > 0 ? `GPU 忙碌中，正在等待空闲… ${s}s` : `正在生成预览… ${s}s`;
    if (txt) txt.textContent = label;
    const t = $("#livePrevToast"); if (t) t.lastChild.textContent = label;
  }, 500);
}
function hideLivePrevBusy(view) {
  clearInterval(livePrevBusyTimer); livePrevBusyTimer = null;
  const el = _busyEl(view); if (el) el.classList.add("hidden");
  _busyToast(false);
}
async function runLivePreview(view, blob, params, depth) {
  depth = depth || 0;
  const seq = ++livePrevSeq;
  showLivePrevBusy(view, depth);   // 请求在途：给用户明确进度反馈，不再静默等待
  const fd = new FormData();
  fd.append("file", blob, "preview.jpg");
  // 预览沿用用户当前放大倍率等参数，保证与「开始增强」的结果一致、不偏糊；只需把输出固化成本地 JPEG 加速传输
  const merged = Object.assign({}, params, {
    output_format: "JPEG",
    quality: "100",
    ai_gpu_uuid: getDevice(),
  });
  Object.entries(merged).forEach(([k, v]) => fd.append(k, v));
  let resp;
  try {
    resp = await fetch("/api/enhance/preview", { method: "POST", body: fd });
  } catch (_) {
    if (seq === livePrevSeq) hideLivePrevBusy(view);
    return;   // 网络异常：静默忽略（预览是辅助功能，不打扰主流程）
  }
  if (seq !== livePrevSeq) return;   // 已被更新请求取代：计时交给新请求，这里不再收尾
  if (!resp.ok) {
    if (resp.status === 429) {
      // 上一次 DLSS 渲染还没结束：稍后自动用最新参数重试（最多 3 次）
      if (depth < 3) setTimeout(() => {
        if (seq === livePrevSeq) runLivePreview(view, blob, params, depth + 1);
      }, 1500);
      else hideLivePrevBusy(view);
    }
    // 其它失败（含 "Another GPU render is already running" 等并发冲突）：一律静默跳过，
    // 等待下一次参数变更再触发，绝不弹错打断用户。
    else hideLivePrevBusy(view);
    return;
  }
  const outBlob = await resp.blob();
  if (seq !== livePrevSeq) return;
  hideLivePrevBusy(view);
  applyPreviewUrl(view, URL.createObjectURL(outBlob));
}

async function fireLivePreview(view) {
  if (livePrevBusy) return;
  // 队列（真实 DLSS 增强/插帧任务）正在跑时，GPU 槽位被占用，预览会撞并发锁而失败——此时跳过预览
  if (qRunning) return;
  let blob;
  try {
    if (view === "image") {
      const img = $("sOrig");
      if (!img || !img.src) return;
      blob = await previewToCanvas(img, 1920);   // 原尺寸内不缩放，避免预览发糊
    } else {
      const vid = $("vsOrig");
      if (!vid || !vid.src || !vid.duration) return;
      blob = await previewToCanvas(vid, 1920);   // 视频当前帧同样保持原分辨率
    }
  } catch (_) { return; }
  livePrevBusy = true;
  try {
    await runLivePreview(view, blob, view === "image" ? dlssImageFields() : dlssVideoFields());
  } catch (_) {
    // 实时预览失败静默忽略，不影响主流程
  } finally {
    livePrevBusy = false;
  }
}

function scheduleLivePreview(view) {
  if (!getLivePreview()) return;
  if (qRunning) return;   // 真实增强队列运行中：不再调度预览，避免抢占 GPU
  clearTimeout(livePrevTimer);
  livePrevTimer = setTimeout(() => fireLivePreview(view), 300);
}

/* 真实增强任务启动前调用：取消待触发的预览调度，并等待在途预览彻底结束，
   确保下一个真实任务启动时 GPU 槽位空闲，不会撞 "Another GPU render" 失败 */
function waitForPreviewIdle(timeout) {
  clearTimeout(livePrevTimer);
  const limit = timeout || 10000;
  return new Promise((res) => {
    const t0 = Date.now();
    (function ck() {
      if (!livePrevBusy || Date.now() - t0 > limit) return res();
      setTimeout(ck, 150);
    })();
  });
}

/* 给图片/视频单个处理的所有参数控件挂上「变更即预览」 */
function bindLivePreview() {
  const IMG_CHANGE = ["imNrPreset","imNrStyle","imUp","imMask","imModel","imFormat","imQuality","imRename","imSuffix"];
  const IMG_INPUT  = ["imNrInt","imLtone","imLstr","imSkin"];
  const VID_CHANGE = ["vdNrPreset","vdNrStyle","vdUp","vdMask","vdModel","vdCodec","vdContainer","vdQuality","vdHdr","vdRename","vdSuffix"];
  const VID_INPUT  = ["vdNrInt","vdLtone","vdLstr","vdSkin"];
  IMG_CHANGE.concat(IMG_INPUT).forEach((id) => {
    const el = $(id); if (el) el.addEventListener("change", () => scheduleLivePreview("image"));
  });
  IMG_INPUT.forEach((id) => {
    const el = $(id); if (el) el.addEventListener("input", () => scheduleLivePreview("image"));
  });
  VID_CHANGE.concat(VID_INPUT).forEach((id) => {
    const el = $(id); if (el) el.addEventListener("change", () => scheduleLivePreview("video"));
  });
  VID_INPUT.forEach((id) => {
    const el = $(id); if (el) el.addEventListener("input", () => scheduleLivePreview("video"));
  });
  // 更换视频文件后，隐藏并清理上一次的实时预览帧
  const vsInp = $("vsFiles");
  if (vsInp) vsInp.addEventListener("change", () => {
    const w = $("vsPrevWrap"); if (w) w.classList.add("hidden");
    const pv = $("vsPrev");
    if (pv && pv.dataset.prevUrl) { URL.revokeObjectURL(pv.dataset.prevUrl); delete pv.dataset.prevUrl; }
  });
  // 用户点「切换显示」（原视频/增强后/前后对比）时收起实时预览浮层，避免挡住想看的画面
  const vsMode = $("vsModeBtn");
  if (vsMode) vsMode.addEventListener("click", () => { const w = $("vsPrevWrap"); if (w) w.classList.add("hidden"); });
}

/* ===================== 引擎 ===================== */
async function checkEngines() {
  try {
    enginesInfo = await (await fetch("/api/engines")).json();
    const badge = $("engineBadge");
    const parts = [];
    if (enginesInfo.ai_available) parts.push("LaMa");
    if (enginesInfo.mat_available) parts.push("MAT");
    if (enginesInfo.propainter_available) parts.push("ProPainter");
    if (enginesInfo.nvenc && parts.length) badge.className = "badge ok";
    if (parts.length) { badge.textContent = `AI 就绪（${parts.join(" + ")}）`; badge.className = "badge ok"; }
    else if (enginesInfo.nvenc) { badge.textContent = "GPU 硬件加速就绪 · 未装 AI"; badge.className = "badge warn"; }
    else { badge.textContent = "未装 AI 引擎 · 可用快速模式"; badge.className = "badge warn"; }
  } catch (_) { $("engineBadge").textContent = "引擎检测失败"; badge("warn"); }
  syncAllEngineSelects();
}
function badge(c) { $("engineBadge").className = "badge " + c; }

function optionEnabled(key) {
  if (!enginesInfo) return true;      // 未知引擎时先全部可用
  if (key === "ai") return !!enginesInfo.ai_available;
  if (key === "mat") return !!enginesInfo.mat_available;
  if (key === "propainter") return !!enginesInfo.propainter_available;
  return true; // auto / opencv 恒可用
}
const ENGINE_OPTIONS = [
  { v: "auto", label: "自动（AI 优先）" },
  { v: "ai", label: "AI · LaMa" },
  { v: "mat", label: "AI · MAT（人像更佳）" },
  { v: "propainter", label: "ProPainter（人物保持佳）" },
  { v: "opencv", label: "快速 · OpenCV" },
];
function syncEngineSelect(sel, mode) {
  // 不适用或未安装的引擎：直接移除该项，而不是置灰。
  //  视频 → 去掉 MAT；图片 → 去掉 ProPainter；模型未装 → 同样去掉。
  const prev = sel.value;
  sel.innerHTML = "";
  ENGINE_OPTIONS.forEach((opt) => {
    const take = (opt.v === "mat" && mode === "video")
      || (opt.v === "propainter" && mode === "image")
      || !optionEnabled(opt.v);
    if (take) return;
    const o = document.createElement("option");
    o.value = opt.v; o.textContent = opt.label;
    sel.appendChild(o);
  });
  if (sel.querySelector(`option[value="${prev}"]`)) sel.value = prev;
  else if (sel.querySelector('option[value="ai"]')) sel.value = "ai";
  else sel.value = sel.querySelector("option")?.value || "auto";
}
function syncAllEngineSelects() {
  const m = state.view === "video" ? "video" : "image";
  syncEngineSelect($("engineSel"), m);
  syncEngineSelect($("batchEngineSel"), m);
}

/* ===================== 媒体模式 UI ===================== */
function modeUI(mode) {
  const isVideo = mode === "video";
  $("uploadLabel").textContent = "第一步 · 上传" + (isVideo ? "本地视频" : "图片");
  $("dzIcon").innerHTML = isVideo ? ICO("film") : ICO("image");
  $("dzTitle").textContent = "点击或拖拽" + (isVideo ? "视频" : "图片") + "到此处";
  $("dzSub").textContent = isVideo ? "支持 mp4 / mov / webm / m4v 等格式" : "支持 jpg / png / webp / bmp 等格式";
  $("fileInput").accept = isVideo ? "video/*" : "image/*";
  $("qualityBox").classList.toggle("hidden", !isVideo);
  $("timeline").classList.toggle("hidden", !isVideo);
  $("segBar").classList.toggle("hidden", !isVideo);
  $("batchInput").accept = isVideo ? "video/*" : "image/*";
  $("batchSub").textContent = "支持多选，或拖拽" + (isVideo ? "视频" : "图片") + "到此处";
  $("batchQualityBox").classList.toggle("hidden", !isVideo);
  syncAllEngineSelects();
}
function updateModeUI() {
  if (state.view === "image") modeUI("image");
  else if (state.view === "video") modeUI("video");
}

/* ===================== 上传（单个） ===================== */
function bindDropzone(dzId, inputId, onFiles) {
  const dz = $(dzId), input = $(inputId);
  dz.addEventListener("click", (e) => { if (e.target.closest("a,button")) return; input.click(); });
  ["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) onFiles(e.dataTransfer.files); });
  input.addEventListener("change", () => { if (input.files.length) onFiles(input.files); });
}
bindDropzone("dropzone", "fileInput", (files) => handleFile(files[0]));

async function handleFile(file) {
  const isVideo = VID_EXT.test(file.name);
  const ov = $("mediaLoading");
  if (ov) {
    $("mediaLoadingTxt").textContent = isVideo
      ? "正在上传并检测水印…（视频较大，请稍候）"
      : "正在上传…";
    ov.classList.remove("hidden");
  }
  const fd = new FormData(); fd.append("file", file);
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || "上传失败");
    await onStaged(await r.json());
    $("mediaNameEl").textContent = file.name;
    notifyOk("文件上传成功，请框选水印");
  } catch (e) { notifyErr(e.message); }
  finally { if (ov) ov.classList.add("hidden"); }
}

/* ===================== 载入阶段 ===================== */
async function onStaged(data) {
  state.stagingId = data.staging_id;
  const isVideo = !!data.video_url;
  state.kind = isVideo ? "video" : "image";
  let boxes = cloneBoxes(data.boxes || []);
  if (!boxes.length) boxes = [{ id: uid(), x: 0.72, y: 0.76, w: 0.24, h: 0.16 }];
  state.boxes = boxes;
  state.autoBoxes = cloneBoxes(data.boxes || []);
  state.activeBoxId = boxes[0]?.id;

  // 用服务端返回的真实宽高锁定预览比例，杜绝"加载后突然变大一圈"
  const w = data.width || (isVideo ? 16 : 4);
  const h = data.height || (isVideo ? 9 : 3);
  $("previewWrap").style.setProperty("--ar", `${w} / ${h}`);  // 锁定比例并按屏高自动缩放

  const v = $("srcVideo"), img = $("srcImg");
  v.classList.toggle("hidden", !isVideo);
  img.classList.toggle("hidden", isVideo);
  if (isVideo) {
    v.src = data.video_url;
    state._mediaVideoSrc = data.video_url;
    state._mediaVideoPoster = data.preview || null;
    if (data.preview) v.poster = data.preview;
  } else {
    img.src = data.image_url;
    state._mediaImgSrc = data.image_url;
  }
  renderBoxes($("boxesLayer"));
  setStep(2);
  $("stageCard").classList.remove("hidden");
  $("dropzone").classList.add("hidden");   // 拖拽与导入共用同一媒体框：载入后隐藏上传区
  resetTimeline();
  $("stageCard").scrollIntoView({ behavior: "smooth" });
}

/* 载入后仍可在左侧预览上拖入新文件直接替换 */
(() => {
  const sc = $("stageCard");
  ["dragover", "dragenter"].forEach((ev) => sc.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); sc.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => sc.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); sc.classList.remove("drag"); }));
  sc.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]); });
})();

/* 更换文件按钮：直接唤起文件选择 */
$("swapBtn").addEventListener("click", () => $("fileInput").click());
/* 清空当前单个编辑器预览，回到上传状态（用于切换图片/视频类型时清掉残留媒体） */
function resetSingleEditor() {
  state.stagingId = null; state.kind = null;
  state.boxes = []; state.activeBoxId = null;
  const v = $("srcVideo"), img = $("srcImg");
  v.pause(); v.removeAttribute("src"); v.load();
  img.removeAttribute("src");
  v.classList.add("hidden"); img.classList.add("hidden");
  $("boxesLayer").innerHTML = "";
  $("boxTags").innerHTML = "";
  $("boxList").classList.add("hidden");
  $("stageCard").classList.add("hidden");
  $("dropzone").classList.remove("hidden");
  resetTimeline();
}

/* ===================== 视频时间轴（自定义，杜绝原生进度条遮挡水印框） ===================== */
const srcVideo = $("srcVideo"), seekBar = $("seekBar");
let seeking = false;
function resetTimeline() { seekBar.value = 0; $("curTime").textContent = "0:00"; $("durTime").textContent = "0:00"; $("playBtn").innerHTML = ICO("play"); }
srcVideo.addEventListener("loadedmetadata", () => { seekBar.max = srcVideo.duration || 100; $("durTime").textContent = fmtTime(srcVideo.duration); initSegmentBar(srcVideo.duration); });
srcVideo.addEventListener("timeupdate", () => { if (!seeking) seekBar.value = srcVideo.currentTime; $("curTime").textContent = fmtTime(srcVideo.currentTime); });
srcVideo.addEventListener("play", () => { $("playBtn").innerHTML = ICO("pause"); });
srcVideo.addEventListener("pause", () => { $("playBtn").innerHTML = ICO("play"); });
$("playBtn").addEventListener("click", () => { if (srcVideo.paused) srcVideo.play(); else srcVideo.pause(); });
/* 视频全屏查看 */
const fsBtn = $("fsBtn");
fsBtn.addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else $("previewWrap").requestFullscreen({ navigationUI: "hide" }).catch(() => notifyErr("当前浏览器不支持或拒绝全屏"));
});
/* ===================== 片段选择：只对选中的起止时间片段做去水印 ===================== */
const segBar = $("segBar"), segToggle = $("segToggle"), segStart = $("segStart"), segEnd = $("segEnd");
const segStartCap = $("segStartCap"), segEndCap = $("segEndCap"), segFill = $("segFill");
function syncSegment() {
  const max = segEnd.max ? Number(segEnd.max) : 1;
  let st = Number(segStart.value), en = Number(segEnd.value);
  if (en - st < 0.2) en = Math.min(st + 0.2, max);
  if (st > en - 0.2) st = Math.max(0, en - 0.2);
  segStart.value = st; segEnd.value = en;
  segStartCap.textContent = fmtTime(st); segEndCap.textContent = fmtTime(en);
  if (segFill) { const p = max > 0 ? ((en - st) / max) * 100 : 0; const l = max > 0 ? (st / max) * 100 : 0; segFill.style.left = l + "%"; segFill.style.width = p + "%"; }
}
function initSegmentBar(dur) {
  const d = (dur > 0 ? dur : 1);
  segStart.max = d; segEnd.max = d;
  segStart.value = 0; segEnd.value = d;
  syncSegment();
}
segToggle.addEventListener("change", () => { const on = segToggle.checked; segStart.disabled = !on; segEnd.disabled = !on; });
segStart.addEventListener("input", syncSegment);
segEnd.addEventListener("input", syncSegment);
segReset.addEventListener("click", () => {
  segToggle.checked = false; segStart.disabled = true; segEnd.disabled = true;
  segStart.value = 0; if (segEnd.max > 0) segEnd.value = segEnd.max; syncSegment();
});
document.addEventListener("fullscreenchange", () => { fsBtn.innerHTML = document.fullscreenElement ? ICO("minimize") : ICO("maximize"); });
document.addEventListener("fullscreenerror", () => notifyErr("全屏失败"));
/* 空格键播放/暂停（全屏时最常用，普通视图同样生效）；输入框内按键不拦截 */
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space") return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
  if (srcVideo.currentSrc) { e.preventDefault(); if (srcVideo.paused) srcVideo.play(); else srcVideo.pause(); }
});
seekBar.addEventListener("pointerdown", () => { seeking = true; });
["pointerup", "pointercancel"].forEach((ev) => seekBar.addEventListener(ev, () => { seeking = false; }));
seekBar.addEventListener("input", () => { srcVideo.currentTime = Number(seekBar.value); $("curTime").textContent = fmtTime(seekBar.value); });

/* ===================== 多水印框：调色板 + 独立管理列表 ===================== */
const BOX_COLORS = ["#34d399", "#60a5fa", "#a78bfa", "#fbbf24", "#fb7185", "#22d3ee", "#f472b6", "#4ade80"];
function ensureBoxProps(arr) {
  (arr || []).forEach((b, i) => {
    if (!b.color) b.color = BOX_COLORS[i % BOX_COLORS.length];
    if (typeof b.label !== "string") b.label = "";
  });
}
function spawnBoxEl(box, idx) {
  const el = document.createElement("div");
  el.className = "wm-box" + (box.id === state.activeBoxId ? " active" : "");
  el.dataset.id = box.id;
  const label = document.createElement("div");
  label.className = "wm-label"; label.textContent = box.label || `水印 ${idx + 1}`;
  el.appendChild(label);
  setupBoxInteractions(el, box);
  applyBoxToEl(box, el);
  return el;
}
function renderBoxes(layer) {
  if (!layer) return;
  state.layer = layer;
  ensureBoxProps(state.boxes);
  layer.innerHTML = "";
  state.boxes.forEach((box, idx) => layer.appendChild(spawnBoxEl(box, idx)));
  renderBoxList();
}
function applyBoxToEl(box, el) {
  el.style.setProperty("--box-color", box.color || "#34d399");
  el.style.left = (box.x * 100) + "%";
  el.style.top = (box.y * 100) + "%";
  el.style.width = (box.w * 100) + "%";
  el.style.height = (box.h * 100) + "%";
}
function selectBox(id) {
  state.activeBoxId = id;
  if (state.layer) state.layer.querySelectorAll(".wm-box").forEach((el) => el.classList.toggle("active", el.dataset.id === id));
  renderBoxList();
}
function boxSourceArr() {
  // 批量模式下删除当前素材的框；否则操作单编辑器框。
  if (state.boxMode === "batch") {
    const it = currentBatch()[state.activeBatchIndex];
    return it && Array.isArray(it.boxes) ? it.boxes : null;
  }
  return state.boxes;
}
function removeBoxById(id) {
  const arr = boxSourceArr();
  if (!arr) return;
  const i = arr.findIndex((b) => b.id === id);
  if (i < 0) return;
  arr.splice(i, 1);
  if (state.activeBoxId === id) state.activeBoxId = arr[0]?.id || null;
  if (state.boxMode === "batch") { renderBatchBoxes(); renderBatchFiles(); }
  else if (state.layer) renderBoxes(state.layer);
}
// 按当前模式重绘水印框预览（单个或批量当前素材）
function rerenderBoxes() {
  if (state.boxMode === "batch") { renderBatchBoxes(); renderBatchFiles(); }
  else if (state.layer) renderBoxes(state.layer);
}
/* 每个水印框一个条目：改颜色 / 重命名 / 删除。
   单个 → 右侧面板的列表；批量 → 预览卡下方的队列（同一套逻辑） */
function renderBoxList() {
  const isBatch = state.boxMode === "batch";
  const host = $(isBatch ? "batchBoxList" : "boxTags");
  if (!host) return;
  const arr = boxSourceArr();
  if (!arr) return;
  const wrap = $(isBatch ? "batchBoxWrap" : "boxList");
  if (wrap) wrap.classList.toggle("hidden", arr.length === 0);
  ensureBoxProps(arr);
  host.innerHTML = "";
  arr.forEach((box, idx) => {
    const chip = document.createElement("div");
    chip.className = "chip" + (box.id === state.activeBoxId ? " chip-active" : "");
    chip.dataset.id = box.id;
    chip.style.setProperty("--box-color", box.color || "#34d399");
    chip.addEventListener("click", () => selectBox(box.id));

    const sw = document.createElement("label");
    sw.className = "chip-swatch"; sw.title = "更改颜色";
    const picker = document.createElement("input");
    picker.type = "color"; picker.value = box.color || "#34d399";
    picker.addEventListener("input", (e) => { box.color = e.target.value; rerenderBoxes(); });
    sw.appendChild(picker);

    const name = document.createElement("input");
    name.type = "text"; name.className = "chip-name"; name.placeholder = `水印 ${idx + 1}`;
    name.value = box.label || "";
    name.title = "重命名";
    name.addEventListener("click", (e) => e.stopPropagation());
    name.addEventListener("change", () => { box.label = name.value.trim(); rerenderBoxes(); });
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") name.blur(); });

    const del = document.createElement("button");
    del.className = "chip-del"; del.title = "删除此水印框"; del.innerHTML = ICO("trash");
    del.addEventListener("click", (e) => { e.stopPropagation(); removeBoxById(box.id); });

    chip.append(sw, name, del);
    host.appendChild(chip);
  });
}
function setupBoxInteractions(el, box) {
  let mode = null, sx = 0, sy = 0, orig = null;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const MIN = 0.02;

  const tolFor = (r) => Math.min(12, Math.max(5, Math.min(r.width, r.height) * 0.22));
  const probe = (e, r) => {
    const tol = tolFor(r);
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const nL = x <= tol, nR = x >= r.width - tol, nT = y <= tol, nB = y >= r.height - tol;
    const mk = (nL && nT) ? "nw" : (nL && nB) ? "sw" : (nR && nT) ? "ne" : (nR && nB) ? "se"
      : nL ? "w" : nR ? "e" : nT ? "n" : nB ? "s" : "move";
    const cursor = mk === "move" ? "move"
      : (mk === "nw" || mk === "se") ? "nwse-resize"
      : (mk === "ne" || mk === "sw") ? "nesw-resize"
      : (mk === "n" || mk === "s") ? "ns-resize" : "ew-resize";
    return { mk, cursor };
  };

  el.addEventListener("pointerdown", (e) => {
    selectBox(box.id);
    mode = probe(e, el.getBoundingClientRect()).mk;
    sx = e.clientX; sy = e.clientY;
    orig = { ...box };
    el.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  // 悬停时根据所处位置切换鼠标样式（内=移动，边缘=缩放）
  el.addEventListener("mousemove", (e) => { el.style.cursor = probe(e, el.getBoundingClientRect()).cursor; });
  el.addEventListener("pointermove", (e) => {
    if (!mode || !state.layer) return;
    const r = state.layer.getBoundingClientRect();
    const dx = (e.clientX - sx) / r.width, dy = (e.clientY - sy) / r.height;
    const right = orig.x + orig.w, bottom = orig.y + orig.h;
    const b = { ...orig };
    if (mode === "move") { b.x = clamp(orig.x + dx, 0, 1 - orig.w); b.y = clamp(orig.y + dy, 0, 1 - orig.h); }
    else if (mode === "e")  { b.w = clamp(orig.w + dx, MIN, 1 - orig.x); }
    else if (mode === "w")  { const nx = clamp(orig.x + dx, 0, right - MIN); b.x = nx; b.w = right - nx; }
    else if (mode === "s")  { b.h = clamp(orig.h + dy, MIN, 1 - orig.y); }
    else if (mode === "n")  { const ny = clamp(orig.y + dy, 0, bottom - MIN); b.y = ny; b.h = bottom - ny; }
    else if (mode === "se") { b.w = clamp(orig.w + dx, MIN, 1 - orig.x); b.h = clamp(orig.h + dy, MIN, 1 - orig.y); }
    else if (mode === "ne") { b.w = clamp(orig.w + dx, MIN, 1 - orig.x); const ny = clamp(orig.y + dy, 0, bottom - MIN); b.y = ny; b.h = bottom - ny; }
    else if (mode === "sw") { const nx = clamp(orig.x + dx, 0, right - MIN); b.x = nx; b.w = right - nx; b.h = clamp(orig.h + dy, MIN, 1 - orig.y); }
    else if (mode === "nw") { const nx = clamp(orig.x + dx, 0, right - MIN); b.x = nx; b.w = right - nx; const ny = clamp(orig.y + dy, 0, bottom - MIN); b.y = ny; b.h = bottom - ny; }
    Object.assign(box, b);
    applyBoxToEl(box, el);
  });
  const end = (e) => { mode = null; try { el.releasePointerCapture(e.pointerId); } catch (_) {} };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}

function addBox() {
  const b = { id: uid(), x: 0.38, y: 0.38, w: 0.22, h: 0.16 };
  state.boxes.push(b); state.activeBoxId = b.id;
  if (state.layer) renderBoxes(state.layer);
  notify(`已添加第 ${state.boxes.length} 个水印框`);
}
async function autoDetect() {
  if (!state.stagingId) return notifyErr("请先载入文件，再自动检测");
  setBusy($("autoBtn"), true, "检测中…");
  try {
    const body = { staging_id: state.stagingId };
    if (state.view === "video") body.time = $("srcVideo").currentTime || 0;  // 检测当前播放帧
    const r = await fetch("/api/detect", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok) {
      const detail = (await r.json().catch(() => null))?.detail || "";
      throw new Error(detail === "Not Found"
        ? "后端接口未生效：请完全停止并重新运行 python start.py（服务器没有热更新，需重启才能加载新接口）"
        : (detail || "检测失败"));
    }
    let boxes = cloneBoxes((await r.json()).boxes || []);
    if (!boxes.length) { notify("未检测到明显水印，已给出建议框，可手动调整"); boxes = [{ id: uid(), x: 0.72, y: 0.78, w: 0.25, h: 0.17 }]; }
    state.boxes = boxes;
    state.activeBoxId = boxes[0]?.id;
    if (state.layer) renderBoxes(state.layer);
    notifyOk(`自动检测到 ${boxes.length} 处水印（可拖动调整）`);
  } catch (e) {
    notifyErr("自动检测请求失败：" + (e.message || e) + "\n已给出右下角建议框，可手动微调");
    state.boxes = [{ id: uid(), x: 0.7, y: 0.78, w: 0.25, h: 0.17 }];
    state.activeBoxId = state.boxes[0]?.id;
    if (state.layer) renderBoxes(state.layer);
  } finally {
    setBusy($("autoBtn"), false, ICO("sparkle") + " 自动检测");
  }
}
$("addBoxBtn").addEventListener("click", addBox);
$("autoBtn").addEventListener("click", autoDetect);
$("clearBtn").addEventListener("click", () => {
  if (!state.boxes.length) return;
  state.boxes = []; state.activeBoxId = null;
  if (state.layer) renderBoxes(state.layer);
  notify("已清空全部水印框");
});

/* ===================== 模板：保存 / 应用 / 列表 / 删除 ===================== */
$("saveTplBtn").addEventListener("click", async () => {
  const name = $("tplName").value.trim();
  if (!name) return notifyErr("请先填写模板名称");
  if (!state.boxes.length) return notifyErr("请先框选至少一个水印");
  const payload = { name, boxes: state.boxes.map(stripId), engine: $("engineSel").value, quality: "balanced", device: "cuda" };
  try {
    const r = await fetch("/api/templates", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!r.ok) throw new Error((await r.json()).detail || "保存失败");
    const msg = $("saveTplMsg");
    msg.innerHTML = ICO("check") + ` 已保存模板「${escapeHtml(name)}」`; msg.className = "tpl-save-msg ok";
    $("tplName").value = "";
    setTimeout(() => { msg.textContent = ""; msg.className = "tpl-save-msg"; }, 2500);
    notifyOk("模板已保存");
    await refreshTemplates();
  } catch (e) { const m = $("saveTplMsg"); m.innerHTML = ICO("x") + " " + escapeHtml(e.message); m.className = "tpl-save-msg err"; }
});

async function fetchTemplates() {
  const j = await (await fetch("/api/templates")).json();
  return j.templates || [];
}
function refreshTplSelects(tpls) {
  const single = $("tplSelSingle");
  single.innerHTML = '<option value="">选择模板…</option>';
  const batch = $("batchTplSel");
  batch.innerHTML = '<option value="">（不选模板 · 用上方已框水印）</option>';
  const itemSel = $("batchItemTplSel");
  if (itemSel) itemSel.innerHTML = '<option value="">跟随全局模板</option>';
  tpls.forEach((t) => {
    const o = document.createElement("option");
    o.value = t.name; o.textContent = `${t.name}（${(t.boxes || []).length} 处遮罩）`;
    single.appendChild(o.cloneNode(true));
    batch.appendChild(o.cloneNode(true));
    if (itemSel) itemSel.appendChild(o.cloneNode(true));
  });
}
function drawMaskPreview(canvas, boxes) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#111827"; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = "rgba(255,255,255,0.04)";
  for (let i = 0; i < 40; i++) { const x = Math.random() * W, y = Math.random() * H; ctx.beginPath(); ctx.arc(x, y, 1, 0, 7); ctx.fill(); }
  ctx.font = "12px sans-serif"; ctx.fillStyle = "#6b7694"; ctx.textAlign = "center";
  ctx.fillText("遮罩预览（16:9）", W / 2, H / 2);
  (boxes || []).forEach((b, i) => {
    const px = b.x * W, py = b.y * H, pw = Math.max(2, b.w * W), ph = Math.max(2, b.h * H);
    ctx.fillStyle = "rgba(99,102,241,0.38)";
    ctx.strokeStyle = "#8b5cf6"; ctx.lineWidth = 2;
    ctx.fillRect(px, py, pw, ph);
    ctx.strokeRect(px, py, pw, ph);
    const la = (i % 4);
    ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(px + pw / 2, py + ph / 2, 3, 0, 7); ctx.fill();
    ctx.fillStyle = "#c4b5fd"; ctx.font = "bold 11px sans-serif"; ctx.textAlign = "left";
    ctx.fillText(String(i + 1), px + 4, py + 12);
    ctx.fillStyle = `hsl(${230 + la * 30} 70% 55%)`;
    ctx.globalAlpha = 0.5; ctx.fillRect(px + pw - 10, py, 10, 10); ctx.globalAlpha = 1;
  });
}
async function refreshTemplates() {
  try {
    const tpls = await fetchTemplates();
    refreshTplSelects(tpls);
    const list = $("tplList");
    $("tplEmpty").classList.toggle("hidden", tpls.length > 0);
    list.innerHTML = "";
    tpls.forEach((t) => {
      const row = document.createElement("div");
      row.className = "tpl-item";
      const can = document.createElement("canvas");
      can.className = "tpl-canvas";
      can.width = 320; can.height = 180;
      const info = document.createElement("div"); info.className = "tpl-meta";
      (t.boxes || []).forEach((_, i) => info.appendChild(spanTag("遮罩 " + (i + 1))));
      if (!t.boxes || !t.boxes.length) info.appendChild(spanTag("无遮罩"));
      info.appendChild(spanTag(fmtEngine(t.engine)));
      const nameEl = document.createElement("div");
      nameEl.className = "tpl-name";
      nameEl.innerHTML = ICO("pin") + ` <span class="tpl-name-text">${escapeHtml(t.name)}</span>`;
      row.appendChild(nameEl);
      const body = document.createElement("div");
      body.style.cssText = "display:flex;flex-direction:column;gap:10px";
      const actRow = document.createElement("div"); actRow.className = "tpl-actions";
      const renameB = document.createElement("button"); renameB.className = "btn small"; renameB.innerHTML = ICO("edit") + " 重命名";
      const delB = document.createElement("button"); delB.className = "btn small danger"; delB.innerHTML = ICO("trash") + " 删除";
      actRow.appendChild(renameB); actRow.appendChild(delB);
      body.append(can, info, actRow);
      row.appendChild(body);
      renameB.addEventListener("click", () => { inlineRenameTemplate(t, row, nameEl); });
      delB.addEventListener("click", () => deleteTemplate(t.name));
      list.appendChild(row);
      drawMaskPreview(can, t.boxes);
    });
  } catch (_) { /* 模板接口不可用则忽略 */ }
}
function spanTag(txt) { const s = document.createElement("span"); s.className = "meta-chip"; s.textContent = txt; return s; }
const fmtEngine = (e) => ({ ai: "AI", mat: "MAT", propainter: "ProPainter", opencv: "快速", auto: "自动" }[e] || "自动");

/* 单个选择模板：
   ① 下拉变更 → 更新右侧「N 处遮罩」状态信息 + 绘制遮罩预览（可视化 mock boxes）
   ② 点击「应用」按钮 → 才真正把遮罩应用到水印框列表 / 预览画面 */
function setTplSelInfo(tpl) {
  const info = $("tplSelInfo");
  const frame = $("tplPreviewFrame");
  const wrap = $("tplPreviewWrap");
  if (!tpl || !tpl.name) {
    info.textContent = "未选择";
    wrap.hidden = true;
    frame.innerHTML = "";
    return;
  }
  const cnt = (tpl.boxes || []).length;
  info.textContent = `${cnt} 处遮罩 · ${fmtEngine(tpl.engine)}`;
  wrap.hidden = false;
  frame.innerHTML = "";
  (tpl.boxes || []).forEach((b, i) => {
    const el = document.createElement("div");
    el.className = "wm-box-mock";
    el.style.left = (b.x * 100) + "%";
    el.style.top  = (b.y * 100) + "%";
    el.style.width = (b.w * 100) + "%";
    el.style.height = (b.h * 100) + "%";
    el.textContent = String(i + 1);
    frame.appendChild(el);
  });
}
$("tplSelSingle").addEventListener("change", async () => {
  const name = $("tplSelSingle").value;
  try {
    const t = name ? (await fetchTemplates()).find((x) => x.name === name) : null;
    setTplSelInfo(t);
  } catch (e) { setTplSelInfo(null); notifyErr("加载模板失败：" + e.message); }
});
$("applyTplBtn").addEventListener("click", async () => {
  const name = $("tplSelSingle").value;
  if (!name) return notifyErr("请先在上方选择一个模板");
  try {
    const t = (await fetchTemplates()).find((x) => x.name === name);
    if (!t) return notifyErr("模板「" + name + "」不存在或已被删除");
    applyTemplate(t, true);
  } catch (e) { notifyErr("应用模板失败：" + e.message); }
});

function applyTemplate(t, showNotice) {
  const boxes = cloneBoxes(t.boxes || []);
  if (!boxes.length) { notifyErr("该模板没有遮罩"); return; }
  state.boxes = boxes; state.activeBoxId = boxes[0]?.id;
  // 无论是否已载入文件都要刷新「水印框」管理列表，让用户立刻看到已套用的遮罩；
  // 已载入时再同步渲染到画面。
  renderBoxList();
  if (state.layer) renderBoxes(state.layer);
  const mode = state.view === "video" ? "video" : "image";
  const ve = t.engine === "propainter" && mode === "video" ? "propainter" : (t.engine === "mat" && mode === "image" ? "mat" : (t.engine || "auto"));
  const engineSel = $("engineSel");
  if (engineSel.querySelector(`option[value="${t.engine}"]`) && !engineSel.querySelector(`option[value="${t.engine}"]`).disabled) {
    engineSel.value = t.engine;
  } else if (t.engine === "mat" || (t.engine && !engineSel.querySelector(`option[value="${t.engine}"]`))) {
    engineSel.value = (mode === "image" && optionEnabled("mat")) ? "mat" : (optionEnabled("ai") ? "ai" : "auto");
  }
  if (showNotice) {
    notifyOk(`已应用模板「${t.name}」：` + t.boxes.length + " 处遮罩");
    if (!state.stagingId) { notify("提示：请在「单个处理」中载入文件后开始处理"); gotoView(state.view); gotoSub("single"); }
    else $("stageCard").scrollIntoView({ behavior: "smooth" });
  }
}
async function deleteTemplate(name) {
  if (!confirm(`确定删除模板「${name}」吗？`)) return;
  try {
    await fetch("/api/templates/" + encodeURIComponent(name), { method: "DELETE" });
    await refreshTemplates(); notifyOk("模板已删除");
  } catch (e) { notifyErr("删除失败：" + e.message); }
}

/* 模板卡片上的「✏️ 重命名」：把名称行换成输入框，回车/失焦提交，Esc 取消。 */
function inlineRenameTemplate(t, row, nameEl) {
  const input = document.createElement("input");
  input.type = "text"; input.className = "tpl-rename-input";
  input.value = t.name;
  input.maxLength = 60;
  const done = () => {
    const newName = input.value.trim();
    if (!newName) { renderName(nameEl, t.name); return; }
    if (newName === t.name) { renderName(nameEl, t.name); return; }
    input.disabled = true;
    fetch("/api/templates/rename", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_name: t.name, new_name: newName }),
    }).then(async (r) => {
      if (!r.ok) throw new Error(((await r.json()).detail) || "重命名失败");
      notifyOk(`模板「${t.name}」已重命名为「${newName}」`);
      await refreshTemplates();
    }).catch((e) => {
      notifyErr(e.message);
      renderName(nameEl, t.name);
    });
  };
  const onKey = (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
    else if (ev.key === "Escape") { ev.preventDefault(); renderName(nameEl, t.name); }
  };
  nameEl.innerHTML = "";
  nameEl.appendChild(input);
  input.addEventListener("keydown", onKey);
  input.addEventListener("blur", done);
  input.focus(); input.select();
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function renderName(nameEl, name) {
  nameEl.innerHTML = ICO("pin") + ` <span class="tpl-name-text">${escapeHtml(name)}</span>`;
}

/* ===================== 单个：开始处理（空闲直接跑，有任务在跑则自动入队） ===================== */
$("startBtn").addEventListener("click", () => {
  if (!state.stagingId) return notifyErr("请先在单个处理中载入文件");
  if (!state.boxes.length) return notifyErr("请先框选至少一个水印");
  const isVideo = state.view === "video";
  const endpoint = isVideo ? "/api/process" : "/api/process_image";
  const payload = {
    staging_id: state.stagingId,
    boxes: state.boxes.map(stripId),
    engine: $("engineSel").value,
    quality: isVideo ? $("qualitySel").value : "balanced",
    device: "cuda",
  };
  if (isVideo && segToggle.checked) {
    const st = Math.max(0, Number(segStart.value));
    payload.seg_start = st;
    payload.seg_end = Math.max(st + 0.1, Number(segEnd.value));
  }
  qAdd({
    kind: isVideo ? "video" : "image",
    title: "去除水印 · " + (isVideo ? "视频" : "图片"),
    run: async () => {
      setBusy($("startBtn"), true, "提交中…");
      try {
        const r = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        if (!r.ok) throw new Error((await r.json()).detail || "启动失败");
        state.taskId = (await r.json()).task_id;
        $("progressCard").classList.remove("hidden");
        $("progressCard").scrollIntoView({ behavior: "smooth" });
        setBusy($("startBtn"), false, "处理中…");
        await awaitTask(state.taskId, {
          update: (s) => { $("progressBar").style.width = (s.progress || 0) + "%"; $("statusMsg").textContent = s.message || ""; },
          done: (s) => {
            setStep(3);
            const url = "/api/download/" + state.taskId;
            $("progressTitle").innerHTML = ICO("check") + " 处理完成";
            $("resultVideo").classList.toggle("hidden", !isVideo);
            $("resultImg").classList.toggle("hidden", isVideo);
            if (isVideo) $("resultVideo").src = url; else $("resultImg").src = url;
            $("downloadBtn").href = url;
            state.resultKind = isVideo ? "video" : "image";
            // 保留上方原图预览（stageCard），结果展示在下方，避免左列出现大片空白
            $("progressCard").classList.add("hidden");
            $("resultCard").classList.remove("hidden");
            $("resultCard").scrollIntoView({ behavior: "smooth" });
          }
        });
        return { kind: isVideo ? "video" : "image", title: "去除水印" + (isVideo ? "（视频）" : "（图片）"), url: "/api/download/" + state.taskId, text: "" };
      } finally { setBusy($("startBtn"), false, "开始去除水印"); }
    }
  });
});

/* ===================== 单任务进度 ===================== */
let pollTimer = null;
function pollStatus() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const s = await (await fetch("/api/status/" + state.taskId)).json();
      $("progressBar").style.width = s.progress + "%";
      $("statusMsg").textContent = s.message || "";
      if (s.status === "done") {
        clearInterval(pollTimer); setStep(3);
        const url = "/api/download/" + state.taskId;
        const isVideo = state.view === "video";
        pushHistory({ kind: isVideo ? "video" : "image", title: "去除水印" + (isVideo ? "（视频）" : "（图片）"), url, text: "" });
        $("progressTitle").innerHTML = ICO("check") + " 处理完成";
        $("resultVideo").classList.toggle("hidden", !isVideo);
        $("resultImg").classList.toggle("hidden", isVideo);
        if (isVideo) $("resultVideo").src = url; else $("resultImg").src = url;
        $("downloadBtn").href = url;
        state.resultKind = isVideo ? "video" : "image";
        // 保留上方原图预览（stageCard），结果展示在下方，避免左列出现大片空白
        $("progressCard").classList.add("hidden");
        $("resultCard").classList.remove("hidden");
        $("resultCard").scrollIntoView({ behavior: "smooth" });
      } else if (s.status === "error") {
        clearInterval(pollTimer);
        $("progressTitle").innerHTML = ICO("x") + " 处理失败"; $("progressTitle").classList.add("err-title");
        $("statusMsg").textContent = s.message || "未知错误";
        notifyErr(s.message || "处理失败");
      }
    } catch (_) {}
  }, 600);
}

/* ===================== 批量文件选择（多选 / 文件夹 / 拖拽） ===================== */
function bindBatchDz(dzId, inputId, onFiles) {
  const dz = $(dzId), input = $(inputId);
  dz.addEventListener("click", () => input.click());
  ["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) onFiles([...e.dataTransfer.files]); });
  input.addEventListener("change", () => { if (input.files.length) onFiles([...input.files]); input.value = ""; });
}
bindBatchDz("batchDrop", "batchInput", addBatchFiles);
$("batchFolderInput").addEventListener("change", (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  addBatchFiles(files);
});
$("folderBtn").addEventListener("click", () => $("batchFolderInput").click());

function acceptedExt() {
  return state.view === "video" ? VID_EXT : IMG_EXT;
}
function addBatchFiles(files) {
  const re = acceptedExt();
  const ok = files.filter((f) => !f.name || re.test(f.name));
  ok.forEach((f) => {
    // 按类型分离：图片进 batchImages，视频进 batchVideos
    const isVid = VID_EXT.test(f.name);
    const arr = isVid ? state.batchVideos : state.batchImages;
    if (!arr.some((x) => x.name === f.name && x.size === f.size)) {
      arr.push({ name: f.name, size: f.size, file: f, url: URL.createObjectURL(f), boxes: [], templateName: "", manual: false, stagingId: null });
    }
  });
  renderBatchFiles();
  notifyOk(`已添加 ${ok.length} 个${state.view === "video" ? "视频" : "图片"}（当前 ${currentBatch().length} 个）${ok.length !== files.length ? "，已过滤不支持的格式" : ""}`);
}
function removeBatchFile(i) {
  const arr = currentBatch();
  const it = arr[i];
  if (!it) return;
  if (it.url) URL.revokeObjectURL(it.url);
  arr.splice(i, 1);
  if (state.activeBatchIndex === i) state.activeBatchIndex = null;
  else if (state.activeBatchIndex !== null && state.activeBatchIndex > i) state.activeBatchIndex -= 1;
  renderBatchFiles();
  if (state.activeBatchIndex === null) { $("batchPreviewCard").classList.add("hidden"); state.boxMode = "single"; }
  else selectBatchItem(state.activeBatchIndex);
}
function renderBatchFiles() {
  const ul = $("batchFileList");
  const arr = currentBatch();
  ul.classList.toggle("hidden", arr.length === 0);
  ul.innerHTML = "";
  arr.forEach((f, i) => {
    const li = document.createElement("li");
    li.className = "file-item" + (i === state.activeBatchIndex ? " active" : "");
    const hasCfg = (f.manual && f.boxes.length) || f.templateName;
    li.innerHTML = `<span class="file-ico">${state.view === "video" ? ICO("film") : ICO("image")}</span><span class="file-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}${hasCfg ? `<em class="file-cfg">✓设置</em>` : ""}</span><span class="file-size">${fmtSize(f.size)}</span>`;
    li.addEventListener("click", (e) => { if (!e.target.closest(".file-rm")) selectBatchItem(i); });
    const rm = document.createElement("button");
    rm.className = "file-rm"; rm.textContent = "×"; rm.title = "移除";
    rm.addEventListener("click", (e) => { e.stopPropagation(); removeBatchFile(i); });
    li.appendChild(rm);
    ul.appendChild(li);
  });
}

/* ===================== 批量：逐项预览 / 模板 / 手动框选 ===================== */
function renderBatchBoxes() {
  const item = currentBatch()[state.activeBatchIndex];
  const layer = $("batchPreviewLayer");
  if (!item || !layer) return;
  state.boxMode = "batch";
  state.layer = layer;
  layer.innerHTML = "";
  (item.boxes || []).forEach((box, idx) => layer.appendChild(spawnBoxEl(box, idx)));
  renderBoxList();
}
function selectBatchItem(i) {
  const item = currentBatch()[i];
  if (!item) return;
  state.activeBatchIndex = i;
  state.boxMode = "batch";
  const isVideo = state.view === "video";
  const v = $("batchPreviewVideo"), img = $("batchPreviewImg");
  v.classList.toggle("hidden", !isVideo);
  img.classList.toggle("hidden", isVideo);
  if (isVideo) v.src = item.url; else img.src = item.url;
  $("batchPreviewName").textContent = item.name;
  $("batchItemTplSel").value = item.templateName || "";
  $("batchPreviewCard").classList.remove("hidden");
  renderBatchBoxes();
  document.querySelectorAll("#batchFileList .file-item").forEach((el, ix) => el.classList.toggle("active", ix === i));
}
function addBatchBox() {
  const item = currentBatch()[state.activeBatchIndex];
  if (!item) return notifyErr("请先在左侧点击选择要编辑的素材");
  if (!Array.isArray(item.boxes)) item.boxes = [];
  const b = { id: uid(), x: 0.4, y: 0.4, w: 0.2, h: 0.15 };
  item.boxes.push(b); item.manual = true; state.activeBoxId = b.id;
  renderBatchBoxes(); renderBatchFiles();
  notify(`已为「${item.name}」添加第 ${item.boxes.length} 个水印框`);
}
function clearBatchBoxes() {
  const item = currentBatch()[state.activeBatchIndex];
  if (!item) return;
  item.boxes = []; item.manual = false; state.activeBoxId = null;
  renderBatchBoxes(); renderBatchFiles();
  notify(`已清空「${item.name}」的手动框，将改用模板/全局水印`);
}
async function batchItemAuto() {
  const item = currentBatch()[state.activeBatchIndex];
  if (!item) return notifyErr("请先点击选择要检测的素材");
  setBusy($("batchItemAuto"), true, "检测中…");
  try {
    if (!item.stagingId) {
      const fd = new FormData(); fd.append("files", item.file);
      const url = state.view === "video" ? "/api/upload_multi" : "/api/upload_images";
      const r = await fetch(url, { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json()).detail || "上传失败");
      item.stagingId = (await r.json()).items[0].staging_id;
    }
    const body = { staging_id: item.stagingId };
    if (state.view === "video") body.time = 0;
    const r = await fetch("/api/detect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json()).detail || "检测失败");
    const bs = (await r.json()).boxes || [];
    if (!bs.length) return notify("未检测到明显水印，可手动框选");
    item.boxes = cloneBoxes(bs); item.manual = true;
    renderBatchBoxes(); renderBatchFiles();
    notifyOk(`为「${item.name}」检测到 ${bs.length} 处水印（可拖动调整）`);
  } catch (e) { notifyErr("自动检测失败：" + e.message); }
  finally { setBusy($("batchItemAuto"), false, ICO("sparkle") + " 自动检测"); }
}
$("batchItemTplSel").addEventListener("change", (e) => {
  const item = currentBatch()[state.activeBatchIndex];
  if (!item) return;
  item.templateName = e.target.value; renderBatchFiles();
});
$("batchItemAddBox").addEventListener("click", addBatchBox);
$("batchItemClear").addEventListener("click", clearBatchBoxes);
$("batchItemAuto").addEventListener("click", batchItemAuto);
$("batchPreviewVideo").addEventListener("loadedmetadata", () => {
  const v = $("batchPreviewVideo");
  if (v.videoWidth) $("batchPreviewWrap").style.setProperty("--ar", `${v.videoWidth} / ${v.videoHeight}`);
});
$("batchPreviewImg").addEventListener("load", () => {
  const im = $("batchPreviewImg");
  if (im.naturalWidth) $("batchPreviewWrap").style.setProperty("--ar", `${im.naturalWidth} / ${im.naturalHeight}`);
});

/* ===================== 批量处理（空闲直接跑，有任务在跑则自动入队） ===================== */
$("batchBtn").addEventListener("click", () => {
  const files = currentBatch();
  if (!files.length) return notifyErr("请先选择文件或文件夹");
  const isVideo = state.view === "video";
  qAdd({
    kind: "file",
    title: "批量去水印" + (isVideo ? "（视频）" : "（图片）") + " · " + files.length + " 项",
    run: async () => {
      setBusy($("batchBtn"), true, "上传中…");
      try {
        const fd = new FormData();
        files.forEach((f) => fd.append("files", f.file));
        const up = await fetch(isVideo ? "/api/upload_multi" : "/api/upload_images", { method: "POST", body: fd });
        if (!up.ok) throw new Error((await up.json()).detail || "上传失败");
        const uj = await up.json();

        const payload = { items: uj.items.map((i) => i.staging_id), names: uj.items.map((i) => i.name), engine: $("batchEngineSel").value, device: "cuda" };
        if (isVideo) payload.quality = $("batchQualitySel").value;
        const tpl = $("batchTplSel").value;
        if (tpl) payload.template_name = tpl;
        else payload.boxes = state.boxes.map(stripId);
        // 逐项配置：手动框优先；其次该项模板；为空则跟随全局（per_item 为 null）
        payload.per_item = files.map((it) => {
          if (it.manual && it.boxes && it.boxes.length) return { boxes: it.boxes.map(stripId) };
          return it.templateName ? { template_name: it.templateName } : null;
        });

        const b = await fetch(isVideo ? "/api/batch" : "/api/image_batch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        if (!b.ok) throw new Error((await b.json()).detail || "批量启动失败");
        const j = await b.json();
        state.batchId = j.batch_id;
        state.resultKind = isVideo ? "video" : "image";
        $("batchCard").classList.remove("hidden");
        $("batchCard").scrollIntoView({ behavior: "smooth" });
        await pollBatch(j.batch_id);
        return { kind: "file", title: "批量去水印（" + (j.total || files.length) + " 项）", url: `/api/batch/${j.batch_id}/zip` };
      } finally { setBusy($("batchBtn"), false, "批量开始去水印"); }
    }
  });
});

let batchTimer = null;
function pollBatch(batchId) {
  if (batchTimer) clearInterval(batchTimer);
  return new Promise((resolve, reject) => {
    const total = { done: false, fails: 0 };
    batchTimer = setInterval(async () => {
      try {
        const resp = await fetch("/api/batch/" + batchId);
        if (!resp.ok) throw new Error("服务响应异常 (" + resp.status + ")");
        const s = await resp.json();
        const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
        $("batchBar").style.width = pct + "%";
        $("batchMsg").textContent = `已完成 ${s.done} / ${s.total}${s.status === "done" ? " · 全部完成 ✓" : " · 处理中…"}`;
        const body = $("batchBody");
        body.innerHTML = "";
        s.tasks.forEach((t, i) => {
          const tr = document.createElement("tr");
          const tag = t.status === "done" ? "tag-ok" : (t.status === "error" ? "tag-err" : "tag-run");
          const statusText = t.status === "done" ? "完成" : (t.status === "error" ? "失败" : (t.status === "processing" ? "处理中" : "排队中"));
          const act = t.download_url ? `<a href="${t.download_url}">下载</a>` : (t.status === "error" ? "—" : "处理中");
          tr.innerHTML = `<td>${i + 1}</td><td class="file-cell" title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</td><td class="${tag}">${statusText}</td><td><div class="mini-progress"><div class="mini-bar" style="width:${t.progress || 0}%"></div></div><span class="mini-pct">${t.progress || 0}%</span></td><td>${act}</td>`;
          body.appendChild(tr);
        });
        total.fails = 0; // 单次成功即重置连续失败计数
        if (s.status === "done") { if (!total.done) { total.done = true; clearInterval(batchTimer); const z = $("batchZipBtn"); z.classList.remove("hidden"); z.href = `/api/batch/${batchId}/zip`; resolve(s); } }
        else if (s.status === "error" || s.status === "failed") { if (!total.done) { total.done = true; clearInterval(batchTimer); reject(new Error(s.message || "批量处理失败")); } }
      } catch (e) {
        // 网络/服务异常：允许短暂重试，避免因一次抖动就失败；连续失败过多则终止以免阻塞整个任务队列
        total.fails++;
        if (total.fails >= 6) { if (!total.done) { total.done = true; clearInterval(batchTimer); reject(new Error("批量进度查询失败：" + (e && e.message ? e.message : String(e)))); } }
      }
    }, 800);
  });
}

$("againBtn").addEventListener("click", () => location.reload());

/* ===================== 结果中心：任务队列 + 历史 ===================== */
const HISTORY_KEY = "jijing_results_v1";
let qHistory = [];
let qQueue = [];
let qRunning = false;

function loadHistory() { try { qHistory = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); } catch (_) { qHistory = []; } }
function saveHistory() { try { localStorage.setItem(HISTORY_KEY, JSON.stringify(qHistory)); } catch (_) {} }
function pushHistory(h) { qHistory.unshift(Object.assign({ status: "done", time: Date.now() }, h)); if (qHistory.length > 200) qHistory.length = 200; saveHistory(); renderHistory(); }
function clearHistory() { qHistory = []; saveHistory(); renderHistory(); }

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
async function postJson(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || ("请求失败 " + url));
  return j;
}
async function awaitTask(taskId, opts) {
  opts = opts || {};
  for (;;) {
    const s = await (await fetch("/api/status/" + taskId)).json();
    if (opts.update) { try { opts.update(s); } catch (_) {} }
    if (s.status === "done") { if (opts.done) { try { opts.done(s); } catch (_) {} } return s; }
    if (s.status === "error" || s.status === "failed" || s.status === "fail") {
      const e = new Error(s.message || "任务失败");
      if (opts.error) { try { opts.error(s); } catch (_) {} }
      throw e;
    }
    await sleep(700);
  }
}

/* ---- 队列操作：无空闲则自动入队，有任务在跑时新任务排队等待 ---- */
function qAdd(item, silent) {
  qQueue.push(Object.assign({ id: uid(), status: "ready", error: null }, item));
  renderQueue();
  if (!silent) notify("已加入队列，共 " + qQueue.length + " 项");
  qTick();
}
function qRemove(id) {
  const it = qQueue.find((x) => x.id === id);
  if (it && it.status === "running") return notifyErr("运行中的任务不可移除");
  qQueue = qQueue.filter((x) => x.id !== id);
  renderQueue();
}
let qStarted = false;
async function qTick() {
  if (qRunning) return; // 已在运行，自动吃下后续队列项
  if (!qQueue.length) return;
  qRunning = true;
  qStarted = true;
  renderQueue();
  let hadError = false;
  while (qQueue.length) {
    const it = qQueue[0];
    it.status = "running"; it.error = null; renderQueue();
    try {
      await waitForPreviewIdle();   // 等待实时预览清场，避免与真实增强抢 GPU 槽位
      const res = await it.run();
      it.status = "done"; renderQueue();
      qQueue.shift(); renderQueue();
      if (res) pushHistory(res);
    } catch (e) {
      hadError = true;
      it.status = "error"; it.error = e.message || String(e); renderQueue();
      pushHistory({ status: "error", kind: "file", title: "失败 · " + (it.title || "任务"), text: it.error, time: Date.now() });
      notifyErr((it.title || "任务") + " 失败：" + it.error);
      qQueue.shift(); renderQueue();
      await sleep(400);
    }
  }
  qRunning = false;
  qStarted = false;
  renderQueue();
  if (hadError) notifyErr("队列处理结束：部分任务失败，请查看任务一览");
  else notifyOk("全部任务处理完成");
}

/* ---- 渲染 ---- */
const QSTATUS = { ready: "待处理", running: "运行中", done: "完成", error: "失败" };
function renderQueue() {
  const box = $("rfQueue"); if (!box) return;
  $("qCount").textContent = qQueue.length;
  const running = qQueue.filter((x) => x.status === "running").length;
  // 底部最小化进度条
  const max = $("rfMax");
  if (max) {
    if (running) {
      max.classList.remove("hidden");
      max.innerHTML = '<span class="rf-dot"></span> 队列运行中：' + running + " 项";
      max.onclick = openPanel;
    } else max.classList.add("hidden");
  }
  box.innerHTML = "";
  if (!qQueue.length) { box.innerHTML = '<div class="rf-empty">队列为空 · 点击「生成/增强」按钮会自动加入队列</div>'; return; }
  qQueue.forEach((it) => {
    const row = document.createElement("div"); row.className = "rf-qitem";
    const canDel = it.status !== "running";
    row.innerHTML = '<div class="rf-qmain"><div class="rf-qtitle">' + escapeHtml(it.title || "任务") + '</div><div class="rf-qmeta">' + escapeHtml(it.meta || "") + '</div></div><span class="rf-status ' + it.status + '">' + (QSTATUS[it.status] || it.status) + '</span><button class="rf-qdel" ' + (canDel ? "" : "disabled") + ' title="移除">' + ICO("x") + '</button>';
    row.querySelector(".rf-qdel").addEventListener("click", () => qRemove(it.id));
    box.appendChild(row);
  });
}
function renderHistory() {
  const box = $("rfHistory"); if (!box) return;
  $("hCount").textContent = qHistory.length;
  box.innerHTML = "";
  if (!qHistory.length) { box.innerHTML = '<div class="rf-empty">暂无历史结果</div>'; return; }
  qHistory.forEach((h) => {
    const row = document.createElement("div"); row.className = "rf-hitem";
    const icon = ICO(h.status === "error" ? "x" : h.kind === "video" ? "film" : h.kind === "text" ? "edit" : h.kind === "file" ? "box" : "image");
    row.innerHTML = '<div class="rf-hico">' + icon + '</div><div class="rf-hmain"><div class="rf-htitle">' + escapeHtml(h.title || "结果") + '</div><div class="rf-htime">' + new Date(h.time).toLocaleString() + (h.status === "error" ? " · 失败" : "") + '</div></div><div class="rf-hact"></div>';
    const act = row.querySelector(".rf-hact");
    if (h.status === "error") { const s = document.createElement("span"); s.className = "rf-status error"; s.textContent = "失败"; act.appendChild(s); }
    else if (h.kind === "text") { const b = document.createElement("button"); b.className = "btn small rf-qrun"; b.textContent = "复制"; b.addEventListener("click", () => { if (navigator.clipboard) navigator.clipboard.writeText(h.text || "").then(() => notifyOk("已复制")); }); act.appendChild(b); if (h.text) { const t = document.createElement("button"); t.className = "btn small rf-qrun ghost"; t.textContent = "详情"; t.addEventListener("click", () => notify(String(h.text).slice(0, 400))); act.appendChild(t); } }
    else if (h.url) { const a = document.createElement("a"); a.className = "btn small rf-qrun"; a.href = h.url; a.target = "_blank"; a.textContent = "下载"; act.appendChild(a); }
    box.appendChild(row);
    // 可预览项（有结果 url 的图片/视频）：悬浮预览 + 点击灯箱查看
    if (h.url && (h.kind === "image" || h.kind === "video")) {
      row.classList.add("previewable");
      const main = row.querySelector(".rf-hmain");
      main.addEventListener("mouseenter", (ev) => showHistPreview(ev, h));
      main.addEventListener("mousemove", (ev) => moveHistPreview(ev));
      main.addEventListener("mouseleave", () => hideHistPreview());
      main.addEventListener("click", () => openHistLightbox(h));
    }
  });
}

/* ---- 历史条目的悬浮缩略预览（fixed 浮层挂到 body） ---- */
let _histPrevEl = null;
function ensureHistPrev() {
  if (_histPrevEl) return _histPrevEl;
  _histPrevEl = document.createElement("div");
  _histPrevEl.className = "hist-prev";
  document.body.appendChild(_histPrevEl);
  return _histPrevEl;
}
function showHistPreview(ev, h) {
  const el = ensureHistPrev();
  el.innerHTML = "";
  if (h.kind === "video") { const v = document.createElement("video"); v.src = h.url; v.muted = true; v.loop = true; v.preload = "metadata"; v.poster = h.preview || ""; el.appendChild(v); v.play().catch(() => {}); }
  else { const img = document.createElement("img"); img.src = h.url; img.loading = "lazy"; img.alt = h.title || ""; img.onerror = () => { el.innerHTML = '<span class="hist-prev-ph">无法预览此文件</span>'; }; el.appendChild(img); }
  el.classList.add("show");
  moveHistPreview(ev);
}
function moveHistPreview(ev) {
  const el = ensureHistPrev(); if (!el.classList.contains("show")) return;
  const pad = 14, gap = 16;
  let x = ev.clientX + gap; let y = ev.clientY + gap;
  const rw = el.offsetWidth, rh = el.offsetHeight;
  if (x + rw > innerWidth - pad) x = ev.clientX - rw - gap;
  if (y + rh > innerHeight - pad) y = innerHeight - rh - pad;
  el.style.left = Math.max(pad, x) + "px";
  el.style.top = Math.max(pad, y) + "px";
}
function hideHistPreview() {
  const el = ensureHistPrev();
  el.classList.remove("show");
  const vid = el.querySelector("video"); if (vid) { vid.pause(); }
  el.innerHTML = "";
}

/* ---- 历史条目的灯箱大图/视频查看 ---- */
function ensureHistLightbox() {
  let lb = document.getElementById("histLb");
  if (lb) return lb;
  lb = document.createElement("div");
  lb.className = "hist-lb hidden"; lb.id = "histLb";
  lb.innerHTML = '<div class="hist-lb__inner"><button class="hist-lb__close" title="关闭">\u2715</button><div class="hist-lb__media"></div><div class="hist-lb__cap"></div></div>';
  document.body.appendChild(lb);
  lb.addEventListener("click", (e) => { if (e.target === lb || e.target.closest(".hist-lb__close")) closeHistLightbox(); });
  return lb;
}
function openHistLightbox(h) {
  const lb = ensureHistLightbox();
  const box = lb.querySelector(".hist-lb__media");
  box.innerHTML = "";
  if (h.kind === "video") { const v = document.createElement("video"); v.src = h.url; v.controls = true; v.poster = h.preview || ""; v.style.maxWidth = "92vw"; v.style.maxHeight = "86vh"; box.appendChild(v); }
  else { const img = document.createElement("img"); img.src = h.url; img.alt = h.title || ""; img.onerror = () => { box.innerHTML = '<span class="hist-prev-ph">无法预览此文件</span>'; }; box.appendChild(img); }
  lb.querySelector(".hist-lb__cap").textContent = h.title || "结果预览";
  lb.classList.remove("hidden");
}
function closeHistLightbox() {
  const lb = document.getElementById("histLb"); if (!lb) return;
  lb.classList.add("hidden");
  const v = lb.querySelector("video"); if (v) { v.pause(); v.removeAttribute("src"); v.load(); }
  lb.querySelector(".hist-lb__media").innerHTML = "";
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeHistLightbox(); });

/* ---- 结果中心：收起/展开都通过右侧手柄标签切换；展开时挤压主界面(留空间)，收起时空间归还 ---- */
function openPanel() { const r = $("results"); if (r) r.classList.add("open"); const m = $("rfMax"); if (m) m.classList.add("hidden"); }
function closePanel() { const r = $("results"); if (r) r.classList.remove("open"); }
function togglePanel() {
  const r = $("results"); if (!r) return;
  const opening = r.classList.toggle("open");
  if (opening) { const m = $("rfMax"); if (m) m.classList.add("hidden"); }
}
function on(id, ev, fn) { const el = $(id); if (el) el.addEventListener(ev, fn); }
on("resTab", "click", togglePanel);
closePanel();
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
document.querySelectorAll(".rf-sub").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll(".rf-sub").forEach((b) => b.classList.toggle("active", b === btn));
  const t = btn.dataset.rfsub;
  const q = $("rfQueue"), h = $("rfHistory");
  if (q) q.classList.toggle("hidden", t !== "queue");
  if (h) h.classList.toggle("hidden", t !== "history");
  // 底部清空按钮跟随标签：队列页显示「清空队列」，历史页显示「清空历史」
  const qb = $("qClearBtn"), hb = $("hClearBtn");
  if (qb) qb.classList.toggle("hidden", t !== "queue");
  if (hb) hb.classList.toggle("hidden", t !== "history");
}));
on("qClearBtn", "click", () => { if (qRunning) return notifyErr("运行中不可清空队列"); qQueue = []; renderQueue(); });
on("hClearBtn", "click", () => { if (confirm("确定清空全部历史记录？")) clearHistory(); });

/* ===================== 初始化 ===================== */
loadHistory();
renderHistory();
renderQueue();
updateModeUI();
checkEngines();
refreshTemplates();
initSettingsUI();
bindLivePreview();