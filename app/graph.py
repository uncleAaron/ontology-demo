import json
from collections import deque
from .db import connect
PROJECT='payments'  # Fixed demo principal, not client-supplied authorization.

def object_dict(r):
    d=dict(r);d['properties']=json.loads(d['properties']);return d

def graph(path,center='svc-payment',depth=2,status='confirmed',at='2026-09-30T00:00:00Z',limit=40,relation=None):
    with connect(path) as c:
        c.execute('BEGIN')
        nodes={r['id']:object_dict(r) for r in c.execute('SELECT * FROM objects WHERE project=?',(PROJECT,))}
        if center not in nodes: raise KeyError('对象不存在或不可访问')
        edges=[]
        for r in c.execute('SELECT r.*,t.label FROM relations r JOIN relation_types t ON t.id=r.type'):
            d=dict(r)
            if d['source'] not in nodes or d['target'] not in nodes:continue
            from .knowledge import relation_state
            d=relation_state(c,d)
            if d is None:continue
            if relation and d['type']!=relation:continue
            active=d['valid_from']<=at and (not d['valid_to'] or at<d['valid_to'])
            if status=='confirmed' and (d['status']!='confirmed' or not active):continue
            if status=='candidate' and (d['status']!='candidate' or not active):continue
            d['active_at_query']=active;edges.append(d)
        visited={center};queue=deque([(center,0)]);truncated=False
        while queue:
            node,level=queue.popleft()
            neighbors={e['target'] if e['source']==node else e['source'] for e in edges if node in (e['source'],e['target'])}
            if level>=depth:
                if neighbors-visited:truncated=True
                continue
            for nxt in sorted(neighbors):
                if nxt in visited:continue
                if len(visited)>=limit:truncated=True;continue
                visited.add(nxt);queue.append((nxt,level+1))
        selected=[e for e in edges if e['source'] in visited and e['target'] in visited]
        refs=[{'document_id':e['document_id'],'revision':e['source_revision']} for e in selected if e['document_id'] and e.get('source_revision')]
        return {'nodes':[nodes[k] for k in sorted(visited)],'edges':selected,'knowledge_refs':refs, 'truncated':truncated,'scope':{'project':PROJECT,'center':center,'depth':depth,'status':status,'at':at},'model_version':'1.0','mode':'seeded-demo'}

def find_path(path,start,end,depth=6):
    g=graph(path,start,depth,'confirmed',limit=100)
    ids={n['id'] for n in g['nodes']}
    if end not in ids:return {'nodes':[],'edges':[],'message':'当前授权范围和深度内未找到有向路径'}
    queue=deque([(start,[start],[])]);seen={start}
    while queue:
        node,ns,es=queue.popleft()
        if node==end:return {'nodes':ns,'edges':es,'message':'有向关联路径，不代表因果'}
        if len(es)>=depth:continue
        for e in g['edges']:
            if e['source']==node and e['target'] not in seen:
                seen.add(e['target']);queue.append((e['target'],ns+[e['target']],es+[e['id']]))
    return {'nodes':[],'edges':[],'message':'当前授权范围和深度内未找到有向路径'}
