import json
from dataclasses import replace
import httpx
import pytest
from app.context import assemble,size
from app.model import ModelSettings,ModelClient,ModelOutputTruncated,ModelToolBudgetExceeded,ModelError
from app.agent import run_agent
from app.db import init,connect
from app.knowledge import withdraw
from app.tools import schemas

@pytest.fixture
def db(tmp_path):
    p=str(tmp_path/'budget.sqlite3');init(p);return p

def call(name,args,cid='c'):
    return {'role':'assistant','content':None,'tool_calls':[{'id':cid,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}

def evidence_ids(messages):
    found=[]
    for m in messages:
        if m['role']=='tool':
            payload=json.loads(m['content'])
            if payload.get('evidence_id'):found.append(payload['evidence_id'])
        elif m['role']=='user' and (m['content'].startswith('服务端检索上下文') or m['content'].startswith('压缩证据上下文')):
            payload=json.loads(m['content'].split('：',1)[1])
            found.extend(e['evidence_id'] for e in payload.get('evidence',[]))
    return list(dict.fromkeys(found))

def answer(messages):return call('submit_answer',{'summary':'基于已有资料形成简短草稿。','citations':evidence_ids(messages)[:2],'unknowns':['真实运行状态'],'next_steps':['人工审核']})

class Client:
    def __init__(self,steps,max_calls=12,max_rounds=6):
        self.steps=iter(steps);self.settings=ModelSettings('https://example.test/chat/completions','test','key',max_calls=max_calls,max_rounds=max_rounds);self.calls=0
    def complete(self,messages,tools):
        self.calls+=1;step=next(self.steps)
        if isinstance(step,Exception):raise step
        return step(messages,tools)

def closing_answer(messages,tools):
    assert [t['function']['name'] for t in tools]==['submit_answer']
    assert '仅允许 submit_answer' in messages[0]['content']
    return answer(messages)

def test_over_budget_batch_gets_one_closing_request_without_execution(db):
    def batch(messages,tools):
        message=call('search_documents',{'query':'支付'})
        message['tool_calls']=[call('search_documents',{'query':'支付'},f'c{i}')['tool_calls'][0] for i in range(12)]
        return message
    events=[];client=Client([batch,closing_answer])
    result,ev=run_agent(db,{'id':'t','kind':'knowledge','prompt':'支付资料'},client,record=lambda *x:events.append(x))
    assert result['budget']['closing'] and result['budget']['tool_calls']==1
    assert client.calls==2 and ev and not any(e['label']=='search_documents' for e in ev)
    assert any(e[0]=='batch_skipped' for e in events)

def test_query_limit_reserves_submission(db):
    client=Client([lambda m,t:call('search_documents',{'query':'支付'}),closing_answer],max_calls=2)
    result,_=run_agent(db,{'id':'t','kind':'knowledge','prompt':'支付资料'},client)
    assert result['budget']['tool_calls']==2 and result['budget']['closing']

def test_provider_batch_larger_than_total_limit_also_gets_closing(db):
    client=Client([ModelToolBudgetExceeded('超过总额度'),closing_answer])
    result,_=run_agent(db,{'id':'t','kind':'knowledge','prompt':'支付资料'},client)
    assert client.calls==2 and result['budget']['tool_calls']==1 and result['budget']['closing']

def test_last_model_round_is_submission_only(db):
    client=Client([lambda m,t:{'role':'assistant','content':'需要进一步核对','tool_calls':[]},closing_answer],max_rounds=2)
    result,_=run_agent(db,{'id':'t','kind':'knowledge','prompt':'支付资料'},client)
    assert result['citations'] and result['budget']['model_rounds']==2

def test_truncation_retries_only_as_bounded_submission(db):
    client=Client([ModelOutputTruncated('截断'),closing_answer])
    result,_=run_agent(db,{'id':'t','kind':'knowledge','prompt':'支付资料'},client)
    assert result['budget']['truncations']==1 and result['budget']['closing']
    client=Client([ModelOutputTruncated('截断'),ModelOutputTruncated('截断')])
    result,ev=run_agent(db,{'id':'t','kind':'knowledge','prompt':'支付资料'},client)
    assert result['partial'] and ev and client.calls==2 and '未保存不完整草稿' in result['artifacts'][0]['text']

def test_closing_does_not_execute_queries_or_accept_unknown_citations(db):
    def query(m,t):return call('search_documents',{'query':'支付'})
    client=Client([query],max_rounds=1)
    result,ev=run_agent(db,{'id':'t','kind':'knowledge','prompt':'支付资料'},client)
    assert result['partial'] and not any(e['label']=='search_documents' for e in ev)
    client=Client([lambda m,t:call('submit_answer',{'summary':'假结论','citations':['invented'],'unknowns':[],'next_steps':[]})],max_rounds=1)
    result,_=run_agent(db,{'id':'t','kind':'knowledge','prompt':'支付资料'},client)
    assert result['partial'] and '假结论' not in result['artifacts'][0]['text']

def test_withdrawal_during_closing_blocks_draft(db):
    def revoke(messages,tools):
        with connect(db) as c:withdraw(c,'kb-runbook',{'expected_generation':1,'reason':'收尾撤回'})
        return closing_answer(messages,tools)
    result,_=run_agent(db,{'id':'t','kind':'knowledge','prompt':'支付回调排障'},Client([revoke],max_rounds=1))
    assert result['partial'] and result['knowledge_warnings']

def test_compaction_preserves_question_and_chunk_tail_without_tool_fragments():
    settings=ModelSettings('https://example.test/chat/completions','test','key',max_context_chars=15000)
    body='甲'*1000+'不可丢弃的尾部校验码'
    chunk={'id':'chunk-a','document_id':'doc','revision':1,'start_char':6000,'end_char':6000+len(body),'body':body}
    ev=[{'id':'e1','label':'预先读取资料片段','payload':{'chunk':chunk}},{'id':'e2','label':'read_chunk','payload':chunk}]
    messages=[{'role':'system','content':'规则'},{'role':'user','content':'原问题'}, {'role':'assistant','content':'x'*30000,'tool_calls':[],'reasoning_content':'保留旧对话时必须保留的思考'}]
    packed,metric=assemble(messages,ev,settings,schemas('knowledge'))
    assert metric['compacted'] and metric['output_reserved_tokens']==4096
    assert packed[1]['content']=='原问题' and all(m['role'] in {'system','user'} for m in packed)
    assert '尾部校验码' in json.dumps(packed,ensure_ascii=False)
    assert json.dumps(packed,ensure_ascii=False).count('尾部校验码')==1
    assert metric['estimated_input_tokens']+4096<=settings.max_context_tokens

def test_impossibly_small_budget_rejects_without_request():
    settings=replace(ModelSettings('https://example.test/chat/completions','test','key'),max_context_chars=100)
    with pytest.raises(ValueError):assemble([{'role':'system','content':'rules'},{'role':'user','content':'q'}],[],settings,schemas('knowledge'))

def test_transport_forces_submit_in_closing_and_typed_truncation():
    settings=ModelSettings('https://example.test/chat/completions','test','key')
    def handler(req):
        payload=json.loads(req.content)
        assert payload['tool_choice']=={'type':'function','function':{'name':'submit_answer'}}
        return httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{}}]})
    with pytest.raises(ModelOutputTruncated):ModelClient(settings,httpx.MockTransport(handler)).complete([], [t for t in schemas('knowledge') if t['function']['name']=='submit_answer'])

@pytest.mark.parametrize('value',['bad','8191','262145','8192'])
def test_invalid_context_configuration(monkeypatch,value):
    monkeypatch.setenv('ONTOLOGY_MODEL_URL','https://example.test/chat/completions');monkeypatch.setenv('ONTOLOGY_MODEL_NAME','test');monkeypatch.setenv('ONTOLOGY_MODEL_KEY','key')
    monkeypatch.setenv('ONTOLOGY_MODEL_CONTEXT_TOKENS',value)
    monkeypatch.setenv('ONTOLOGY_MODEL_MAX_OUTPUT_TOKENS','8192')
    with pytest.raises(ModelError):ModelSettings.from_env()
