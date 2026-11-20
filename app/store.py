import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("CREATE TABLE IF NOT EXISTS lineage (run_id TEXT PRIMARY KEY, root TEXT NOT NULL, current INTEGER NOT NULL DEFAULT 0)")
    return con

def has(path, run_id):
    with connect(path) as con: return con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone() is not None

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def save_successor(path, result, root, released):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        applied=con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False))).rowcount==1
        if applied:
            con.execute('INSERT OR IGNORE INTO lineage(run_id,root,current) VALUES(?,?,0)', (run_id,root))
            if released:
                con.execute('UPDATE lineage SET current=0 WHERE root=?',(root,))
                con.execute('UPDATE lineage SET current=1 WHERE run_id=?',(run_id,))
    return get(path,run_id),applied

def lineage_of(path, run_id):
    with connect(path) as con:
        row=con.execute('SELECT root FROM lineage WHERE run_id=?',(run_id,)).fetchone()
        root=row['root'] if row else run_id
        cur=con.execute('SELECT run_id FROM lineage WHERE root=? AND current=1',(root,)).fetchone()
        current=cur['run_id'] if cur else root
    return {'root':root,'current':current,'is_current':current==run_id}

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)
