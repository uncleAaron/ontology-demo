import json
import sqlite3
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS types(id TEXT PRIMARY KEY,label TEXT NOT NULL,properties TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relation_types(id TEXT PRIMARY KEY,label TEXT NOT NULL,source_type TEXT NOT NULL,target_type TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,title TEXT NOT NULL,version TEXT NOT NULL,body TEXT NOT NULL,project TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY,type TEXT NOT NULL REFERENCES types(id),label TEXT NOT NULL,project TEXT NOT NULL,properties TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relations(id TEXT PRIMARY KEY,source TEXT NOT NULL REFERENCES objects(id),target TEXT NOT NULL REFERENCES objects(id),type TEXT NOT NULL REFERENCES relation_types(id),status TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT,origin TEXT NOT NULL,document_id TEXT REFERENCES documents(id));
CREATE INDEX IF NOT EXISTS relation_source ON relations(source,type);
CREATE INDEX IF NOT EXISTS relation_target ON relations(target,type);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,kind TEXT NOT NULL,scenario TEXT NOT NULL,prompt TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT,completed_at TEXT,attempts INTEGER NOT NULL DEFAULT 0,lease_until REAL,result TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),label TEXT NOT NULL,payload TEXT NOT NULL,origin TEXT NOT NULL,observed_at TEXT NOT NULL,classification TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_calls(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL REFERENCES tasks(id),attempt INTEGER NOT NULL,name TEXT NOT NULL,arguments TEXT NOT NULL,status TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL REFERENCES tasks(id),label TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
'''

def connect(path):
    conn=sqlite3.connect(path,timeout=10)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn

def init(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with connect(path) as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript(SCHEMA)
        for table,column,definition in [('tasks','mode',"TEXT NOT NULL DEFAULT 'demo'"),('tasks','worker_token','TEXT'),('evidence','attempt','INTEGER NOT NULL DEFAULT 1'),('steps','attempt','INTEGER NOT NULL DEFAULT 1')]:
            if column not in {r['name'] for r in c.execute('PRAGMA table_info('+table+')')}:
                c.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' '+definition)
        c.execute("INSERT OR REPLACE INTO meta VALUES('schema_version','2')")
        if c.execute("SELECT 1 FROM meta WHERE key='seed'").fetchone(): return
        types=[('ticket','客诉',['编号','描述']),('feature','功能',['名称','负责人']),('service','服务',['名称','仓库']),('version','版本',['版本号']),('commit','提交',['提交号','仓库']),('defect','缺陷',['问题范围']),('deployment','部署',['环境','状态']),('test','验证',['结果','环境']),('requirement','需求',['目标','验收条件']),('repo','仓库',['名称'])]
        c.executemany('INSERT INTO types VALUES(?,?,?)',[(a,b,json.dumps(v,ensure_ascii=False)) for a,b,v in types])
        rels=[('concerns','涉及','ticket','feature'),('implemented_by','由服务实现','feature','service'),('has_version','拥有版本','service','version'),('contains','包含','version','commit'),('fixes','修复','commit','defect'),('uses','使用版本','deployment','version'),('verifies','验证','test','deployment'),('requires','要求','requirement','feature'),('in_repo','对应仓库','service','repo'),('reported','报告','ticket','defect')]
        c.executemany('INSERT INTO relation_types VALUES(?,?,?,?)',rels)
        docs=[('kb-runbook','支付回调排障手册','1.4','支付成功但订单未更新时，检查回调记录、订单状态和重试日志。E42 表示更新未完成。历史相似问题只能提供排查线索，不能证明本次根因。','payments'),('kb-release','v2.3 修复说明','1.0','提交 abc123 修复回调重试路径中的订单状态更新问题。计划随 v2.3 发布。部署状态以发布平台的有效记录为准。','payments'),('kb-accept','支付需求与验收标准','2.0','支付成功后订单应更新为已支付。重复回调不得重复记账。更新失败后允许安全重试。验证需要指定版本与环境。','payments'),('kb-private','其他项目内部资料','1.0','不应进入支付项目的查询或图谱。','other')]
        c.executemany('INSERT INTO documents VALUES(?,?,?,?,?)',docs)
        objs=[('cs-1042','ticket','CS-1042 客诉',{'描述':'已付款但订单未支付'}),('payment','feature','支付功能',{'负责人':'交易产品组'}),('svc-payment','service','支付服务',{'环境':'production','负责人':'支付研发组'}),('v2.3','version','v2.3',{'状态':'版本存在不代表已部署'}),('abc123','commit','abc123',{'说明':'重试路径修复'}),('bug-42','defect','BUG-42',{'范围':'回调重试'}),('deploy-23','deployment','生产部署记录',{'状态':'模拟缓存：已部署','来源':'发布平台缓存'}),('verify-23','test','支付回调验证',{'状态':'请查任务实时证据'}),('req-17','requirement','REQ-17 幂等回调',{'目标':'重复回调不重复记账'}),('payment-repo','repo','payment-repo',{'说明':'示例代码仓库'}),('v2.2','version','v2.2',{'状态':'历史版本'})]
        c.executemany('INSERT INTO objects VALUES(?,?,?,?,?)',[(i,t,l,'payments',json.dumps(p,ensure_ascii=False)) for i,t,l,p in objs])
        c.execute('INSERT INTO objects VALUES(?,?,?,?,?)',('secret-service','service','隐藏服务','other','{}'))
        edges=[('e1','cs-1042','payment','concerns','confirmed','客服记录','kb-runbook',None),('e2','payment','svc-payment','implemented_by','confirmed','服务目录','kb-runbook',None),('e3','svc-payment','v2.3','has_version','confirmed','发布清单','kb-release',None),('e4','v2.3','abc123','contains','confirmed','发布清单','kb-release',None),('e5','abc123','bug-42','fixes','confirmed','代码评审','kb-release',None),('e6','deploy-23','v2.3','uses','confirmed','发布平台缓存','kb-release',None),('e7','verify-23','deploy-23','verifies','candidate','模型提议，待确认','kb-accept',None),('e8','req-17','payment','requires','confirmed','需求系统','kb-accept',None),('e9','svc-payment','payment-repo','in_repo','confirmed','服务目录','kb-release',None),('e10','cs-1042','bug-42','reported','candidate','历史相似问题匹配','kb-runbook',None),('e11','svc-payment','v2.2','has_version','expired','历史发布记录','kb-release','2026-09-29T00:00:00Z'),('e12','payment','secret-service','implemented_by','confirmed','其他项目',None,None)]
        for i,a,b,t,status,origin,doc,end in edges:
            expected=c.execute('SELECT source_type,target_type FROM relation_types WHERE id=?',(t,)).fetchone()
            actual=[c.execute('SELECT type FROM objects WHERE id=?',(x,)).fetchone()[0] for x in [a,b]]
            assert list(expected)==actual
            c.execute('INSERT INTO relations VALUES(?,?,?,?,?,?,?,?,?)',(i,a,b,t,status,'2026-09-01T00:00:00Z',end,origin,doc))
        c.execute("INSERT INTO meta VALUES('seed','1')")
