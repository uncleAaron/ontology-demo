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
from pydantic import BaseModel,Field,ConfigDict
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

app=FastAPI(title='Ontology Workbench API',version='0.3.0',lifespan=lifespan)
class TaskInput(BaseModel):
    mode:Literal['demo','model']='demo'
    kind:Literal['knowledge','complaint','operations','requirements','coding']
    document_ids:list[str]=Field(default_factory=list,max_length=20)
    scenario:Literal['unreleased','unverified','verified','conflict','stale']='unverified'
    prompt:str=Field(default='客户已经付款，订单仍显示未支付。请排查。',min_length=1,max_length=2000)

@app.get('/api/health')
def health():
    from .model import ModelSettings,ModelError
    try:ModelSettings.from_env();configured=True
    except ModelError:configured=False
    return {'status':'ok','mode':'demo-and-optional-model','database':'sqlite','model_connected':False,'model_configured':configured,'business_connectors':'simulated'}
@app.get('/api/documents')
def documents(q:str=Query('',max_length=100),include_inactive:bool=False):
    with connect(DB) as c:return [dict(r) for r in c.execute('SELECT d.id,d.project,r.title,r.body,r.version,h.status,h.revision,h.published_revision,h.generation,(SELECT count(*) FROM document_chunks ch WHERE ch.document_id=d.id AND ch.revision=h.published_revision) indexed_chunks FROM documents d JOIN document_heads h ON h.document_id=d.id JOIN document_revisions r ON r.document_id=d.id AND r.revision=h.revision WHERE d.project=? AND (? OR h.status="active") AND (instr(r.title,?)>0 OR instr(r.body,?)>0) ORDER BY d.id',(PROJECT,include_inactive,q,q))]
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
def tasks_create(body:TaskInput):
    if body.kind=='knowledge' and body.mode!='model':raise HTTPException(422,'资料问答需要模型辅助模式')
    if body.document_ids and body.mode!='model':raise HTTPException(422,'指定资料仅用于模型辅助模式')
    if body.mode=='model':
        from .model import ModelSettings,ModelError
        try:ModelSettings.from_env()
        except ModelError as e:raise HTTPException(503,str(e))
    try:tid=create_task(DB,body.kind,body.scenario,body.prompt,body.mode,body.document_ids)
    except ValueError as e:raise HTTPException(422,str(e))
    return {'id':tid,'status':'queued'}
@app.get('/api/tasks')
def tasks_list():
    with connect(DB) as c:return [dict(r) for r in c.execute('SELECT id,kind,mode,status,created_at FROM tasks ORDER BY created_at DESC LIMIT 30')]
@app.get('/api/tasks/{task_id}')
def task_get(task_id:str):
    with connect(DB) as c:
        r=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not r:raise HTTPException(404,'任务不存在')
        t=dict(r);t['result']=json.loads(t['result']) if t['result'] else None
        t['document_ids']=json.loads(t['document_ids'])
        t['evidence']=[dict(e)|{'payload':json.loads(e['payload'])} for e in c.execute('SELECT * FROM evidence WHERE task_id=? AND attempt=?',(task_id,t['attempts']))]
        from .knowledge import task_warnings
        t['knowledge_warnings']=task_warnings(c,task_id,t['attempts'])
        t['knowledge_tracking']='tracked' if c.execute('SELECT 1 FROM task_knowledge_refs WHERE task_id=? AND attempt=?',(task_id,t['attempts'])).fetchone() else 'no-document-dependencies-recorded'
        t['steps']=[dict(e) for e in c.execute('SELECT * FROM steps WHERE task_id=? AND attempt=? ORDER BY id',(task_id,t['attempts']))]
        t['tool_calls']=[dict(x)|{'arguments':json.loads(x['arguments']),'result':json.loads(x['result'])} for x in c.execute('SELECT * FROM tool_calls WHERE task_id=? AND attempt=? ORDER BY id',(task_id,t['attempts']))]
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
        from .knowledge import register_document
        register_document(c,doc_id)
    return {'id':doc_id,'project':PROJECT,'status':'demo-imported'}

@app.get('/api/search')
def search_api(q:str=Query(min_length=1,max_length=2000),document_id:list[str]=Query(default=[]),limit:int=Query(8,ge=1,le=20)):
    from .retrieval import search,validate_selection
    with connect(DB) as c:
        try:validate_selection(c,document_id)
        except ValueError as e:raise HTTPException(422,str(e))
        return search(c,q,document_id,limit)

@app.get('/api/chunks/{chunk_id}')
def chunk_api(chunk_id:str):
    from .retrieval import read_chunk
    with connect(DB) as c:
        try:return read_chunk(c,chunk_id,require_active=False)
        except ValueError:raise HTTPException(404,'片段不存在或不可访问')

class RelationInput(BaseModel):
    source:str
    target:str
    type:str
    origin:str=Field(min_length=1,max_length=300)
    document_id:str|None=None

@app.post('/api/relations/suggestions',status_code=201)
def suggest_relation(body:RelationInput):
    with connect(DB) as c:
        c.execute('BEGIN IMMEDIATE')
        a=c.execute('SELECT * FROM objects WHERE id=? AND project=?',(body.source,PROJECT)).fetchone()
        b=c.execute('SELECT * FROM objects WHERE id=? AND project=?',(body.target,PROJECT)).fetchone()
        t=c.execute('SELECT * FROM relation_types WHERE id=?',(body.type,)).fetchone()
        if not a or not b:raise HTTPException(404,'对象不存在或不可访问')
        if not t or (a['type'],b['type'])!=(t['source_type'],t['target_type']):raise HTTPException(422,'关系不符合本体类型约束')
        if body.document_id:
            from .knowledge import head
            try:h=head(c,body.document_id)
            except KeyError:raise HTTPException(404,'来源不存在或不可访问')
            if h['status']!='active':raise HTTPException(409,'来源尚未生效或已撤回')
        relation_id='rel-'+uuid.uuid4().hex[:12]
        c.execute('INSERT INTO relations VALUES(?,?,?,?,?,?,?,?,?)',(relation_id,body.source,body.target,body.type,'candidate','2026-09-30T00:00:00Z',None,body.origin,body.document_id))
        if body.document_id:c.execute('INSERT INTO relation_sources VALUES(?,?,?,1)',(relation_id,body.document_id,h['published_revision']))
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
        edges.append({'id':e['id']+'-used','source':e['id'],'target':'decision' if t['result'] else task_id,'label':'冲突记录' if e['classification']=='conflict' else '引用','status':'candidate' if e['classification']=='conflict' else 'confirmed','origin':e['origin']})
    return {'nodes':nodes,'edges':edges,'truncated':False,'scope':{'task_id':task_id,'snapshot':True},'mode':'historical-task-snapshot','knowledge_warnings':t['knowledge_warnings'],'knowledge_tracking':t['knowledge_tracking']}


class MaintenanceInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_generation:int=Field(ge=1)
    reason:str=Field(min_length=1,max_length=1000)
class RevisionInput(MaintenanceInput):
    title:str=Field(min_length=1,max_length=200)
    body:str=Field(min_length=1,max_length=100000)
    version:str=Field(min_length=1,max_length=30)
class ReviewInput(MaintenanceInput):
    decision:Literal['approve','reject']
class RelationReviewInput(MaintenanceInput):
    expected_document_revision:int=Field(ge=1)

def maintenance_write(operation,identifier,body):
    from .knowledge import Conflict
    try:
        with connect(DB) as c:
            c.execute('BEGIN IMMEDIATE')
            return operation(c,identifier,body.model_dump())
    except KeyError:raise HTTPException(404,'资料或关系不存在或不可访问')
    except Conflict as e:raise HTTPException(409,str(e))

@app.get('/api/documents/{document_id}/maintenance')
def document_maintenance(document_id:str):
    from .knowledge import maintenance
    try:
        with connect(DB) as c:
            c.execute('BEGIN')
            return maintenance(c,document_id)
    except KeyError:raise HTTPException(404,'资料不存在或不可访问')

@app.post('/api/documents/{document_id}/revisions',status_code=201)
def document_revision(document_id:str,body:RevisionInput):
    from .knowledge import revise
    return maintenance_write(revise,document_id,body)

@app.post('/api/documents/{document_id}/review')
def document_review(document_id:str,body:ReviewInput):
    from .knowledge import review
    return maintenance_write(review,document_id,body)

@app.post('/api/documents/{document_id}/withdraw')
def document_withdraw(document_id:str,body:MaintenanceInput):
    from .knowledge import withdraw
    return maintenance_write(withdraw,document_id,body)

@app.post('/api/relations/{relation_id}/review')
def relation_review(relation_id:str,body:RelationReviewInput):
    from .knowledge import review_relation
    return maintenance_write(review_relation,relation_id,body)

app.mount('/',StaticFiles(directory=ROOT/'static',html=True),name='frontend')
