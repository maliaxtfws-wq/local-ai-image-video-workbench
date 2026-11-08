const sequenceBatchUI = { current: null, list: [], selection: null, fetching: false, renderKey: null };
const sequenceBatchBusy = new Set(["preparing", "running", "pausing"]);
const sequenceBatchLabels = { preparing: "正在保存首图", draft: "首图已导入，等待开始", running: "批量连续生成中",
  pausing: "完成当前图片或镜头后暂停", paused: "已暂停", interrupted: "中断记录已恢复", failed: "已停止，请检查错误", completed: "本批次已完成" };

function updateSequenceBatchControls() {
  const plan = sequenceUI.current;
  const batch = sequenceBatchUI.current;
  const busy = anyBusy() || sequenceUI.actionBusy;
  const validPlan = Boolean(plan && !plan.sequence_batch_id && plan.shots.length && plan.planned_chunks >= plan.total_chunks);
  for (const id of ["sequenceBatchFolder", "scanSequenceBatchFolder", "sequenceBatchDirectory", "sequenceBatchFiles", "sequenceBatchCompliance"]) {
    $(id).disabled = busy || !validPlan;
  }
  $("importSequenceBatch").disabled = busy || !validPlan || !sequenceBatchUI.selection || !$("sequenceBatchCompliance").checked;
  $("sequenceBatchLibrary").disabled = busy || !sequenceBatchUI.list.length;
  const importing = batch && batch.imported_images < batch.total_images;
  $("startSequenceBatch").disabled = busy || !batch || batch.status === "completed" || (!importing && !state.ready);
  $("startSequenceBatch").textContent = !batch ? "开始批量连续生成" : importing ? "继续导入未完成的首图"
    : batch.status === "completed" ? "本批次已全部完成" : batch.completed_shots ? "继续未完成的批量镜头" : "开始批量连续生成";
  show("pauseSequenceBatch", Boolean(batch && sequenceBatchBusy.has(batch.status)));
  $("pauseSequenceBatch").disabled = sequenceUI.actionBusy || batch?.status === "pausing";
  $("openSequenceBatchFolder").disabled = !batch || batch.phase !== "generating";
}

function renderSequenceBatch(batch) {
  sequenceBatchUI.current = batch;
  const label = sequenceBatchLabels[batch.status] || batch.status;
  $("sequenceBatchStatus").textContent = `${label} · 首图导入 ${batch.imported_images}/${batch.total_images} · 完成 ${batch.completed_images} 组 · 视频 ${batch.completed_shots}/${batch.total_shots}${batch.error ? ` · ${batch.error}` : ""}`;
  $("sequenceBatchOutput").textContent = `本批次输出：${batch.output_folder}`;
  $("sequenceBatchProgress").max = Math.max(1, batch.phase === "importing" ? batch.total_images : batch.total_shots);
  $("sequenceBatchProgress").value = batch.phase === "importing" ? batch.imported_images : batch.completed_shots;
  show("sequenceBatchProgress", true);
  const renderKey = JSON.stringify(batch.items);
  if (sequenceBatchUI.renderKey !== renderKey) {
    const cards = batch.items.map((item, index) => {
      const card = document.createElement("li"); card.className = "sequence-batch-card";
      if (item.first_image_url) {
        const image = document.createElement("img"); image.src = item.first_image_url;
        image.alt = `第 ${index + 1} 组首图：${item.relative_path}`; image.loading = "lazy";
        card.append(image);
      }
      const title = document.createElement("strong"); title.textContent = `${index + 1}. ${item.relative_path}`;
      const progress = document.createElement("p");
      progress.textContent = `${item.first_image ? (sequenceLabels[item.status] || item.status) : "首图待导入"} · ${item.completed_shots}/${item.total_shots} 镜头`;
      const folder = document.createElement("p"); folder.className = "microcopy sequence-batch-path";
      folder.textContent = item.output_folder;
      const results = document.createElement("details");
      const summary = document.createElement("summary"); summary.textContent = `本首图的镜头视频（${item.videos.length}）`;
      results.append(summary);
      item.videos.forEach((video, shotIndex) => {
        const link = document.createElement("a"); link.href = video.video_url;
        link.download = video.filename; link.textContent = `镜头 ${shotIndex + 1} · 下载视频`;
        results.append(link);
      });
      card.append(title, progress, folder, results);
      const merged = item.merge;
      if (merged?.status === "completed" && merged.video_url) {
        const link = document.createElement("a"); link.href = merged.video_url;
        link.download = merged.filename; link.textContent = `下载本首图完整视频 · ${merged.seconds.toFixed(2)} 秒（已去重）`;
        card.append(link);
      } else if (item.total_shots && item.completed_shots === item.total_shots) {
        const note = document.createElement("p");
        note.textContent = merged?.status === "running" ? "正在自动去重合并……" : (merged?.error || "分段已完成，可合并完整视频。");
        const button = document.createElement("button"); button.className = "secondary";
        button.textContent = "合并本首图完整视频";
        button.addEventListener("click", () => sequenceBatchAction(async () => {
          await api(`/api/sequences/${item.sequence_id}/merge`, {method:"POST"});
          state.sequenceBusy = true;
          const payload = await api(`/api/sequence-batches/${batch.id}`);
          renderSequenceBatch(payload.batch);
        }));
        button.disabled = merged?.status === "running";
        card.append(note, button);
      }
      return card;
    });
    $("sequenceBatchQueue").replaceChildren(...cards);
    sequenceBatchUI.renderKey = renderKey;
  }
  const option = Array.from($("sequenceBatchLibrary").options).find((entry) => entry.value === batch.id);
  if (option) option.textContent = `${batch.total_images} 张首图 · ${label} · ${batch.completed_shots}/${batch.total_shots}`;
  $("sequenceBatchLibrary").value = batch.id;
  localStorage.setItem(`h3-selected-sequence-batch-${batch.sequence_id}`, batch.id);
  if (batch.current_job) {
    state.videoJobId = batch.current_job.id;
    renderVideoJob(batch.current_job);
  }
  updateControls();
}

function resetSequenceBatches() {
  sequenceBatchUI.current = null; sequenceBatchUI.list = []; sequenceBatchUI.selection = null;
  sequenceBatchUI.renderKey = null;
  $("sequenceBatchQueue").replaceChildren();
  $("sequenceBatchLibrary").replaceChildren(new Option("尚无首图批次", ""));
  $("sequenceBatchStatus").textContent = "请先选择或拆分一份剧本，再导入首图。";
  $("sequenceBatchSelection").textContent = "尚未读取首图。导入不会自动生成视频。";
  $("sequenceBatchOutput").textContent = "";
  $("sequenceBatchFiles").value = ""; $("sequenceBatchDirectory").value = "";
  show("sequenceBatchProgress", false);
}

async function refreshSequenceBatches(planId) {
  const payload = await api(`/api/sequences/${encodeURIComponent(planId)}/batches`);
  if (sequenceUI.current?.id !== planId) return;
  resetSequenceBatches();
  sequenceBatchUI.list = payload.batches;
  $("sequenceBatchLibrary").replaceChildren(new Option("选择本剧本的首图批次", ""), ...payload.batches.map((batch) =>
    new Option(`${batch.total_images} 张首图 · ${sequenceBatchLabels[batch.status] || batch.status} · ${batch.completed_shots}/${batch.total_shots}`, batch.id)));
  const selected = payload.batches.find((batch) => sequenceBatchBusy.has(batch.status))
    || payload.batches.find((batch) => batch.id === localStorage.getItem(`h3-selected-sequence-batch-${planId}`))
    || payload.batches[0];
  if (selected) renderSequenceBatch(selected);
  else updateControls();
}

async function pollSequenceBatch() {
  const batch = sequenceBatchUI.current;
  if (!batch || sequenceBatchUI.fetching || sequenceUI.actionBusy) return;
  sequenceBatchUI.fetching = true;
  const version = sequenceUI.actionVersion;
  try {
    const payload = await api(`/api/sequence-batches/${batch.id}`);
    if (sequenceBatchUI.current?.id !== batch.id || version !== sequenceUI.actionVersion) return;
    const changed = batch.status !== payload.batch.status ||
      JSON.stringify(batch.items.map(item => item.merge?.status)) !== JSON.stringify(payload.batch.items.map(item => item.merge?.status));
    renderSequenceBatch(payload.batch);
    if (changed) await refreshEngine();
  } catch (error) {
    sequenceError(`批次进度暂时无法读取：${error.message}。只会重新读取状态，不会重发任务。`);
  } finally { sequenceBatchUI.fetching = false; }
}

async function sequenceBatchAction(action) {
  return sequenceAction(async () => {
    state.sequenceBatchActionBusy = true; updateControls();
    try { await action(); }
    finally { state.sequenceBatchActionBusy = false; }
  });
}

function showSequenceBatchSelection(selection) {
  sequenceBatchUI.selection = selection;
  const shots = sequenceUI.current?.shots.length || 0;
  $("sequenceBatchSelection").textContent = `已读取 ${selection.file_count} 张首图，每图 ${shots} 镜头，合计 ${selection.file_count * shots} 段。${selection.files.map((file) => file.relative_path).join("、")}`;
  updateControls();
}

async function scanSequenceBatchFolder() {
  sequenceBatchUI.selection = null;
  $("sequenceBatchSelection").textContent = "正在读取并检查首图，大图会自动准备兼容副本……";
  const folder = $("sequenceBatchFolder").value.trim();
  const payload = await api("/api/batch-folders/scan", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({path:folder})});
  localStorage.setItem("h3-sequence-batch-folder", folder);
  showSequenceBatchSelection(payload.selection);
}

async function uploadSequenceBatchFiles(files) {
  sequenceBatchUI.selection = null;
  const selected = Array.from(files).filter((file) => /\.(png|jpe?g|webp)$/i.test(file.name))
    .sort((a, b) => (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name, "zh-CN", {numeric:true}));
  if (!selected.length || selected.length > 200) throw new Error("请选择 1 到 200 张 PNG、JPEG 或 WebP 首图。");
  if (selected.some((file) => !file.size || file.size > 200 * 1024 * 1024)) throw new Error("存在空图片或超过 200MB 的图片，请检查后重新选择。");
  let selectionId = null;
  const imported = [];
  for (const [index, file] of selected.entries()) {
    $("sequenceBatchSelection").textContent = `正在导入 ${index + 1}/${selected.length}：${file.name}，尚未生成视频。`;
    const form = new FormData();
    if (selectionId) form.append("selection_id", selectionId);
    form.append("image", file, file.name);
    const payload = await api("/api/batch-images/import", {method:"POST", body:form});
    selectionId = payload.selection.selection_id;
    imported.push(...payload.selection.files);
  }
  showSequenceBatchSelection({selection_id:selectionId, file_count:imported.length, files:imported});
}

$("scanSequenceBatchFolder").addEventListener("click", () => sequenceBatchAction(scanSequenceBatchFolder));
for (const id of ["sequenceBatchDirectory", "sequenceBatchFiles"]) {
  $(id).addEventListener("change", (event) => sequenceBatchAction(() => uploadSequenceBatchFiles(event.target.files)));
}
$("sequenceBatchCompliance").addEventListener("change", updateControls);
$("importSequenceBatch").addEventListener("click", () => sequenceBatchAction(async () => {
  if (!sequenceBatchUI.selection || !$("sequenceBatchCompliance").checked) throw new Error("请读取首图并确认素材可合法使用。");
  await saveShotEdits();
  if (typeof saveSequenceSettings === "function") {
    if (sequenceUI.settingsDirty && sequenceUI.current.shots.some(shot => shot.job_id)) {
      throw new Error("本剧本已生成；请先按当前设置复制镜头表，再导入首图批次。");
    }
    await saveSequenceSettings();
  }
  const payload = await api(`/api/sequences/${sequenceUI.current.id}/batches`, {method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify({selection_id:sequenceBatchUI.selection.selection_id, compliance_confirmed:true})});
  sequenceBatchUI.selection = null;
  sequenceBatchUI.list.unshift(payload.batch);
  $("sequenceBatchLibrary").append(new Option("正在保存首图", payload.batch.id));
  state.sequenceBusy = sequenceBatchBusy.has(payload.batch.status);
  renderSequenceBatch(payload.batch);
}));
$("startSequenceBatch").addEventListener("click", () => sequenceBatchAction(async () => {
  const payload = await api(`/api/sequence-batches/${sequenceBatchUI.current.id}/start`, {method:"POST"});
  state.sequenceBusy = true;
  renderSequenceBatch(payload.batch);
}));
$("pauseSequenceBatch").addEventListener("click", () => sequenceBatchAction(async () => {
  const payload = await api(`/api/sequence-batches/${sequenceBatchUI.current.id}/pause`, {method:"POST"});
  renderSequenceBatch(payload.batch);
}));
$("sequenceBatchLibrary").addEventListener("change", () => sequenceBatchAction(async () => {
  const id = $("sequenceBatchLibrary").value;
  if (!id) return;
  const payload = await api(`/api/sequence-batches/${encodeURIComponent(id)}`);
  sequenceBatchUI.renderKey = null;
  renderSequenceBatch(payload.batch);
}));
$("openSequenceBatchFolder").addEventListener("click", () => sequenceBatchAction(() =>
  api(`/api/sequence-batches/${sequenceBatchUI.current.id}/open-folder`, {method:"POST"})));
$("sequenceBatchFolder").value = localStorage.getItem("h3-sequence-batch-folder") || $("sequenceBatchFolder").value;
