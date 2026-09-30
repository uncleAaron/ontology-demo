import json
import os
import threading
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI,HTTPException,Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from .db import init,connect
from .graph import graph,find_path,object_dict,PROJECT
from .engine import create_task,process_one
ROOT=Path(__file__).resolve().parent.parent
DB=os.environ.get('ONTOLOGY_DB',str(ROOT/'data/demo.sqlite3'))

@asynccontextmanager
async def lifespan(app):
    init(DB);stop=threading.Event()
    def worker():
        while not stop.is_set():
            try:busy=process_one(DB)
            except Exception:
                import logging;logging.exception('Worker error');busy=False
            if not busy:stop.wait(.3)
    thread=threading.Thread(target=worker,daemon=True);thread.start()
    yield
    stop.set();thread.join(timeout=15)

app=FastAPI(title='Ontology Demo API',version='0.1.0',lifespan=lifespan)
class TaskInput(BaseModel):
    kind:Literal['complaint','operations','requirements','coding']
    scenario:Literal['unreleased','unverified','verified','conflict','stale']='unverified'
    prompt:str=Field(default='客户已经付款，订单仍显示未支付。请排查。',min_length=1,max_length=2000)

@app.get('/api/health')
def health():return {'status':'ok','mode':'demo','database':'sqlite','model_connected':False}
@app.get('/api/documents')
def documents(q:str=Query('',max_length=100)):
    with connect(DB) as c:return [dict(r) for r in c.execute('SELECT * FROM documents WHERE project=? AND (instr(title,?)>0 OR instr(body,?)>0) ORDER BY id',(PROJECT,q,q))]
@app.get('/api/ontology')
def ontology():
    with connect(DB) as c:
        return {'version':'1.0','types':[dict(r)|{'properties':json.loads(r['properties'])} for r in c.execute('SELECT * FROM types')],'relations':[dict(r) for r in c.execute('SELECT * FROM relation_types')]}
@app.get('/api/objects')
def objects():
    with connect(DB) as c:return [object_dict(r) for r in c.execute('SELECT * FROM objects WHERE project=?',(PROJECT,))]
@app.get('/api/graph')
def graph_api(center:str='svc-payment',depth:int=Query(2,ge=0,le=6),status:Literal['confirmed','candidate','all']='confirmed',limit:int=Query(40,ge=1,le=100),at:datetime=Query(datetime.fromisoformat('2026-09-30T00:00:00+00:00')),relation:str|None=None):
    try:return graph(DB,center,depth,status,at=at.isoformat().replace("+00:00","Z"),limit=limit,relation=relation)
    except KeyError:raise HTTPException(404,'对象不存在或不可访问')
@app.get('/api/paths')
def paths(start:str,end:str,depth:int=Query(6,ge=1,le=6)):
    try:return find_path(DB,start,end,depth)
    except KeyError:raise HTTPException(404,'对象不存在或不可访问')
@app.post('/api/tasks',status_code=202)
def tasks_create(body:TaskInput):return {'id':create_task(DB,body.kind,body.scenario,body.prompt),'status':'queued'}
@app.get('/api/tasks')
def tasks_list():
    with connect(DB) as c:return [dict(r) for r in c.execute('SELECT id,kind,status,created_at FROM tasks ORDER BY created_at DESC LIMIT 30')]
@app.get('/api/tasks/{task_id}')
def task_get(task_id:str):
    with connect(DB) as c:
        r=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not r:raise HTTPException(404,'任务不存在')
        t=dict(r);t['result']=json.loads(t['result']) if t['result'] else None
        t['evidence']=[dict(e)|{'payload':json.loads(e['payload'])} for e in c.execute('SELECT * FROM evidence WHERE task_id=?',(task_id,))]
        t['steps']=[dict(e) for e in c.execute('SELECT * FROM steps WHERE task_id=? ORDER BY id',(task_id,))]
        return t
class DocumentInput(BaseModel):
    title:str=Field(min_length=1,max_length=200)
    body:str=Field(min_length=1,max_length=100000)
    version:str=Field(default='1.0',min_length=1,max_length=30)

@app.post('/api/documents',status_code=201)
def document_create(body:DocumentInput):
    doc_id='doc-'+uuid.uuid4().hex[:12]
    with connect(DB) as c:
        c.execute('INSERT INTO documents VALUES(?,?,?,?,?)',(doc_id,body.title,body.version,body.body,PROJECT))
    return {'id':doc_id,'project':PROJECT,'status':'demo-imported'}

class RelationInput(BaseModel):
    source:str
    target:str
    type:str
    origin:str=Field(min_length=1,max_length=300)
    document_id:str|None=None

@app.post('/api/relations/suggestions',status_code=201)
def suggest_relation(body:RelationInput):
    with connect(DB) as c:
        a=c.execute('SELECT * FROM objects WHERE id=? AND project=?',(body.source,PROJECT)).fetchone()
        b=c.execute('SELECT * FROM objects WHERE id=? AND project=?',(body.target,PROJECT)).fetchone()
        t=c.execute('SELECT * FROM relation_types WHERE id=?',(body.type,)).fetchone()
        if not a or not b:raise HTTPException(404,'对象不存在或不可访问')
        if not t or (a['type'],b['type'])!=(t['source_type'],t['target_type']):raise HTTPException(422,'关系不符合本体类型约束')
        if body.document_id and not c.execute('SELECT 1 FROM documents WHERE id=? AND project=?',(body.document_id,PROJECT)).fetchone():raise HTTPException(404,'来源不存在或不可访问')
        relation_id='rel-'+uuid.uuid4().hex[:12]
        c.execute('INSERT INTO relations VALUES(?,?,?,?,?,?,?,?,?)',(relation_id,body.source,body.target,body.type,'candidate','2026-09-30T00:00:00Z',None,body.origin,body.document_id))
    return {'id':relation_id,'status':'candidate','message':'仅登记候选关系，不作为已确认事实'}

@app.get('/api/tasks/{task_id}/evidence-graph')
def task_graph(task_id:str):
    t=task_get(task_id)
    nodes=[{'id':task_id,'type':'task','label':'任务 '+task_id[-6:],'properties':{'kind':t['kind'],'status':t['status']}}]
    edges=[]
    if t['result']:
        nodes.append({'id':'decision','type':'decision','label':t['result']['title'],'properties':{'rule_version':t['result']['rule_version'],'decision':t['result']['decision']}})
        edges.append({'id':'result','source':task_id,'target':'decision','label':'交付','status':'confirmed','origin':'任务记录'})
    for e in t['evidence']:
        nodes.append({'id':e['id'],'type':'evidence','label':e['label'],'properties':e['payload']|{'source':e['origin'],'observed_at':e['observed_at'],'classification':e['classification']}})
        edges.append({'id':e['id']+'-used','source':e['id'],'target':'decision','label':'冲突记录' if e['classification']=='conflict' else '引用','status':'candidate' if e['classification']=='conflict' else 'confirmed','origin':e['origin']})
    return {'nodes':nodes,'edges':edges,'truncated':False,'scope':{'task_id':task_id,'snapshot':True},'mode':'historical-task-snapshot'}

app.mount('/',StaticFiles(directory=ROOT/'static',html=True),name='frontend')
