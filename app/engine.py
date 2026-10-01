"""Deterministic demo workflows. Model and business APIs are intentionally not connected."""
import difflib
import json
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime,timezone
from pathlib import Path
from .db import connect

def now():return datetime.now(timezone.utc).isoformat()
SCENARIOS={'unreleased','unverified','verified','conflict','stale'}
KINDS={'complaint','operations','requirements','coding'}

def create_task(path,kind,scenario,prompt,mode="demo",document_ids=None):
    tid='task-'+uuid.uuid4().hex[:12]
    with connect(path) as c:
        from .retrieval import validate_selection
        validate_selection(c,document_ids or [])
        c.execute('INSERT INTO tasks(id,kind,scenario,prompt,status,created_at,mode,document_ids) VALUES(?,?,?,?,?,?,?,?)',(tid,kind,scenario,prompt,'queued',now(),mode,json.dumps(document_ids or [])))
    return tid

def coding_fixture():
    before='def callback(order, callback_id):\n    if callback_id in order["seen"]:\n        return order\n    order["seen"].add(callback_id)\n    if order["fail_once"]:\n        order["fail_once"] = False\n        raise RuntimeError("update failed")\n    order["status"] = "paid"\n    order["charges"] += 1\n    return order\n'
    after='def callback(order, callback_id):\n    if callback_id in order["seen"]:\n        return order\n    if order["fail_once"]:\n        order["fail_once"] = False\n        raise RuntimeError("update failed")\n    order["status"] = "paid"\n    order["charges"] += 1\n    order["seen"].add(callback_id)\n    return order\n'
    tests='''
import unittest
class CallbackTests(unittest.TestCase):
    def fresh(self, fail=False):
        return dict(seen=set(),fail_once=fail,status="unpaid",charges=0)
    def test_success(self):
        order=self.fresh(); callback(order,"a"); self.assertEqual(order["status"],"paid")
    def test_duplicate(self):
        order=self.fresh(); callback(order,"a"); callback(order,"a"); self.assertEqual(order["charges"],1)
    def test_retry(self):
        order=self.fresh(True)
        with self.assertRaises(RuntimeError):callback(order,"a")
        callback(order,"a"); self.assertEqual(order["status"],"paid"); self.assertEqual(order["charges"],1)
if __name__=="__main__":unittest.main()
'''
    with tempfile.TemporaryDirectory(prefix='ontology-fixture-') as tmp:
        results=[]
        for label,code in [('before',before),('after',after)]:
            p=Path(tmp)/f'{label}.py';p.write_text(code+tests)
            r=subprocess.run([sys.executable,'-I',str(p)],cwd=tmp,capture_output=True,text=True,timeout=10)
            results.append({'stage':label,'exit_code':r.returncode,'output':r.stdout+r.stderr})
    return {'patch':''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='a/callback.py',tofile='b/callback.py')),'tests':results,'mode':'curated-fixture','limitation':'仅执行内置可信样例，不接受任意代码；此示例不是生产支付实现，也不是通用安全沙箱。'}

class LeaseLost(RuntimeError):pass

def process_one(path,model_client=None):
    from .model import ModelError
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE tasks SET status='queued',lease_until=NULL WHERE status='running' AND lease_until<? AND attempts<3",(time.time(),))
        c.execute("UPDATE tasks SET status='failed',error='重试次数已耗尽' WHERE status='running' AND lease_until<? AND attempts>=3",(time.time(),))
        row=c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:return False
        task=dict(row);task['attempts']+=1;token=uuid.uuid4().hex
        c.execute("UPDATE tasks SET status='running',started_at=?,lease_until=?,attempts=attempts+1,worker_token=? WHERE id=?",(now(),time.time()+60,token,task['id']))
    def own(c):
        r=c.execute('SELECT status,worker_token,lease_until FROM tasks WHERE id=?',(task['id'],)).fetchone()
        if not r or r['status']!='running' or r['worker_token']!=token or r['lease_until']<time.time():raise LeaseLost()
    def heartbeat():
        with connect(path) as c:
            c.execute('BEGIN IMMEDIATE');own(c)
            c.execute('UPDATE tasks SET lease_until=? WHERE id=?',(time.time()+60,task['id']))
    def save_evidence(c,e,idx):
        c.execute('INSERT OR IGNORE INTO evidence(id,task_id,label,payload,origin,observed_at,classification,attempt) VALUES(?,?,?,?,?,?,?,?)',(e.get('id',f"{task['id']}-a{task['attempts']}-e{idx}"),task['id'],e['label'],json.dumps(e['payload'],ensure_ascii=False),e['origin'],now(),e['classification'],task['attempts']))
        for ref in e['payload'].get('knowledge_refs',[]):
            c.execute('INSERT OR IGNORE INTO task_knowledge_refs VALUES(?,?,?,?,?)',(task['id'],task['attempts'],e.get('id',f"{task['id']}-a{task['attempts']}-e{idx}"),ref['document_id'],ref['revision']))
    def record(name,args,status,result,evidence=None):
        with connect(path) as c:
            c.execute('BEGIN IMMEDIATE');own(c)
            c.execute('INSERT INTO tool_calls(task_id,attempt,name,arguments,status,result,created_at) VALUES(?,?,?,?,?,?,?)',(task['id'],task['attempts'],name,json.dumps(args,ensure_ascii=False),status,json.dumps(result,ensure_ascii=False),now()))
            c.execute('INSERT INTO steps(task_id,label,detail,created_at,attempt) VALUES(?,?,?,?,?)',(task['id'],name,'工具／模型调用：'+status,now(),task['attempts']))
            if evidence:save_evidence(c,evidence,0)
    try:
        if task['mode']=='model':
            from .agent import run_agent
            result,evidence=run_agent(path,task,client=model_client,heartbeat=heartbeat,record=record)
        else:
            result,evidence=execute(task)
            # Fixed templates use only their original seed baseline, never endorse a new source revision.
            evidence[0]['payload']['knowledge_refs']=[{'document_id':d,'revision':1} for d in ['kb-runbook','kb-release','kb-accept']]
        with connect(path) as c:
            c.execute('BEGIN IMMEDIATE');own(c)
            for idx,e in enumerate(evidence):save_evidence(c,e,idx)
            from .knowledge import task_warnings,block_result
            result=block_result(result,task_warnings(c,task['id'],task['attempts']))
            if task['mode']=='demo':
                steps=[('确定任务范围','支付演示项目；仅使用预置对象与可信样例'),('获取上下文','排障手册、需求验收标准和已确认对象关联'),('核实证据','模拟连接器返回部署与验证记录；对比来源和时效'),('交付结果','记录确定性判断、未验证范围与交付物；未执行外部动作')]
                for label,detail in steps:c.execute('INSERT INTO steps(task_id,label,detail,created_at,attempt) VALUES(?,?,?,?,?)',(task['id'],label,detail,now(),task['attempts']))
            c.execute("UPDATE tasks SET status=?,result=?,completed_at=?,lease_until=NULL WHERE id=?",('partial' if result.get('partial') else 'completed',json.dumps(result,ensure_ascii=False),now(),task['id']))
    except LeaseLost:
        pass  # An expired owner cannot overwrite the current attempt.
    except Exception as exc:
        message=str(exc) if isinstance(exc,ModelError) else '任务执行失败，请检查服务配置或工具状态'
        with connect(path) as c:
            c.execute("UPDATE tasks SET status='failed',error=?,lease_until=NULL WHERE id=? AND worker_token=? AND status='running'",(message,task['id'],token))
        import logging
        if isinstance(exc,ModelError):logging.error('Task %s failed (%s)',task['id'],type(exc).__name__)
        else:logging.exception('Task %s failed unexpectedly',task['id'])
    return True

def execute(task):
    s=task['scenario'];fresh=s!='stale';deployed=s in {'verified','unverified','stale'};verified=s in {'verified','stale'}
    decision='confirmed' if fresh and deployed and verified else 'insufficient'
    titles={'unreleased':'代码已修复，尚未上线','unverified':'已部署，尚不能确认修复','verified':'本次问题已修复并验证','conflict':'部署来源冲突，不能确认修复','stale':'证据已过期，需要重新取证'}
    ev=[{'label':'修复内容','payload':{'commit':'abc123','version':'v2.3','document':'kb-release'},'origin':'代码记录（模拟）','classification':'fact'}, {'label':'生产部署状态','payload':{'deployed':deployed,'version':'v2.3' if deployed else 'v2.2','fresh':fresh,'authority':'deployment-platform'},'origin':'发布平台连接器（模拟）','classification':'fact'},{'label':'业务验证','payload':{'passed':verified,'fresh':fresh,'scope':'回调更新与重复回调'},'origin':'验证记录连接器（模拟）','classification':'fact'}]
    if s=='conflict':ev.append({'label':'冲突的图谱缓存','payload':{'deployed':True,'authoritative':False,'resolution':'以当前发布平台记录为准'},'origin':'图谱缓存（模拟）','classification':'conflict'})
    result={'title':titles[s],'decision':decision,'rule_version':'fix-confirmation@1','scope':'支付功能 / production / 回调问题','next_action':'人工审核回复草稿' if decision=='confirmed' else '补齐当前生产部署与验证证据','mode':'deterministic-demo','external_actions':False,'artifacts':[]}
    kind=task['kind']
    if kind=='complaint':result['artifacts']=[{'title':'客户回复草稿','text':'支付回调导致的订单状态问题已完成修复，并已通过针对性验证。' if decision=='confirmed' else '我们正在核实订单状态问题，目前尚不能确认修复已生效。'}]
    elif kind=='operations':
        result['artifacts']=[{'title':'运维分析','text':'候选原因：回调重试路径的订单更新错误。依据：修复说明和历史症状；仍需本次请求日志验证因果。\n当前部署判断：'+titles[s]+'。\n下一步：按订单与请求 ID 查询更新失败及重试记录。'}]
    elif kind=='requirements':
        result['title']='需求草稿已生成';result['decision']='draft';result['next_action']='人工评审范围和待确认业务规则';result['artifacts']=[{'title':'REQ-17 需求草稿','text':'目标：支付回调可安全重试，避免订单状态与支付结果不一致。\n范围：回调处理与订单更新。\n验收：1.正常回调更新状态；2.重复回调不重复记账；3.失败后重试成功。\n待确认：重试上限、超时、人工补偿流程及并发事务边界。\n来源：kb-accept@2.0。'}]
    elif kind=='coding':
        fixture=coding_fixture();result['title']='示例补丁与测试结果';result['decision']='draft';result['next_action']='审核示例补丁，生产实现需独立验证';result['coding']=fixture;result['artifacts']=[{'title':'修复说明','text':'将去重标记移动到状态更新成功之后。样例测试先复现重试失败，再验证修改。生产实现仍需事务、并发控制和实际代码评审。'}]
    return result,ev
