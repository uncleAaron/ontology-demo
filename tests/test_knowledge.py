import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from app import main
from app.db import init,connect
from app.knowledge import revise,review,withdraw,Conflict,maintenance,head
from app.graph import graph,find_path
from app.tools import invoke
from app.engine import create_task,process_one
from test_model import ScriptedClient,call,submit

@pytest.fixture
def db(tmp_path):
    p=str(tmp_path/'maintenance.sqlite3');init(p);return p

@pytest.fixture
def client(db,monkeypatch):
    monkeypatch.setattr(main,'DB',db)
    # No background worker: each test controls task interleaving.
    with TestClient(main.app) as client:yield client

def update(db,doc='kb-release',generation=1):
    with connect(db) as c:
        c.execute('BEGIN IMMEDIATE')
        return revise(c,doc,dict(expected_generation=generation,title='新版修复说明',body='变更后的真实来源内容',version='2.0',reason='源文档已修订'))

def approve(db,doc='kb-release',generation=2):
    with connect(db) as c:
        c.execute('BEGIN IMMEDIATE')
        return review(c,doc,dict(expected_generation=generation,decision='approve',reason='核对原文通过'))

def retract(db,doc='kb-release',generation=1):
    with connect(db) as c:
        c.execute('BEGIN IMMEDIATE')
        return withdraw(c,doc,dict(expected_generation=generation,reason='来源错误，撤回'))

def test_revisions_block_tools_and_graph_until_individual_review(client,db):
    old=client.get('/api/documents/kb-release/maintenance').json()
    result=client.post('/api/documents/kb-release/revisions',json=dict(expected_generation=1,title='新说明',body='新正文',version='2',reason='上游修订'))
    assert result.status_code==201 and result.json()['status']=='needs_review'
    assert 'kb-release' not in {d['id'] for d in client.get('/api/documents').json()}
    with pytest.raises(ValueError):invoke(db,{'kind':'complaint'},'read_document',{'document_id':'kb-release'})
    assert not find_path(db,'cs-1042','abc123')['nodes']
    assert any(e['status']=='source_stale' for e in graph(db,'svc-payment',6,'all')['edges'])
    assert client.post('/api/documents/kb-release/review',json={'expected_generation':2,'decision':'approve','reason':'通过'}).status_code==200
    assert invoke(db,{'kind':'complaint'},'read_document',{'document_id':'kb-release'})['body']=='新正文'
    assert 'e3' not in {e['id'] for e in graph(db,'svc-payment',6)['edges']}
    r=client.post('/api/relations/e3/review',json={'expected_generation':1,'expected_document_revision':2,'reason':'确认服务仍拥有该版本'})
    assert r.status_code==200
    assert 'e3' in {e['id'] for e in graph(db,'svc-payment',6)['edges']}
    assert 'e4' not in {e['id'] for e in graph(db,'svc-payment',6)['edges']}
    new=client.get('/api/documents/kb-release/maintenance').json()
    assert new['versions'][1]==old['versions'][0]
    assert '新正文' in new['diff']
    assert [e['action'] for e in new['events']]==['relation_confirmed','approve','source_updated']

def test_rejection_and_withdrawal_never_revive_old_version(client,db):
    update(db)
    r=client.post('/api/documents/kb-release/review',json={'expected_generation':2,'decision':'reject','reason':'内容不完整'})
    assert r.json()['status']=='rejected'
    assert client.post('/api/documents/kb-release/review',json={'expected_generation':3,'decision':'approve','reason':'重复批准'}).status_code==409
    assert 'kb-release' not in {d['id'] for d in client.get('/api/documents').json()}
    assert client.post('/api/documents/kb-release/withdraw',json={'expected_generation':3,'reason':'撤回'}).status_code==200
    assert client.post('/api/documents/kb-release/revisions',json=dict(expected_generation=4,title='a',body='a',version='3',reason='不应恢复')).status_code==409
    # History stays inspectable only in the maintenance view.
    assert len(client.get('/api/documents/kb-release/maintenance').json()['versions'])==2

def test_parallel_update_one_wins_and_replayed_request_conflicts(db):
    def attempt(_):
        try:update(db);return True
        except Conflict:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sum(pool.map(attempt,range(2)))==1
    with pytest.raises(Conflict):update(db)
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM document_revisions WHERE document_id='kb-release'").fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM maintenance_events').fetchone()[0]==1

def test_old_approval_and_relation_confirmation_are_rejected(client,db):
    update(db);update(db,generation=2)
    assert client.post('/api/documents/kb-release/review',json={'expected_generation':2,'decision':'approve','reason':'过时批准'}).status_code==409
    approve(db,generation=3)
    data={'expected_generation':1,'expected_document_revision':2,'reason':'引用旧来源'}
    assert client.post('/api/relations/e3/review',json=data).status_code==409
    data['expected_document_revision']=3
    assert client.post('/api/relations/e3/review',json=data).status_code==200
    assert client.post('/api/relations/e3/review',json=data).status_code==409
    assert client.post('/api/relations/e11/review',json=data).status_code==409

def test_private_docs_and_relations_stay_inaccessible(client,db):
    assert client.get('/api/documents/kb-private/maintenance').status_code==404
    assert client.post('/api/documents/kb-private/withdraw',json={'expected_generation':1,'reason':'test'}).status_code==404
    assert client.post('/api/relations/e12/review',json={'expected_generation':1,'expected_document_revision':1,'reason':'test'}).status_code==404
    all_docs=client.get('/api/documents?include_inactive=true').json()
    assert 'kb-private' not in {d['id'] for d in all_docs}

def test_withdrawal_filters_substring_search_and_new_suggestions(client,db):
    retract(db)
    out=invoke(db,{'kind':'complaint'},'search_documents',{'query':'v2.3'})
    assert out['documents']==[] and out['knowledge_refs']==[]
    assert client.post('/api/relations/suggestions',json={'source':'svc-payment','target':'v2.3','type':'has_version','origin':'manual','document_id':'kb-release'}).status_code==409

def test_model_source_changed_during_final_request_blocks_draft(db):
    tid=create_task(db,'requirements','verified','查修复说明','model')
    def invalidate_and_submit(messages):
        retract(db)
        return submit(messages)
    process_one(db,ScriptedClient([call('read_document',{'document_id':'kb-release'}),invalidate_and_submit]))
    with connect(db) as c:
        t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone();r=json.loads(t['result'])
        assert t['status']=='partial' and r['decision']=='insufficient'
        assert r['knowledge_warnings'][0]['status']=='withdrawn'
        assert all(a['title']!='模型草稿（未审核）' for a in r['artifacts'])
        assert c.execute('SELECT revision FROM task_knowledge_refs WHERE task_id=?',(tid,)).fetchone()[0]==1
        assert '提交 abc123' in c.execute('SELECT payload FROM evidence WHERE task_id=?',(tid,)).fetchone()[0]

def test_engine_final_transaction_rechecks_even_after_agent_returns(db,monkeypatch):
    from app import agent
    tid=create_task(db,'requirements','verified','test','model')
    def finished(path,task,**kwargs):
        ev={'label':'source','payload':{'knowledge_refs':[{'document_id':'kb-release','revision':1}]},'origin':'test','classification':'fact'}
        retract(db)
        return {'decision':'confirmed','artifacts':[{'title':'unsafe','text':'outdated'}]},[ev]
    monkeypatch.setattr(agent,'run_agent',finished)
    process_one(db)
    with connect(db) as c:
        t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        assert t['status']=='partial' and 'unsafe' not in t['result']

def test_historical_task_snapshot_kept_with_current_warning(db,monkeypatch):
    tid=create_task(db,'requirements','verified','test')
    process_one(db)
    monkeypatch.setattr(main,'DB',db)
    before=main.task_get(tid)
    retract(db)
    after=main.task_get(tid)
    assert after['result']==before['result'] and after['evidence']==before['evidence']
    assert before['knowledge_warnings']==[] and after['knowledge_warnings']
    assert main.task_graph(tid)['knowledge_warnings']
    with connect(db) as c:
        report=maintenance(c,'kb-release')
        assert report['tasks'][0]['task_id']==tid
    # A new deterministic fixture cannot borrow a changed source's authority.
    new=create_task(db,'complaint','verified','test');process_one(db)
    assert main.task_get(new)['status']=='partial'

def test_read_tool_and_graph_report_exact_revision(db):
    read=invoke(db,{'kind':'requirements'},'read_document',{'document_id':'kb-release'})
    assert read['knowledge_refs']==[{'document_id':'kb-release','revision':1}]
    assert {'document_id':'kb-release','revision':1} in graph(db)['knowledge_refs']
    update(db);approve(db)
    assert invoke(db,{'kind':'requirements'},'read_document',{'document_id':'kb-release'})['knowledge_refs'][0]['revision']==2

def test_reinitialization_preserves_revision_history_and_blocking(db):
    update(db);retract(db,generation=2)
    init(db);init(db)
    with connect(db) as c:
        assert head(c,'kb-release')['status']=='withdrawn'
        assert len(maintenance(c,'kb-release')['versions'])==2
        assert c.execute("SELECT revision FROM relation_sources WHERE relation_id='e3'").fetchone()[0]==1

def test_failed_publish_rolls_back_projection_and_audit(db):
    update(db)
    with connect(db) as c:
        c.execute("CREATE TRIGGER fail_publish BEFORE INSERT ON maintenance_events WHEN NEW.action='approve' BEGIN SELECT RAISE(ABORT,'simulated crash'); END")
    with pytest.raises(Exception):approve(db)
    with connect(db) as c:
        assert head(c,'kb-release')['status']=='needs_review'
        assert c.execute("SELECT version FROM documents WHERE id='kb-release'").fetchone()[0]=='1.0'
        assert c.execute('SELECT count(*) FROM maintenance_events').fetchone()[0]==1

def test_next_model_call_stops_after_source_is_withdrawn(db):
    from app.agent import run_agent
    calls=[]
    client=ScriptedClient([call('read_document',{'document_id':'kb-release'})])
    def record(name,args,status,result,evidence=None):
        calls.append(name)
        if name=='read_document':retract(db)
    result,ev=run_agent(db,{'id':'inline','kind':'requirements','prompt':'test','scenario':'verified'},client,record=record)
    assert result['partial'] and len(ev)==1
    assert calls.count('model_request')==1  # No second provider request with obsolete context.

def test_v2_database_upgrade_preserves_imports_and_does_not_invent_task_refs(db):
    with connect(db) as c:
        for name in ['chunk_terms','document_chunks','maintenance_events','task_knowledge_refs','relation_sources','document_heads','document_revisions']:
            c.execute('DROP TABLE '+name)
        c.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        c.execute("INSERT INTO documents VALUES('old-import','旧导入','7','保留原文','payments')")
        c.execute("INSERT INTO tasks(id,kind,prompt,scenario,status,created_at) VALUES('old-task','requirements','x','verified','completed','2026-09-01')")
    init(db)
    with connect(db) as c:
        report=maintenance(c,'old-import')
        assert report['versions'][0]['body']=='保留原文'
        assert report['head']['published_revision']==1
        assert c.execute('SELECT count(*) FROM task_knowledge_refs').fetchone()[0]==0
        assert c.execute("SELECT status FROM tasks WHERE id='old-task'").fetchone()[0]=='completed'
