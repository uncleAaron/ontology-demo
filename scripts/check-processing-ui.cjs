// DOM + real API/Worker + local mock model: no external model credentials or fees.
const {JSDOM}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'processing-ui-'));
let server,dom,modelCalls=0;const modelErrors=[],serverLogs=[];
const model=http.createServer(async(req,res)=>{
 try{
  let raw='';for await(const part of req)raw+=part;
  const body=JSON.parse(raw);
  const name=body.tools[0].function.name,data=JSON.parse(body.messages[1].content);
  modelCalls++;
  let args;
  if(name==='submit_analysis')args={observations:[{text:'双人复核要求',support:{chunk_id:data.chunks[0].id,quote:data.chunks[0].body.slice(0,20)}}]};
  else {assert.equal(name,'submit_candidates');args={candidates:[{kind:'summary',title:'双人复核摘要',body:'北极校验须双人复核。',supports:[data.observations[0].support]}]};}
  res.writeHead(200,{'Content-Type':'application/json'});
  res.end(JSON.stringify({choices:[{finish_reason:'tool_calls',message:{role:'assistant',content:null,tool_calls:[{id:'result',type:'function',function:{name,arguments:JSON.stringify(args)}}]}}]}));
 }catch(e){modelErrors.push(e.message);res.writeHead(500);res.end('{}');}
});
const until=async(fn)=>{for(let i=0;i<250;i++){if(fn())return;await new Promise(r=>setTimeout(r,20));}throw Error('UI timeout: '+dom?.window.document.querySelector('#alert').textContent);};
(async()=>{
 await new Promise(r=>model.listen(0,'127.0.0.1',r));
 const port=process.env.ONTOLOGY_PROCESSING_UI_PORT||'8769',base='http://127.0.0.1:'+port;
 server=spawn(process.env.PYTHON||'python3',['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',port],{cwd:root,env:{...process.env,HTTP_PROXY:'',HTTPS_PROXY:'',ALL_PROXY:'',http_proxy:'',https_proxy:'',all_proxy:'',NO_PROXY:'*',no_proxy:'*',ONTOLOGY_DB:path.join(temp,'test.sqlite3'),ONTOLOGY_MODEL_URL:`http://127.0.0.1:${model.address().port}/chat/completions`,ONTOLOGY_MODEL_NAME:'mock',ONTOLOGY_MODEL_KEY:'local-test'},stdio:['ignore','ignore','pipe']});
 server.stderr.on('data',data=>serverLogs.push(data.toString()));
 let ready=false;for(let i=0;i<100;i++){try{if((await fetch(base+'/api/health')).ok){ready=true;break;}}catch{}await new Promise(r=>setTimeout(r,50));}assert(ready);
 const post=async(url,body)=>{const response=await fetch(base+url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert(response.ok,await response.clone().text());return response.json();};
 const source=await post('/api/documents',{title:'<script>航线手册</script>',body:'北极校验须双人复核。校验码为 AURORA-729。'});
 dom=new JSDOM(fs.readFileSync(path.join(root,'static/index.html'),'utf8'),{url:base,runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window,d=w.document,get=s=>d.querySelector(s),errors=[];
 w.fetch=(url,options)=>fetch(new URL(url,base),options);w.addEventListener('error',e=>errors.push(e.message));
 w.eval(fs.readFileSync(path.join(root,'static/app.js'),'utf8'));
 await until(()=>get('#connection').textContent.includes('已连接'));
 get('[data-page="knowledge"]').click();await until(()=>get('[data-doc="'+source.id+'"]'));
 get('[data-doc="'+source.id+'"]').click();await until(()=>get('#process-source')&&get('#doc-reader').textContent.includes(source.id));
 get('#process-source').click();await until(()=>get('.knowledge-review'));
 assert.equal(modelCalls,2);assert(get('#processing-panel').textContent.includes('待审核'));
 assert.equal(get('#processing-panel script'),null);
 let found=await (await fetch(base+'/api/knowledge?q='+encodeURIComponent('双人复核'))).json();assert.equal(found.length,0);
 get('[data-source-chunk]').click();await until(()=>get('.source-chunk').textContent.includes('AURORA-729'));
 const form=get('.knowledge-review');form.querySelector('[name="reason"]').value='已对照原文逐字核实';
 form.dispatchEvent(new w.SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:form.querySelector('[value="approve"]')}));
 await until(()=>get('.knowledge-review [value="withdraw"]'));
 found=await (await fetch(base+'/api/knowledge?q='+encodeURIComponent('双人复核'))).json();assert.equal(found.length,1);
 const active=get('.knowledge-review');active.querySelector('[name="reason"]').value='撤回演示';
 active.dispatchEvent(new w.SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:active.querySelector('[value="withdraw"]')}));
 await until(()=>get('#processing-panel').textContent.includes('已撤回')&&!get('.knowledge-review'));
 found=await (await fetch(base+'/api/knowledge?q='+encodeURIComponent('双人复核'))).json();assert.equal(found.length,0);
 assert.deepEqual(errors,[]);assert.deepEqual(modelErrors,[]);
 console.log('Processing DOM/API/Worker passed: enqueue → batch checkpoint → candidates → original quote → review → published search → withdraw.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>{
 dom?.window.close();model.close();if(server){server.on('close',()=>fs.rmSync(temp,{recursive:true,force:true}));server.kill();}else fs.rmSync(temp,{recursive:true,force:true});
});
