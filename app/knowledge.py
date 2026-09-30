"""Document-granularity maintenance for the fixed demo principal."""
import json
import difflib
from datetime import datetime, timezone
from .graph import PROJECT

SCHEMA = '''
CREATE TABLE IF NOT EXISTS document_revisions(
 document_id TEXT NOT NULL REFERENCES documents(id),revision INTEGER NOT NULL,
 title TEXT NOT NULL,body TEXT NOT NULL,version TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(document_id,revision));
CREATE TABLE IF NOT EXISTS document_heads(
 document_id TEXT PRIMARY KEY REFERENCES documents(id),revision INTEGER NOT NULL,published_revision INTEGER NOT NULL,
 generation INTEGER NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relation_sources(
 relation_id TEXT PRIMARY KEY REFERENCES relations(id),document_id TEXT NOT NULL REFERENCES documents(id),
 revision INTEGER NOT NULL,generation INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS task_knowledge_refs(
 task_id TEXT NOT NULL REFERENCES tasks(id),attempt INTEGER NOT NULL,evidence_id TEXT NOT NULL,
 document_id TEXT NOT NULL REFERENCES documents(id),revision INTEGER NOT NULL,
 PRIMARY KEY(task_id,attempt,evidence_id,document_id,revision));
CREATE INDEX IF NOT EXISTS knowledge_refs_document ON task_knowledge_refs(document_id,revision);
CREATE TABLE IF NOT EXISTS maintenance_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,document_id TEXT NOT NULL REFERENCES documents(id),
 action TEXT NOT NULL,revision INTEGER NOT NULL,reason TEXT NOT NULL,actor TEXT NOT NULL,
 details TEXT NOT NULL,created_at TEXT NOT NULL);
'''

class Conflict(ValueError):pass

def now():return datetime.now(timezone.utc).isoformat()

def migrate(c):
    c.executescript(SCHEMA)
    c.execute("INSERT OR IGNORE INTO document_revisions SELECT id,1,title,body,version,'既有演示资料迁移',? FROM documents",(now(),))
    c.execute("INSERT OR IGNORE INTO document_heads SELECT id,1,1,1,'active' FROM documents")
    c.execute('INSERT OR IGNORE INTO relation_sources SELECT id,document_id,1,1 FROM relations WHERE document_id IS NOT NULL')
    c.execute("INSERT OR REPLACE INTO meta VALUES('schema_version','3')")

def event(c,doc_id,action,revision,reason,details=None):
    c.execute('INSERT INTO maintenance_events(document_id,action,revision,reason,actor,details,created_at) VALUES(?,?,?,?,?,?,?)',
              (doc_id,action,revision,reason,'demo-maintainer',json.dumps(details or {},ensure_ascii=False),now()))

def register_document(c,doc_id):
    d=c.execute('SELECT * FROM documents WHERE id=?',(doc_id,)).fetchone()
    c.execute('INSERT INTO document_revisions VALUES(?,?,?,?,?,?,?)',(doc_id,1,d['title'],d['body'],d['version'],'演示导入',now()))
    c.execute("INSERT INTO document_heads VALUES(?,1,1,1,'active')",(doc_id,))
    event(c,doc_id,'import',1,'演示身份直接导入，不代表企业审核')

def head(c,doc_id):
    h=c.execute('SELECT h.* FROM document_heads h JOIN documents d ON d.id=h.document_id WHERE d.id=? AND d.project=?',(doc_id,PROJECT)).fetchone()
    if not h:raise KeyError('资料不存在或不可访问')
    return dict(h)

def expect(c,doc_id,generation):
    h=head(c,doc_id)
    if h['generation']!=generation:raise Conflict('版本已变化，请刷新后重新审核')
    if h['status']=='withdrawn':raise Conflict('资料已撤回，本期不支持恢复')
    return h

def revise(c,doc_id,data):
    h=expect(c,doc_id,data['expected_generation']);rev=h['revision']+1
    c.execute('INSERT INTO document_revisions VALUES(?,?,?,?,?,?,?)',(doc_id,rev,data['title'],data['body'],data['version'],data['reason'],now()))
    c.execute("UPDATE document_heads SET revision=?,generation=generation+1,status='needs_review' WHERE document_id=?",(rev,doc_id))
    event(c,doc_id,'source_updated',rev,data['reason'])
    return head(c,doc_id)

def review(c,doc_id,data):
    h=expect(c,doc_id,data['expected_generation'])
    if h['status']!='needs_review':raise Conflict('当前没有待审核修订')
    if data['decision']=='approve':
        r=c.execute('SELECT * FROM document_revisions WHERE document_id=? AND revision=?',(doc_id,h['revision'])).fetchone()
        c.execute('UPDATE documents SET title=?,body=?,version=? WHERE id=?',(r['title'],r['body'],r['version'],doc_id))
        c.execute("UPDATE document_heads SET published_revision=revision,status='active',generation=generation+1 WHERE document_id=?",(doc_id,))
    else:c.execute("UPDATE document_heads SET status='rejected',generation=generation+1 WHERE document_id=?",(doc_id,))
    event(c,doc_id,data['decision'],h['revision'],data['reason'])
    return head(c,doc_id)

def withdraw(c,doc_id,data):
    h=expect(c,doc_id,data['expected_generation'])
    c.execute("UPDATE document_heads SET status='withdrawn',generation=generation+1 WHERE document_id=?",(doc_id,))
    event(c,doc_id,'withdraw',h['revision'],data['reason'])
    return head(c,doc_id)

def relation_state(c,r):
    """Only call for already authorized endpoints; no hidden-source metadata leaks."""
    d=dict(r)
    if not d['document_id']:return d
    try:h=head(c,d['document_id'])
    except KeyError:return None
    source=c.execute('SELECT * FROM relation_sources WHERE relation_id=?',(d['id'],)).fetchone()
    d['source_revision']=source['revision'] if source else None
    d['generation']=source['generation'] if source else 0
    d['recorded_status']=d['status']
    if not source or h['status']!='active' or source['revision']!=h['published_revision']:
        d['status']='source_stale'
    return d

def review_relation(c,relation_id,data):
    r=c.execute('SELECT r.* FROM relations r JOIN objects a ON a.id=r.source JOIN objects b ON b.id=r.target WHERE r.id=? AND a.project=? AND b.project=?',(relation_id,PROJECT,PROJECT)).fetchone()
    if not r or not r['document_id']:raise KeyError('关系不存在或不可审核')
    h=head(c,r['document_id']);s=c.execute('SELECT * FROM relation_sources WHERE relation_id=?',(relation_id,)).fetchone()
    if not s or s['generation']!=data['expected_generation'] or h['status']!='active' or h['published_revision']!=data['expected_document_revision']:
        raise Conflict('来源或关系已变化，请刷新并重新核实')
    if r['status']=='expired' or r['valid_to'] and r['valid_to']<=now():raise Conflict('已过期关系不能由复核恢复')
    c.execute('UPDATE relation_sources SET revision=?,generation=generation+1 WHERE relation_id=?',(h['published_revision'],relation_id))
    c.execute("UPDATE relations SET status='confirmed' WHERE id=?",(relation_id,))
    event(c,r['document_id'],'relation_confirmed',h['published_revision'],data['reason'],{'relation_id':relation_id})
    return {'id':relation_id,'status':'confirmed','source_revision':h['published_revision']}

def valid_refs(c,refs):
    warnings=[]
    for ref in refs:
        try:h=head(c,ref['document_id'])
        except KeyError:
            warnings.append({'document_id':ref['document_id'],'revision':ref['revision'],'status':'unavailable'});continue
        if h['status']!='active' or h['published_revision']!=ref['revision']:
            warnings.append(dict(ref,status=h['status'],current_revision=h['revision']))
    return warnings

def task_warnings(c,task_id,attempt):
    refs=[dict(r) for r in c.execute('SELECT DISTINCT document_id,revision FROM task_knowledge_refs WHERE task_id=? AND attempt=?',(task_id,attempt))]
    return valid_refs(c,refs)

def block_result(result,warnings):
    if not warnings:return result
    return {'title':'知识来源已变化，需重新取证','decision':'insufficient','partial':True,'rule_version':result.get('rule_version','knowledge@1'),'mode':result.get('mode'),'scope':result.get('scope'),'external_actions':False,'knowledge_warnings':warnings,'artifacts':[{'title':'交付已阻断','text':'本次引用资料已更新、待复核或撤回，原草稿不作为可交付结果。历史证据仅供审计，请重新取证。'}],'next_action':'复核知识后创建新任务'}

def maintenance(c,doc_id):
    h=head(c,doc_id)
    versions=[dict(r) for r in c.execute('SELECT * FROM document_revisions WHERE document_id=? ORDER BY revision DESC LIMIT 21',(doc_id,))]
    relations=[]
    for r in c.execute('SELECT r.* FROM relations r JOIN objects a ON a.id=r.source JOIN objects b ON b.id=r.target WHERE r.document_id=? AND a.project=? AND b.project=? ORDER BY r.id LIMIT 101',(doc_id,PROJECT,PROJECT)):
        item=relation_state(c,r)
        if item:relations.append(item)
    tasks=[dict(r) for r in c.execute('SELECT DISTINCT k.task_id,k.attempt,k.revision,t.status FROM task_knowledge_refs k JOIN tasks t ON t.id=k.task_id WHERE document_id=? ORDER BY t.created_at DESC LIMIT 101',(doc_id,))]
    events=[dict(r)|{'details':json.loads(r['details'])} for r in c.execute('SELECT * FROM maintenance_events WHERE document_id=? ORDER BY id DESC LIMIT 51',(doc_id,))]
    baseline=c.execute('SELECT * FROM document_revisions WHERE document_id=? AND revision=?',(doc_id,h['published_revision'] if h['published_revision']!=h['revision'] else max(1,h['revision']-1))).fetchone()
    latest=versions[0]
    diff=''.join(difflib.unified_diff((baseline['title']+'\n'+baseline['body']).splitlines(True),(latest['title']+'\n'+latest['body']).splitlines(True),fromfile='revision-'+str(baseline['revision']),tofile='revision-'+str(latest['revision'])))
    return {'diff':diff,'head':h,'versions':versions[:20],'relations':relations[:100],'tasks':tasks[:100],'events':events[:50],
            'truncated':{'versions':len(versions)>20,'relations':len(relations)>100,'tasks':len(tasks)>100,'events':len(events)>50},
            'coverage':'文档级直接关系与已登记任务引用；不含片段、多来源、间接派生及旧任务缺失血缘',
            'identity':'demo-maintainer（固定演示身份，尚未接入企业鉴权）'}
