"""Explicit opt-in adapter for a tool-calling Chat Completions compatible endpoint."""
import json
import os
import time
from dataclasses import dataclass,field
from urllib.parse import urlsplit
import httpx

class ModelError(RuntimeError):
    """Safe, user-facing errors; never include provider body, credentials or URL."""

@dataclass(frozen=True)
class ModelSettings:
    endpoint:str
    model:str
    api_key:str=field(repr=False)
    timeout:float=20
    max_rounds:int=6
    max_calls:int=12
    max_context_chars:int=48000
    max_output_tokens:int=1200

    @classmethod
    def from_env(cls):
        endpoint=os.getenv('ONTOLOGY_MODEL_URL','').strip()
        model=os.getenv('ONTOLOGY_MODEL_NAME','').strip()
        key=os.getenv('ONTOLOGY_MODEL_KEY','').strip()
        if not endpoint or not model or not key:raise ModelError('模型未配置：需要服务地址、模型名称和服务端凭据')
        parsed=urlsplit(endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelError('模型地址不允许包含凭据、查询参数或片段')
        if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'localhost','127.0.0.1','::1'}):
            raise ModelError('模型地址必须使用 HTTPS；本机测试允许 HTTP')
        if not parsed.hostname or not parsed.path.endswith('/chat/completions'):
            raise ModelError('请配置完整的 Chat Completions 接口地址')
        return cls(endpoint,model,key)

class ModelClient:
    def __init__(self,settings,transport=None):
        self.settings=settings;self.transport=transport

    def complete(self,messages,tools):
        s=self.settings
        if len(json.dumps(messages,ensure_ascii=False))>s.max_context_chars:
            raise ModelError('上下文超过本次任务上限，已停止模型调用')
        try:
            with httpx.Client(timeout=s.timeout,follow_redirects=False,transport=self.transport) as client:
                start=time.monotonic()
                with client.stream('POST',s.endpoint,headers={'Authorization':'Bearer '+s.api_key},json={
                    'model':s.model,'messages':messages,'tools':tools,'tool_choice':'auto',
                    'parallel_tool_calls':False,'max_tokens':s.max_output_tokens,
                }) as response:
                    if response.status_code!=200:raise ModelError('模型服务拒绝或未完成请求（HTTP '+str(response.status_code)+'）')
                    raw=bytearray()
                    for block in response.iter_bytes():
                        raw.extend(block)
                        if len(raw)>512000 or time.monotonic()-start>s.timeout:raise ModelError('模型响应超过大小或时间上限')
                payload=json.loads(raw)
            if not isinstance(payload,dict) or not isinstance(payload.get('choices'),list) or not payload['choices']:
                raise ModelError('模型响应缺少有效的 choices')
            choice=payload['choices'][0]
            if not isinstance(choice,dict):raise ModelError('模型响应的 choice 格式无效')
            if choice.get('finish_reason')=='length':
                raise ModelError('模型输出达到 token 上限而被截断，请缩小任务或增加输出预算')
            if choice.get('finish_reason') in {'content_filter','insufficient_system_resource'}:
                raise ModelError('模型服务未完成生成：内容过滤或资源不足')
            message=choice.get('message')
            if not isinstance(message,dict):raise ModelError('模型响应缺少有效的 message')
            calls=message.get('tool_calls') or []
            if not isinstance(calls,list):raise ModelError('模型 tool_calls 必须是数组')
            if len(calls)>4:raise ModelError('模型一次请求的工具数量过多（最多4个）')
            ids=set();clean=[]
            for call in calls:
                f=call['function'];cid=call['id']
                if not isinstance(cid,str) or not cid or cid in ids or call.get('type')!='function':raise ValueError()
                if not isinstance(f['name'],str) or not isinstance(f['arguments'],str) or len(f['arguments'])>12000:raise ValueError()
                ids.add(cid);clean.append({'id':cid,'type':'function','function':{'name':f['name'],'arguments':f['arguments']}})
            result={'role':'assistant','content':message.get('content'),'tool_calls':clean}
            if result['content'] is not None and not isinstance(result['content'],str):raise ValueError()
            if 'reasoning_content' in message:
                reasoning=message['reasoning_content']
                if reasoning is not None and not isinstance(reasoning,str):raise ValueError()
                result['reasoning_content']=reasoning
            return result
        except ModelError:raise
        except httpx.TimeoutException:raise ModelError('模型请求超时，请稍后重试') from None
        except httpx.HTTPError:raise ModelError('无法连接模型服务') from None
        except (ValueError,KeyError,TypeError,IndexError):raise ModelError('模型返回格式不符合工具调用协议') from None
