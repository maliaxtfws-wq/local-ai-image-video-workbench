const sequenceUI = { current: null, dirty: false, fetching: false, actionBusy: false, list: [], previewKey: null,
  firstImageFile: null, firstImageUrl: null, firstImageSaving: false, actionVersion: 0, settingsDirty: false };
const sequenceBusyStates = new Set(["planning", "running", "pausing", "merging"]);
const sequenceLabels = { planning: "本地模型正在拆镜", draft: "镜头表已就绪", running: "连续生成中",
  merging: "分段已完成，正在去重合并", pausing: "将在当前镜头或拆镜批次结束后暂停", paused: "已暂停", failed: "已停止，请检查错误", interrupted: "已恢复中断记录", completed: "全部镜头已完成" };
const shotLabels = { pending: "待生成", running: "生成中", completed: "已完成", failed: "未完成", interrupted: "中断待恢复" };
const sequenceSettingIds = ["sequenceQuality", "sequenceSteps", "sequenceAspect", "sequenceImageFit", "sequenceTargetShots", "sequenceContinuationStrategy"];
$("sequenceQuality").replaceChildren(...Array.from($("videoQuality").options).map(option => new Option(option.text.replace(" · 20 步", ""), option.value)));

function syncSequenceSteps() {
  const turbo = $("sequenceQuality").value.startsWith("draft");
  for (const option of $("sequenceSteps").options) option.disabled = turbo ? option.value !== "8" : option.value === "8";
  if (turbo) $("sequenceSteps").value = "8";
  else if ($("sequenceSteps").value === "8") $("sequenceSteps").value = "20";
}

function readSequenceSettings() {
  return {quality: $("sequenceQuality").value, steps: Number($("sequenceSteps").value),
    aspect: $("sequenceAspect").value, image_fit: $("sequenceImageFit").value,
    target_shots: Number($("sequenceTargetShots").value), continuation_strategy: $("sequenceContinuationStrategy").value};
}

function renderSequenceFraming() {
  const plan = sequenceUI.current, preview = $("sequenceFramingPreview");
  const available = Boolean(plan?.first_image && !sequenceUI.firstImageFile);
  show("sequenceFramingPreview", available);
  if (available) {
    const query = new URLSearchParams(readSequenceSettings());
    const url = `/api/sequences/${plan.id}/framing-preview?${query}`;
    if (preview.getAttribute("src") !== url) preview.src = url;
  } else preview.removeAttribute("src");
}

async function saveSequenceSettings() {
  const plan = sequenceUI.current;
  if (!plan || plan.shots.some(shot => shot.job_id)) return;
  const payload = await api(`/api/sequences/${plan.id}/settings`, {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(readSequenceSettings())});
  sequenceUI.settingsDirty = false;
  renderSequence(payload.sequence, true);
}

for (const id of sequenceSettingIds) $(id).addEventListener("change", () => {
  sequenceUI.settingsDirty = true;
  if (id === "sequenceContinuationStrategy" && $(id).value === "dual_reference") {
    if (!["standard", "native"].includes($("sequenceQuality").value)) $("sequenceQuality").value = "standard";
    if (Number($("sequenceSteps").value) < 20) $("sequenceSteps").value = "20";
  }
  syncSequenceSteps(); renderSequenceFraming();
});
syncSequenceSteps();

function sequenceError(message = "") {
  $("sequenceError").textContent = message;
  show("sequenceError", Boolean(message));
}

function updateSequenceControls() {
  const plan = sequenceUI.current;
  const busy = anyBusy() || sequenceUI.actionBusy;
  const ownBusy = Boolean(plan && sequenceBusyStates.has(plan.status));
  for (const id of sequenceSettingIds) $(id).disabled = busy;
  const started = Boolean(plan?.shots.some(shot => shot.job_id));
  $("saveSequenceSettings").disabled = busy || !plan || started;
  $("copySequenceSettings").disabled = busy || !plan || plan.planned_chunks !== plan.total_chunks;
  $("replanSequence").disabled = busy || !plan || !state.promptReady;
  $("collectSequenceOutputs").disabled = busy || !plan || !plan.completed_shots;
  $("openSequenceFolder").disabled = !plan?.output_folder;
  $("mergeSequenceVideo").disabled = busy || !plan || !plan.total_shots || plan.completed_shots !== plan.total_shots || plan.planned_chunks !== plan.total_chunks;
  renderSequenceMerge(plan?.merge);
  $("sequenceSettingsHint").textContent = started
    ? "此方案已生成，设置不会修改已有镜头。选择新设置后点‘复制镜头表’，或按新数量重新拆镜；旧方案保留。"
    : "这里是本剧本独立设置，开始前可保存修改。修改镜头数量需重新拆镜；Turbo 固定 8 步，标准/高清可选更多步数。";
  $("sequenceOutputFolder").textContent = plan?.output_folder ? `本首图影片文件夹：${plan.output_folder}` : "同一张首图的连续镜头会集中到同一文件夹；旧成片可点击归集，原文件保留。";
  $("planScriptButton").disabled = busy || !state.promptReady || Boolean(plan);
  $("scriptFile").disabled = busy || Boolean(plan);
  $("scriptTitle").disabled = busy || Boolean(plan);
  $("scriptText").disabled = busy || Boolean(plan);
  $("newSequenceButton").disabled = busy;
  $("sequenceLibrary").disabled = busy;
  const frameLocked = busy || Boolean(plan?.shots.some((s) => s.job_id));
  $("sequenceFirstImage").disabled = frameLocked;
  $("saveSequenceFirstImage").disabled = frameLocked || !plan || !sequenceUI.firstImageFile;
  $("clearSequenceFirstImage").disabled = frameLocked || !(sequenceUI.firstImageFile || plan?.first_image);
  renderSequenceFirstImage();
  $("saveShotsButton").disabled = busy || !plan || !sequenceUI.dirty;
  $("startSequenceButton").disabled = busy || !plan || plan.status === "completed"
    || !(plan.planned_chunks < plan.total_chunks ? state.promptReady : state.ready);
  $("startSequenceButton").textContent = plan?.status === "completed" ? "全部镜头已完成" : plan?.planned_chunks < plan?.total_chunks
    ? "继续未完成的拆镜" : (plan?.shots.some((s) => s.job_id) ? "继续未完成镜头" : "开始连续生成");
  $("pauseSequenceButton").disabled = sequenceUI.actionBusy || plan?.status === "pausing";
  show("pauseSequenceButton", ownBusy && plan.status !== "merging");
  $("continuationFile").disabled = busy;
  $("clearContinuation").disabled = busy;
  if (!state.videoPollTimer) generateButton.textContent = state.importedReferenceId ? "根据导入视频继续生成" : "生成约 5 秒视频";
  if (typeof updateSequenceBatchControls === "function") updateSequenceBatchControls();
}

function renderSequenceMerge(merged) {
  const ready = merged?.status === "completed" && merged.video_url;
  const labels = {running: "正在精确去重并合并，原分段保持不变……", failed: "合并失败，可单独重试：", interrupted: "合并中断，可单独重试："};
  $("sequenceMergeStatus").textContent = ready
    ? `完整视频已就绪 · ${merged.seconds.toFixed(2)} 秒 · 去除 ${merged.removed_frames} 个续接重叠帧 · 原分段已保留`
    : (labels[merged?.status] || "本首图全部镜头生成后自动合并；旧成片可点击下方按钮。") + (merged?.error || "");
  $("mergeSequenceVideo").textContent = ready ? "检查 / 重新合并完整视频" : "合并完整视频（保留分段）";
  show("sequenceMergedDownload", Boolean(ready)); show("sequenceMergedPreview", Boolean(ready));
  const preview = $("sequenceMergedPreview");
  if (ready) {
    const url = `${merged.video_url}?v=${encodeURIComponent(merged.filename)}`;
    $("sequenceMergedDownload").href = url;
    $("sequenceMergedDownload").download = merged.filename;
    if (preview.getAttribute("src") !== url) preview.src = url;
  } else if (preview.getAttribute("src")) { preview.removeAttribute("src"); preview.load(); }
}

function renderSequence(plan, force = false) {
  const changedPlan = sequenceUI.current?.id !== plan.id;
  if (changedPlan && sequenceUI.current) resetSequenceFirstImage();
  sequenceUI.current = plan;
  if (changedPlan || force) {
    $("scriptTitle").value = plan.title;
    $("scriptText").value = plan.script;
  }
  if (changedPlan || (force && !sequenceUI.settingsDirty)) {
    sequenceUI.settingsDirty = false;
    $("sequenceQuality").value = plan.quality;
    $("sequenceSteps").value = String(plan.steps || (plan.quality.startsWith("draft") ? 8 : 20));
    $("sequenceAspect").value = plan.aspect;
    $("sequenceImageFit").value = plan.image_fit || "crop";
    $("sequenceTargetShots").value = String(plan.target_shots || 0);
    $("sequenceContinuationStrategy").value = plan.continuation_strategy || "previous";
    syncSequenceSteps();
  }
  $("sequenceStatus").textContent = `${sequenceLabels[plan.status] || plan.status} · 拆镜 ${plan.planned_chunks} / ${plan.total_chunks} 批 · 成片 ${plan.completed_shots} / ${plan.total_shots} 镜头${sequenceUI.dirty ? " · 有未保存修改" : ""}`;
  const libraryOption = Array.from($("sequenceLibrary").options).find((option) => option.value === plan.id);
  if (libraryOption) libraryOption.textContent = `${plan.title} · ${sequenceLabels[plan.status] || plan.status} · ${plan.completed_shots}/${plan.total_shots}`;
  sequenceError(plan.error || "");
  const existingCount = $("shotList").children.length;
  if (force || changedPlan || (!sequenceUI.dirty && existingCount !== plan.shots.length)) {
    const cards = plan.shots.map((shot, index) => {
      const card = document.createElement("li");
      card.className = "shot-card";
      const header = document.createElement("header");
      const title = document.createElement("strong");
      title.textContent = `${index + 1}. ${shot.title}`;
      const badge = document.createElement("span"); badge.dataset.shotStatus = index;
      header.append(title, badge);
      const source = document.createElement("details");
      const summary = document.createElement("summary"); summary.textContent = "对应剧本原文";
      const sourceText = document.createElement("p"); sourceText.textContent = shot.source_text;
      source.append(summary, sourceText);
      const label = document.createElement("label");
      label.htmlFor = `shotPrompt${index}`; label.textContent = `镜头 ${index + 1} 提示词`;
      const input = document.createElement("textarea"); input.id = label.htmlFor;
      input.value = shot.prompt; input.maxLength = 5000; input.dataset.shotPrompt = index;
      input.addEventListener("input", () => { sequenceUI.dirty = true; updateSequenceControls(); });
      const result = document.createElement("div"); result.dataset.shotResult = index;
      card.append(header, source, label, input, result);
      return card;
    });
    $("shotList").replaceChildren(...cards);
  }
  plan.shots.forEach((shot, index) => {
    const badge = document.querySelector(`[data-shot-status="${index}"]`);
    const input = $(`shotPrompt${index}`);
    if (badge) badge.textContent = shotLabels[shot.status] || shot.status;
    if (input) input.disabled = sequenceBusyStates.has(plan.status) || shot.status === "completed";
    const result = document.querySelector(`[data-shot-result="${index}"]`);
    if (result && shot.result?.video_url && !result.children.length) {
      const link = document.createElement("a"); link.href = shot.result.video_url;
      link.textContent = "下载这一镜头"; link.download = shot.result.filename || "H3.mp4";
      result.append(link);
    }
  });
  show("sequenceActions", true);
  show("sequenceStartHint", true);
  const visibleJob = plan.current_job || [...plan.shots].reverse().find((s) => s.status === "completed")?.result;
  if (visibleJob && !state.batch.running) {
    const currentShot = plan.shots.find((s) => s.job_id === visibleJob.id);
    if (currentShot && sequenceBusyStates.has(plan.status)) {
      prompt.value = currentShot.prompt;
      characterLock.value = plan.character_lock;
      $("promptCount").textContent = `${prompt.value.length} / 5000`;
    }
    const previewKey = `${visibleJob.id}:${visibleJob.status}`;
    if (sequenceUI.previewKey !== previewKey || !["completed", "failed", "cancelled"].includes(visibleJob.status)) {
      state.videoJobId = visibleJob.id;
      renderVideoJob(visibleJob);
      sequenceUI.previewKey = previewKey;
    }
  }
  localStorage.setItem("h3-selected-sequence", plan.id);
  updateControls();
}

async function refreshSequenceLibrary() {
  const payload = await api("/api/sequences");
  sequenceUI.list = payload.sequences;
  const select = $("sequenceLibrary");
  select.replaceChildren(new Option("选择本机镜头表", ""), ...payload.sequences.map((p) =>
    new Option(`${p.title} · ${sequenceLabels[p.status] || p.status} · ${p.completed_shots}/${p.total_shots}`, p.id)));
  if (sequenceUI.current) select.value = sequenceUI.current.id;
}

async function loadSequence(id) {
  sequenceUI.dirty = false;
  sequenceUI.settingsDirty = false;
  const payload = await api(`/api/sequences/${encodeURIComponent(id)}`);
  resetSequenceFirstImage();
  renderSequence(payload.sequence, true);
  $("sequenceLibrary").value = id;
  if (typeof refreshSequenceBatches === "function") await refreshSequenceBatches(id);
}

async function pollSequence() {
  if (sequenceUI.fetching || !sequenceUI.current || sequenceUI.actionBusy) return;
  sequenceUI.fetching = true;
  const id = sequenceUI.current.id;
  const actionVersion = sequenceUI.actionVersion;
  try {
    const payload = await api(`/api/sequences/${encodeURIComponent(id)}`);
    if (sequenceUI.current?.id === id && actionVersion === sequenceUI.actionVersion && !sequenceUI.actionBusy) {
      const statusChanged = sequenceUI.current.status !== payload.sequence.status;
      renderSequence(payload.sequence);
      if (statusChanged) { await refreshEngine(); await refreshSequenceLibrary(); }
      if (typeof pollSequenceBatch === "function") await pollSequenceBatch();
    }
  } catch (error) {
    sequenceError(`暂时无法读取进度：${error.message}。恢复连接后将重新读取，不会重发生成请求。`);
  } finally { sequenceUI.fetching = false; }
}

async function saveShotEdits() {
  if (!sequenceUI.dirty) return;
  const prompts = sequenceUI.current.shots.map((_, index) => $(`shotPrompt${index}`).value);
  const payload = await api(`/api/sequences/${sequenceUI.current.id}/edit`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({prompts}),
  });
  sequenceUI.dirty = false;
  renderSequence(payload.sequence, true);
}

async function sequenceAction(action) {
  if (sequenceUI.actionBusy) return;
  sequenceUI.actionBusy = true;
  sequenceUI.actionVersion += 1;
  sequenceError(); updateControls();
  try { await action(); }
  catch (error) { sequenceError(error.message); }
  finally { sequenceUI.actionBusy = false; updateControls(); }
}

function resetSequenceFirstImage() {
  if (sequenceUI.firstImageUrl) URL.revokeObjectURL(sequenceUI.firstImageUrl);
  sequenceUI.firstImageFile = null;
  sequenceUI.firstImageUrl = null;
  $("sequenceFirstImage").value = "";
}

function renderSequenceFirstImage() {
  const plan = sequenceUI.current;
  const preview = $("sequenceFirstPreview");
  const url = sequenceUI.firstImageUrl || plan?.first_image_url;
  if (url) {
    if (preview.getAttribute("src") !== url) preview.src = url;
  } else preview.removeAttribute("src");
  show("sequenceFirstPreview", Boolean(url));
  renderSequenceFraming();
  $("sequenceFirstStatus").textContent = sequenceUI.firstImageSaving
    ? "正在本机检查、压缩并保存首帧，请稍候……"
    : sequenceUI.firstImageFile
      ? `已选择 ${sequenceUI.firstImageFile.name}，尚未保存。${plan ? "可保存到当前镜头表，或开始生成时自动保存。" : "请先拆分剧本；刷新前需要保存到镜头表。"}`
      : plan?.first_image
        ? "本镜头表的原始角色图已保存在本机；首段用于起始画面，双参考方案还会逐段读取。"
        : "尚未设置剧本专用首帧。可先选择图片；大图自动压缩，不裁剪、不修改原图。";
}

async function saveSequenceFirstImage(file = sequenceUI.firstImageFile) {
  const plan = sequenceUI.current;
  if (!file || !plan) return;
  sequenceUI.firstImageSaving = true;
  renderSequenceFirstImage();
  try {
    const form = new FormData(); form.append("image", file, file.name);
    const result = await api(`/api/sequences/${plan.id}/first-image`, {method:"POST", body:form});
    Object.assign(plan, {first_image: result.first_image, first_image_url: result.first_image_url, reference_id: null});
    $("sequenceFramingPreview").removeAttribute("src");
    resetSequenceFirstImage();
  } finally { sequenceUI.firstImageSaving = false; renderSequenceFirstImage(); }
}

$("sequenceFirstImage").addEventListener("change", () => {
  const file = $("sequenceFirstImage").files[0];
  if (!file) return;
  if (!/\.(png|jpe?g|webp)$/i.test(file.name) || file.size <= 0 || file.size > 200 * 1024 * 1024) {
    $("sequenceFirstImage").value = "";
    sequenceError("请选择非空的 PNG、JPEG 或 WebP 图片，单张最多 200MB。");
    return;
  }
  resetSequenceFirstImage();
  sequenceUI.firstImageFile = file;
  sequenceUI.firstImageUrl = URL.createObjectURL(file);
  sequenceError(); updateControls();
});
$("saveSequenceFirstImage").addEventListener("click", () => sequenceAction(() => saveSequenceFirstImage()));
$("clearSequenceFirstImage").addEventListener("click", () => sequenceAction(async () => {
  const plan = sequenceUI.current;
  if (plan?.first_image) {
    await api(`/api/sequences/${plan.id}/first-image/remove`, {method:"POST"});
    plan.first_image = false; plan.first_image_url = null;
  }
  resetSequenceFirstImage(); renderSequenceFirstImage();
}));

$("planScriptButton").addEventListener("click", () => sequenceAction(async () => {
  if (!$("scriptText").value.trim()) throw new Error("请先粘贴或导入完整剧本。");
  const payload = await api("/api/sequences", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({script: $("scriptText").value, title: $("scriptTitle").value,
      character_lock: characterLock.value, model_profile: $("promptModelProfile").value,
      ...readSequenceSettings()}),
  });
  sequenceUI.dirty = false;
  state.sequenceBusy = true;
  renderSequence(payload.sequence, true);
  await refreshSequenceLibrary();
  if (typeof refreshSequenceBatches === "function") await refreshSequenceBatches(payload.sequence.id);
}));

$("saveShotsButton").addEventListener("click", () => sequenceAction(saveShotEdits));
$("startSequenceButton").addEventListener("click", () => sequenceAction(async () => {
  if (sequenceUI.settingsDirty && sequenceUI.current.shots.some(shot => shot.job_id)) {
    throw new Error("这份方案已开始生成。请先按当前设置复制镜头表，不能中途更换画幅或步数。");
  }
  await saveShotEdits();
  await saveSequenceSettings();
  const plan = sequenceUI.current;
  const needsPlanning = plan.planned_chunks < plan.total_chunks;
  const firstStart = !plan.shots.some((s) => s.job_id);
  if (firstStart && sequenceUI.firstImageFile) {
    await saveSequenceFirstImage();
  } else if (!needsPlanning && firstStart && !plan.first_image && videoImage.files[0] && !state.importedReferenceId) {
    await saveSequenceFirstImage(videoImage.files[0]);
  }
  const payload = await api(`/api/sequences/${plan.id}/start`, { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify(firstStart && !plan.first_image && state.importedReferenceId ? {reference_id:state.importedReferenceId} : {}),
  });
  state.sequenceBusy = true;
  renderSequence(payload.sequence, true);
}));
$("pauseSequenceButton").addEventListener("click", () => sequenceAction(async () => {
  const payload = await api(`/api/sequences/${sequenceUI.current.id}/pause`, {method:"POST"});
  renderSequence(payload.sequence);
}));
$("reloadSequenceButton").addEventListener("click", () => sequenceAction(() => loadSequence(sequenceUI.current.id)));
$("sequenceLibrary").addEventListener("change", () => {
  const id = $("sequenceLibrary").value;
  if (id) sequenceAction(async () => { await saveShotEdits(); await loadSequence(id); });
});
$("newSequenceButton").addEventListener("click", () => sequenceAction(async () => {
  await saveShotEdits();
  sequenceUI.current = null; sequenceUI.dirty = false;
  sequenceUI.settingsDirty = false;
  resetSequenceFirstImage();
  localStorage.removeItem("h3-selected-sequence");
  if (typeof resetSequenceBatches === "function") resetSequenceBatches();
  $("scriptTitle").value = "剧本连续生成"; $("scriptText").value = "";
  $("sequenceQuality").value = "draft"; $("sequenceSteps").value = "8";
  $("sequenceAspect").value = "auto"; $("sequenceImageFit").value = "contain"; $("sequenceTargetShots").value = "12";
  $("sequenceContinuationStrategy").value = "previous";
  syncSequenceSteps();
  $("sequenceLibrary").value = ""; $("shotList").replaceChildren();
  $("sequenceStatus").textContent = "请粘贴或导入新的剧本。原镜头表仍保存在本机列表中。";
  show("sequenceActions", false); show("sequenceStartHint", false); sequenceError(); updateControls();
}));
$("saveSequenceSettings").addEventListener("click", () => sequenceAction(saveSequenceSettings));
async function copySequenceWithSettings(replan) {
  const config = readSequenceSettings();
  await saveShotEdits();
  const payload = await api(`/api/sequences/${sequenceUI.current.id}/copy`, {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify({...config, replan})});
  resetSequenceFirstImage();
  renderSequence(payload.sequence, true);
  await refreshSequenceLibrary();
  if (typeof refreshSequenceBatches === "function") await refreshSequenceBatches(payload.sequence.id);
  await refreshEngine();
}
$("copySequenceSettings").addEventListener("click", () => sequenceAction(() => copySequenceWithSettings(false)));
$("replanSequence").addEventListener("click", () => sequenceAction(() => copySequenceWithSettings(true)));
$("collectSequenceOutputs").addEventListener("click", () => sequenceAction(async () => {
  const payload = await api(`/api/sequences/${sequenceUI.current.id}/collect`, {method:"POST"});
  renderSequence(payload.sequence, true);
}));
$("mergeSequenceVideo").addEventListener("click", () => sequenceAction(async () => {
  const payload = await api(`/api/sequences/${sequenceUI.current.id}/merge`, {method:"POST"});
  state.sequenceBusy = true;
  renderSequence(payload.sequence);
}));
$("openSequenceFolder").addEventListener("click", () => sequenceAction(() =>
  api(`/api/sequences/${sequenceUI.current.id}/open-folder`, {method:"POST"})));
$("scriptFile").addEventListener("change", () => sequenceAction(async () => {
  const file = $("scriptFile").files[0]; if (!file) return;
  if (file.size > 1024 * 1024) throw new Error("文本文件超过 1MB，请按集拆分。");
  const text = new TextDecoder("utf-8", {fatal:true}).decode(await file.arrayBuffer());
  if (text.length > 100000) throw new Error("单份剧本最多 10 万字，请按集拆分。");
  $("scriptText").value = text;
  $("scriptTitle").value = file.name.replace(/\.(txt|md)$/i, "").slice(0, 100);
}));

function clearImportedReference() {
  state.importedReferenceId = null;
  $("continuationFile").value = "";
  $("continuationPreview").removeAttribute("src");
  $("continuationPreview").load();
  show("continuationPreview", false); show("clearContinuation", false);
  $("continuationImportStatus").textContent = "尚未导入参考视频。";
  updateControls();
}

async function importContinuation(file) {
  if (!file || anyBusy() || sequenceUI.actionBusy) return;
  clearImportedReference();
  if (file.size > 512 * 1024 * 1024) { setError("video", "视频超过 512MB，请只导出末尾几秒再拖入。"); return; }
  state.importingVideo = true; updateControls(); setError("video");
  $("continuationImportStatus").textContent = `正在本机读取 ${file.name} 并准备末尾参考片段……`;
  try {
    const form = new FormData(); form.append("quality", $("videoQuality").value); form.append("video", file, file.name);
    const payload = await api("/api/continuation-sources", {method:"POST", body:form});
    state.importedReferenceId = payload.source.id;
    videoImage.value = ""; previewVideoFile(null);
    $("continuationImportStatus").textContent = `已导入 ${payload.source.filename} · ${payload.source.width} × ${payload.source.height} · 末尾 22 帧${payload.source.has_audio ? "与声音" : "（原片无音轨）"}。填写下一镜头提示词后即可继续。`;
    $("continuationPreview").src = payload.source.video_url;
    show("continuationPreview", true); show("clearContinuation", true);
  } catch (error) {
    $("continuationImportStatus").textContent = "视频导入失败，请检查文件后重试。";
    setError("video", error.message);
  } finally { state.importingVideo = false; updateControls(); }
}

$("continuationFile").addEventListener("change", (event) => importContinuation(event.target.files[0]));
$("clearContinuation").addEventListener("click", clearImportedReference);
const continuationDrop = $("continuationDropZone");
["dragenter", "dragover"].forEach((name) => continuationDrop.addEventListener(name, (event) => {
  event.preventDefault(); continuationDrop.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => continuationDrop.addEventListener(name, (event) => {
  event.preventDefault(); continuationDrop.classList.remove("dragging");
}));
continuationDrop.addEventListener("drop", (event) => importContinuation(event.dataTransfer.files[0]));
videoImage.addEventListener("change", () => { if (videoImage.files[0] && state.importedReferenceId) clearImportedReference(); });

(async () => {
  try {
    await refreshSequenceLibrary();
    const id = sequenceUI.list.find((p) => sequenceBusyStates.has(p.status))?.id
      || localStorage.getItem("h3-selected-sequence");
    if (id && sequenceUI.list.some((p) => p.id === id)) await loadSequence(id);
  } catch (error) { sequenceError(`镜头表暂时无法加载：${error.message}`); }
  updateControls();
})();
setInterval(pollSequence, 2500);
