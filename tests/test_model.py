import json
import sqlite3
from dataclasses import replace
import httpx
import pytest
from app.db import init,connect
from app.engine import create_task,process_one
from app.agent import run_agent
from app.model import ModelSettings,ModelClient,ModelError
from app.tools import validate,invoke

@pytest.fixture
def db(tmp_path):
    p=str(tmp_path/'db.sqlite3');init(p);return p

def settings():return ModelSettings('https://models.example.test/v1/chat/completions','test-model','secret-test-key')

def call(name,args,cid='c1'):
    return {'role':'assistant','content':None,'tool_calls':[{'id':cid,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}

class ScriptedClient:
    def __init__(self,turns):self.turns=iter(turns);self.settings=settings()
    def complete(self,messages,tools):
        nxt=next(self.turns)
        if isinstance(nxt,Exception):raise nxt
        return nxt(messages) if callable(nxt) else nxt

def submit(messages):
    refs=[json.loads(m['content'])['evidence_id'] for m in messages if m['role']=='tool' and 'evidence_id' in json.loads(m['content'])]
    return call('submit_answer',{'summary':'模拟证据显示需要核实。','citations':refs,'unknowns':['本次请求日志'],'next_steps':['人工审核']})

def test_transport_contract_and_secret_redaction():
    def handler(req):
        body=json.loads(req.content)
        assert req.headers['Authorization']=='Bearer secret-test-key'
        assert body['model']=='test-model' and body['parallel_tool_calls'] is False
        return httpx.Response(200,json={'choices':[{'message':call('inspect_runtime',{})}]})
    c=ModelClient(settings(),httpx.MockTransport(handler))
    assert c.complete([{'role':'user','content':'test'}],[])['tool_calls'][0]['function']['name']=='inspect_runtime'
    assert 'secret-test-key' not in repr(settings())
    c=ModelClient(settings(),httpx.MockTransport(lambda req:httpx.Response(401,text='secret-test-key')))
    with pytest.raises(ModelError) as e:c.complete([],[])
    assert 'secret-test-key' not in str(e.value)

@pytest.mark.parametrize('response',[{}, {'choices':[]},{'choices':[{'message':{'tool_calls':'bad'}}]}])
def test_malformed_provider(response):
    client=ModelClient(settings(),httpx.MockTransport(lambda req:httpx.Response(200,json=response)))
    with pytest.raises(ModelError):client.complete([],[])

def test_reasoning_is_preserved_across_tool_rounds(db):
    def handler(req):
        messages=json.loads(req.content)['messages']
        if not any(m['role']=='tool' for m in messages):
            message=call('inspect_runtime',{})|{'reasoning_content':'检查运行证据','content':'先取证'}
        else:
            prior=next(m for m in messages if m['role']=='assistant')
            assert prior['reasoning_content']=='检查运行证据'
            assert prior['content']=='先取证'
            message=submit(messages)
        return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':'tool_calls'}]})
    result,evidence=run_agent(db,{'id':'t','kind':'complaint','prompt':'排查','scenario':'unverified'},ModelClient(settings(),httpx.MockTransport(handler)))
    assert evidence and result['citations']

@pytest.mark.parametrize('choice,reason',[
    ({'message':call('inspect_runtime',{}),'finish_reason':'length'},'截断'),
    ({'message':{'tool_calls':[{}]*5}},'工具数量过多'),
    ({'message':{'tool_calls':'bad'}},'必须是数组'),
    ({},'message'),
])
def test_specific_protocol_errors(choice,reason):
    client=ModelClient(settings(),httpx.MockTransport(lambda req:httpx.Response(200,json={'choices':[choice]})))
    with pytest.raises(ModelError,match=reason):client.complete([],[])

def test_timeout_and_context_budget():
    def slow(req):raise httpx.ReadTimeout('provider details secret-test-key')
    with pytest.raises(ModelError,match='超时'):ModelClient(settings(),httpx.MockTransport(slow)).complete([],[])
    c=ModelClient(replace(settings(),max_context_chars=3))
    with pytest.raises(ModelError,match='上下文'):c.complete([{'content':'long'}],[])

def test_config_rejects_unsafe_and_missing(monkeypatch):
    for k in ['ONTOLOGY_MODEL_URL','ONTOLOGY_MODEL_KEY','ONTOLOGY_MODEL_NAME']:monkeypatch.delenv(k,raising=False)
    with pytest.raises(ModelError):ModelSettings.from_env()
    monkeypatch.setenv('ONTOLOGY_MODEL_KEY','secret');monkeypatch.setenv('ONTOLOGY_MODEL_NAME','test')
    monkeypatch.setenv('ONTOLOGY_MODEL_URL','http://external.test/v1/chat/completions')
    with pytest.raises(ModelError):ModelSettings.from_env()
    monkeypatch.setenv('ONTOLOGY_MODEL_URL','https://secret@example.test/v1/chat/completions')
    with pytest.raises(ModelError):ModelSettings.from_env()
    monkeypatch.setenv('ONTOLOGY_MODEL_URL','http://127.0.0.1:9999/v1/chat/completions')
    assert ModelSettings.from_env().model=='test'

def test_registry_rejects_unknown_extra_and_private(db):
    task={'kind':'complaint','scenario':'verified'}
    for name,args in [('shell',{'command':'ls'}),('get_object',{'object_id':'payment','project':'other'}),('inspect_code_example',{})]:
        with pytest.raises(ValueError):validate(name,args,'complaint')
    with pytest.raises(ValueError):invoke(db,task,'get_object',{'object_id':'secret-service'})
    with pytest.raises(ValueError):invoke(db,task,'read_document',{'document_id':'kb-private'})

@pytest.mark.parametrize('scenario,expected',[('verified','confirmed'),('conflict','insufficient'),('stale','insufficient')])
def test_agent_runtime_gate_and_citations(db,scenario,expected):
    tid=create_task(db,'complaint',scenario,'模拟支付排查','model')
    client=ScriptedClient([call('inspect_runtime',{}),submit])
    assert process_one(db,client)
    with connect(db) as c:
        t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        result=json.loads(t['result'])
        assert t['status']=='completed' and result['decision']==expected
        assert result['mode']=='model-assisted-simulation'
        assert c.execute('SELECT count(*) FROM evidence WHERE task_id=?',(tid,)).fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM tool_calls WHERE task_id=?',(tid,)).fetchone()[0]==4
        assert result['citations'][0].startswith(tid)

def test_fabricated_citation_rejected_and_errors_bounded(db):
    bad=call('submit_answer',{'summary':'已修复','citations':['invented'],'unknowns':[],'next_steps':[]})
    r,ev=run_agent(db,{'id':'x','kind':'complaint','prompt':'test','scenario':'verified'},ScriptedClient([bad,bad,bad]))
    assert r['partial'] and r['decision']=='insufficient' and not ev

def test_plain_prose_does_not_set_verified_status(db):
    client=ScriptedClient([{'role':'assistant','content':'已修复','tool_calls':[]}]*6)
    r,_=run_agent(db,{'id':'x','kind':'complaint','prompt':'test','scenario':'verified'},client)
    assert r['partial'] and r['decision']=='insufficient'

def test_provider_failure_preserves_evidence(db):
    tid=create_task(db,'complaint','verified','test','model')
    process_one(db,ScriptedClient([call('inspect_runtime',{}),ModelError('模型请求超时')]))
    with connect(db) as c:
        assert c.execute('SELECT status FROM tasks WHERE id=?',(tid,)).fetchone()[0]=='failed'
        assert c.execute('SELECT count(*) FROM evidence WHERE task_id=?',(tid,)).fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM tool_calls WHERE status='error'").fetchone()[0]==1

def test_lease_owner_cannot_overwrite(db):
    tid=create_task(db,'complaint','verified','test','model')
    def lose_lease(messages):
        with connect(db) as c:c.execute("UPDATE tasks SET worker_token='new-owner' WHERE id=?",(tid,))
        return call('inspect_runtime',{})
    process_one(db,ScriptedClient([lose_lease]))
    with connect(db) as c:
        row=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        assert row['status']=='running' and row['result'] is None
        assert c.execute('SELECT count(*) FROM evidence').fetchone()[0]==0

def test_schema_upgrade_preserves_old_tasks(tmp_path):
    p=str(tmp_path/'old.sqlite3')
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,kind TEXT,scenario TEXT,prompt TEXT,status TEXT,created_at TEXT,started_at TEXT,completed_at TEXT,attempts INTEGER DEFAULT 0,lease_until REAL,result TEXT,error TEXT)')
        c.execute("INSERT INTO tasks(id,kind,status) VALUES('old','complaint','completed')")
    init(p);init(p)
    with connect(p) as c:
        assert c.execute("SELECT mode FROM tasks WHERE id='old'").fetchone()[0]=='demo'
        assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]=='3'
