'use strict';
const $ = id => document.getElementById(id);
const uid = () => crypto.randomUUID().replaceAll('-', '');
const active = new Set(['starting','uploading','queued','running','saving','cancelling']);
let optimizerReady = false;
let outputFolderReady = false;
let pipelineReady = false;
let styleReferenceReady = false;
let optimizationDraft=null, optimizationUndo=null;
let project = null, shotId = null, token = null, pending = null, working = false, dirty = false;
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function notice(message, error=false) { $('notice').textContent=message; $('notice').className=error?'error':''; }
async function api(url, options={}) {
  if(options.method && options.method !== 'GET') options.headers={...(options.headers||{}),'X-H3-CSRF':token};
  const response=await fetch(url,options);
  const data=await response.json().catch(()=>({error:`本地请求失败 (${response.status})` }));
  if(!response.ok) {const error=new Error(data.error||`请求失败 ${response.status}`);error.status=response.status;throw error;}
  return data;
}
const post = (url, value) => api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
function draft() {
  dirty=true;
  try {localStorage.setItem(`storyboard-draft-${project.id}`,JSON.stringify({project,shotId}));} catch (_) {notice('本地草稿缓存已满，请点击保存修改。',true);}
  $('saveProject').textContent='保存修改 · 未保存';
}
function controls() {
  const busy=working||!!pending||(typeof batchBusy==='function'&&batchBusy());
  $('generate').disabled=busy||!shotId;
  $('optimizeShot').disabled=busy||!shotId||!optimizerReady;
  $('applyOptimization').disabled=busy||!optimizationDraft;
  $('saveProject').disabled=working||!project;
  $('createProject').disabled=busy;
  $('projectSelect').disabled=busy;
  $('styleFile').disabled=working||!project||!styleReferenceReady;
  $('clearStyle').disabled=working||!project?.style_asset||!styleReferenceReady;
  $('cancel').disabled=!pending;
  document.querySelectorAll('[data-make-role]').forEach(b=>b.disabled=busy);
  $('outputFolderSettings').disabled=working||!outputFolderReady;
  if(typeof batchControls==='function')batchControls();
}
async function listProjects() {
  const data=await api('/api/storyboards');
  $('projectSelect').innerHTML='<option value="">选择项目</option>'+data.projects.map(p=>`<option value="${p.id}">${esc(p.title)}</option>`).join('');
  if(project) $('projectSelect').value=project.id;
  return data.projects;
}
async function loadProject(id) {
  project=(await api(`/api/storyboards/${id}`)).project;
  const saved=JSON.parse(localStorage.getItem(`storyboard-draft-${id}`)||'null');
  dirty=false;
  if(saved && saved.project.revision===project.revision) {
    Object.assign(project,{title:saved.project.title,style:saved.project.style,style_asset:saved.project.style_asset??project.style_asset??'',characters:saved.project.characters,shots:saved.project.shots,character_pipeline:saved.project.character_pipeline||project.character_pipeline});
    shotId=saved.shotId; dirty=true; notice('已恢复尚未保存的本机草稿。');
  } else {shotId=project.shots[0]?.id||null;notice('项目已载入。');}
  localStorage.setItem('storyboard-last',id);
  $('projectSelect').value=id;
  const running=project.runs.find(r=>active.has(r.status));
  pending=running?{id:running.id,kind:'shot',pid:id}:null;
  render();
  if(typeof loadBatches==='function')await loadBatches();
}
async function save() {
  if(!project) return;
  project=(await post(`/api/storyboards/${project.id}`,project)).project;
  localStorage.removeItem(`storyboard-draft-${project.id}`);
  dirty=false;$('saveProject').textContent='保存修改';
  await listProjects();notice('角色和分镜已保存到本机。');
}
function render() {
  $('empty').hidden=!!project;$('editor').hidden=!project;
  if(!project) return;
  $('projectTitle').value=project.title;$('style').value=project.style;
  $('saveProject').textContent=dirty?'保存修改 · 未保存':'保存修改';
  renderStyleReference();renderCharacters();renderShots();controls();
}
function renderStyleReference() {
  const asset=project?.assets[project.style_asset];
  $('stylePreview').hidden=!asset;
  if(asset) $('stylePreview').src=asset.url;
  else $('stylePreview').removeAttribute('src');
  $('styleFile').value='';
  $('styleReferenceStatus').textContent=!styleReferenceReady
    ?'画风参考功能等待工作台切换，当前生成任务继续运行。'
    :asset?`已选：${asset.original_name||'画风参考图'}。保存后所有分镜共用此图，新增与复制的分镜也会沿用。`
    :'未设置画风参考图，使用上方画风文字。';
}
function renderCharacters() {
  $('characters').innerHTML=project.characters.map((r,i)=>{
    const asset=project.assets[r.asset];
    return `<article class="role-card" data-role="${r.id}"><strong>角色库 ${i+1}</strong>${asset?`<img src="${esc(asset.url)}" alt="${esc(r.name)}的固定参考">`:'<p class="microcopy">尚无照片，将根据描述创建人物。</p>'}<label>角色名称<input data-role-field="name" value="${esc(r.name)}" maxlength="40"></label><label>固定外貌／服装<textarea data-role-field="description" rows="3" maxlength="800" placeholder="有照片时可留空；没有照片时描述人物外貌">${esc(r.description)}</textarea></label><label>上传或替换身份参考<input data-role-upload type="file" accept="image/png,image/jpeg,image/webp"></label><div class="buttons"><button data-make-role="${r.id}" class="secondary">按描述生成单人参考</button><button data-clear-role="${r.id}" class="text-button">移除照片</button></div><div class="candidate" data-candidate="${r.id}" hidden></div></article>`;
  }).join('');showCandidate();controls();
}
function showCandidate(){const c=JSON.parse(localStorage.getItem('storyboard-pending-role')||'null');if(!c?.url||c.pid!==project?.id)return;const box=document.querySelector(`[data-candidate="${c.roleId}"]`);if(box){box.hidden=false;box.innerHTML=`<p>单人参考候选</p><img src="${esc(c.url)}" alt="新角色候选"><button data-adopt-role="${c.roleId}" data-url="${esc(c.url)}">采用为角色参考</button><p class="microcopy">采用前，原角色参考保持不变。</p>`;}}
function currentShot(){return project?.shots.find(s=>s.id===shotId);}
function renderShots() {
  $('optimizationPreview').hidden=true;
  $('shotList').innerHTML=project.shots.map((s,i)=>`<button data-shot="${s.id}" class="secondary ${s.id===shotId?'selected':''}">${i+1}. ${esc(s.title)}</button>`).join('');
  const s=currentShot();$('shotEditor').hidden=!s;
  if(s) {
    $('shotTitle').value=s.title;$('action').value=s.action;
    renderCast();
    $('guidePreview').hidden=!s.guide;
    if(s.guide) $('guidePreview').src=project.assets[s.guide].url;
  }
  renderResults();controls();
  if(typeof renderBatchDraft==='function')renderBatchDraft();
}
function renderCast(){const s=currentShot();if(!s)return;
  if(typeof batchTargets==='function')batchTargets();
  $('cast').innerHTML=project.characters.map(r=>`<label><input type="checkbox" data-cast="${r.id}" ${s.cast.includes(r.id)?'checked':''}>${s.cast.includes(r.id)?String.fromCharCode(65+s.cast.indexOf(r.id))+' · ':''}${esc(r.name)}${r.asset?'（照片）':'（文字）'}</label>`).join('');
}
let resultsPage=0, resultsContext='', resultsHTML='';
const resultsPageSize=20;
function renderResults(){
  const context=JSON.stringify([project.id,shotId]);
  if(context!==resultsContext){resultsContext=context;resultsPage=0;resultsHTML='';}
  const allRuns=project.runs.filter(r=>r.shot_id===shotId).slice().reverse();
  const pages=Math.max(1,Math.ceil(allRuns.length/resultsPageSize));
  resultsPage=Math.max(0,Math.min(resultsPage,pages-1));
  const runs=allRuns.slice(resultsPage*resultsPageSize,(resultsPage+1)*resultsPageSize);
  $('resultsPageInfo').textContent=`共 ${allRuns.length} 个版本 · 第 ${resultsPage+1}/${pages} 页 · 最新在前`;
  $('resultsPrev').disabled=resultsPage===0;
  $('resultsNext').disabled=resultsPage>=pages-1;
  const html=runs.length?runs.map(r=>`<article class="result-card">${r.image_url?`<a href="${esc(r.image_url)}" target="_blank" rel="noopener"><img loading="lazy" decoding="async" src="${esc(r.image_url)}" alt="分镜生成结果"></a><p><a href="${esc(r.image_url)}" download="${esc(r.filename)}">下载原图</a></p><button class="secondary" data-crop-url="${esc(r.image_url)}">截取并固定其中一个角色</button>`:''}<p class="microcopy">${esc(r.snapshot?.batch?`批量第 ${r.snapshot.batch.index} 张 · ${r.snapshot.batch.source_name}`:'')}</p><strong>${esc(({completed:'已完成',failed:'失败',interrupted:'中断',cancelled:'已停止'})[r.status]||r.stage||'生成中')}</strong><p class="microcopy">${esc(r.model)} · Seed ${esc(r.seed)} · ${esc(r.steps)} 步</p>${r.export_path?`<p class="microcopy">已另存：${esc(r.export_path)}</p>`:''}${r.error||r.export_error?`<p>${esc(r.error||r.export_error)}</p>`:''}<details><summary>本次生成记录</summary><pre>${esc(r.snapshot?.prompt||'')}</pre></details></article>`).join(''):'<p class="microcopy">生成后，所有版本会显示在这里。</p>';
  if(html!==resultsHTML){$('results').innerHTML=html;resultsHTML=html;}
  $('promptPreview').textContent=allRuns[0]?.snapshot?.prompt||'';
}
$('resultsPrev').onclick=()=>{resultsPage--;renderResults();};
$('resultsNext').onclick=()=>{resultsPage++;renderResults();};
async function upload(file){const form=new FormData();form.append('image',file,file.name||'reference.png');const {asset}=await api(`/api/storyboards/${project.id}/assets`,{method:'POST',body:form});project.assets[asset.id]=asset;return asset.id;}
async function guarded(fn){if(working)return;working=true;controls();try{await fn();}catch(e){notice(e.message,true);}finally{working=false;controls();}}
$('createProject').onclick=()=>guarded(async()=>{
  if(dirty) await save();
  project=(await post('/api/storyboards',{title:$('newTitle').value||'我的人物分镜'})).project;
  project.characters.push({id:uid(),name:'主角',description:'',asset:''});
  const s={id:uid(),title:'第一镜',cast:[project.characters[0].id],action:'',guide:''};project.shots.push(s);shotId=s.id;
  await save();localStorage.setItem('storyboard-last',project.id);render();if(typeof loadBatches==='function')await loadBatches();
});
$('projectSelect').onchange=()=>guarded(async()=>{const id=$('projectSelect').value;if(!id)return;if(dirty)await save();await loadProject(id);});
$('saveProject').onclick=()=>guarded(save);
$('projectTitle').oninput=()=>{project.title=$('projectTitle').value;draft();};
$('style').oninput=()=>{project.style=$('style').value;draft();};
$('styleFile').onchange=()=>{const file=$('styleFile').files[0];if(file&&project&&styleReferenceReady)guarded(async()=>{
  project.style_asset=await upload(file);draft();await save();renderStyleReference();
  notice('画风参考已保存，之后提交的所有分镜自动沿用；已提交的批次保持原设置。');
});};
$('clearStyle').onclick=()=>{if(project&&styleReferenceReady)guarded(async()=>{
  project.style_asset='';draft();await save();renderStyleReference();
  notice('已取消画风参考，之后提交的分镜使用画风文字。');
});};
$('addCharacter').onclick=()=>{if(project.characters.length>=30)return notice('每个项目最多 30 个角色。',true);project.characters.push({id:uid(),name:`人物 ${project.characters.length+1}`,description:'',asset:''});draft();renderCharacters();renderCast();};
$('characters').addEventListener('input',e=>{const field=e.target.dataset.roleField;if(!field)return;const r=project.characters.find(r=>r.id===e.target.closest('[data-role]').dataset.role);r[field]=e.target.value;draft();if(field==='name')renderCast();});
$('characters').addEventListener('change',e=>{if(!e.target.hasAttribute('data-role-upload')||!e.target.files[0])return;const id=e.target.closest('[data-role]').dataset.role;const file=e.target.files[0];guarded(async()=>{const r=project.characters.find(r=>r.id===id);r.asset=await upload(file);draft();await save();renderCharacters();renderCast();});});
$('characters').addEventListener('click',e=>{const clear=e.target.dataset.clearRole,make=e.target.dataset.makeRole,adopt=e.target.dataset.adoptRole;
  if(clear){project.characters.find(r=>r.id===clear).asset='';draft();renderCharacters();renderCast();}
  if(make)guarded(async()=>{if(pending)throw new Error('请等待当前生成完成。');const r=project.characters.find(r=>r.id===make);if(!r.description.trim())throw new Error('请先填写此人的外貌描述。');await save();const form=new FormData();Object.entries({mode:'create',image_engine:$('engine').value,prompt:`单人角色参考照片，画面只有一人，清楚展示面部和半身，简单背景。人物描述：${r.description}。风格：${project.style}`,quality:$('quality').value,aspect:'portrait'}).forEach(([k,v])=>form.append(k,v));const {job}=await api('/api/images/generate',{method:'POST',body:form});pending={id:job.id,kind:'role',roleId:make,pid:project.id};localStorage.setItem('storyboard-pending-role',JSON.stringify(pending));$('progress').textContent='正在生成单人参考候选…';});
  if(adopt)guarded(async()=>{const r=project.characters.find(r=>r.id===adopt);const url=e.target.dataset.url;const response=await fetch(url);if(!response.ok)throw new Error('候选图片读取失败。');const blob=await response.blob();r.asset=await upload(new File([blob],'character.png',{type:'image/png'}));draft();await save();localStorage.removeItem('storyboard-pending-role');renderCharacters();renderCast();});
});
function addShot(copy=false){if(project.shots.length>=200)return notice('每个项目最多 200 个分镜。',true);const old=copy?currentShot():null;const s={id:uid(),title:`第 ${project.shots.length+1} 镜`,cast:old?[...old.cast]:project.characters.slice(0,1).map(r=>r.id),action:old?.action||'',guide:old?.guide||''};project.shots.push(s);shotId=s.id;draft();renderShots();}
$('addShot').onclick=()=>addShot();$('duplicateShot').onclick=()=>addShot(true);
$('shotList').onclick=e=>{if(e.target.dataset.shot){shotId=e.target.dataset.shot;renderShots();}};
$('shotTitle').oninput=()=>{currentShot().title=$('shotTitle').value;draft();const b=$('shotList').querySelector(`[data-shot="${shotId}"]`);if(b)b.textContent=`${project.shots.findIndex(s=>s.id===shotId)+1}. ${currentShot().title}`;};
$('action').oninput=()=>{currentShot().action=$('action').value;draft();};
$('cast').onchange=e=>{const id=e.target.dataset.cast;if(!id)return;const s=currentShot();if(e.target.checked){if(s.cast.length>=8){e.target.checked=false;return notice('每个分镜最多 8 名主要角色。',true);}s.cast.push(id);}else s.cast=s.cast.filter(r=>r!==id);draft();renderCast();};
$('guideFile').onchange=()=>{const file=$('guideFile').files[0],s=currentShot();if(file&&s)guarded(async()=>{s.guide=await upload(file);draft();await save();renderShots();});};
$('clearGuide').onclick=()=>{if(currentShot()){currentShot().guide='';$('guideFile').value='';draft();renderShots();}};
$('engine').onchange=()=>{$('modelBadge').textContent=$('engine').value==='qwen21'?'Qwen-Image 2.1 · 本地':'Qwen-Image-Edit 2511 · 原有方案';$('referenceHint').textContent=$('engine').value==='qwen21'?'新版最多 10 张参考图（含人物、构图、画风）；12GB 显存建议先从 1–3 张开始。':'原有引擎最多 3 张参考图（含人物、构图、画风）；预览 12 步，精细 20 步。';};
$('generate').onclick=()=>guarded(async()=>{
  if(pending)throw new Error('请等待当前任务完成。');
  const s=currentShot();if(!s?.action.trim()||!s.cast.length)throw new Error('请选择出场角色并填写动作和场景。');
  await save();
  const form=new FormData();Object.entries({mode:'storyboard',storyboard_project_id:project.id,storyboard_shot_id:shotId,storyboard_request_id:uid(),image_engine:$('engine').value,aspect:$('aspect').value,quality:$('quality').value,export_dir:taskOutputFolder(),seed:$('seed').value}).forEach(([k,v])=>form.append(k,v));
  const {job}=await api('/api/images/generate',{method:'POST',body:form});pending={id:job.id,kind:'shot',pid:project.id};$('progress').textContent='分镜已提交，正在准备本地模型…';await refreshRuns();
});
$('cancel').onclick=()=>guarded(async()=>{if(pending)await api(`/api/images/jobs/${pending.id}/cancel`,{method:'POST'});});
async function refreshRuns(){if(!project)return;const data=(await api(`/api/storyboards/${project.id}`)).project;project.runs=data.runs;project.assets={...project.assets,...data.assets};renderResults();}
async function poll(){if(!pending||working)return;try{
  const {job}=await api(`/api/images/jobs/${pending.id}`);
  $('progress').textContent=job.stage+(job.error?'\n'+job.error:'');
  if(!active.has(job.status)){
    const done=pending;pending=null;controls();
    if(done.kind==='role'&&job.status==='completed'&&done.pid===project?.id){localStorage.setItem('storyboard-pending-role',JSON.stringify({...done,url:job.image_url}));showCandidate();}
    else if(done.kind==='role')localStorage.removeItem('storyboard-pending-role');
    await refreshRuns();
  }
}catch(e){notice('状态读取失败，请勿重复提交。'+e.message,true);if(e.status===404){pending=null;localStorage.removeItem('storyboard-pending-role');await refreshRuns();controls();}}}
async function init(){working=true;controls();try{const status=await api('/api/status');token=status.csrf_token;outputFolderReady=!!status.storyboard_output_folder;updateOutputSummary();pipelineReady=!!status.character_pipeline_workflow;styleReferenceReady=!!status.storyboard_style_reference;optimizerReady=!!status.storyboard_prompt_optimizer;for(const option of $('promptProfile').options){const p=status.prompt_ai?.profiles?.[option.value];option.textContent=p?.label||option.textContent;option.disabled=!p?.installed;option.dataset.model=p?.model||'';}updatePromptInfo();const projects=await listProjects();const id=new URLSearchParams(location.search).get('project')||localStorage.getItem('storyboard-last');if(projects.length)await loadProject(projects.find(p=>p.id===id)?.id||projects[0].id);else notice('工作台已连接。新建项目即可开始。');const role=JSON.parse(localStorage.getItem('storyboard-pending-role')||'null');if(role&&role.pid===project?.id&&!pending&&!role.url)pending=role;showCandidate();}catch(e){notice(e.message,true);}finally{working=false;controls();}}
let cropImage=null, cropBox=null, cropStart=null;
function cropPoint(e){const rect=$('cropCanvas').getBoundingClientRect();return {x:Math.max(0,Math.min(cropImage.width,(e.clientX-rect.left)*cropImage.width/rect.width)),y:Math.max(0,Math.min(cropImage.height,(e.clientY-rect.top)*cropImage.height/rect.height))};}
function drawCrop(){const ctx=$('cropCanvas').getContext('2d');ctx.clearRect(0,0,cropImage.width,cropImage.height);ctx.drawImage(cropImage,0,0);if(cropBox){ctx.strokeStyle='#5bd8ff';ctx.lineWidth=Math.max(2,cropImage.width/300);ctx.strokeRect(cropBox.x,cropBox.y,cropBox.w,cropBox.h);}}
$('results').onclick=e=>{const url=e.target.dataset.cropUrl;if(!url)return;guarded(async()=>{cropImage=new Image();cropImage.src=url;await cropImage.decode();cropBox=null;cropStart=null;$('cropCanvas').width=cropImage.width;$('cropCanvas').height=cropImage.height;$('cropRole').innerHTML=project.characters.map(r=>`<option value="${r.id}">${esc(r.name)}</option>`).join('');$('cropHint').textContent='请先框选一位人物。';$('saveCrop').disabled=true;drawCrop();$('cropDialog').showModal();});};
$('cropCanvas').onpointerdown=e=>{cropStart=cropPoint(e);$('cropCanvas').setPointerCapture(e.pointerId);};
$('cropCanvas').onpointermove=e=>{if(!cropStart)return;const p=cropPoint(e);cropBox={x:Math.round(Math.min(p.x,cropStart.x)),y:Math.round(Math.min(p.y,cropStart.y)),w:Math.round(Math.abs(p.x-cropStart.x)),h:Math.round(Math.abs(p.y-cropStart.y))};drawCrop();};
$('cropCanvas').onpointerup=()=>{cropStart=null;const valid=cropBox&&cropBox.w>=32&&cropBox.h>=32;$('saveCrop').disabled=!valid;$('cropHint').textContent=valid?`已选择 ${cropBox.w} × ${cropBox.h}，请确认只包含目标人物。`:'框选区域太小，请重新拖动。';};
$('closeCrop').onclick=()=>$('cropDialog').close();
$('saveCrop').onclick=()=>guarded(async()=>{if(!cropBox||cropBox.w<32||cropBox.h<32)return;const r=project.characters.find(r=>r.id===$('cropRole').value);if(!r)throw new Error('请选择角色。');const out=document.createElement('canvas');out.width=cropBox.w;out.height=cropBox.h;out.getContext('2d').drawImage(cropImage,cropBox.x,cropBox.y,cropBox.w,cropBox.h,0,0,cropBox.w,cropBox.h);const blob=await new Promise(resolve=>out.toBlob(resolve,'image/png'));r.asset=await upload(new File([blob],'fixed-character.png',{type:'image/png'}));draft();await save();$('cropDialog').close();renderCharacters();renderCast();notice(`已保存 ${r.name} 的固定参考，原分镜图片保留。`);});
setInterval(poll,2500);init();

function promptFingerprint(){const s=currentShot();return JSON.stringify({pid:project?.id,shot:s,roles:project?.characters,style:project?.style,style_asset:project?.style_asset,aspect:$('aspect').value});}
function updatePromptInfo(){if(!optimizerReady){$('promptModelInfo').textContent='新版提示词接口已准备，等待工作台切换；当前视频队列继续运行。';return;}$('promptModelInfo').textContent=$('promptProfile').selectedOptions[0]?.dataset.model||'模型未就绪';}
$('promptProfile').onchange=updatePromptInfo;
$('optimizeShot').onclick=()=>guarded(async()=>{
  if(pending)throw new Error('请等待当前生成完成。');
  const s=currentShot();if(!s?.action.trim()||!s.cast.length)throw new Error('先选择人物并填写动作。');
  await save();const fingerprint=promptFingerprint();const pid=project.id,sid=shotId;
  notice('本地模型正在整理人物、动作和构图，首次加载可能较慢…');
  const result=await post('/api/optimize-prompt',{mode:'storyboard',project_id:pid,shot_id:sid,aspect:$('aspect').value,model_profile:$('promptProfile').value});
  const text=result.optimization.image_prompt;
  if(!text?.trim()||text.length>2600)throw new Error('优化结果为空或过长，原文已保留。');
  if(fingerprint!==promptFingerprint())throw new Error('等待期间分镜或角色发生变化，原文保持不变，请重新优化。');
  optimizationDraft={fingerprint,pid,sid};optimizationUndo=null;
  $('optimizedAction').value=text;$('optimizationPreview').hidden=false;$('undoOptimization').disabled=true;
  $('optimizationWarnings').textContent=result.optimization.warnings.join('；');
  notice(`优化完成：${result.model}，${result.elapsed_seconds} 秒。检查建议后点击采用，当前原文未修改。`);
});
$('applyOptimization').onclick=()=>guarded(async()=>{
  if(!optimizationDraft||optimizationDraft.fingerprint!==promptFingerprint())throw new Error('人物或分镜已改变，请重新优化，避免套用过期建议。');
  const text=$('optimizedAction').value.trim();if(!text||text.length>2600)throw new Error('提示词须为 1–2600 字。');
  optimizationUndo={pid:project.id,sid:shotId,original:currentShot().action,applied:text};
  currentShot().action=text;$('action').value=text;draft();await save();
  optimizationDraft=null;$('undoOptimization').disabled=false;notice('已采用并保存优化提示词，角色参考保持不变。');
});
$('undoOptimization').onclick=()=>guarded(async()=>{
  if(!optimizationUndo||optimizationUndo.pid!==project.id||optimizationUndo.sid!==shotId||currentShot().action!==optimizationUndo.applied)throw new Error('分镜已再次修改，无法直接恢复，请保留当前编辑。');
  currentShot().action=optimizationUndo.original;$('action').value=currentShot().action;draft();await save();optimizationUndo=null;optimizationDraft={fingerprint:promptFingerprint(),pid:project.id,sid:shotId};$('undoOptimization').disabled=true;notice('已恢复采用前原文。');
});

function taskOutputFolder(){
  const folder=$('imageOutputFolder').value.trim();
  if(folder&&!outputFolderReady)throw new Error('保存位置功能尚未启用，请在当前批次结束后重启工作台。');
  return folder;
}
function updateOutputSummary(){
  const folder=$('imageOutputFolder').value.trim();
  $('outputFolderHint').textContent=outputFolderReady?'仅用于新提交的任务，已开始的批次保持原设置。图片也会保留在项目中；留空使用默认位置。':'保存位置功能已安装，需在当前批次结束后重启工作台启用。';
  $('batchOutputSummary').textContent='本批次保存到：'+(folder||($('batchWorkflow').value==='character_sequence'?'用户图片目录 / AI-Workbench':'项目默认结果文件夹'))+'。开始后固定质量与保存位置。';
}
$('imageOutputFolder').oninput=updateOutputSummary;
$('clearImageOutputFolder').onclick=()=>{$('imageOutputFolder').value='';updateOutputSummary();};
$('chooseImageOutputFolder').onclick=()=>guarded(async()=>{
  notice('请选择保存文件夹；如果窗口没有置前，可查看任务栏。');
  const result=await post('/api/image-output-folder',{});
  if(result.folder){$('imageOutputFolder').value=result.folder;notice('已选择本次任务保存位置。');}
  else notice('已取消选择，保存位置保持不变。');
  updateOutputSummary();
});
function syncImageQuality(){
  const upgraded=$('engine').value==='qwen21';
  for(const id of ['quality','batchQuality']){
    $(id).options[0].textContent=upgraded?'预览 · 新版 25 步':'预览 · 原有方案 12 步';
    $(id).options[1].textContent=upgraded?'精细 · 新版 40 步':'精细 · 原有方案 20 步';
  }
  $('batchQuality').value=$('quality').value;
}
$('quality').addEventListener('change',syncImageQuality);
$('engine').addEventListener('change',syncImageQuality);
$('batchQuality').onchange=()=>{$('quality').value=$('batchQuality').value;syncImageQuality();};
$('batchWorkflow').addEventListener('change',updateOutputSummary);
syncImageQuality();
