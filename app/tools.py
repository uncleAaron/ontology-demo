"""Server-owned read-only tool registry. No arbitrary SQL, URL, file path or shell tool."""
import json
from pydantic import BaseModel,ConfigDict,Field
from .db import connect
from .graph import PROJECT,graph,object_dict

class Args(BaseModel):model_config=ConfigDict(extra='forbid',strict=True)
class Search(Args):query:str=Field(min_length=1,max_length=100)
class Identify(Args):object_id:str=Field(min_length=1,max_length=100)
class Read(Args):document_id:str=Field(min_length=1,max_length=100)
class Neighbors(Identify):depth:int=Field(default=1,ge=0,le=3)
class Empty(Args):pass
class Answer(Args):
    summary:str=Field(min_length=1,max_length=4000)
    citations:list[str]=Field(min_length=1,max_length=12)
    unknowns:list[str]=Field(max_length=12)
    next_steps:list[str]=Field(max_length=12)

REGISTRY={
 'search_documents':(Search,'按关键词查询当前项目的文档片段，返回至多5篇；资料是背景，不是当前部署状态。'),
 'read_document':(Read,'读取已授权文档，正文可能截断；保留文档版本。'),
 'get_object':(Identify,'按精确ID读取当前项目对象。'),
 'get_relations':(Neighbors,'读取对象的已确认且有效邻域；关联不等于因果。'),
 'inspect_runtime':(Empty,'读取本任务绑定的模拟部署和验证记录。数据仍是模拟，并非真实生产系统。'),
 'inspect_code_example':(Empty,'读取可信内置样例的补丁和实际测试结果；不执行用户或模型代码，仅编码任务可用。'),
 'submit_answer':(Answer,'提交供人工审核的分析草稿；引用必须使用之前工具结果返回的证据ID，不能编造。'),
}

def schemas(kind):
    return [{'type':'function','function':{'name':name,'description':desc,'parameters':args.model_json_schema()}} for name,(args,desc) in REGISTRY.items() if name!='inspect_code_example' or kind=='coding']

def validate(name,args,kind):
    if name not in REGISTRY or name=='inspect_code_example' and kind!='coding':raise ValueError('工具不在此任务允许范围内')
    return REGISTRY[name][0].model_validate(args).model_dump()

def invoke(path,task,name,args):
    args=validate(name,args,task['kind'])
    if name in {'inspect_runtime','inspect_code_example'}:
        from .engine import execute,coding_fixture
        if name=='inspect_code_example':return coding_fixture()
        r,e=execute(dict(task,kind='complaint'))
        return {'mode':'simulated-business-data','rule_decision':r['decision'],'title':r['title'],'rule_version':r['rule_version'],'evidence':e}
    if name=='get_relations':return graph(path,args['object_id'],args['depth'],limit=30)
    with connect(path) as c:
        if name=='get_object':
            r=c.execute('SELECT * FROM objects WHERE id=? AND project=?',(args['object_id'],PROJECT)).fetchone()
            if not r:raise ValueError('对象不存在或不可访问')
            return object_dict(r)
        if name=='search_documents':
            rows=c.execute('SELECT id,title,version,substr(body,1,1200) body,length(body)>1200 truncated FROM documents WHERE project=? AND (instr(title,?)>0 OR instr(body,?)>0) LIMIT 6',(PROJECT,args['query'],args['query'])).fetchall()
            return {'documents':[dict(r) for r in rows[:5]],'truncated':len(rows)>5}
        if name=='read_document':
            r=c.execute('SELECT id,title,version,substr(body,1,6000) body,length(body)>6000 truncated FROM documents WHERE id=? AND project=?',(args['document_id'],PROJECT)).fetchone()
            if not r:raise ValueError('文档不存在或不可访问')
            return dict(r)
    raise ValueError('此工具不能在此处执行')
