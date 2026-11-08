const $ = (id) => document.getElementById(id);

const state = {
  ready: false,
  imageReady: false,
  cutoutReady: false,
  promptReady: false,
  csrfToken: null,
  workspace: "video",
  videoJobId: null,
  imageJobId: null,
  videoPollTimer: null,
  imagePollTimer: null,
  optimizerBusy: false,
  promptProfiles: {},
  continueFromJobId: null,
  videoImageUrl: null,
  referenceUrls: [null, null, null],
  optimization: null,
  sequenceBusy: false,
  importingVideo: false,
  importedReferenceId: null,
  generatedImageUrl: null,
  batch: {
    files: [],
    items: [],
    scanning: false,
    running: false,
    stopRequested: false,
    pauseRequested: false,
    paused: false,
    resumeWaiter: null,
    cancelSent: false,
    currentJobId: null,
    frozenSettings: null,
    validationError: null,
  },
  imageBatch: {
    files: [],
    items: [],
    scanning: false,
    running: false,
    stopRequested: false,
    pauseRequested: false,
    paused: false,
    resumeWaiter: null,
    cancelSent: false,
    currentJobId: null,
    frozenSettings: null,
    validationError: null,
  },
};

const prompt = $("prompt");
const characterLock = $("characterLock");
const videoImage = $("videoImage");
const dropZone = $("dropZone");
const imagePreview = $("imagePreview");
const generateButton = $("generateButton");
const continueButton = $("continueButton");
const cancelButton = $("cancelButton");
const generateImageButton = $("generateImageButton");
const cancelImageButton = $("cancelImageButton");
const batchFolder = $("batchFolder");
const batchFolderPath = $("batchFolderPath");
const scanBatchFolderButton = $("scanBatchFolderButton");
const batchCompliance = $("batchCompliance");
const startBatchButton = $("startBatchButton");
const stopBatchButton = $("stopBatchButton");
const pauseBatchButton = $("pauseBatchButton");
const imageBatchFolder = $("imageBatchFolder");
const imageBatchFolderPath = $("imageBatchFolderPath");
const scanImageBatchFolderButton = $("scanImageBatchFolderButton");
const startImageBatchButton = $("startImageBatchButton");
const stopImageBatchButton = $("stopImageBatchButton");
const pauseImageBatchButton = $("pauseImageBatchButton");

const BATCH_TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const BATCH_IMAGE_EXTENSION = /\.(png|jpe?g|webp)$/i;
const BATCH_MAX_FILES = 200;
const batchPathCollator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });

function show(id, visible = true) {
  $(id).classList.toggle("hidden", !visible);
}

function setError(kind, message = "") {
  const target = kind === "image" ? $("imageErrorBox") : $("videoErrorBox");
  target.textContent = message;
  target.classList.toggle("hidden", !message);
}

function anyBusy() {
  return Boolean(state.videoPollTimer || state.imagePollTimer || state.optimizerBusy || state.batch.scanning || state.batch.running || state.imageBatch.scanning || state.imageBatch.running || state.sequenceBusy || state.importingVideo || state.sequenceBatchActionBusy);
}

function updateImageModeControls() {
  const mode = $("imageMode").value;
  const editing = mode === "edit";
  const hasPrimary = Boolean($("reference1").files[0]);
  const referenceGuided = editing || hasPrimary;
  show("referenceArea", true);
  show("referenceSlotWrap2", editing || hasPrimary);
  show("referenceSlotWrap3", editing || hasPrimary);
  $("referenceGrid").classList.toggle("primary-only", !editing && !hasPrimary);
  $("reference2").disabled = !hasPrimary;
  $("reference3").disabled = !hasPrimary;
  $("referenceSlotWrap2").classList.toggle("unavailable", editing && !hasPrimary);
  $("referenceSlotWrap3").classList.toggle("unavailable", editing && !hasPrimary);
  $("imageAspect").disabled = referenceGuided;
  show("imageAspectHint", referenceGuided);
  $("imageAspectHint").textContent = editing
    ? "固定人物编辑会按第 1 张母版的比例匹配模型支持的尺寸。"
    : "有参考图时，会按第 1 张图的比例匹配模型支持的尺寸。";
  $("referenceIntro").innerHTML = editing
    ? "<strong>第 1 张是已经确认的身份母版。</strong>第 2、3 张应是同一人物，只补充姿态和细节；冲突时以第 1 张为准。"
    : "<strong>参考图可选。</strong>不上传就按文字创建；上传后，第 1 张决定主要人物，第 2、3 张补充姿态、服装或风格，最终结果才是新的母版候选。不同人物的脸可能发生融合。";
  $("referenceLabel1").textContent = editing ? "1 · 身份母版（必选）" : "1 · 主要人物参考（可选）";
  $("referenceLabel2").textContent = editing ? "2 · 侧面 / 全身 / 姿态（可选）" : "2 · 角度 / 构图 / 姿态（可选）";
  $("referenceLabel3").textContent = editing ? "3 · 表情 / 服装 / 标志物（可选）" : "3 · 服装 / 风格 / 细节（可选）";
  $("clearReference1").textContent = editing ? "移除母版及补充图" : "移除主要参考及补充图";
  generateImageButton.textContent = editing
    ? "固定人物生成新图片"
    : (hasPrimary ? "参考图片创建角色母版" : "按文字创建角色母版");
  updateReferenceSummary();
  updateCutoutAvailability();
}

function updateCutoutAvailability() {
  const checkbox = $("referenceCutout");
  const hasPrimary = Boolean($("reference1").files[0]);
  const relevant = $("imageMode").value === "edit" || hasPrimary;
  const available = state.cutoutReady && hasPrimary;
  show("referenceCutoutRow", relevant);
  checkbox.disabled = !available;
  checkbox.closest(".toggle-row").classList.toggle("unavailable", !available);
  checkbox.closest(".toggle-row").title = !state.cutoutReady
    ? "本地 BiRefNet 抠图模型尚未准备完整"
    : (hasPrimary ? "" : "请先上传第 1 张人物参考图");
  if (!available) checkbox.checked = false;
}

function updateControls() {
  const busy = anyBusy();
  generateButton.disabled = busy || !state.ready;
  continueButton.disabled = busy || !state.ready || !state.continueFromJobId;
  generateImageButton.disabled = busy || !state.imageReady;
  $("optimizeButton").disabled = busy || !state.promptReady;
  show("cancelButton", Boolean(state.videoPollTimer));
  show("cancelImageButton", Boolean(state.imagePollTimer));
  const batchLocked = state.batch.running || state.batch.scanning || state.imageBatch.running || state.imageBatch.scanning || state.sequenceBusy;
  [prompt, characterLock, $("videoQuality"), $("videoAspect"), $("videoSeed"), batchFolderPath, batchFolder, batchCompliance]
    .forEach((control) => { control.disabled = batchLocked; });
  scanBatchFolderButton.disabled = batchLocked || !batchFolderPath.value.trim();
  startBatchButton.disabled = busy || !state.ready || !state.batch.files.length || Boolean(state.batch.validationError) || !batchCompliance.checked;
  show("stopBatchButton", state.batch.running);
  stopBatchButton.disabled = state.batch.running && state.batch.stopRequested;
  show("pauseBatchButton", state.batch.running);
  pauseBatchButton.disabled = state.batch.stopRequested;
  pauseBatchButton.textContent = state.batch.paused ? "继续生成" : (state.batch.pauseRequested ? "取消暂停" : "暂停批次");
  pauseBatchButton.setAttribute("aria-pressed", String(state.batch.pauseRequested));
  const hasPrimaryReference = Boolean($("reference1").files[0]);
  const referenceGuided = $("imageMode").value === "edit" || hasPrimaryReference;
  $("imageMode").disabled = batchLocked;
  $("imageQuality").disabled = batchLocked;
  $("imageSeed").disabled = batchLocked;
  $("imageAspect").disabled = batchLocked || referenceGuided;
  $("reference1").disabled = batchLocked;
  $("reference2").disabled = batchLocked || !hasPrimaryReference;
  $("reference3").disabled = batchLocked || !hasPrimaryReference;
  imageBatchFolderPath.disabled = busy;
  imageBatchFolder.disabled = busy;
  scanImageBatchFolderButton.disabled = busy || !imageBatchFolderPath.value.trim();
  startImageBatchButton.disabled = busy || !state.imageReady || !state.imageBatch.files.length || Boolean(state.imageBatch.validationError);
  show("stopImageBatchButton", state.imageBatch.running);
  stopImageBatchButton.disabled = state.imageBatch.running && state.imageBatch.stopRequested;
  show("pauseImageBatchButton", state.imageBatch.running);
  pauseImageBatchButton.disabled = state.imageBatch.stopRequested;
  pauseImageBatchButton.textContent = state.imageBatch.paused ? "继续生成" : (state.imageBatch.pauseRequested ? "取消暂停" : "暂停批次");
  pauseImageBatchButton.setAttribute("aria-pressed", String(state.imageBatch.pauseRequested));
  $("imageBatchCutout").disabled = busy || !state.cutoutReady;
  $("imageBatchCutoutRow").classList.toggle("unavailable", !state.cutoutReady);
  $("imageBatchCutoutRow").title = state.cutoutReady ? "" : "本地 BiRefNet 抠图模型尚未准备完整";
  if (!state.cutoutReady) $("imageBatchCutout").checked = false;
  if (typeof updateSequenceControls === "function") updateSequenceControls();
}

function updatePromptModelControls() {
  const select = $("promptModelProfile");
  const fallbackProfiles = {
    stable: { label: "稳定模式", installed: false, model_ready: false },
    creative: { label: "自由创作模式", installed: false, model_ready: false },
  };
  const profiles = Object.keys(state.promptProfiles).length ? state.promptProfiles : fallbackProfiles;
  for (const option of select.options) {
    const profile = profiles[option.value] || fallbackProfiles[option.value];
    option.disabled = !profile?.installed;
    option.textContent = `${profile?.label || option.value}${profile?.installed ? "" : " · 未安装"}`;
  }
  if (!profiles[select.value]?.installed && profiles.stable?.installed) select.value = "stable";
  const selected = profiles[select.value];
  state.promptReady = Boolean(selected?.installed);
  $("promptModelHint").textContent = select.value === "creative"
    ? "自由创作模式：低拒绝实验模型，适合合法的虚构与成年人题材；输出仍需人工检查。"
    : "稳定模式：中文与六字段结构化输出更可靠。";
  localStorage.setItem("h3-prompt-model-profile", select.value);
}

function engineBadge(kind, text) {
  const badge = $("engineBadge");
  badge.className = `badge ${kind}`;
  badge.innerHTML = `<span></span>${text}`;
}

async function api(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  if (method !== "GET" && state.csrfToken) {
    options.headers = { ...(options.headers || {}), "X-H3-CSRF": state.csrfToken };
  }
  const response = await fetch(url, options);
  let payload;
  try { payload = await response.json(); }
  catch { payload = { error: `服务器返回了无法识别的内容（HTTP ${response.status}）` }; }
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function refreshEngine() {
  try {
    const payload = await api("/api/status");
    state.csrfToken = payload.csrf_token;
    const upgradedImages = payload.image_models?.model === "Qwen-Image-2.1 INT8";
    $("imageEngine").querySelector('[value="qwen21"]').disabled = !upgradedImages;
    if (!state.imageEngineInitialized) { $("imageEngine").value = upgradedImages ? "qwen21" : "legacy"; state.imageEngineInitialized = true; }
    $("imageEngineNote").textContent = upgradedImages ? "Qwen-Image 2.1 INT8 · 原版可切换" : "Qwen-Image 2512 / Edit 2511 · 本地 Q3";
    state.sequenceBusy = Boolean(payload.active_sequence);
    state.ready = Boolean(payload.ready);
    state.imageReady = Boolean(payload.image_models?.ready);
    state.cutoutReady = Boolean(payload.image_models?.cutout?.ready);
    state.promptProfiles = payload.prompt_ai?.profiles || {
      stable: {
        label: "稳定模式",
        installed: Boolean(payload.prompt_ai?.installed),
        model_ready: Boolean(payload.prompt_ai?.model_ready),
      },
      creative: { label: "自由创作模式", installed: false, model_ready: false },
    };
    updatePromptModelControls();
    updateCutoutAvailability();
    if (state.ready && state.imageReady && state.promptReady) engineBadge("ready", "本地模型已就绪");
    else if (!payload.engine?.healthy) engineBadge("error", "视频引擎未连接");
    else if (!payload.models?.ready) engineBadge("checking", "视频模型仍在准备");
    else engineBadge("checking", "部分本地能力未就绪");

    if (payload.active_job) {
      state.videoJobId = payload.active_job.id;
      renderVideoJob(payload.active_job);
      startVideoPolling();
    }
    if (payload.active_image_job) {
      state.imageJobId = payload.active_image_job.id;
      renderImageJob(payload.active_image_job);
      startImagePolling();
    }
    updateControls();
  } catch (error) {
    state.ready = false;
    state.imageReady = false;
    state.cutoutReady = false;
    state.promptReady = false;
    state.promptProfiles = {};
    state.sequenceBusy = false;
    updatePromptModelControls();
    updateCutoutAvailability();
    updateControls();
    engineBadge("error", "工作台连接失败");
    setError(state.workspace, error.message);
  }
}

function switchWorkspace(workspace) {
  state.workspace = workspace;
  show("videoWorkspace", workspace === "video");
  show("imageWorkspace", workspace === "image");
  $("videoTab").classList.toggle("active", workspace === "video");
  $("imageTab").classList.toggle("active", workspace === "image");
  $("videoTab").setAttribute("aria-selected", String(workspace === "video"));
  $("imageTab").setAttribute("aria-selected", String(workspace === "image"));
}

function previewVideoFile(file) {
  if (file && state.importedReferenceId && typeof clearImportedReference === "function") clearImportedReference();
  if (state.videoImageUrl) URL.revokeObjectURL(state.videoImageUrl);
  if (!file) {
    state.videoImageUrl = null;
    imagePreview.removeAttribute("src");
    dropZone.classList.remove("has-image");
    show("clearImage", false);
    return;
  }
  state.videoImageUrl = URL.createObjectURL(file);
  imagePreview.src = state.videoImageUrl;
  dropZone.classList.add("has-image");
  show("clearImage", true);
}

function previewReference(index, file) {
  if (state.referenceUrls[index]) URL.revokeObjectURL(state.referenceUrls[index]);
  const preview = $(`referencePreview${index + 1}`);
  const slot = preview.closest(".reference-slot");
  if (!file) {
    state.referenceUrls[index] = null;
    preview.removeAttribute("src");
    slot.classList.remove("has-image");
    show(`clearReference${index + 1}`, false);
    updateImageModeControls();
    return;
  }
  state.referenceUrls[index] = URL.createObjectURL(file);
  preview.src = state.referenceUrls[index];
  slot.classList.add("has-image");
  show(`clearReference${index + 1}`, true);
  updateImageModeControls();
}

function updateReferenceSummary() {
  const inputs = [$("reference1"), $("reference2"), $("reference3")];
  const count = inputs.filter((input) => Boolean(input.files[0])).length;
  if ($("imageMode").value === "create" && !inputs[0].files[0]) {
    $("referenceSummary").textContent = "未上传参考图，将按文字创建母版";
  } else if ($("imageMode").value === "create") {
    $("referenceSummary").textContent = `已选择 ${count} / 3；空槽会复用第 1 张主要参考`;
  } else {
    $("referenceSummary").textContent = `已选择 ${count} / 3${inputs[0].files[0] ? "；空槽会复用人物母版" : "；请先上传人物母版"}`;
  }
}

function clearReference(index) {
  if (index === 0) {
    clearReference(1);
    clearReference(2);
  }
  const input = $(`reference${index + 1}`);
  input.value = "";
  previewReference(index, null);
}

function renderVideoJob(job) {
  show("emptyResult", false);
  show("jobStatus", !["completed", "failed", "cancelled"].includes(job.status));
  $("statusText").textContent = job.stage || "处理中";
  $("statusMeta").textContent = job.width ? `${job.width} × ${job.height} · Seed ${job.seed}` : `Seed ${job.seed}`;
  const hasProgress = job.progress_max > 0 && job.progress_value > 0;
  $("jobStatus").classList.toggle("indeterminate", !hasProgress);
  $("progressBar").style.width = `${hasProgress ? Math.min(100, (job.progress_value / job.progress_max) * 100) : 8}%`;
  $("statusDetail").textContent = hasProgress
    ? `采样进度 ${job.progress_value} / ${job.progress_max}。随后还要解码画面、声音并封装 MP4。`
    : "首次加载大模型可能需要较长时间，请保持本窗口打开。";

  if (job.status === "completed") {
    state.continueFromJobId = job.id;
    show("continueButton", true);
    show("continueHint", true);
    show("jobStatus", false);
    show("videoResult", true);
    $("videoPlayer").src = job.video_url;
    $("downloadLink").href = job.video_url;
    $("downloadLink").download = job.filename || "MiniMax-H3.mp4";
    const audioText = job.media?.has_audio ? "含原生音轨" : "未检测到音轨";
    const continuationText = job.continuation ? " · 续接片段（开头约 0.9 秒为重叠区）" : "";
    $("videoInfo").textContent = `${job.width} × ${job.height} · 24fps · ${audioText} · Seed ${job.seed}${continuationText}`;
    stopVideoPolling();
  } else if (job.status === "failed") {
    show("jobStatus", false);
    setError("video", job.error || "生成失败，请查看日志。");
    stopVideoPolling();
  } else if (job.status === "cancelled") {
    $("statusText").textContent = "已停止";
    $("statusDetail").textContent = "这次任务已停止，没有覆盖以前的结果。";
    stopVideoPolling();
  }
  updateControls();
}

function renderImageJob(job) {
  show("emptyImageResult", false);
  show("imageJobStatus", !["completed", "failed", "cancelled"].includes(job.status));
  $("imageStatusText").textContent = job.stage || "处理中";
  $("imageStatusMeta").textContent = job.seed !== undefined ? `Seed ${job.seed}` : "";
  const hasProgress = job.progress_max > 0 && job.progress_value > 0;
  $("imageJobStatus").classList.toggle("indeterminate", !hasProgress);
  $("imageProgressBar").style.width = `${hasProgress ? Math.min(100, (job.progress_value / job.progress_max) * 100) : 8}%`;
  $("imageStatusDetail").textContent = hasProgress
    ? `采样进度 ${job.progress_value} / ${job.progress_max}。完成后会自动保存图片并释放显存。`
    : (job.stage?.includes("抠出")
      ? "BiRefNet 正在 CPU 上逐张隔离人物并铺浅灰底；只处理上传副本。"
      : "正在按需启动图片引擎；第一次通常会更久。");

  if (job.status === "completed") {
    show("imageJobStatus", false);
    show("imageResult", true);
    state.generatedImageUrl = job.image_url;
    $("generatedImage").src = job.image_url;
    $("imageDownloadLink").href = job.image_url;
    $("imageDownloadLink").download = job.filename || "Qwen-Image.png";
    const repeated = job.reference_policy?.duplicated_primary
      ? (job.mode === "create" ? " · 空缺参考已复用第 1 张" : " · 空缺参考已复用母版")
      : "";
    const cutoutCount = job.reference_preprocess?.processed_slots?.length || 0;
    const cutout = cutoutCount ? ` · 已本地抠图 ${cutoutCount} 张` : "";
    $("imageInfo").textContent = `${job.width} × ${job.height} · Seed ${job.seed}${cutout}${repeated}`;
    stopImagePolling();
  } else if (job.status === "failed") {
    show("imageJobStatus", false);
    setError("image", job.error || "图片生成失败，请查看日志。");
    stopImagePolling();
  } else if (job.status === "cancelled") {
    $("imageStatusText").textContent = "已停止";
    $("imageStatusDetail").textContent = "这次任务已停止，没有覆盖以前的结果。";
    stopImagePolling();
  }
  updateControls();
}

async function pollVideoJob() {
  if (!state.videoJobId) return;
  try { renderVideoJob((await api(`/api/jobs/${state.videoJobId}`)).job); }
  catch (error) { setError("video", error.message); }
}

async function pollImageJob() {
  if (!state.imageJobId) return;
  try { renderImageJob((await api(`/api/images/jobs/${state.imageJobId}`)).job); }
  catch (error) { setError("image", error.message); }
}

function startVideoPolling() {
  if (!state.videoPollTimer) state.videoPollTimer = setInterval(pollVideoJob, 1800);
  updateControls();
}
function stopVideoPolling() {
  if (state.videoPollTimer) clearInterval(state.videoPollTimer);
  state.videoPollTimer = null;
  updateControls();
}
function startImagePolling() {
  if (!state.imagePollTimer) state.imagePollTimer = setInterval(pollImageJob, 1800);
  updateControls();
}
function stopImagePolling() {
  if (state.imagePollTimer) clearInterval(state.imagePollTimer);
  state.imagePollTimer = null;
  updateControls();
}

function batchFilePath(file) {
  return (file.webkitRelativePath || file.name).replaceAll("\\", "/");
}

function sortBatchFiles(files) {
  return files
    .map((file, selectionIndex) => ({ file, path: batchFilePath(file), selectionIndex }))
    .sort((left, right) => (
      batchPathCollator.compare(left.path, right.path)
      || left.path.localeCompare(right.path)
      || left.selectionIndex - right.selectionIndex
    ));
}

function renderBatchQueue() {
  const queue = $("batchQueue");
  const statusLabels = {
    pending: "等待中",
    checking: "检查中",
    uploading: "上传中",
    running: "生成中",
    completed: "✓ 完成",
    failed: "✕ 失败",
    cancelled: "— 未生成",
  };
  const rows = state.batch.items.map((item) => {
    const row = document.createElement("li");
    row.className = "batch-item";
    const name = document.createElement("span");
    name.className = "batch-item-name";
    name.textContent = `${String(item.index + 1).padStart(3, "0")} · ${item.path}`;
    const status = document.createElement("strong");
    const visualStatus = ["completed", "failed", "cancelled"].includes(item.status) ? item.status : (item.status === "pending" ? "" : "running");
    status.className = `batch-item-status ${visualStatus}`.trim();
    status.textContent = item.statusText || statusLabels[item.status] || item.status;
    row.append(name, status);
    const preparation = item.preparation;
    if (preparation?.compressed) {
      const detail = document.createElement("span");
      detail.className = "batch-image-preparation";
      const mb = (bytes) => `${(bytes / 1024 / 1024).toFixed(2)}MB`;
      detail.textContent = `已自动压缩 · ${preparation.original_width}×${preparation.original_height} / ${mb(preparation.original_size)} → ${preparation.width}×${preparation.height} / ${mb(preparation.size)} · 完整画面`;
      row.append(detail);
    }
    if (item.job?.video_url) {
      const actions = document.createElement("span");
      actions.className = "batch-item-actions";
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "text-button";
      preview.textContent = "预览";
      preview.setAttribute("aria-label", `预览 ${item.path} 的视频`);
      preview.addEventListener("click", () => previewBatchVideo(item.job));
      const download = document.createElement("a");
      download.href = item.job.video_url;
      download.download = item.job.filename || "MiniMax-H3.mp4";
      download.textContent = "下载";
      download.setAttribute("aria-label", `下载 ${item.path} 生成的视频`);
      actions.append(preview, download);
      row.append(actions);
    }
    return row;
  });
  queue.replaceChildren(...rows);
  show("batchQueue", Boolean(rows.length));
}

function previewBatchVideo(job) {
  state.continueFromJobId = job.id;
  show("continueButton", true);
  show("continueHint", true);
  show("emptyResult", false);
  show("videoResult", true);
  $("videoPlayer").src = job.video_url;
  $("downloadLink").href = job.video_url;
  $("downloadLink").download = job.filename || "MiniMax-H3.mp4";
  const audioText = job.media?.has_audio ? "含原生音轨" : "未检测到音轨";
  $("videoInfo").textContent = `${job.width} × ${job.height} · 24fps · ${audioText} · Seed ${job.seed} · 批量独立片段，可从本段续接`;
  updateControls();
}

function installBatchSelection(entries, message) {
  state.batch.files = entries;
  state.batch.items = entries.map((entry, index) => ({
    index,
    path: entry.path,
    preparation: entry.preparation,
    status: "pending",
    statusText: "等待中",
    job: null,
  }));
  state.batch.validationError = null;
  state.batch.stopRequested = false;
  state.batch.currentJobId = null;
  batchCompliance.checked = false;
  $("batchProgress").max = Math.max(1, state.batch.items.length);
  $("batchProgress").value = 0;
  show("batchProgressWrap", false);
  $("batchSelectionStatus").textContent = message;
  startBatchButton.textContent = entries.length
    ? `开始批量生成（${entries.length} 段 × 约 5 秒）`
    : "开始批量生成";
  renderBatchQueue();
  updateControls();
}

async function scanLocalBatchFolder() {
  const folderPath = batchFolderPath.value.trim();
  setError("video");
  if (!folderPath) {
    batchFolderPath.focus();
    setError("video", "请先填写本机图片文件夹路径。");
    return;
  }
  state.batch.scanning = true;
  state.batch.files = [];
  state.batch.items = [];
  state.batch.validationError = "正在读取文件夹";
  batchCompliance.checked = false;
  $("batchSelectionStatus").textContent = `正在本机检查 ${folderPath}，超限照片会自动压缩为兼容副本；原图保留不动，请稍候……`;
  renderBatchQueue();
  updateControls();
  try {
    const payload = await api("/api/batch-folders/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: folderPath }),
    });
    const selection = payload.selection;
    if (!selection?.selection_id || !Array.isArray(selection.files) || !selection.files.length) {
      throw new Error("服务器没有返回可用的图片列表。");
    }
    const entries = selection.files.map((item) => ({
      selectionId: selection.selection_id,
      serverFileId: item.id,
      path: item.relative_path,
      size: item.size,
      width: item.width,
      height: item.height,
      preparation: item.preparation,
    }));
    batchFolder.value = "";
    localStorage.setItem("h3-batch-folder-path", folderPath);
    installBatchSelection(
      entries,
      `文件夹 ${selection.folder_name}：已导入 ${entries.length} 张图片，其中 ${entries.filter((entry) => entry.preparation?.compressed).length} 张已自动压缩；保持完整画面，原图未改动。`,
    );
  } catch (error) {
    state.batch.files = [];
    state.batch.items = [];
    state.batch.validationError = error.message;
    $("batchSelectionStatus").textContent = `读取失败：${error.message}`;
    startBatchButton.textContent = "开始批量生成";
    renderBatchQueue();
    setError("video", error.message);
  } finally {
    state.batch.scanning = false;
    updateControls();
  }
}

async function updateBatchSelection() {
  if (state.batch.running || state.batch.scanning) return;
  const allFiles = Array.from(batchFolder.files || []);
  const imageFiles = allFiles.filter((file) => BATCH_IMAGE_EXTENSION.test(batchFilePath(file)));
  const ignoredCount = allFiles.length - imageFiles.length;
  const invalidFiles = imageFiles.filter((file) => file.size <= 0 || file.size > 200 * 1024 * 1024);
  const sortedFiles = sortBatchFiles(imageFiles);
  const exceedsLimit = sortedFiles.length > BATCH_MAX_FILES;
  state.batch.files = exceedsLimit ? [] : sortedFiles;
  state.batch.validationError = exceedsLimit
    ? `一次最多处理 ${BATCH_MAX_FILES} 张图片；当前找到 ${sortedFiles.length} 张，请拆成较小的文件夹。`
    : (invalidFiles.length
      ? `${invalidFiles.length} 张图片为空文件或超过自动压缩的 200MB 输入上限，请重新选择。`
      : null);
  const ignoredText = ignoredCount ? `；已忽略 ${ignoredCount} 个非图片文件` : "";
  if (state.batch.validationError || !sortedFiles.length) {
    const error = state.batch.validationError || `没有找到可用的 PNG、JPEG 或 WebP 图片${ignoredText}。`;
    installBatchSelection([], error);
    state.batch.validationError = error;
    updateControls();
    return;
  }
  state.batch.scanning = true;
  state.batch.files = [];
  state.batch.items = [];
  setError("video");
  renderBatchQueue();
  updateControls();
  let selectionId = null;
  const entries = [];
  try {
    for (const [index, entry] of sortedFiles.entries()) {
      $("batchSelectionStatus").textContent = `正在本机检查并自动压缩 ${index + 1} / ${sortedFiles.length}：${entry.path}；保持完整画面，原图未改动……`;
      const form = new FormData();
      if (selectionId) form.append("selection_id", selectionId);
      form.append("image", entry.file, entry.file.name);
      const payload = await api("/api/batch-images/import", { method: "POST", body: form });
      const selection = payload.selection;
      const item = selection?.files?.[0];
      if (!selection?.selection_id || !item?.id) throw new Error("服务器没有返回照片导入记录。");
      selectionId = selection.selection_id;
      entries.push({selectionId, serverFileId: item.id, path: entry.path,
        size: item.size, width: item.width, height: item.height, preparation: item.preparation});
    }
    installBatchSelection(entries, `已导入 ${entries.length} 张照片，其中 ${entries.filter((entry) => entry.preparation?.compressed).length} 张已自动压缩；保持完整画面，原图未改动${ignoredText}。`);
  } catch (error) {
    installBatchSelection([], `照片导入失败：${error.message}`);
    state.batch.validationError = error.message;
    setError("video", error.message);
  } finally {
    state.batch.scanning = false;
    updateControls();
  }
}

async function inspectBatchImage(file) {
  if (file.size <= 0) throw new Error(`${batchFilePath(file)} 是空文件。`);
  if (file.size > 25 * 1024 * 1024) throw new Error(`${batchFilePath(file)} 超过 25MB。`);
  let width;
  let height;
  if (typeof createImageBitmap === "function") {
    const bitmap = await createImageBitmap(file);
    width = bitmap.width;
    height = bitmap.height;
    bitmap.close();
  } else {
    const objectUrl = URL.createObjectURL(file);
    try {
      const dimensions = await new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve({ width: image.naturalWidth, height: image.naturalHeight });
        image.onerror = () => reject(new Error("图片无法解码。"));
        image.src = objectUrl;
      });
      ({ width, height } = dimensions);
    } finally {
      URL.revokeObjectURL(objectUrl);
    }
  }
  if (!width || !height) throw new Error(`${batchFilePath(file)} 无法读取尺寸。`);
  if (width * height > 40_000_000) throw new Error(`${batchFilePath(file)} 超过 4000 万像素。`);
  return { width, height };
}

function batchFormData(entry, settings) {
  const form = new FormData();
  form.append("prompt", settings.prompt);
  form.append("character_lock", settings.characterLock);
  form.append("quality", settings.quality);
  form.append("aspect", settings.aspect);
  form.append("seed", settings.seed);
  if (entry.file) {
    form.append("image", entry.file, entry.file.name);
  } else {
    form.append("batch_selection_id", entry.selectionId);
    form.append("batch_file_id", entry.serverFileId);
  }
  return form;
}

async function recoverBatchSubmission(submittedAfter, expectedBatchItemId = null) {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    if (attempt > 1) await new Promise((resolve) => setTimeout(resolve, 900));
    try {
      const payload = await api("/api/status");
      state.csrfToken = payload.csrf_token;
      const active = payload.active_job;
      const exactBatchMatch = expectedBatchItemId && active?.batch_item_id === expectedBatchItemId;
      const recentBrowserUpload = !expectedBatchItemId && active?.id && Number(active.created_at || 0) >= submittedAfter - 2;
      if (exactBatchMatch || recentBrowserUpload) return active;
    } catch {
      // A read-only status query may be retried; the generation POST is never retried.
    }
  }
  return null;
}

async function waitForBatchVideoJob(jobId, item) {
  let readFailures = 0;
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 1800));
    let job;
    try {
      job = (await api(`/api/jobs/${jobId}`)).job;
      readFailures = 0;
    } catch (error) {
      readFailures += 1;
      if (error.status === 404) throw new Error("服务器已找不到当前任务，批次已停止。");
      if (readFailures < 3) {
        $("batchStatus").textContent = `暂时读不到当前任务状态（${readFailures} / 3），只会重试查询，不会重复提交视频。`;
      } else {
        $("batchStatus").textContent = "当前任务状态暂时无法确认；工作台会保持锁定并继续只读查询。可点击停止退出队列，不会重复提交视频。";
        if (state.batch.stopRequested) throw new Error("停止后仍无法确认当前任务状态。");
        await new Promise((resolve) => setTimeout(resolve, 3200));
      }
      continue;
    }
    item.job = job;
    item.status = BATCH_TERMINAL_STATUSES.has(job.status) ? job.status : "running";
    item.statusText = BATCH_TERMINAL_STATUSES.has(job.status) ? null : (job.stage || "生成中");
    const sampler = job.progress_max > 0 && job.progress_value > 0
      ? ` · 采样 ${job.progress_value} / ${job.progress_max}`
      : "";
    $("batchStatus").textContent = `正在处理 ${item.index + 1} / ${state.batch.items.length}：${item.path} · ${job.stage || "生成中"}${sampler}${state.batch.pauseRequested ? " · 本段完成后暂停" : ""}`;
    renderBatchQueue();
    if (BATCH_TERMINAL_STATUSES.has(job.status)) return job;
  }
}

function markUnstartedBatchItems() {
  state.batch.items.forEach((item) => {
    if (["pending", "checking"].includes(item.status)) {
      item.status = "cancelled";
      item.statusText = "— 未生成";
    }
  });
  renderBatchQueue();
}

function toggleBatchPause() {
  if (!state.batch.running || state.batch.stopRequested) return;
  state.batch.pauseRequested = !state.batch.pauseRequested;
  if (state.batch.pauseRequested) {
    $("batchStatus").textContent = "已请求暂停；已提交的视频会继续完成，之后不再提交下一张。";
  } else {
    state.batch.paused = false;
    $("batchStatus").textContent = "已继续批次，将从剩余队列接着生成。";
    state.batch.resumeWaiter?.();
    state.batch.resumeWaiter = null;
  }
  updateControls();
}

async function waitForBatchResume(completedCount) {
  while (state.batch.pauseRequested && !state.batch.stopRequested) {
    state.batch.paused = true;
    $("batchStatus").textContent = `批次已暂停：已完成 ${completedCount} / ${state.batch.items.length}。点击“继续生成”处理剩余图片，请保持本页面打开。`;
    updateControls();
    await new Promise((resolve) => { state.batch.resumeWaiter = resolve; });
    state.batch.resumeWaiter = null;
  }
  state.batch.paused = false;
}

async function startBatchGeneration() {
  setError("video");
  const text = prompt.value.trim();
  if (!text) { prompt.focus(); setError("video", "请先输入视频提示词。"); return; }
  if (!state.batch.files.length || state.batch.validationError) {
    batchFolder.focus();
    setError("video", state.batch.validationError || "请先选择包含参考图的文件夹。");
    return;
  }
  if (!batchCompliance.checked) {
    batchCompliance.focus();
    setError("video", "开始批量生成前，请确认素材与提示词可合法使用。");
    return;
  }
  if (anyBusy()) { setError("video", "已有本地 AI 任务正在运行，请等待它完成。"); return; }

  state.batch.running = true;
  state.batch.pauseRequested = false;
  state.batch.paused = false;
  state.batch.resumeWaiter = null;
  state.batch.stopRequested = false;
  state.batch.cancelSent = false;
  state.batch.currentJobId = null;
  state.batch.frozenSettings = {
    prompt: text,
    characterLock: characterLock.value,
    quality: $("videoQuality").value,
    aspect: $("videoAspect").value,
    seed: $("videoSeed").value.trim(),
  };
  state.batch.items.forEach((item) => { item.status = "pending"; item.statusText = "等待中"; item.job = null; });
  $("batchProgress").max = state.batch.items.length;
  $("batchProgress").value = 0;
  show("batchProgressWrap", true);
  renderBatchQueue();
  updateControls();
  pauseBatchButton.focus();

  let completedCount = 0;
  let failureMessage = "";
  let recoveredSubmissionSeen = false;
  try {
    const statusPayload = await api("/api/status");
    state.csrfToken = statusPayload.csrf_token;
    if (!statusPayload.ready || statusPayload.active_job || statusPayload.active_image_job) {
      throw new Error("工作台尚未空闲，批量任务没有开始。");
    }

    for (let index = 0; index < state.batch.files.length; index += 1) {
      const entry = state.batch.files[index];
      const item = state.batch.items[index];
      item.status = "checking";
      item.statusText = "检查中";
      $("batchStatus").textContent = `正在预检 ${index + 1} / ${state.batch.items.length}：${item.path}`;
      renderBatchQueue();
      try {
        if (entry.file) {
          await inspectBatchImage(entry.file);
        } else if (!entry.selectionId || !entry.serverFileId) {
          throw new Error("本机文件编号无效，请重新读取文件夹。");
        }
      } catch (error) {
        item.status = "failed";
        item.statusText = "✕ 预检失败";
        renderBatchQueue();
        throw new Error(error.message.includes(item.path) ? error.message : `${item.path}：${error.message}`);
      }
      item.status = "pending";
      item.statusText = "等待中";
      if (state.batch.stopRequested) break;
    }

    for (let index = 0; index < state.batch.files.length; index += 1) {
      if (state.batch.stopRequested) break;
      await waitForBatchResume(completedCount);
      if (state.batch.stopRequested) break;
      const entry = state.batch.files[index];
      const item = state.batch.items[index];
      item.status = "uploading";
      item.statusText = "上传中";
      $("batchStatus").textContent = `正在提交 ${index + 1} / ${state.batch.items.length}：${item.path}`;
      renderBatchQueue();
      let submitted;
      let recoveredSubmission = false;
      const submittedAfter = Date.now() / 1000;
      try {
        submitted = (await api("/api/generate", {
          method: "POST",
          body: batchFormData(entry, state.batch.frozenSettings),
        })).job;
        if (!submitted?.id) throw new Error("服务器没有返回可识别的任务编号。");
      } catch (error) {
        if (!error.status) {
          submitted = await recoverBatchSubmission(submittedAfter, entry.serverFileId || null);
          recoveredSubmission = Boolean(submitted);
          recoveredSubmissionSeen ||= recoveredSubmission;
        }
        if (!submitted) {
          item.status = "failed";
          item.statusText = "✕ 提交失败";
          failureMessage = `${item.path}：${error.message}。为避免重复生成，剩余队列已停止。`;
          throw error;
        }
        failureMessage = `${item.path}：提交响应曾中断，已通过只读状态查询找回当前任务；本张结束后停止剩余队列，且没有重发生成请求。`;
      }
      state.batch.currentJobId = submitted.id;
      state.videoJobId = submitted.id;
      item.job = submitted;
      item.status = "running";
      item.statusText = submitted.stage || "生成中";
      renderBatchQueue();
      if (state.batch.stopRequested && !state.batch.cancelSent) {
        state.batch.cancelSent = true;
        $("batchStatus").textContent = "已取得当前任务编号，正在补发取消；剩余图片不会提交。";
        try {
          await api(`/api/jobs/${submitted.id}/cancel`, { method: "POST" });
        } catch (error) {
          $("batchStatus").textContent = `已停止提交剩余图片；当前任务取消请求失败，将等待它结束：${error.message}`;
        }
      }
      let finalJob;
      try {
        finalJob = await waitForBatchVideoJob(submitted.id, item);
      } catch (error) {
        item.status = "failed";
        item.statusText = "✕ 状态确认失败";
        failureMessage = `${item.path}：${error.message}。剩余队列已停止。`;
        renderBatchQueue();
        throw error;
      }
      state.batch.currentJobId = null;
      if (finalJob.status !== "completed" || !finalJob.video_url) {
        const stopped = finalJob.status === "cancelled" && state.batch.stopRequested;
        item.status = stopped ? "cancelled" : "failed";
        item.statusText = stopped ? "— 已停止" : "✕ 生成失败";
        failureMessage = stopped ? "" : `${item.path}：${finalJob.error || "任务没有产生可用视频"}。剩余队列已停止。`;
        break;
      }
      item.status = "completed";
      item.statusText = "✓ 完成";
      item.job = finalJob;
      completedCount += 1;
      $("batchProgress").value = completedCount;
      previewBatchVideo(finalJob);
      renderBatchQueue();
      if (recoveredSubmission) break;
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  } catch (error) {
    failureMessage ||= error.message;
  } finally {
    state.batch.running = false;
    state.batch.pauseRequested = false;
    state.batch.paused = false;
    state.batch.resumeWaiter = null;
    state.batch.currentJobId = null;
    if (completedCount < state.batch.items.length) markUnstartedBatchItems();
    const stoppedText = state.batch.stopRequested
      ? "批次已停止"
      : (recoveredSubmissionSeen ? "批次为避免重复已停止" : (failureMessage ? "批次因错误停止" : "批量生成完成"));
    $("batchStatus").textContent = `${stoppedText}：已完成 ${completedCount} / ${state.batch.items.length}。${failureMessage}`.trim();
    if (failureMessage) setError("video", failureMessage);
    updateControls();
    if (!startBatchButton.disabled) startBatchButton.focus();
  }
}

async function stopBatchGeneration() {
  if (!state.batch.running || state.batch.stopRequested) return;
  state.batch.stopRequested = true;
  state.batch.pauseRequested = false;
  state.batch.resumeWaiter?.();
  state.batch.resumeWaiter = null;
  updateControls();
  stopBatchButton.disabled = true;
  $("batchStatus").textContent = "正在停止当前任务；已完成的视频会保留，剩余图片不会提交。";
  if (state.batch.currentJobId && !state.batch.cancelSent) {
    state.batch.cancelSent = true;
    try {
      await api(`/api/jobs/${state.batch.currentJobId}/cancel`, { method: "POST" });
    } catch (error) {
      $("batchStatus").textContent = `已停止提交剩余图片；当前任务取消请求失败，将等待它结束：${error.message}`;
    }
  }
}

function renderImageBatchQueue() {
  const queue = $("imageBatchQueue");
  const statusLabels = {
    pending: "等待中",
    uploading: "提交中",
    running: "生成中",
    completed: "✓ 完成",
    failed: "✕ 失败",
    cancelled: "— 未生成",
  };
  const rows = state.imageBatch.items.map((item) => {
    const row = document.createElement("li");
    row.className = "batch-item";
    const name = document.createElement("span");
    name.className = "batch-item-name";
    name.textContent = `${String(item.index + 1).padStart(3, "0")} · ${item.path}`;
    const status = document.createElement("strong");
    const visualStatus = ["completed", "failed", "cancelled"].includes(item.status)
      ? item.status
      : (item.status === "pending" ? "" : "running");
    status.className = `batch-item-status ${visualStatus}`.trim();
    status.textContent = item.statusText || statusLabels[item.status] || item.status;
    row.append(name, status);
    if (item.preparation?.compressed) {
      const detail = document.createElement("span");
      detail.className = "batch-image-preparation";
      const mb = (bytes) => `${(bytes / 1024 / 1024).toFixed(2)}MB`;
      detail.textContent = `已自动压缩 · ${item.preparation.original_width}×${item.preparation.original_height} / ${mb(item.preparation.original_size)} → ${item.preparation.width}×${item.preparation.height} / ${mb(item.preparation.size)} · 完整画面`;
      row.append(detail);
    }
    if (item.job?.image_url) {
      const actions = document.createElement("span");
      actions.className = "batch-item-actions";
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "text-button";
      preview.textContent = "预览";
      preview.setAttribute("aria-label", `预览 ${item.path} 生成的图片`);
      preview.addEventListener("click", () => previewImageBatchResult(item.job, item.path));
      const download = document.createElement("a");
      download.href = item.job.image_url;
      download.download = item.job.filename || "Qwen-Image.png";
      download.textContent = "下载";
      download.setAttribute("aria-label", `下载 ${item.path} 生成的图片`);
      actions.append(preview, download);
      row.append(actions);
    }
    return row;
  });
  queue.replaceChildren(...rows);
  show("imageBatchQueue", Boolean(rows.length));
}

function previewImageBatchResult(job, sourcePath = "") {
  if (!job?.image_url) return;
  show("emptyImageResult", false);
  show("imageJobStatus", false);
  show("imageResult", true);
  state.generatedImageUrl = job.image_url;
  $("generatedImage").src = job.image_url;
  $("imageDownloadLink").href = job.image_url;
  $("imageDownloadLink").download = job.filename || "Qwen-Image.png";
  const cutoutCount = job.reference_preprocess?.processed_slots?.length || 0;
  const cutout = cutoutCount ? ` · 已本地抠图 ${cutoutCount} 张` : "";
  const source = sourcePath || job.batch_relative_path || "批量参考图";
  $("imageInfo").textContent = `${job.width} × ${job.height} · Seed ${job.seed}${cutout} · 来源 ${source}`;
}

function installImageBatchSelection(entries, message) {
  state.imageBatch.files = entries;
  state.imageBatch.items = entries.map((entry, index) => ({
    index,
    path: entry.path,
    preparation: entry.preparation,
    status: "pending",
    statusText: "等待中",
    job: null,
  }));
  state.imageBatch.validationError = null;
  state.imageBatch.stopRequested = false;
  state.imageBatch.currentJobId = null;
  $("imageBatchProgress").max = Math.max(1, entries.length);
  $("imageBatchProgress").value = 0;
  show("imageBatchProgressWrap", false);
  $("imageBatchSelectionStatus").textContent = message;
  startImageBatchButton.textContent = entries.length
    ? `开始批量生成（${entries.length} 张）`
    : "开始批量生成图片";
  renderImageBatchQueue();
  updateControls();
}

async function scanLocalImageBatchFolder() {
  const folderPath = imageBatchFolderPath.value.trim();
  setError("image");
  if (!folderPath) {
    imageBatchFolderPath.focus();
    setError("image", "请先填写本机参考图文件夹路径。");
    return;
  }
  if (anyBusy()) {
    setError("image", "已有本地 AI 任务正在运行，请等待它完成。");
    return;
  }
  state.imageBatch.scanning = true;
  state.imageBatch.files = [];
  state.imageBatch.items = [];
  state.imageBatch.validationError = "正在读取文件夹";
  $("imageBatchSelectionStatus").textContent = `正在本机检查 ${folderPath}，超限照片会自动压缩；原图保留不动，请稍候……`;
  renderImageBatchQueue();
  updateControls();
  try {
    const payload = await api("/api/batch-folders/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: folderPath }),
    });
    const selection = payload.selection;
    if (!selection?.selection_id || !Array.isArray(selection.files) || !selection.files.length) {
      throw new Error("服务器没有返回可用的参考图列表。");
    }
    const entries = selection.files.map((item) => ({
      selectionId: selection.selection_id,
      serverFileId: item.id,
      path: item.relative_path,
      size: item.size,
      width: item.width,
      height: item.height,
      preparation: item.preparation,
    }));
    imageBatchFolder.value = "";
    localStorage.setItem("h3-image-batch-folder-path", folderPath);
    installImageBatchSelection(
      entries,
      `文件夹 ${selection.folder_name}：已导入 ${entries.length} 张参考图，其中 ${entries.filter((entry) => entry.preparation?.compressed).length} 张已自动压缩；原图未改动。`,
    );
  } catch (error) {
    state.imageBatch.files = [];
    state.imageBatch.items = [];
    state.imageBatch.validationError = error.message;
    $("imageBatchSelectionStatus").textContent = `读取失败：${error.message}`;
    startImageBatchButton.textContent = "开始批量生成图片";
    renderImageBatchQueue();
    setError("image", error.message);
  } finally {
    state.imageBatch.scanning = false;
    updateControls();
  }
}

async function updateImageBatchSelection() {
  if (anyBusy()) return;
  const allFiles = Array.from(imageBatchFolder.files || []);
  const imageFiles = allFiles.filter((file) => BATCH_IMAGE_EXTENSION.test(batchFilePath(file)));
  const ignoredCount = allFiles.length - imageFiles.length;
  const invalidFiles = imageFiles.filter((file) => file.size <= 0 || file.size > 200 * 1024 * 1024);
  const sortedFiles = sortBatchFiles(imageFiles);
  const exceedsLimit = sortedFiles.length > BATCH_MAX_FILES;
  const validationError = exceedsLimit
    ? `一次最多处理 ${BATCH_MAX_FILES} 张参考图；当前选择 ${sortedFiles.length} 张。`
    : (invalidFiles.length ? `${invalidFiles.length} 张图片为空文件或超过 200MB 输入上限。` : null);
  const ignoredText = ignoredCount ? `；已忽略 ${ignoredCount} 个非图片文件` : "";
  if (validationError || !sortedFiles.length) {
    const error = validationError || `没有找到可用的 PNG、JPEG 或 WebP 图片${ignoredText}。`;
    installImageBatchSelection([], error);
    state.imageBatch.validationError = error;
    updateControls();
    return;
  }

  state.imageBatch.scanning = true;
  state.imageBatch.files = [];
  state.imageBatch.items = [];
  setError("image");
  renderImageBatchQueue();
  updateControls();
  let selectionId = null;
  const entries = [];
  try {
    for (const [index, entry] of sortedFiles.entries()) {
      $("imageBatchSelectionStatus").textContent = `正在本机导入 ${index + 1} / ${sortedFiles.length}：${entry.path}；超限照片会自动压缩，原图不变……`;
      const form = new FormData();
      if (selectionId) form.append("selection_id", selectionId);
      form.append("image", entry.file, entry.file.name);
      const payload = await api("/api/batch-images/import", { method: "POST", body: form });
      const selection = payload.selection;
      const item = selection?.files?.[0];
      if (!selection?.selection_id || !item?.id) throw new Error("服务器没有返回参考图导入记录。");
      selectionId = selection.selection_id;
      entries.push({
        selectionId,
        serverFileId: item.id,
        path: entry.path,
        size: item.size,
        width: item.width,
        height: item.height,
        preparation: item.preparation,
      });
    }
    installImageBatchSelection(
      entries,
      `已导入 ${entries.length} 张参考图，其中 ${entries.filter((entry) => entry.preparation?.compressed).length} 张已自动压缩；原图未改动${ignoredText}。`,
    );
  } catch (error) {
    installImageBatchSelection([], `参考图导入失败：${error.message}`);
    state.imageBatch.validationError = error.message;
    setError("image", error.message);
  } finally {
    state.imageBatch.scanning = false;
    updateControls();
  }
}

function imageBatchFormData(entry, settings) {
  const form = new FormData();
  form.append("mode", settings.mode);
  form.append("image_engine", settings.imageEngine || "qwen21");
  form.append("prompt", settings.prompt);
  form.append("character_lock", settings.characterLock);
  form.append("negative_prompt", settings.negativePrompt);
  form.append("quality", settings.quality);
  form.append("reference_preprocess", settings.cutout ? "person_cutout" : "none");
  form.append("seed", settings.seed);
  form.append("batch_selection_id", entry.selectionId);
  form.append("batch_file_id", entry.serverFileId);
  return form;
}

async function recoverImageBatchSubmission(submittedAfter, expectedBatchItemId) {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    if (attempt > 1) await new Promise((resolve) => setTimeout(resolve, 900));
    try {
      const payload = await api("/api/status");
      state.csrfToken = payload.csrf_token;
      const active = payload.active_image_job;
      const exactBatchMatch = active?.batch_item_id === expectedBatchItemId;
      const recentMatch = active?.id && Number(active.created_at || 0) >= submittedAfter - 2;
      if (exactBatchMatch || recentMatch) return active;
    } catch {
      // Only status reads are retried; the image generation POST is never retried.
    }
  }
  return null;
}

async function waitForImageBatchJob(jobId, item) {
  let readFailures = 0;
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 1800));
    let job;
    try {
      job = (await api(`/api/images/jobs/${jobId}`)).job;
      readFailures = 0;
    } catch (error) {
      readFailures += 1;
      if (error.status === 404) throw new Error("服务器已找不到当前图片任务，批次已停止。");
      if (readFailures < 3) {
        $("imageBatchStatus").textContent = `暂时读不到当前任务状态（${readFailures} / 3），只重试查询，不会重复提交图片。`;
      } else {
        $("imageBatchStatus").textContent = "当前图片状态暂时无法确认；工作台会继续只读查询。可点击停止，不会重复提交。";
        if (state.imageBatch.stopRequested) throw new Error("停止后仍无法确认当前图片任务状态。");
        await new Promise((resolve) => setTimeout(resolve, 3200));
      }
      continue;
    }
    item.job = job;
    item.status = BATCH_TERMINAL_STATUSES.has(job.status) ? job.status : "running";
    item.statusText = BATCH_TERMINAL_STATUSES.has(job.status) ? null : (job.stage || "生成中");
    const sampler = job.progress_max > 0 && job.progress_value > 0
      ? ` · 采样 ${job.progress_value} / ${job.progress_max}`
      : "";
    $("imageBatchStatus").textContent = `正在处理 ${item.index + 1} / ${state.imageBatch.items.length}：${item.path} · ${job.stage || "生成中"}${sampler}${state.imageBatch.pauseRequested ? " · 本张完成后暂停" : ""}`;
    renderImageBatchQueue();
    if (BATCH_TERMINAL_STATUSES.has(job.status)) return job;
  }
}

function markUnstartedImageBatchItems() {
  state.imageBatch.items.forEach((item) => {
    if (["pending", "uploading"].includes(item.status)) {
      item.status = "cancelled";
      item.statusText = "— 未生成";
    }
  });
  renderImageBatchQueue();
}

function toggleImageBatchPause() {
  if (!state.imageBatch.running || state.imageBatch.stopRequested) return;
  state.imageBatch.pauseRequested = !state.imageBatch.pauseRequested;
  if (state.imageBatch.pauseRequested) {
    $("imageBatchStatus").textContent = "已请求暂停；当前图片会继续完成，之后不再提交下一张。";
  } else {
    state.imageBatch.paused = false;
    $("imageBatchStatus").textContent = "已继续批次，将从剩余参考图接着生成。";
    state.imageBatch.resumeWaiter?.();
    state.imageBatch.resumeWaiter = null;
  }
  updateControls();
}

async function waitForImageBatchResume(completedCount) {
  while (state.imageBatch.pauseRequested && !state.imageBatch.stopRequested) {
    state.imageBatch.paused = true;
    $("imageBatchStatus").textContent = `图片批次已暂停：已完成 ${completedCount} / ${state.imageBatch.items.length}。点击“继续生成”处理剩余参考图。`;
    updateControls();
    await new Promise((resolve) => { state.imageBatch.resumeWaiter = resolve; });
    state.imageBatch.resumeWaiter = null;
  }
  state.imageBatch.paused = false;
}

async function startImageBatchGeneration() {
  setError("image");
  const text = prompt.value.trim();
  if (!text) {
    prompt.focus();
    setError("image", "请先输入这批图片共同使用的提示词。");
    return;
  }
  if (!state.imageBatch.files.length || state.imageBatch.validationError) {
    imageBatchFolder.focus();
    setError("image", state.imageBatch.validationError || "请先批量导入参考图。");
    return;
  }
  if (anyBusy()) {
    setError("image", "已有本地 AI 任务正在运行，请等待它完成。");
    return;
  }

  state.imageBatch.running = true;
  state.imageBatch.stopRequested = false;
  state.imageBatch.pauseRequested = false;
  state.imageBatch.paused = false;
  state.imageBatch.resumeWaiter = null;
  state.imageBatch.cancelSent = false;
  state.imageBatch.currentJobId = null;
  state.imageBatch.frozenSettings = {
    mode: $("imageMode").value,
    imageEngine: $("imageEngine").value,
    prompt: text,
    characterLock: characterLock.value,
    negativePrompt: state.optimization?.negative_prompt || "",
    quality: $("imageQuality").value,
    seed: $("imageSeed").value.trim(),
    cutout: $("imageBatchCutout").checked,
  };
  state.imageBatch.items.forEach((item) => {
    item.status = "pending";
    item.statusText = "等待中";
    item.job = null;
  });
  $("imageBatchProgress").max = state.imageBatch.items.length;
  $("imageBatchProgress").value = 0;
  show("imageBatchProgressWrap", true);
  renderImageBatchQueue();
  updateControls();
  pauseImageBatchButton.focus();

  let completedCount = 0;
  let failureMessage = "";
  let recoveredSubmissionSeen = false;
  try {
    const statusPayload = await api("/api/status");
    state.csrfToken = statusPayload.csrf_token;
    if (!statusPayload.image_models?.ready || statusPayload.active_job || statusPayload.active_image_job || statusPayload.active_sequence) {
      throw new Error("本地图片模型尚未空闲，批次没有开始。");
    }

    for (let index = 0; index < state.imageBatch.files.length; index += 1) {
      if (state.imageBatch.stopRequested) break;
      await waitForImageBatchResume(completedCount);
      if (state.imageBatch.stopRequested) break;
      const entry = state.imageBatch.files[index];
      const item = state.imageBatch.items[index];
      if (!entry.selectionId || !entry.serverFileId) {
        item.status = "failed";
        item.statusText = "✕ 凭据无效";
        throw new Error(`${item.path}：本机参考图编号无效，请重新导入。`);
      }
      item.status = "uploading";
      item.statusText = "提交中";
      $("imageBatchStatus").textContent = `正在提交 ${index + 1} / ${state.imageBatch.items.length}：${item.path}`;
      renderImageBatchQueue();
      let submitted;
      let recoveredSubmission = false;
      const submittedAfter = Date.now() / 1000;
      try {
        submitted = (await api("/api/images/generate", {
          method: "POST",
          body: imageBatchFormData(entry, state.imageBatch.frozenSettings),
        })).job;
        if (!submitted?.id) throw new Error("服务器没有返回可识别的图片任务编号。");
      } catch (error) {
        if (!error.status) {
          submitted = await recoverImageBatchSubmission(submittedAfter, entry.serverFileId);
          recoveredSubmission = Boolean(submitted);
          recoveredSubmissionSeen ||= recoveredSubmission;
        }
        if (!submitted) {
          item.status = "failed";
          item.statusText = "✕ 提交失败";
          failureMessage = `${item.path}：${error.message}。为避免重复生成，剩余队列已停止。`;
          throw error;
        }
        failureMessage = `${item.path}：提交响应曾中断，已通过只读查询找回当前任务；本张结束后停止剩余队列，且没有重发。`;
      }
      state.imageBatch.currentJobId = submitted.id;
      state.imageJobId = submitted.id;
      item.job = submitted;
      item.status = "running";
      item.statusText = submitted.stage || "生成中";
      renderImageBatchQueue();
      if (state.imageBatch.stopRequested && !state.imageBatch.cancelSent) {
        state.imageBatch.cancelSent = true;
        try {
          await api(`/api/images/jobs/${submitted.id}/cancel`, { method: "POST" });
        } catch (error) {
          $("imageBatchStatus").textContent = `已停止提交剩余参考图；当前图片取消失败，将等待它结束：${error.message}`;
        }
      }
      let finalJob;
      try {
        finalJob = await waitForImageBatchJob(submitted.id, item);
      } catch (error) {
        item.status = "failed";
        item.statusText = "✕ 状态确认失败";
        failureMessage = `${item.path}：${error.message}。剩余队列已停止。`;
        renderImageBatchQueue();
        throw error;
      }
      state.imageBatch.currentJobId = null;
      if (finalJob.status !== "completed" || !finalJob.image_url) {
        const stopped = finalJob.status === "cancelled" && state.imageBatch.stopRequested;
        item.status = stopped ? "cancelled" : "failed";
        item.statusText = stopped ? "— 已停止" : "✕ 生成失败";
        failureMessage = stopped ? "" : `${item.path}：${finalJob.error || "任务没有产生可用图片"}。剩余队列已停止。`;
        break;
      }
      item.status = "completed";
      item.statusText = "✓ 完成";
      item.job = finalJob;
      completedCount += 1;
      $("imageBatchProgress").value = completedCount;
      previewImageBatchResult(finalJob, item.path);
      renderImageBatchQueue();
      if (recoveredSubmission) break;
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  } catch (error) {
    failureMessage ||= error.message;
  } finally {
    state.imageBatch.running = false;
    state.imageBatch.pauseRequested = false;
    state.imageBatch.paused = false;
    state.imageBatch.resumeWaiter = null;
    state.imageBatch.currentJobId = null;
    if (completedCount < state.imageBatch.items.length) markUnstartedImageBatchItems();
    const stoppedText = state.imageBatch.stopRequested
      ? "图片批次已停止"
      : (recoveredSubmissionSeen ? "图片批次为避免重复已停止" : (failureMessage ? "图片批次因错误停止" : "批量图片生成完成"));
    $("imageBatchStatus").textContent = `${stoppedText}：已完成 ${completedCount} / ${state.imageBatch.items.length}。${failureMessage}`.trim();
    state.imageBatch.validationError = "本批参考图已使用；如需再次生成，请重新导入。";
    if (failureMessage) setError("image", failureMessage);
    updateControls();
  }
}

async function stopImageBatchGeneration() {
  if (!state.imageBatch.running || state.imageBatch.stopRequested) return;
  state.imageBatch.stopRequested = true;
  state.imageBatch.pauseRequested = false;
  state.imageBatch.resumeWaiter?.();
  state.imageBatch.resumeWaiter = null;
  updateControls();
  $("imageBatchStatus").textContent = "正在停止当前图片；已完成结果会保留，剩余参考图不会提交。";
  if (state.imageBatch.currentJobId && !state.imageBatch.cancelSent) {
    state.imageBatch.cancelSent = true;
    try {
      await api(`/api/images/jobs/${state.imageBatch.currentJobId}/cancel`, { method: "POST" });
    } catch (error) {
      $("imageBatchStatus").textContent = `已停止提交剩余参考图；当前图片取消失败，将等待它结束：${error.message}`;
    }
  }
}

async function generateVideo(continueFromJobId = null) {
  if (anyBusy()) { setError("video", "已有本地 AI 任务正在运行，请完成后再生成。"); return; }
  continueFromJobId ||= state.importedReferenceId;
  setError("video");
  const text = prompt.value.trim();
  if (!text) { prompt.focus(); setError("video", "请先输入提示词。"); return; }
  if (continueFromJobId && videoImage.files[0]) { videoImage.value = ""; previewVideoFile(null); }
  const form = new FormData();
  form.append("prompt", text);
  form.append("character_lock", characterLock.value);
  form.append("quality", $("videoQuality").value);
  form.append("aspect", $("videoAspect").value);
  form.append("seed", $("videoSeed").value.trim());
  if (continueFromJobId) form.append("continue_from_job_id", continueFromJobId);
  else if (videoImage.files[0]) form.append("image", videoImage.files[0], videoImage.files[0].name);
  show("videoResult", false);
  show("emptyResult", false);
  show("jobStatus", true);
  $("jobStatus").classList.add("indeterminate");
  $("statusText").textContent = videoImage.files[0] ? "检查参考图并准备工作流" : "准备工作流";
  $("statusDetail").textContent = videoImage.files[0]
    ? "过大的照片会自动压缩后再生成；保留完整画面，原图不变，请稍候。"
    : "所有内容都在本机处理。";
  try {
    const payload = await api("/api/generate", { method: "POST", body: form });
    state.videoJobId = payload.job.id;
    renderVideoJob(payload.job);
    startVideoPolling();
  } catch (error) {
    if (error.payload?.job) {
      state.videoJobId = error.payload.job.id;
      renderVideoJob(error.payload.job);
      startVideoPolling();
    } else setError("video", error.message);
  }
  updateControls();
}

async function generateImage() {
  setError("image");
  const mode = $("imageMode").value;
  const refs = [$("reference1"), $("reference2"), $("reference3")];
  const hasReferences = refs.some((input) => Boolean(input.files[0]));
  const text = prompt.value.trim();
  if (!text) {
    prompt.focus();
    setError("image", hasReferences ? "请写明希望保留的人物特征，以及想改变的画面。" : "请先输入角色或画面提示词。");
    return;
  }
  if (hasReferences && !refs[0].files[0]) {
    setError("image", "请先上传第 1 张主要人物参考，再添加第 2、3 张补充图。");
    return;
  }
  if (mode === "edit" && !refs[0].files[0]) {
    setError("image", "固定人物模式需要第 1 张角色母版。");
    return;
  }
  const form = new FormData();
  form.append("mode", mode);
  form.append("image_engine", $("imageEngine").value);
  form.append("prompt", text);
  form.append("character_lock", characterLock.value);
  form.append("negative_prompt", state.optimization?.negative_prompt || "");
  form.append("quality", $("imageQuality").value);
  form.append("reference_preprocess", hasReferences && $("referenceCutout").checked ? "person_cutout" : "none");
  if (mode === "create" && !hasReferences) form.append("aspect", $("imageAspect").value);
  form.append("seed", $("imageSeed").value.trim());
  const fields = ["reference_master", "reference_pose", "reference_detail"];
  refs.forEach((input, index) => {
    if (input.files[0]) form.append(fields[index], input.files[0], `reference-${index + 1}-${input.files[0].name}`);
  });
  show("imageResult", false);
  show("emptyImageResult", false);
  show("imageJobStatus", true);
  $("imageJobStatus").classList.add("indeterminate");
  $("imageStatusText").textContent = "准备图片工作流";
  $("imageStatusDetail").textContent = hasReferences && $("referenceCutout").checked
    ? "过大的照片先自动压缩，再在 CPU 上逐张抠出人物，最后启动图片模型；原图不变。"
    : (hasReferences
      ? "过大的照片会自动压缩后交给本地 Qwen Edit；保留完整画面，原图不变，第 1 张是主要人物依据。"
      : "图片引擎会按需启动，并与视频模型错峰使用显存。");
  try {
    const payload = await api("/api/images/generate", { method: "POST", body: form });
    state.imageJobId = payload.job.id;
    renderImageJob(payload.job);
    startImagePolling();
  } catch (error) { setError("image", error.message); }
  updateControls();
}

async function optimizePrompt() {
  const text = prompt.value.trim();
  if (!text) { prompt.focus(); show("optimizerStatus", true); $("optimizerStatus").textContent = "请先写下一个画面或动作。"; return; }
  const mode = state.workspace === "image" ? "image" : (state.continueFromJobId ? "continuation" : "video");
  state.optimizerBusy = true;
  updateControls();
  show("optimizerResult", false);
  show("optimizerStatus", true);
  const modelProfile = $("promptModelProfile").value;
  $("optimizerStatus").textContent = modelProfile === "creative"
    ? "本地自由创作模型正在整理提示词，第一次加载大约需要半分钟……"
    : "本地稳定模型正在整理提示词，第一次加载大约需要半分钟……";
  try {
    const payload = await api("/api/optimize-prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, prompt: text, character_lock: characterLock.value, model_profile: modelProfile }),
    });
    state.optimization = payload.optimization;
    $("optimizedImagePrompt").textContent = state.optimization.image_prompt;
    $("optimizedVideoPrompt").textContent = state.optimization.video_prompt;
    $("optimizedContinuationPrompt").textContent = state.optimization.continuation_prompt;
    $("optimizedNegativePrompt").textContent = state.optimization.negative_prompt || "无";
    $("optimizerWarnings").replaceChildren(...(state.optimization.warnings || []).map((warning) => {
      const li = document.createElement("li"); li.textContent = warning; return li;
    }));
    $("optimizerStatus").textContent = `完成 · ${payload.model_label || payload.model || "本地模型"} · 已自动释放显存`;
    show("optimizerResult", true);
  } catch (error) {
    $("optimizerStatus").textContent = error.message;
  } finally {
    state.optimizerBusy = false;
    updateControls();
  }
}

async function useGeneratedAsMaster() {
  if (!state.generatedImageUrl) return;
  try {
    const response = await fetch(state.generatedImageUrl);
    if (!response.ok) throw new Error(`读取图片失败（HTTP ${response.status}）`);
    const blob = await response.blob();
    const file = new File([blob], "frozen-character-master.png", { type: blob.type || "image/png" });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    clearReference(1);
    clearReference(2);
    $("reference1").files = transfer.files;
    previewReference(0, file);
    $("imageMode").value = "edit";
    updateImageModeControls();
    $("useAsMasterButton").textContent = "已设为固定人物母版";
  } catch (error) { setError("image", error.message); }
}

prompt.addEventListener("input", () => { $("promptCount").textContent = `${prompt.value.length} / 5000`; });
characterLock.value = localStorage.getItem("h3-character-lock") || "";
characterLock.addEventListener("input", () => localStorage.setItem("h3-character-lock", characterLock.value));
const savedPromptProfile = localStorage.getItem("h3-prompt-model-profile");
if (["stable", "creative"].includes(savedPromptProfile)) $("promptModelProfile").value = savedPromptProfile;
$("promptModelProfile").addEventListener("change", () => { updatePromptModelControls(); updateControls(); });

document.querySelectorAll("[data-workspace]").forEach((button) => button.addEventListener("click", () => switchWorkspace(button.dataset.workspace)));
$("optimizeButton").addEventListener("click", optimizePrompt);
$("applyCharacterLock").addEventListener("click", () => {
  if (!state.optimization) return;
  characterLock.value = state.optimization.character_lock;
  localStorage.setItem("h3-character-lock", characterLock.value);
});
document.querySelectorAll("[data-apply-prompt]").forEach((button) => button.addEventListener("click", () => {
  if (!state.optimization) return;
  const kind = button.dataset.applyPrompt;
  const fields = { image: "image_prompt", video: "video_prompt", continuation: "continuation_prompt" };
  prompt.value = state.optimization[fields[kind]];
  prompt.dispatchEvent(new Event("input"));
  switchWorkspace(kind === "image" ? "image" : "video");
}));

videoImage.addEventListener("change", () => previewVideoFile(videoImage.files[0]));
$("clearImage").addEventListener("click", () => { videoImage.value = ""; previewVideoFile(null); });
batchFolderPath.value = localStorage.getItem("h3-batch-folder-path") || batchFolderPath.value;
batchFolderPath.addEventListener("input", updateControls);
batchFolderPath.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !scanBatchFolderButton.disabled) scanLocalBatchFolder();
});
scanBatchFolderButton.addEventListener("click", scanLocalBatchFolder);
batchFolder.addEventListener("click", () => { if (!state.batch.running) batchFolder.value = ""; });
batchFolder.addEventListener("change", updateBatchSelection);
batchCompliance.addEventListener("change", updateControls);
startBatchButton.addEventListener("click", startBatchGeneration);
stopBatchButton.addEventListener("click", stopBatchGeneration);
pauseBatchButton.addEventListener("click", toggleBatchPause);
imageBatchFolderPath.value = localStorage.getItem("h3-image-batch-folder-path") || imageBatchFolderPath.value;
imageBatchFolderPath.addEventListener("input", updateControls);
imageBatchFolderPath.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !scanImageBatchFolderButton.disabled) scanLocalImageBatchFolder();
});
scanImageBatchFolderButton.addEventListener("click", scanLocalImageBatchFolder);
imageBatchFolder.addEventListener("click", () => { if (!state.imageBatch.running) imageBatchFolder.value = ""; });
imageBatchFolder.addEventListener("change", updateImageBatchSelection);
$("imageBatchCutout").addEventListener("change", updateControls);
startImageBatchButton.addEventListener("click", startImageBatchGeneration);
stopImageBatchButton.addEventListener("click", stopImageBatchGeneration);
pauseImageBatchButton.addEventListener("click", toggleImageBatchPause);
["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add("dragging"); }));
["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove("dragging"); }));
dropZone.addEventListener("drop", (event) => {
  const file = event.dataTransfer.files[0];
  if (!file) return;
  const transfer = new DataTransfer(); transfer.items.add(file); videoImage.files = transfer.files; previewVideoFile(file);
});

for (let index = 0; index < 3; index += 1) {
  $(`reference${index + 1}`).addEventListener("change", (event) => {
    if (index === 0 && state.referenceUrls[0] && event.target.files[0]) {
      clearReference(1);
      clearReference(2);
    }
    previewReference(index, event.target.files[0]);
  });
  $(`clearReference${index + 1}`).addEventListener("click", () => clearReference(index));
}
$("imageMode").addEventListener("change", updateImageModeControls);

generateButton.addEventListener("click", () => generateVideo());
continueButton.addEventListener("click", () => generateVideo(state.continueFromJobId));
generateImageButton.addEventListener("click", generateImage);
$("useAsMasterButton").addEventListener("click", useGeneratedAsMaster);

cancelButton.addEventListener("click", async () => {
  if (!state.videoJobId) return;
  cancelButton.disabled = true;
  try { renderVideoJob((await api(`/api/jobs/${state.videoJobId}/cancel`, { method: "POST" })).job); }
  catch (error) { setError("video", error.message); }
  finally { cancelButton.disabled = false; }
});
cancelImageButton.addEventListener("click", async () => {
  if (!state.imageJobId) return;
  cancelImageButton.disabled = true;
  try { renderImageJob((await api(`/api/images/jobs/${state.imageJobId}/cancel`, { method: "POST" })).job); }
  catch (error) { setError("image", error.message); }
  finally { cancelImageButton.disabled = false; }
});

document.querySelectorAll(".open-results").forEach((button) => button.addEventListener("click", () => api("/api/open-results", { method: "POST" }).catch((error) => setError(state.workspace, error.message))));
$("shutdownButton").addEventListener("click", async () => {
  if (!confirm("关闭本地工作台并释放显存吗？")) return;
  await api("/api/shutdown", { method: "POST" });
  document.body.innerHTML = '<main class="shell"><section class="panel"><h2>工作台已关闭</h2><p class="subtitle">本次启动的图片、视频和提示词服务都已释放，现在可以关闭这个窗口。</p></section></main>';
});

updateCutoutAvailability();
refreshEngine();
updateImageModeControls();
updateReferenceSummary();
setInterval(() => { if (!anyBusy()) refreshEngine(); }, 30000);
