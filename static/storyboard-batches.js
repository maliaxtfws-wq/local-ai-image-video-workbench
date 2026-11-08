'use strict';
let batchRows=[], batches=[], selectedBatch='', batchRequestId=null, batchPolling=false, batchPage=0, batchCacheWarning='';
let batchHistoryHTML='', batchResultsHTML='';
const batchLabels={queued:'待生成',running:'生成中',pausing:'当前张完成后暂停',paused:'已暂停',completed:'已完成',failed:'失败',interrupted:'中断',cancelled:'已停止'};
const batchBusy=()=>batches.some(b=>['running','pausing'].includes(b.status));
const batchSelected=()=>batches.find(b=>b.id===selectedBatch);
const pipelineMode=()=>$('batchWorkflow').value==='character_sequence';
function renderPipelineSettings(){
  if(!project)return;
  const config=project.character_pipeline||{primary_role_id:project.characters[0]?.id,shot_ids:project.shots.slice(0,4).map(s=>s.id)};
  $('pipelineSettings').hidden=!pipelineMode();
  $('pipelineRole').innerHTML=project.characters.map(r=>`<option value="${r.id}" ${r.id===config.primary_role_id?'selected':''}>${esc(r.name)}</option>`).join('');
  for(let act=1;act<=4;act++)$('pipelineShot'+act).innerHTML='<option value="">请选择分镜</option>'+project.shots.map((s,i)=>`<option value="${s.id}" ${s.id===config.shot_ids[act-1]?'selected':''}>${i+1}. ${esc(s.title)}</option>`).join('');
  $('batchTarget').disabled=pipelineMode();
}
function savePipelineSettings(){
  if(!pipelineMode())return;
  project.character_pipeline={primary_role_id:$('pipelineRole').value,shot_ids:[1,2,3,4].map(n=>$('pipelineShot'+n).value)};
  draft();batchRequestId=null;
}
$('batchWorkflow').onchange=()=>{renderPipelineSettings();renderBatchDraft();};
for(const id of ['pipelineRole','pipelineShot1','pipelineShot2','pipelineShot3','pipelineShot4'])$(id).onchange=()=>{savePipelineSettings();renderBatchDraft();};
function batchControls(){
  const busy=working||!!pending||batchBusy();
  $('batchEdit').disabled=busy||!shotId;
  $('batchStart').disabled=busy||!batchRows.length||(pipelineMode()&&!pipelineReady);
  const b=batchSelected(), live=b&&['running','pausing'].includes(b.status);
  $('batchPause').disabled=working||!live||b?.status==='pausing';
  $('batchResume').disabled=busy||!b?.items.some(i=>i.status==='queued'||i.export_error||(b.export_dir&&i.status==='completed'&&!i.export_path));
  $('batchResume').textContent=b?.items.some(i=>i.export_error)?'重试保存并继续':'继续未生成项';
  $('batchRetry').disabled=busy||!b?.items.some(i=>['failed','interrupted','cancelled'].includes(i.status));
}
function targetOptions(sid, selected){
  const s=project.shots.find(s=>s.id===sid);
  return (s?.cast||[]).map(id=>{const r=project.characters.find(r=>r.id===id);return `<option value="${id}" ${id===selected?'selected':''}>角色：${esc(r?.name)}</option>`;}).join('')+`<option value="guide" ${selected==='guide'?'selected':''}>姿势／构图参考</option>`;
}
function batchTargets(){if(!project)return;const old=$('batchTarget').value;$('batchTarget').innerHTML=targetOptions(shotId,old);}
function persistBatchDraft(){
  batchRequestId=null;
  try{localStorage.setItem(`storyboard-batch-draft-${project.id}`,JSON.stringify(batchRows));batchCacheWarning='';}catch(_){batchCacheWarning='列表较大，浏览器无法缓存全部对应关系；导入素材已保存到项目，请先提交批次再关闭页面。';}
}
function renderBatchDraft(){
  if(!project)return;batchTargets();renderPipelineSettings();
  const pages=Math.max(1,Math.ceil(batchRows.length/100));batchPage=Math.min(batchPage,pages-1);
  $('batchPageInfo').textContent=`共 ${batchRows.length} 张 · 第 ${batchPage+1}/${pages} 页`;
  $('batchPrev').disabled=batchPage===0;$('batchNext').disabled=batchPage>=pages-1;
  $('batchRows').innerHTML=batchRows.slice(batchPage*100,(batchPage+1)*100).map((row,offset)=>{
    const index=batchPage*100+offset;
    const asset=project.assets[row.asset_id];
    const mapping=pipelineMode()?'<p class="microcopy">原图 → 人物处理 → 第二幕 → 第三幕 → 第四幕<br>后面三幕共用本组的人物处理结果。</p>':`<label>对应分镜<select data-batch-shot>${project.shots.map((s,i)=>`<option value="${s.id}" ${s.id===row.shot_id?'selected':''}>${i+1}. ${esc(s.title)}</option>`).join('')}</select></label><label>参考对象<select data-batch-target>${targetOptions(row.shot_id,row.target)}</select></label>`;
    return `<article class="batch-row" data-batch-row="${index}"><span>${index+1}</span>${asset?`<img loading="lazy" decoding="async" src="${esc(asset.url)}" alt="参考图 ${index+1}">`:''}<div><strong>${esc(asset?.original_name||'参考图缺失，请移除此行')}</strong>${mapping}</div><div class="buttons"><button class="secondary" data-batch-up ${index===0?'disabled':''} aria-label="上移第 ${index+1} 张">↑</button><button class="secondary" data-batch-remove>移除</button></div></article>`;
  }).join('')||'<p class="microcopy">选择多张图片或一个文件夹后，在这里检查对应关系。</p>';
  batchControls();
}
async function importBatchFiles(fileList){
  const files=Array.from(fileList).filter(f=>/\.(png|jpe?g|webp)$/i.test(f.name)).sort((a,b)=>(a.webkitRelativePath||a.name).localeCompare(b.webkitRelativePath||b.name,'zh-CN',{numeric:true}));
  if(!files.length)throw new Error('没有找到 PNG、JPEG 或 WebP 图片。');
  const failures=[];let imported=0;
  const sid=pipelineMode()?$('pipelineShot1').value:shotId,target=pipelineMode()?$('pipelineRole').value:$('batchTarget').value;
  if(!sid||!target)throw new Error('请先选择分镜和参考对象。');
  for(let i=0;i<files.length;i++){
    $('batchNotice').textContent=`正在导入 ${i+1}/${files.length}：${files[i].name}`;
    try{const asset_id=await upload(files[i]);batchRows.push({asset_id,shot_id:sid,target});imported++;persistBatchDraft();}
    catch(e){failures.push(`${files[i].name}：${e.message}`);}
  }
  renderBatchDraft();$('batchNotice').textContent=`已导入 ${imported}/${files.length} 张；检查对应表后开始生成。`+(failures.length?`\n以下 ${failures.length} 张未导入，可处理后单独重选：\n`+failures.join('\n'):'')+(batchCacheWarning?'\n'+batchCacheWarning:'');
}
for(const id of ['batchFiles','batchFolder'])$(id).onchange=()=>{const files=Array.from($(id).files);guarded(async()=>{try{await importBatchFiles(files);}finally{$(id).value='';renderBatchDraft();}});};
$('batchRows').onchange=e=>{const row=batchRows[Number(e.target.closest('[data-batch-row]')?.dataset.batchRow)];if(!row)return;
  if(e.target.hasAttribute('data-batch-shot')){row.shot_id=e.target.value;const s=project.shots.find(s=>s.id===row.shot_id);if(row.target!=='guide'&&!s.cast.includes(row.target))row.target=s.cast[0]||'guide';}
  if(e.target.hasAttribute('data-batch-target'))row.target=e.target.value;
  persistBatchDraft();renderBatchDraft();
};
$('batchRows').onclick=e=>{const index=Number(e.target.closest('[data-batch-row]')?.dataset.batchRow);if(!Number.isInteger(index))return;
  if(e.target.hasAttribute('data-batch-remove'))batchRows.splice(index,1);
  else if(e.target.hasAttribute('data-batch-up')&&index>0)[batchRows[index-1],batchRows[index]]=[batchRows[index],batchRows[index-1]];
  else return;persistBatchDraft();renderBatchDraft();
};
$('batchPrev').onclick=()=>{batchPage=Math.max(0,batchPage-1);renderBatchDraft();};
$('batchNext').onclick=()=>{batchPage++;renderBatchDraft();};
$('batchClear').onclick=()=>{batchRows=[];persistBatchDraft();renderBatchDraft();};
async function loadBatches(){
  batchRows=JSON.parse(localStorage.getItem(`storyboard-batch-draft-${project.id}`)||'[]');batchRequestId=null;selectedBatch='';batchPage=0;
  $('batchWorkflow').value=project.character_pipeline?'character_sequence':'independent';
  renderPipelineSettings();updateOutputSummary();
  await refreshBatches();renderBatchDraft();
}
async function refreshBatches(){
  if(!project)return;const pid=project.id;
  const data=await api(`/api/storyboards/${pid}/batches`);
  if(project?.id!==pid)return;
  batches=data.batches;
  if(!batches.some(b=>b.id===selectedBatch))selectedBatch=batches[0]?.id||'';
  renderBatchHistory();controls();
}
function renderBatchHistory(){
  const historyHTML=batches.length?batches.map(b=>`<option value="${b.id}">${esc(new Date(b.created_at*1000).toLocaleString())} · ${b.items.length} 张 · ${batchLabels[b.status]||b.status}</option>`).join(''):'<option value="">尚无批次</option>';
  if(historyHTML!==batchHistoryHTML){$('batchHistory').innerHTML=historyHTML;batchHistoryHTML=historyHTML;}
  $('batchHistory').value=selectedBatch;
  const b=batchSelected();
  $('batchProgress').textContent=b?`${batchLabels[b.status]} · 已完成 ${b.items.filter(i=>i.status==='completed').length}/${b.items.length} 张 · ${b.settings.image_engine} · ${b.settings.quality==='draft'?'预览':'精细'}${b.export_dir?' · 保存到 '+b.export_dir:''}`:'';
  const resultsHTML=b?b.items.map((item,index)=>{
    const meta=item.snapshot.batch,asset=project.assets[item.asset_id],ref=item.depends_on?item.reference_image_url:asset?.url;
    return `<article class="batch-row">${ref?`<img loading="lazy" decoding="async" src="${esc(ref)}" alt="本幕实际参考图">`:''}<div><strong>${meta.group_index?`第 ${meta.group_index} 组 · 幕 ${meta.act} · `:index+1+'. '}${esc(meta.source_name)}</strong><p>${esc(item.snapshot.shot_title)} · ${esc(item.stage||batchLabels[item.status]||item.status)}</p>${item.reference_image_url?'<p class="microcopy">引用本组第一幕处理结果</p>':item.depends_on?'<p class="microcopy">等待引用本组人物处理图</p>':''}${item.error||item.export_error?`<p class="error">${esc(item.error||item.export_error)}</p>`:''}${item.export_path?`<p class="microcopy">已保存：${esc(item.export_path)}</p>`:''}${item.image_url?`<a href="${esc(item.image_url)}" target="_blank" rel="noopener">查看结果</a> · <a href="${esc(item.image_url)}" download>下载原图</a>`:''}</div>${item.image_url?`<a href="${esc(item.image_url)}" target="_blank" rel="noopener"><img loading="lazy" decoding="async" src="${esc(item.image_url)}" alt="第 ${index+1} 张生成结果"></a>`:''}</article>`;
  }).join(''):'';
  if(resultsHTML!==batchResultsHTML){$('batchResults').innerHTML=resultsHTML;batchResultsHTML=resultsHTML;}
  batchControls();
}
$('batchHistory').onchange=()=>{selectedBatch=$('batchHistory').value;renderBatchHistory();};
$('batchStart').onclick=()=>guarded(async()=>{
  if(batchBusy()||pending)throw new Error('请等待当前生成完成。');
  if(!batchRows.length)throw new Error('请先导入参考图。');
  if(pipelineMode()&&!pipelineReady)throw new Error('后台尚未启用四幕工作流，请等待工作台更新。');
  savePipelineSettings();await save();batchRequestId=batchRequestId||uid();
  const {batch}=await post(`/api/storyboards/${project.id}/batches`,{request_id:batchRequestId,export_dir:taskOutputFolder(),workflow:$('batchWorkflow').value,items:batchRows,settings:{image_engine:$('engine').value,quality:$('quality').value,aspect:$('aspect').value,seed:$('seed').value}});
  selectedBatch=batch.id;batchRows=[];persistBatchDraft();renderBatchDraft();
  try{await post(`/api/storyboards/${project.id}/batches/${batch.id}/resume`,{});$('batchNotice').textContent='已开始逐张生成，可以关闭浏览器；保持本地工作台运行。';}
  finally{await refreshBatches();}
});
for(const [id,action] of [['batchPause','pause'],['batchResume','resume'],['batchRetry','retry']])$(id).onclick=()=>guarded(async()=>{if(!selectedBatch)return;await post(`/api/storyboards/${project.id}/batches/${selectedBatch}/${action}`,{});await refreshBatches();});
setInterval(async()=>{if(!project||working||batchPolling)return;batchPolling=true;try{await refreshBatches();await refreshRuns();}catch(e){$('batchNotice').textContent='进度读取失败，不会自动重复提交：'+e.message;}finally{batchPolling=false;}},4000);
