import json
import pytest
from fastapi.testclient import TestClient
from app.db import init,connect
from app.knowledge import register_document,revise,review,withdraw
from app.retrieval import search,read_chunk,split_body
from app.engine import create_task,process_one
from app.tools import invoke,validate
from app.model import ModelSettings

@pytest.fixture
def db(tmp_path):
    path=str(tmp_path/'retrieval.sqlite3');init(path);return path

def document(db,did='user-manual',body=None):
    body=body or ('普通背景说明与历史记录。\n'*700)+'\n# 北极校验\n北极校验码为 AURORA-729，必须由双人核对。\n'
    with connect(db) as c:
        c.execute('INSERT INTO documents VALUES(?,?,?,?,?)',(did,'航线操作指南','1',body,'payments'))
        register_document(c,did)
    return body

def test_long_document_tail_and_complete_chunk_coverage(db):
    body=document(db)
    with connect(db) as c:
        out=search(c,'北极校验码是多少',['user-manual'])
        hit=out['chunks'][0]
        assert hit['start_char']>6000 and 'AURORA-729' in hit['body']
        assert body[hit['start_char']:hit['end_char']]==hit['body']
        assert read_chunk(c,hit['id'])['revision']==1
        chunks=list(c.execute('SELECT start_char,end_char,body FROM document_chunks WHERE document_id=? ORDER BY ordinal',('user-manual',)))
        assert chunks[0]['start_char']==0 and chunks[-1]['end_char']==len(body)
        assert all(b['start_char']<=a['end_char'] for a,b in zip(chunks,chunks[1:]))

def test_keywords_do_not_require_contiguous_phrase_and_have_stable_rank(db):
    document(db,body='退款申请需审核。\n审批完毕后由负责人安排重试。')
    with connect(db) as c:
        a=search(c,'退款 重试',['user-manual']);b=search(c,'退款 重试',['user-manual'])
        assert a['chunks'] and a==b and a['chunks'][0]['matched_terms']>=2
        assert not search(c,'完全不存在的稀有词')['chunks']

def test_scope_range_read_and_inactive_source(db):
    document(db)
    task={'kind':'knowledge','document_ids':['user-manual']}
    out=invoke(db,task,'read_document_range',{'document_id':'user-manual','offset':8000,'limit':3000})
    assert out['start_char']==8000 and 'AURORA-729' in out['body']
    with pytest.raises(ValueError):invoke(db,task,'read_document',{'document_id':'kb-runbook'})
    with pytest.raises(ValueError):validate('inspect_runtime',{},'knowledge')
    with connect(db) as c:
        cid=search(c,'北极',['user-manual'])['chunks'][0]['id']
        withdraw(c,'user-manual',{'expected_generation':1,'reason':'撤回测试'})
        assert not search(c,'北极')['chunks']
        with pytest.raises(ValueError):read_chunk(c,cid)
        old=read_chunk(c,cid,require_active=False)
        assert not old['current'] and 'AURORA-729' in old['body']
    with pytest.raises(ValueError):create_task(db,'knowledge','unverified','北极','model',['user-manual'])

def test_revision_approval_indexes_new_source_and_keeps_history(db):
    document(db,body='旧校验码 AURORA-729')
    with connect(db) as c:
        cid=search(c,'校验码')['chunks'][0]['id']
        revise(c,'user-manual',{'title':'航线操作指南','body':'新校验码 AURORA-730','version':'2','reason':'变更','expected_generation':1})
        assert not search(c,'校验码')['chunks']
        review(c,'user-manual',{'decision':'approve','reason':'核对','expected_generation':2})
        hit=search(c,'校验码')['chunks'][0]
        assert hit['revision']==2 and '730' in hit['body']
        assert read_chunk(c,cid,require_active=False)['revision']==1
    init(db)
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM document_chunks WHERE document_id='user-manual'").fetchone()[0]==2

class PreparedClient:
    settings=ModelSettings('https://example.test/chat/completions','test','key')
    def complete(self,messages,tools):
        context=json.loads(messages[-1]['content'].split('：',1)[1])
        ev=context['evidence'];assert ev and any('AURORA-729' in e['data']['chunk']['body'] for e in ev)
        assert all(e['data']['chunk']['document_id']=='user-manual' for e in ev)
        assert 'inspect_runtime' not in {t['function']['name'] for t in tools}
        return {'role':'assistant','content':None,'tool_calls':[{'id':'answer','type':'function','function':{'name':'submit_answer','arguments':json.dumps({'summary':'北极校验码为 AURORA-729。','citations':[ev[0]['evidence_id']],'unknowns':[],'next_steps':[]})}}]}

def test_first_model_request_already_contains_user_tail_and_refs_persist(db):
    document(db)
    tid=create_task(db,'knowledge','unverified','北极校验码是多少','model',['user-manual'])
    process_one(db,PreparedClient())
    with connect(db) as c:
        t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        assert t['status']=='completed',t['error']
        result=json.loads(t['result']);assert result['mode']=='model-assisted-knowledge'
        assert result['citations'] and '729' in result['artifacts'][0]['text']
        assert c.execute('SELECT document_id FROM task_knowledge_refs WHERE task_id=?',(tid,)).fetchone()[0]=='user-manual'

def test_auto_selected_evidence_invalidated_during_generation(db):
    document(db)
    class WithdrawingClient(PreparedClient):
        def complete(self,messages,tools):
            with connect(db) as c:withdraw(c,'user-manual',{'expected_generation':1,'reason':'在途撤回'})
            return super().complete(messages,tools)
    tid=create_task(db,'knowledge','unverified','北极校验码是多少','model',['user-manual'])
    process_one(db,WithdrawingClient())
    with connect(db) as c:
        t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        assert t['status']=='partial'
        assert '729' not in json.loads(t['result'])['artifacts'][0]['text']

def test_api_scope_validation_and_historical_chunks(db,monkeypatch):
    document(db)
    from app import main
    monkeypatch.setattr(main,'DB',db)
    with TestClient(main.app) as client:
        r=client.get('/api/search',params={'q':'北极校验码','document_id':'user-manual'})
        assert r.status_code==200 and '729' in r.json()['chunks'][0]['body']
        cid=r.json()['chunks'][0]['id']
        assert client.get('/api/chunks/'+cid).json()['current']
        assert client.get('/api/search',params={'q':'内部','document_id':'kb-private'}).status_code==422
        assert client.post('/api/tasks',json={'kind':'knowledge','mode':'demo'}).status_code==422

def test_selected_source_without_matching_terms_is_loaded_for_navigation(db):
    document(db,body='本手册有航线背景与人工处理说明。')
    from app.retrieval import initial_context
    with connect(db) as c:
        out=initial_context(c,{'prompt':'zzzzzz','document_ids':['user-manual']})
        assert len(out['chunks'])==1 and out['chunks'][0]['score']==0
        assert '导航' in out['chunks'][0]['retrieval_reason']

def test_no_relevant_source_does_not_call_model_or_replace_with_demo(db):
    class NeverClient:
        settings=PreparedClient.settings
        def complete(self,messages,tools):raise AssertionError('No source must not trigger model')
    tid=create_task(db,'knowledge','unverified','zzzzzzzz','model')
    process_one(db,NeverClient())
    with connect(db) as c:
        t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        assert t['status']=='partial' and '没有匹配' in json.loads(t['result'])['artifacts'][0]['text']
        assert c.execute('SELECT count(*) FROM evidence WHERE task_id=?',(tid,)).fetchone()[0]==0
