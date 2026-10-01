import json
import time
import pytest
from app.db import init,connect
from app.knowledge import register_document,withdraw,revise,Conflict,task_warnings
from app.processing import create_job,process_one_job,job_get,job_action,review_candidate,search_derived,read_derived
from app.model import ModelSettings,ModelError
from app.agent import run_agent
from app.engine import create_task,process_one

@pytest.fixture
def db(tmp_path):
    path=str(tmp_path/'processing.sqlite3');init(path)
    with connect(path) as c:
        c.execute('INSERT INTO documents VALUES(?,?,?,?,?)',('manual','航线手册','1','北极校验码为 AURORA-729，必须由双人核对。','payments'))
        register_document(c,'manual')
    return path

def enqueue(db):
    with connect(db) as c:
        c.execute('BEGIN IMMEDIATE');return create_job(c,'manual',1)['id']

def get(db,jid):
    with connect(db) as c:return job_get(c,jid)

class Fake:
    settings=ModelSettings('http://localhost/chat/completions','mock','mock')
    def __init__(self,callback=lambda name:None,invalid=False):self.callback=callback;self.invalid=invalid;self.calls=[]
    def complete(self,messages,tools):
        name=tools[0]['function']['name'];self.calls.append(name)
        data=json.loads(messages[1]['content'])
        if name=='submit_analysis':
            ch=data['chunks'][0];support={'chunk_id':ch['id'],'quote':ch['body'][:20]}
            args={'observations':[{'text':'必须双人核对','support':support}]}
        else:
            support=data['observations'][0]['support'];args={'candidates':[{'kind':'summary','title':'双人核对规则','body':'校验需双人核对。','supports':[support]},{'kind':'faq','title':'核对流程','body':'如何复核？由两人复核。','supports':[support]}]}
        if self.invalid:
            if name=='submit_analysis':args['observations'][0]['support']['quote']='不存在的逐字引文'
            else:args['candidates'][1]['supports']=[{'chunk_id':support['chunk_id'],'quote':'没有这个引文'}]
        self.callback(name)
        return {'tool_calls':[{'id':'call','type':'function','function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}}]}

def finish(db,jid,client=None):
    client=client or Fake()
    for _ in range(30):
        if get(db,jid)['state'] not in {'queued','running'}:break
        process_one_job(db,client)
    return get(db,jid)

def approve(db,k):
    with connect(db) as c:
        c.execute('BEGIN IMMEDIATE');return review_candidate(c,k['id'],{'decision':'approve','expected_generation':k['generation'],'expected_source_generation':1,'reason':'已核对原文'})

def test_checkpoints_dedup_and_publish_gate(db):
    jid=enqueue(db);assert enqueue(db)==jid
    fake=Fake();process_one_job(db,fake)
    job=get(db,jid);assert job['stage']=='generation' and job['cursor']==job['total_chunks'] and not job['candidates']
    init(db) # Durable analysis survives restart.
    job=finish(db,jid,fake);assert job['state']=='completed' and len(job['candidates'])==2
    assert fake.calls==['submit_analysis','submit_candidates']
    with connect(db) as c:assert not search_derived(c,'双人核对')
    k=approve(db,job['candidates'][0])
    with connect(db) as c:
        assert search_derived(c,'双人核对')[0]['id']==k['id']
        assert not search_derived(c,'双人核对',['kb-release'])
        assert read_derived(c,k['id'],['manual'])['supports']
        with pytest.raises(ValueError):read_derived(c,k['id'],['kb-release'])
        with pytest.raises(Conflict):review_candidate(c,k['id'],{'decision':'approve','expected_generation':1,'expected_source_generation':1,'reason':'重复'})

def test_long_source_processes_all_batches(db):
    with connect(db) as c:
        c.execute('INSERT INTO documents VALUES(?,?,?,?,?)',('long','长文','1','长文规则需要保持原文条件。'*800,'payments'));register_document(c,'long')
        jid=create_job(c,'long',1)['id']
    fake=Fake();job=finish(db,jid,fake)
    assert job['state']=='completed' and job['cursor']==job['total_chunks']>6
    assert len(job['analysis'])==(job['total_chunks']+5)//6

@pytest.mark.parametrize('stage',['analysis','generation'])
def test_invalid_quotes_fail_atomically_and_retry_checkpoint(db,stage):
    jid=enqueue(db)
    if stage=='generation':process_one_job(db,Fake())
    before=get(db,jid)['cursor'];process_one_job(db,Fake(invalid=True))
    job=get(db,jid);assert job['state']=='failed' and not job['candidates'] and job['cursor']==before
    with connect(db) as c:job_action(c,jid,'retry')
    fake=Fake();job=finish(db,jid,fake);assert job['state']=='completed'
    if stage=='generation':assert fake.calls==['submit_candidates']

@pytest.mark.parametrize('action',['withdraw','cancel','expire'])
def test_late_owner_cannot_save_results(db,action):
    jid=enqueue(db)
    def change(name):
        with connect(db) as c:
            c.execute('BEGIN IMMEDIATE')
            if action=='withdraw':withdraw(c,'manual',{'expected_generation':1,'reason':'来源撤回'})
            elif action=='cancel':job_action(c,jid,'cancel')
            else:c.execute('UPDATE processing_jobs SET lease_until=? WHERE id=?',(time.time()-1,jid))
    process_one_job(db,Fake(callback=change));job=get(db,jid)
    assert not job['analysis'] and not job['candidates']
    if action=='expire':assert finish(db,jid)['state']=='completed'
    else:assert job['state']==('stale' if action=='withdraw' else 'cancelled')

def test_expired_recovery_and_retry_are_bounded(db):
    jid=enqueue(db)
    class Bad(Fake):
        def complete(self,*args):raise ModelError('供应商未完成请求')
    for i in range(3):
        process_one_job(db,Bad());assert get(db,jid)['attempts']==i+1
        with connect(db) as c:
            if i<2:job_action(c,jid,'retry')
            else:
                with pytest.raises(Conflict):job_action(c,jid,'retry')
    assert not process_one_job(db,Bad())

@pytest.mark.parametrize('action',['withdraw','revise'])
def test_source_changes_block_review_and_retrieval(db,action):
    jid=enqueue(db);job=finish(db,jid);published=approve(db,job['candidates'][0])
    with connect(db) as c:
        if action=='withdraw':withdraw(c,'manual',{'expected_generation':1,'reason':'撤回'})
        else:revise(c,'manual',{'expected_generation':1,'reason':'更新','title':'新版','body':'新版内容','version':'2'})
        assert not search_derived(c,'双人核对')
        with pytest.raises(ValueError):read_derived(c,published['id'])
        with pytest.raises(Conflict):review_candidate(c,job['candidates'][1]['id'],{'decision':'approve','expected_generation':1,'expected_source_generation':1,'reason':'旧审批'})

def test_withdraw_derived_blocks_inflight_answer_and_history(db,monkeypatch):
    jid=enqueue(db);k=approve(db,finish(db,jid)['candidates'][0])
    class Answer(Fake):
        def complete(self,messages,tools):
            payload=json.loads(messages[2]['content'].split('：',1)[1]);ev=next(e for e in payload['evidence'] if 'knowledge_item' in e['data'])
            args={'summary':'双人核对','citations':[ev['evidence_id']],'unknowns':[],'next_steps':[]}
            return {'tool_calls':[{'id':'answer','function':{'name':'submit_answer','arguments':json.dumps(args)}}]}
    monkeypatch.setattr('app.agent.ModelClient',lambda settings:Answer())
    monkeypatch.setattr('app.agent.ModelSettings.from_env',lambda:Fake.settings)
    tid=create_task(db,'knowledge','unverified','双人核对','model',['manual']);process_one(db)
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM task_derived_refs WHERE task_id=?',(tid,)).fetchone()[0]==1
        assert not task_warnings(c,tid,1)
        review_candidate(c,k['id'],{'decision':'withdraw','expected_generation':k['generation'],'expected_source_generation':1,'reason':'知识撤回'})
        assert task_warnings(c,tid,1)[0]['status']=='knowledge_unavailable'
    # Preloaded refs also block late submission when source document itself remains active.
    k2=approve(db,get(db,jid)['candidates'][1])
    class Late(Answer):
        def complete(self,messages,tools):
            answer=super().complete(messages,tools)
            with connect(db) as c:review_candidate(c,k2['id'],{'decision':'withdraw','expected_generation':k2['generation'],'expected_source_generation':1,'reason':'并发撤回'})
            return answer
    result,_=run_agent(db,{'id':'inline','kind':'knowledge','prompt':'核对流程','document_ids':['manual']},Late())
    assert result['partial'] and result['knowledge_warnings'][0]['status']=='knowledge_unavailable'

def test_api_scopes_config_and_review_conflicts(db,monkeypatch):
    from app import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main,'DB',db);client=TestClient(main.app)
    monkeypatch.setattr(main,'require_model',lambda:None)
    assert client.post('/api/documents/manual/process',json={'expected_generation':2}).status_code==409
    jid=client.post('/api/documents/manual/process',json={'expected_generation':1}).json()['id']
    job=finish(db,jid)
    assert client.get('/api/processing-jobs/'+jid).json()['state']=='completed'
    assert client.get('/api/documents/manual/processing').json()['jobs'][0]['id']==jid
    kid=job['candidates'][0]['id']
    body={'decision':'approve','expected_generation':1,'expected_source_generation':2,'reason':'核对'}
    assert client.post('/api/knowledge/'+kid+'/review',json=body).status_code==409
    body['expected_source_generation']=1
    assert client.post('/api/knowledge/'+kid+'/review',json=body).status_code==200
    assert client.get('/api/knowledge',params={'q':'双人核对'}).json()[0]['id']==kid
    assert client.get('/api/processing-jobs/missing').status_code==404
    with connect(db) as c:c.execute('UPDATE documents SET project="private" WHERE id="manual"')
    assert client.get('/api/processing-jobs/'+jid).status_code==404
    assert client.post('/api/knowledge/'+kid+'/review',json=body).status_code==404

def test_cancel_then_manual_resume_preserves_checkpoint(db):
    jid=enqueue(db);process_one_job(db,Fake())
    with connect(db) as c:job_action(c,jid,'cancel')
    assert enqueue(db)==jid
    fake=Fake();assert finish(db,jid,fake)['state']=='completed'
    assert fake.calls==['submit_candidates']

@pytest.mark.parametrize('response',[{'tool_calls':[]},{'tool_calls':[{'function':{'name':'submit_analysis','arguments':'broken'}}]},{'tool_calls':[{'function':{'name':'unknown','arguments':'{}'}}]}])
def test_malformed_provider_results_never_publish(db,response):
    jid=enqueue(db)
    class Bad(Fake):
        def complete(self,*args):return response
    process_one_job(db,Bad());job=get(db,jid)
    assert job['state']=='failed' and not job['candidates'] and not job['analysis']

def test_derived_rank_applied_before_limit_case_insensitive(db):
    jid=enqueue(db);k=approve(db,finish(db,jid)['candidates'][0])
    with connect(db) as c:
        row=c.execute('SELECT * FROM knowledge_candidates WHERE id=?',(k['id'],)).fetchone()
        for i in range(110):
            c.execute('INSERT INTO knowledge_candidates VALUES(?,?,?,?,?,?,?,?)',(f'a{i:03}',jid,'fact','普通说明','AURORA 一般记录',row['supports'],'active',2))
        c.execute('INSERT INTO knowledge_candidates VALUES(?,?,?,?,?,?,?,?)',('z-best',jid,'faq','AURORA 精确解释','一般记录',row['supports'],'active',2))
        assert search_derived(c,'aurora',limit=1)[0]['id']=='z-best'
