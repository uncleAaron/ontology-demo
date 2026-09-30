import json
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from app import main
from app.db import init,connect
from app.graph import graph,find_path
from app.engine import create_task,process_one,execute

@pytest.fixture
def db(tmp_path):
    path=str(tmp_path/'test.sqlite3');init(path);return path

@pytest.fixture
def client(db,monkeypatch):
    monkeypatch.setattr(main,'DB',db)
    with TestClient(main.app) as c:yield c

def test_graph_project_filter_and_candidate(db):
    g=graph(db,'payment',6)
    assert 'secret-service' not in {n['id'] for n in g['nodes']}
    assert all(e['status']=='confirmed' for e in g['edges'])
    all_edges=graph(db,'payment',6,'all')['edges']
    assert any(e['status']=='candidate' for e in all_edges)
    assert any(e['status']=='expired' for e in all_edges)
    with pytest.raises(KeyError):graph(db,'secret-service')

def test_truncated_and_directed_paths(db):
    assert graph(db,'payment',6,limit=2)['truncated']
    p=find_path(db,'cs-1042','abc123')
    assert p['nodes']==['cs-1042','payment','svc-payment','v2.3','abc123']
    assert find_path(db,'abc123','cs-1042')['nodes']==[]
    assert find_path(db,'payment','secret-service')['nodes']==[]

@pytest.mark.parametrize('scenario,decision',[('unreleased','insufficient'),('unverified','insufficient'),('verified','confirmed'),('conflict','insufficient'),('stale','insufficient')])
def test_decision_gate(scenario,decision):
    r,e=execute({'scenario':scenario,'kind':'complaint'})
    assert r['decision']==decision
    assert r['external_actions'] is False
    if scenario=='conflict':assert any(x['classification']=='conflict' for x in e)

def test_actual_coding_fixture():
    r,_=execute({'scenario':'unverified','kind':'coding'})
    assert r['coding']['tests'][0]['exit_code']!=0
    assert r['coding']['tests'][1]['exit_code']==0
    assert 'Ran 3 tests' in r['coding']['tests'][1]['output']
    assert '+++ b/callback.py' in r['coding']['patch']

def test_persistent_task_and_atomic_claim(db):
    task=create_task(db,'requirements','unverified','需求')
    with ThreadPoolExecutor(max_workers=2) as ex:
        assert sum(ex.map(lambda _:process_one(db),range(2)))==1
    with connect(db) as c:
        r=c.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
        assert r['status']=='completed' and r['attempts']==1
        assert len(c.execute('SELECT * FROM evidence WHERE task_id=?',(task,)).fetchall())==3
        assert json.loads(r['result'])['decision']=='draft'

def test_expired_lease_recovery(db):
    tid=create_task(db,'complaint','verified','排查')
    with connect(db) as c:c.execute("UPDATE tasks SET status='running',lease_until=?,attempts=1 WHERE id=?",(time.time()-10,tid))
    assert process_one(db)
    with connect(db) as c:
        t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        assert t['status']=='completed' and t['attempts']==2

def test_api_documents_and_schema(client):
    assert client.get('/').status_code==200
    assert client.get('/api/health').json()['model_connected'] is False
    assert all(d['project']=='payments' for d in client.get('/api/documents').json())
    r=client.post('/api/documents',json={'title':'新手册','body':'测试回调安全重试'})
    assert r.status_code==201
    docs=client.get('/api/documents',params={'q':'安全重试'}).json()
    assert docs[0]['id']==r.json()['id']
    assert len(client.get('/api/ontology').json()['types'])==10

def test_suggestion_validates_ontology_and_visibility(client):
    r=client.post('/api/relations/suggestions',json={'source':'abc123','target':'payment','type':'contains','origin':'手工校验'})
    assert r.status_code==422
    r=client.post('/api/relations/suggestions',json={'source':'payment','target':'secret-service','type':'implemented_by','origin':'test'})
    assert r.status_code==404
    r=client.post('/api/relations/suggestions',json={'source':'cs-1042','target':'bug-42','type':'reported','origin':'待人工核实'})
    assert r.status_code==201 and r.json()['status']=='candidate'

def test_api_task_lifecycle_and_evidence_graph(client):
    r=client.post('/api/tasks',json={'kind':'complaint','scenario':'conflict','prompt':'排查'})
    assert r.status_code==202
    tid=r.json()['id']
    for _ in range(50):
        t=client.get('/api/tasks/'+tid).json()
        if t['status']=='completed':break
        time.sleep(.05)
    assert t['status']=='completed'
    assert t['result']['decision']=='insufficient'
    g=client.get('/api/tasks/'+tid+'/evidence-graph').json()
    assert len(g['nodes'])==len(t['evidence'])+2
    assert {e['id'] for e in t['evidence']} <= {n['id'] for n in g['nodes']}
    assert client.get('/api/graph',params={'center':'secret-service'}).status_code==404
    assert client.get('/api/graph',params={'depth':1000}).status_code==422
    assert client.post('/api/tasks',json={'kind':'unknown','scenario':'verified','prompt':'a'}).status_code==422
