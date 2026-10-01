"""Revision-bound text chunks and explainable bilingual keyword retrieval."""
import hashlib
import json
import re
from collections import Counter
from .graph import PROJECT

SCHEMA='''
CREATE TABLE IF NOT EXISTS document_chunks(
 id TEXT PRIMARY KEY,document_id TEXT NOT NULL,revision INTEGER NOT NULL,
 ordinal INTEGER NOT NULL,heading TEXT NOT NULL,start_char INTEGER NOT NULL,end_char INTEGER NOT NULL,
 body TEXT NOT NULL,UNIQUE(document_id,revision,ordinal),
 FOREIGN KEY(document_id,revision) REFERENCES document_revisions(document_id,revision));
CREATE TABLE IF NOT EXISTS chunk_terms(
 chunk_id TEXT NOT NULL REFERENCES document_chunks(id),term TEXT NOT NULL,weight INTEGER NOT NULL,
 PRIMARY KEY(chunk_id,term));
CREATE INDEX IF NOT EXISTS chunk_terms_term ON chunk_terms(term);
'''
STOP={'的','什么','怎么','如何','以及','the','and','what','how','this','that','please'}

def tokens(text):
    words=re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+',text.lower())
    out=[]
    for word in words:
        if re.fullmatch(r'[\u3400-\u9fff]+',word) and len(word)>2:
            out.extend(word[i:i+2] for i in range(len(word)-1))
        else:out.append(word)
    return [w for w in out if w not in STOP and len(w)>1]

def split_body(body):
    start=0;ordinal=0;heading='正文'
    headings=list(re.finditer(r'^#{1,6}\s+(.+)$',body,re.M));hi=0
    while start<len(body):
        end=min(start+1200,len(body))
        if end<len(body):
            boundary=body.rfind('\n',start+600,end)
            if boundary>=0:end=boundary+1
        while hi<len(headings) and headings[hi].start()<end:
            heading=headings[hi].group(1);hi+=1
        yield ordinal,start,end,heading,body[start:end]
        ordinal+=1
        if end==len(body):break
        start=end-150

def index_revision(c,doc_id,revision):
    if c.execute('SELECT 1 FROM document_chunks WHERE document_id=? AND revision=?',(doc_id,revision)).fetchone():return
    r=c.execute('SELECT * FROM document_revisions WHERE document_id=? AND revision=?',(doc_id,revision)).fetchone()
    if not r:return
    for ordinal,start,end,heading,body in split_body(r['body']):
        cid='chunk-'+hashlib.sha256(f'{doc_id}:{revision}:{ordinal}:v1'.encode()).hexdigest()[:24]
        c.execute('INSERT INTO document_chunks VALUES(?,?,?,?,?,?,?,?)',(cid,doc_id,revision,ordinal,heading,start,end,body))
        counts=Counter(tokens(body));title=set(tokens(r['title']+' '+heading))
        for term in counts.keys()|title:
            c.execute('INSERT INTO chunk_terms VALUES(?,?,?)',(cid,term,min(counts[term],4)+(10 if term in title else 0)))

def migrate(c):
    c.executescript(SCHEMA)
    for r in c.execute("SELECT document_id,published_revision FROM document_heads WHERE status='active'").fetchall():
        index_revision(c,r['document_id'],r['published_revision'])

def selected_ids(task):
    value=task.get('document_ids',[])
    return json.loads(value) if isinstance(value,str) else value

def validate_selection(c,ids):
    if len(ids)>20 or len(set(ids))!=len(ids):raise ValueError('最多选择20篇不同的资料')
    for did in ids:
        if not c.execute("SELECT 1 FROM documents d JOIN document_heads h ON h.document_id=d.id WHERE d.id=? AND d.project=? AND h.status='active'",(did,PROJECT)).fetchone():
            raise ValueError('所选资料不存在、无权访问或尚未生效')

def scope_sql(ids):
    return (' AND d.id IN ('+','.join('?' for _ in ids)+')',list(ids)) if ids else ('',[])

def chunk_dict(r):
    return {k:r[k] for k in ['id','document_id','revision','ordinal','heading','start_char','end_char','body','title','version']}

def search(c,query,ids=(),limit=8):
    terms=list(dict.fromkeys(tokens(query)))[:48]
    if not terms:return {'chunks':[],'knowledge_refs':[],'mode':'keyword','truncated':False}
    scope,params=scope_sql(ids)
    rows=c.execute('''SELECT ch.*,r.title,r.version,SUM(t.weight) score,COUNT(*) matched_terms
      FROM chunk_terms t JOIN document_chunks ch ON ch.id=t.chunk_id
      JOIN documents d ON d.id=ch.document_id JOIN document_heads h ON h.document_id=d.id
      JOIN document_revisions r ON r.document_id=d.id AND r.revision=ch.revision
      WHERE d.project=? AND h.status='active' AND h.published_revision=ch.revision'''+scope+
      ' AND t.term IN ('+','.join('?' for _ in terms)+') GROUP BY ch.id ORDER BY score DESC,ch.id LIMIT ?',
      [PROJECT,*params,*terms,limit+1]).fetchall()
    chunks=[]
    for r in rows[:limit]:
        item=chunk_dict(r);item.update(score=r['score'],matched_terms=r['matched_terms'],retrieval_reason='关键词匹配')
        chunks.append(item)
    return {'chunks':chunks,'knowledge_refs':[{'document_id':x['document_id'],'revision':x['revision']} for x in chunks],
            'mode':'keyword','query_terms':terms,'truncated':len(rows)>limit}

def read_chunk(c,cid,ids=(),require_active=True):
    scope,params=scope_sql(ids)
    row=c.execute('''SELECT ch.*,r.title,r.version,h.status,h.published_revision FROM document_chunks ch
      JOIN documents d ON d.id=ch.document_id JOIN document_heads h ON h.document_id=d.id
      JOIN document_revisions r ON r.document_id=d.id AND r.revision=ch.revision
      WHERE ch.id=? AND d.project=?'''+scope,[cid,PROJECT,*params]).fetchone()
    if not row or require_active and (row['status']!='active' or row['published_revision']!=row['revision']):
        raise ValueError('片段不存在、不在所选资料范围或来源已失效')
    return chunk_dict(row)|{'source_status':row['status'],'current':row['status']=='active' and row['published_revision']==row['revision'],
                            'knowledge_refs':[{'document_id':row['document_id'],'revision':row['revision']}]}

def initial_context(c,task):
    ids=selected_ids(task)
    validate_selection(c,ids)
    output=search(c,task['prompt'],ids,8)
    if not output['chunks'] and ids:
        # A selected document is never silently ignored. Bounded opening excerpts help navigation.
        scope,params=scope_sql(ids)
        rows=c.execute('''SELECT ch.*,r.title,r.version FROM document_chunks ch
          JOIN documents d ON d.id=ch.document_id JOIN document_heads h ON h.document_id=d.id
          JOIN document_revisions r ON r.document_id=d.id AND r.revision=ch.revision
          WHERE d.project=? AND h.status='active' AND h.published_revision=ch.revision AND ch.ordinal=0'''+scope+' ORDER BY d.id LIMIT 9',[PROJECT,*params]).fetchall()
        output['chunks']=[chunk_dict(r)|{'score':0,'matched_terms':0,'retrieval_reason':'无关键词匹配；所选资料开头，仅用于导航'} for r in rows[:8]]
        output['truncated']=len(rows)>8
        output['knowledge_refs']=[{'document_id':x['document_id'],'revision':x['revision']} for x in output['chunks']]
    output['notice']='仅装入返回的片段，不代表读完全文；无匹配时不得用样例替代依据。'
    return output
