from __future__ import annotations
import json, sqlite3, threading, time, uuid
from contextlib import contextmanager
from pathlib import Path
from .errors import ConflictError

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS principals(id TEXT PRIMARY KEY, platform TEXT NOT NULL, user_id TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', display_name TEXT, created_at INTEGER NOT NULL, UNIQUE(platform,user_id,scope));
CREATE TABLE IF NOT EXISTS global_roles(principal_id TEXT NOT NULL REFERENCES principals(id), role TEXT NOT NULL CHECK(role IN ('agent-builder-user','agent-builder-owner','hermes-admin')), created_at INTEGER NOT NULL, created_by TEXT NOT NULL, PRIMARY KEY(principal_id,role));
CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, profile_name TEXT NOT NULL, display_name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL DEFAULT '', owner_id TEXT NOT NULL REFERENCES principals(id), owner_platform TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('creating','pending_approval','active','disabled','error','deleting','deleted')), risk_level TEXT NOT NULL CHECK(risk_level IN ('human-approval','agent','autonomous','read-only','low','medium','high')), access_policy TEXT NOT NULL CHECK(access_policy IN ('private','shared')), approval_required INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, created_by TEXT NOT NULL, deleted_at INTEGER);
CREATE TABLE IF NOT EXISTS agent_acl(agent_id TEXT NOT NULL REFERENCES agents(id), principal_type TEXT NOT NULL CHECK(principal_type IN ('user','group')), principal_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner','editor','user','auditor')), created_at INTEGER NOT NULL, created_by TEXT NOT NULL, revoked_at INTEGER, PRIMARY KEY(agent_id,principal_type,principal_id));
CREATE TABLE IF NOT EXISTS agent_capabilities(agent_id TEXT NOT NULL REFERENCES agents(id), capability TEXT NOT NULL, configuration TEXT NOT NULL DEFAULT '{}', approval_status TEXT NOT NULL CHECK(approval_status IN ('approved','pending','denied')), created_at INTEGER NOT NULL, created_by TEXT NOT NULL, PRIMARY KEY(agent_id,capability));
CREATE TABLE IF NOT EXISTS agent_integrations(agent_id TEXT NOT NULL REFERENCES agents(id), integration_id TEXT NOT NULL, credential_reference TEXT, resource_scope TEXT, configuration TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(agent_id,integration_id));
CREATE TABLE IF NOT EXISTS agent_skills(agent_id TEXT NOT NULL REFERENCES agents(id), skill_id TEXT NOT NULL, PRIMARY KEY(agent_id,skill_id));
CREATE TABLE IF NOT EXISTS approval_requests(id TEXT PRIMARY KEY, agent_id TEXT REFERENCES agents(id), request_type TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','expired')), requested_by TEXT NOT NULL, requested_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, decided_by TEXT, decided_at INTEGER);
CREATE TABLE IF NOT EXISTS conversation_bindings(platform TEXT NOT NULL, scope_id TEXT NOT NULL, conversation_id TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '', principal_id TEXT NOT NULL, agent_id TEXT NOT NULL REFERENCES agents(id), updated_at INTEGER NOT NULL, PRIMARY KEY(platform,scope_id,conversation_id,thread_id,principal_id));
CREATE TABLE IF NOT EXISTS audit_events(id TEXT PRIMARY KEY, timestamp INTEGER NOT NULL, request_id TEXT, actor_id TEXT, actor_platform TEXT, agent_id TEXT, action TEXT NOT NULL, resource TEXT, decision TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}');
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_profile_live ON agents(profile_name) WHERE status!='deleted';
CREATE INDEX IF NOT EXISTS idx_acl_principal ON agent_acl(principal_id,revoked_at);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_events(agent_id,timestamp);
"""

class Registry:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self._lock=threading.RLock(); self.initialize()
    def connect(self):
        c=sqlite3.connect(self.path,timeout=10,isolation_level=None,check_same_thread=False)
        c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA busy_timeout=10000')
        try:c.execute('PRAGMA journal_mode=WAL')
        except sqlite3.DatabaseError: pass
        c.execute('PRAGMA synchronous=FULL'); return c
    def initialize(self):
        with self.connect() as c:c.executescript(SCHEMA)
        self._migrate_agents_table()
        try:self.path.chmod(0o600)
        except OSError:pass
    def _migrate_agents_table(self):
        with self.connect() as c:
            row=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='agents'").fetchone()
            sql=row[0] if row else ''
            needs_rebuild=('human-approval' not in sql) or ('profile_name TEXT UNIQUE' in sql)
            if needs_rebuild:
                c.execute('ALTER TABLE agents RENAME TO agents_old')
                c.executescript(SCHEMA)
                c.execute("""INSERT INTO agents(id,profile_name,display_name,description,purpose,owner_id,owner_platform,model,status,risk_level,access_policy,approval_required,created_at,updated_at,created_by,deleted_at)
                          SELECT id,profile_name,display_name,description,purpose,owner_id,owner_platform,model,status,
                          CASE risk_level WHEN 'read-only' THEN 'human-approval' WHEN 'low' THEN 'human-approval' WHEN 'medium' THEN 'human-approval' WHEN 'high' THEN 'autonomous' ELSE risk_level END,
                          access_policy,approval_required,created_at,updated_at,created_by,deleted_at FROM agents_old""")
                c.execute('DROP TABLE agents_old')
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_profile_live ON agents(profile_name) WHERE status!='deleted'")
    @contextmanager
    def tx(self):
        with self._lock:
            c=self.connect(); c.execute('BEGIN IMMEDIATE')
            try: yield c; c.commit()
            except BaseException: c.rollback(); raise
            finally:c.close()
    def upsert_principal(self,p):
        now=int(time.time())
        with self.tx() as c:
            c.execute('INSERT INTO principals(id,platform,user_id,scope,display_name,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name',(p.id,p.platform,p.user_id,p.scope,p.display_name,now))
        return p.id
    def grant_global(self,principal_id,role,actor):
        with self.tx() as c:c.execute('INSERT OR IGNORE INTO global_roles VALUES(?,?,?,?)',(principal_id,role,int(time.time()),actor))
    def has_global(self,pid,role):
        with self.connect() as c:return c.execute('SELECT 1 FROM global_roles WHERE principal_id=? AND role=?',(pid,role)).fetchone() is not None
    def is_admin(self,pid): return self.has_global(pid,'hermes-admin') or self.has_global(pid,'agent-builder-owner')
    def is_builder_user(self,pid): return self.is_admin(pid) or self.has_global(pid,'agent-builder-user')
    def insert_agent(self,row,skills=(),integrations=(),capabilities=()):
        now=int(time.time())
        with self.tx() as c:
            try:
                c.execute('''INSERT INTO agents(id,profile_name,display_name,description,purpose,owner_id,owner_platform,model,status,risk_level,access_policy,approval_required,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(row['id'],row['profile_name'],row['display_name'],row.get('description',''),row.get('purpose',''),row['owner_id'],row['owner_platform'],row['model'],row['status'],row['risk_level'],row['access_policy'],int(row.get('approval_required',False)),now,now,row['created_by']))
                c.execute('INSERT INTO agent_acl VALUES(?,?,?,?,?,?,NULL)',(row['id'],'user',row['owner_id'],'owner',now,row['created_by']))
                c.executemany('INSERT INTO agent_skills VALUES(?,?)',[(row['id'],x) for x in skills])
                c.executemany('INSERT INTO agent_integrations VALUES(?,?,NULL,NULL,?)',[(row['id'],x,'{}') for x in integrations])
                c.executemany('INSERT INTO agent_capabilities VALUES(?,?,?,?,?,?)',[(row['id'],x,'{}','approved',now,row['created_by']) for x in capabilities])
            except sqlite3.IntegrityError as e: raise ConflictError(str(e)) from e
    def update_status(self,aid,status):
        with self.tx() as c:
            c.execute('UPDATE agents SET status=?,updated_at=?,deleted_at=CASE WHEN ?="deleted" THEN ? ELSE deleted_at END WHERE id=?',(status,int(time.time()),status,int(time.time()),aid))
            if c.total_changes!=1: raise ConflictError('agent state changed')
    def update_agent(self,aid,fields=None,skills=None,integrations=None,capabilities=None):
        fields=fields or {}; now=int(time.time())
        allowed={'description','purpose','model','risk_level','access_policy','approval_required'}
        unknown=set(fields)-allowed
        if unknown: raise ConflictError('unsupported agent fields: '+', '.join(sorted(unknown)))
        with self.tx() as c:
            if fields:
                sets=', '.join(f'{k}=?' for k in fields)+', updated_at=?'
                vals=[int(v) if k=='approval_required' else v for k,v in fields.items()]+[now,aid]
                c.execute(f'UPDATE agents SET {sets} WHERE id=?',vals)
            else:
                c.execute('UPDATE agents SET updated_at=? WHERE id=?',(now,aid))
            if c.total_changes<1: raise ConflictError('agent not found')
            if skills is not None:
                c.execute('DELETE FROM agent_skills WHERE agent_id=?',(aid,))
                c.executemany('INSERT INTO agent_skills VALUES(?,?)',[(aid,x) for x in skills])
            if integrations is not None:
                c.execute('DELETE FROM agent_integrations WHERE agent_id=?',(aid,))
                c.executemany('INSERT INTO agent_integrations VALUES(?,?,NULL,NULL,?)',[(aid,x,'{}') for x in integrations])
            if capabilities is not None:
                c.execute('DELETE FROM agent_capabilities WHERE agent_id=?',(aid,))
                c.executemany('INSERT INTO agent_capabilities VALUES(?,?,?,?,?,?)',[(aid,x,'{}','approved',now,fields.get('created_by','system')) for x in capabilities])
    def get_agent(self,key,include_deleted=False):
        q='SELECT * FROM agents WHERE (id=? OR profile_name=?)'+('' if include_deleted else " AND status!='deleted'")
        with self.connect() as c:
            r=c.execute(q,(key,key)).fetchone(); return dict(r) if r else None
    def get_role(self,aid,pid):
        with self.connect() as c:
            r=c.execute('SELECT role FROM agent_acl WHERE agent_id=? AND principal_id=? AND revoked_at IS NULL',(aid,pid)).fetchone(); return r[0] if r else None
    def list_agents(self,pid,all_agents=False):
        with self.connect() as c:
            if all_agents: rows=c.execute("SELECT * FROM agents WHERE status!='deleted' ORDER BY created_at").fetchall()
            else: rows=c.execute("SELECT DISTINCT a.* FROM agents a JOIN agent_acl x ON x.agent_id=a.id WHERE x.principal_id=? AND x.revoked_at IS NULL AND a.status!='deleted' ORDER BY a.created_at",(pid,)).fetchall()
            return [dict(x) for x in rows]
    def list_accessible_agents(self,pid):
        with self.connect() as c:
            rows=c.execute("SELECT DISTINCT a.* FROM agents a JOIN agent_acl x ON x.agent_id=a.id WHERE x.principal_id=? AND x.revoked_at IS NULL AND a.status!='deleted' ORDER BY a.created_at",(pid,)).fetchall()
            return [dict(x) for x in rows]
    def acl(self,aid):
        with self.connect() as c:return [dict(x) for x in c.execute('SELECT principal_type,principal_id,role,created_at,created_by FROM agent_acl WHERE agent_id=? AND revoked_at IS NULL',(aid,))]
    def share(self,aid,pid,role,actor):
        with self.tx() as c:c.execute('INSERT INTO agent_acl VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(agent_id,principal_type,principal_id) DO UPDATE SET role=excluded.role,revoked_at=NULL,created_by=excluded.created_by',(aid,'user',pid,role,int(time.time()),actor))
    def replace_user_shares(self,aid,pids,role,actor):
        pids=list(dict.fromkeys(pids or [])); now=int(time.time())
        with self.tx() as c:
            c.execute('UPDATE agent_acl SET revoked_at=? WHERE agent_id=? AND principal_type="user" AND role!="owner" AND revoked_at IS NULL',(now,aid))
            c.executemany('INSERT INTO agent_acl VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(agent_id,principal_type,principal_id) DO UPDATE SET role=excluded.role,revoked_at=NULL,created_by=excluded.created_by',[(aid,'user',pid,role,now,actor) for pid in pids])
    def unshare(self,aid,pid):
        with self.tx() as c:c.execute('UPDATE agent_acl SET revoked_at=? WHERE agent_id=? AND principal_id=? AND role!="owner"',(int(time.time()),aid,pid))
    def bind(self,p,conversation,agent_id,thread=''):
        with self.tx() as c:c.execute('INSERT INTO conversation_bindings VALUES(?,?,?,?,?,?,?) ON CONFLICT(platform,scope_id,conversation_id,thread_id,principal_id) DO UPDATE SET agent_id=excluded.agent_id,updated_at=excluded.updated_at',(p.platform,p.scope,conversation,thread,p.id,agent_id,int(time.time())))
    def binding(self,p,conversation,thread=''):
        with self.connect() as c:
            r=c.execute('SELECT agent_id FROM conversation_bindings WHERE platform=? AND scope_id=? AND conversation_id=? AND thread_id=? AND principal_id=?',(p.platform,p.scope,conversation,thread,p.id)).fetchone(); return r[0] if r else None
    def unbind(self,p,conversation,thread=''):
        with self.tx() as c:
            c.execute('DELETE FROM conversation_bindings WHERE platform=? AND scope_id=? AND conversation_id=? AND thread_id=? AND principal_id=?',(p.platform,p.scope,conversation,thread,p.id))
    def request_approval(self,aid,kind,payload,actor,ttl=86400):
        rid='apr_'+uuid.uuid4().hex[:16]; now=int(time.time())
        with self.tx() as c:c.execute('INSERT INTO approval_requests VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',(rid,aid,kind,json.dumps(payload,separators=(',',':')),'pending',actor,now,now+ttl))
        return rid
    def decide(self,rid,status,actor):
        if status not in {'approved','denied'}: raise ValueError(status)
        with self.tx() as c:
            c.execute("UPDATE approval_requests SET status=?,decided_by=?,decided_at=? WHERE id=? AND status='pending' AND expires_at>?",(status,actor,int(time.time()),rid,int(time.time())))
            if c.total_changes!=1: raise ConflictError('approval is unavailable')
            return dict(c.execute('SELECT * FROM approval_requests WHERE id=?',(rid,)).fetchone())
    def pending(self):
        with self.connect() as c:return [dict(x) for x in c.execute("SELECT * FROM approval_requests WHERE status='pending' AND expires_at>? ORDER BY requested_at",(int(time.time()),))]
    def get_request(self,rid):
        with self.connect() as c:
            r=c.execute('SELECT * FROM approval_requests WHERE id=?',(rid,)).fetchone(); return dict(r) if r else None
    def update_request_payload(self,rid,payload,actor):
        with self.tx() as c:
            c.execute("UPDATE approval_requests SET payload=? WHERE id=? AND status='pending' AND expires_at>?",(json.dumps(payload,separators=(',',':')),rid,int(time.time())))
            if c.total_changes!=1: raise ConflictError('approval is unavailable')
            return dict(c.execute('SELECT * FROM approval_requests WHERE id=?',(rid,)).fetchone())
    def audit(self,actor,action,decision,agent_id=None,resource=None,metadata=None,request_id=None):
        with self.tx() as c:c.execute('INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?,?)',('evt_'+uuid.uuid4().hex,int(time.time()),request_id,actor.id if actor else None,actor.platform if actor else None,agent_id,action,resource,decision,json.dumps(metadata or {},separators=(',',':'))))
    def audit_list(self,limit=200):
        with self.connect() as c:return [dict(x) for x in c.execute('SELECT * FROM audit_events ORDER BY timestamp DESC LIMIT ?',(min(limit,1000),))]
