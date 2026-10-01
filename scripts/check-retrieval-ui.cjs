// DOM + real API/Worker + local mock model: no external model credentials or fees.
const {JSDOM}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'retrieval-ui-'));
let server,dom;const modelErrors=[];
const model=http.createServer(async(req,res)=>{
 try{
  let raw='';for await(const part of req)raw+=part;
  const body=JSON.parse(raw);
  const message=body.messages.find(m=>m.content?.startsWith('服务端检索上下文'));
  const context=JSON.parse(message.content.slice(message.content.indexOf('：')+1));
  assert(context.evidence.some(e=>e.data.chunk.body.includes('AURORA-729')));
  assert(context.evidence.every(e=>e.data.chunk.document_id===context.scope_document_ids[0]));
  const args={summary:'北极校验码为 AURORA-729。',citations:[context.evidence[0].evidence_id],unknowns:[],next_steps:[]};
  res.writeHead(200,{'Content-Type':'application/json'});
  res.end(JSON.stringify({choices:[{finish_reason:'tool_calls',message:{role:'assistant',content:null,tool_calls:[{id:'submit',type:'function',function:{name:'submit_answer',arguments:JSON.stringify(args)}}]}}]}));
 }catch(e){modelErrors.push(e.message);res.writeHead(500);res.end('{}');}
});
const until=async(fn)=>{for(let i=0;i<250;i++){if(fn())return;await new Promise(r=>setTimeout(r,20));}throw Error('UI timeout: '+dom?.window.document.querySelector('#alert').textContent);};
(async()=>{
 await new Promise(r=>model.listen(0,'127.0.0.1',r));
 const port=process.env.ONTOLOGY_RETRIEVAL_UI_PORT||'8768',base='http://127.0.0.1:'+port;
 server=spawn(process.env.PYTHON||'python3',['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',port],{cwd:root,env:{...process.env,ONTOLOGY_DB:path.join(temp,'test.sqlite3'),ONTOLOGY_MODEL_URL:`http://127.0.0.1:${model.address().port}/chat/completions`,ONTOLOGY_MODEL_NAME:'mock',ONTOLOGY_MODEL_KEY:'local-test'},stdio:'ignore'});
 let ready=false;for(let i=0;i<100;i++){try{if((await fetch(base+'/api/health')).ok){ready=true;break;}}catch{}await new Promise(r=>setTimeout(r,50));}assert(ready);
 const post=async(url,body)=>{const response=await fetch(base+url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert(response.ok,await response.clone().text());return response.json();};
 const source=await post('/api/documents',{title:'<script>测试航线手册</script>',body:'背景文字与人工流程。\n'.repeat(800)+'\n# 北极校验\n北极校验码为 AURORA-729。'});
 dom=new JSDOM(fs.readFileSync(path.join(root,'static/index.html'),'utf8'),{url:base,runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window,d=w.document,get=s=>d.querySelector(s),errors=[];
 w.fetch=(url,options)=>fetch(new URL(url,base),options);w.addEventListener('error',e=>errors.push(e.message));
 w.eval(fs.readFileSync(path.join(root,'static/app.js'),'utf8'));
 await until(()=>get('#connection').textContent.includes('已连接'));
 get('[data-page="tasks"]').click();await until(()=>[...get('#task-documents').options].some(o=>o.value===source.id));
 get('#task-kind').value='knowledge';get('#task-kind').dispatchEvent(new w.Event('change'));
 assert.equal(get('#task-mode').value,'model');assert(get('#scenario-field').hidden);
 for(const o of get('#task-documents').options)o.selected=o.value===source.id;
 get('#task-prompt').value='北极校验码是多少';get('#run-task').click();
 await until(()=>get('#task-result').textContent.includes('北极校验码为 AURORA-729。')&&get('[data-chunk]'));
 assert(get('#task-result').textContent.includes('最终引用'));assert.equal(get('#task-result script'),null);
 get('[data-chunk]').click();await until(()=>get('#chunk-preview').textContent.includes('当前有效原文片段'));
 await post('/api/documents/'+source.id+'/withdraw',{expected_generation:1,reason:'撤回联测'});
 get('[data-chunk]').click();await until(()=>get('#chunk-preview').textContent.includes('历史原文片段'));
 const found=await (await fetch(base+'/api/search?q='+encodeURIComponent('北极校验码'))).json();assert(!found.chunks.some(ch=>ch.document_id===source.id));
 assert.deepEqual(errors,[]);assert.deepEqual(modelErrors,[]);
 console.log('Retrieval DOM/API/Worker passed: select own long source → auto-load tail → cite → locate revision → withdraw → blocked search/history warning.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>{
 dom?.window.close();model.close();if(server){server.on('close',()=>fs.rmSync(temp,{recursive:true,force:true}));server.kill();}else fs.rmSync(temp,{recursive:true,force:true});
});
