// Exercise the real page handlers with a local API double; never calls a model.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const nodes=new Map(), storage=new Map(), requests=[];
function node(id){
  if(!nodes.has(id))nodes.set(id,{value:'',innerHTML:'',textContent:'',hidden:false,disabled:false,
    files:[],options: ['quality','batchQuality'].includes(id)?[{},{}]:[],selectedOptions:[],addEventListener(){},removeAttribute(key){delete this[key];},querySelector(){return null;}});
  return nodes.get(id);
}
const document={getElementById:node,querySelectorAll:()=>[],querySelector:()=>null};
let server={id:'project',revision:0,title:'测试',style:'水彩',style_asset:'',characters:[{id:'role',name:'主角',asset:'person',description:''}],
  shots:[{id:'shot',title:'第一幕',cast:['role'],action:'散步',guide:''}],assets:{person:{id:'person',url:'/person.png'}},runs:[]};
const clone=x=>JSON.parse(JSON.stringify(x));
const ctx={document,console,URLSearchParams,location:{search:''},crypto:{randomUUID:()=> 'new-shot'},
  localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
  setInterval(){},FormData:class{append(){}},
  fetch:async(url,options={})=>{
    requests.push(url);
    let data;
    if(url==='/api/status')data={csrf_token:'test',storyboard_style_reference:true};
    else if(url==='/api/storyboards')data={projects:[]};
    else if(url==='/api/storyboards/project/assets'){
      const asset={id:'style',url:'/style.png',original_name:'水彩.png'};server.assets.style=asset;data={asset};
    }else if(url==='/api/storyboards/project'){
      if(options.method==='POST'){server={...JSON.parse(options.body),revision:server.revision+1};}
      data={project:clone(server)};
    }else throw new Error('Unexpected API: '+url);
    return {ok:true,status:200,json:async()=>clone(data)};
  }};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/storyboards.js'),'utf8')+
  '\nthis.styleTest={loadProject,addShot,draft,render,controls,get:()=>project,setReady:v=>{styleReferenceReady=v;}};',ctx);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await settle();
  await ctx.styleTest.loadProject('project');
  assert.equal(node('styleFile').disabled,false);
  assert.equal(node('stylePreview').hidden,true);
  node('styleFile').files=[{name:'水彩.png'}];node('styleFile').onchange();await settle();
  assert.equal(server.style_asset,'style');
  assert.equal(server.characters[0].asset,'person');
  assert.equal(node('stylePreview').src,'/style.png');
  assert.equal(node('stylePreview').hidden,false);
  assert.match(node('notice').textContent,/已提交的批次保持原设置/);
  await ctx.styleTest.loadProject('project');
  assert.equal(node('stylePreview').src,'/style.png');
  ctx.styleTest.addShot(true);
  assert.equal(ctx.styleTest.get().style_asset,'style');
  assert.equal(ctx.styleTest.get().shots.length,2);
  // Restore a local draft without losing the selected shared style.
  await ctx.styleTest.loadProject('project');
  assert.equal(ctx.styleTest.get().style_asset,'style');
  assert.equal(ctx.styleTest.get().shots.length,2);
  node('clearStyle').onclick();await settle();
  assert.equal(server.style_asset,'');
  assert.equal(server.characters[0].asset,'person');
  assert.equal(node('stylePreview').hidden,true);
  assert.equal(node('stylePreview').src,undefined);
  assert.ok(server.assets.style); // Removal only unbinds the style, retaining the source.
  ctx.styleTest.setReady(false);ctx.styleTest.render();
  assert.equal(node('styleFile').disabled,true);
  assert.match(node('styleReferenceStatus').textContent,/等待工作台切换/);
  assert.ok(!requests.some(url=>url.includes('generate')));
  console.log('PASS: style upload/save/reload/draft/duplicate/remove and old-server guard; no generation.');
})().catch(error=>{console.error(error);process.exitCode=1;});
