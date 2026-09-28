"""SQLite metadata, append-only events and immutable note revisions."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

TERMINAL = {'succeeded','partial','failed','cancelled','interrupted'}

def encode(value):
    return json.dumps(value, ensure_ascii=False)

class Conflict(Exception):
    pass

class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ('uploads','cache','artifacts'):
            (self.root/name).mkdir(exist_ok=True)
        self.db = self.root/'nota.sqlite3'
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,name TEXT,path TEXT,sha TEXT,size INTEGER);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,payload TEXT,result TEXT,error TEXT,created REAL,updated REAL,cancel INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,body TEXT);
            CREATE INDEX IF NOT EXISTS events_job ON events(job_id,seq);
            CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,job_id TEXT,version INTEGER);
            CREATE TABLE IF NOT EXISTS revisions(note_id TEXT,version INTEGER,data TEXT,reason TEXT,created REAL,PRIMARY KEY(note_id,version));
            CREATE TABLE IF NOT EXISTS preferences(id TEXT PRIMARY KEY,data TEXT);
            ''')

    @contextmanager
    def runtime_lock(self):
        """Prevent a second backend from marking a live backend's jobs interrupted."""
        file=(self.root/'server.lock').open('a+b')
        try:
            if file.tell()==0:file.write(b'0');file.flush()
            file.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('已有 Nota 后端正在使用此数据目录，请打开现有页面') from None
            yield
        finally:
            file.close()

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def register_file(self, file_id, name, path, sha, size):
        with self.connect() as c:
            c.execute('INSERT INTO files VALUES(?,?,?,?,?)',(file_id,name,str(path),sha,size))

    def file(self, file_id):
        with self.connect() as c:
            row = c.execute('SELECT * FROM files WHERE id=?',(file_id,)).fetchone()
        if not row:
            raise KeyError('文件不存在')
        return dict(row)

    def new_job(self, kind, payload):
        jid = uuid.uuid4().hex
        now = time.time()
        with self.connect() as c:
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,0)',(jid,kind,'queued',encode(payload),None,None,now,now))
        self.emit(jid,'status',status='queued',message='排队中')
        return jid

    def job(self, jid):
        with self.connect() as c:
            row = c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if not row:
            raise KeyError('任务不存在')
        d = dict(row)
        for key in ('payload','result'):
            d[key] = json.loads(d[key]) if d[key] else None
        return d

    def active_count(self):
        with self.connect() as c:
            return c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]

    def jobs(self):
        with self.connect() as c:
            ids = [r[0] for r in c.execute('SELECT id FROM jobs ORDER BY created DESC LIMIT 100')]
        return [self.job(i) for i in ids]

    def set_job(self, jid, status, result=None, error=None):
        with self.connect() as c:
            c.execute('UPDATE jobs SET status=?,result=?,error=?,updated=? WHERE id=?',
                      (status,encode(result) if result is not None else None,error,time.time(),jid))
        self.emit(jid,'status',status=status,message=error or status,result=result)

    def emit(self, jid, kind, **data):
        with self.connect() as c:
            c.execute('INSERT INTO events(job_id,body) VALUES(?,?)',(jid,encode({'kind':kind,**data})))

    def events(self, jid, after=0):
        with self.connect() as c:
            return [{'seq':r['seq'],**json.loads(r['body'])} for r in c.execute(
                'SELECT seq,body FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT 200',(jid,after))]

    def cancel(self, jid):
        with self.connect() as c:
            c.execute("UPDATE jobs SET cancel=1 WHERE id=? AND status IN ('queued','running')",(jid,))

    def recover(self):
        with self.connect() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM jobs WHERE status IN ('queued','running')")]
        for jid in ids:
            self.set_job(jid,'interrupted',error='服务重启，任务已中断；可从已完成步骤重试')

    def create_note(self, jid, data):
        nid = uuid.uuid4().hex
        with self.connect() as c:
            c.execute('INSERT INTO notes VALUES(?,?,1)',(nid,jid))
            c.execute('INSERT INTO revisions VALUES(?,?,?,?,?)',(nid,1,encode(data),'生成',time.time()))
        return nid

    def note(self, nid, version=None):
        with self.connect() as c:
            n = c.execute('SELECT * FROM notes WHERE id=?',(nid,)).fetchone()
            if not n:
                raise KeyError('笔记不存在')
            v = version or n['version']
            r = c.execute('SELECT * FROM revisions WHERE note_id=? AND version=?',(nid,v)).fetchone()
        if not r:
            raise KeyError('版本不存在')
        return {'id':nid,'job_id':n['job_id'],'version':v,'latest_version':n['version'],
                'data':json.loads(r['data']),'reason':r['reason']}

    def save_revision(self, nid, expected, data, reason):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            cur = c.execute('UPDATE notes SET version=version+1 WHERE id=? AND version=?',(nid,expected))
            if cur.rowcount != 1:
                raise Conflict('笔记已被修改，请刷新后再试；本次内容未覆盖新版本')
            c.execute('INSERT INTO revisions VALUES(?,?,?,?,?)',(nid,expected+1,encode(data),reason,time.time()))
        return expected+1

    def history(self,nid):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT version,reason,created FROM revisions WHERE note_id=? ORDER BY version DESC',(nid,))]

    def preferences(self, data=None):
        with self.connect() as c:
            if data is not None:
                c.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',('default',encode(data)))
            r = c.execute('SELECT data FROM preferences WHERE id=?',('default',)).fetchone()
        return json.loads(r[0]) if r else {}
