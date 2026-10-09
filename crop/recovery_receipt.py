"""Private local recovery evidence, not authentication or a secret signature.
Only the trusted local operator publishes receipts. Never accept receipt data via HTTP.
"""
from __future__ import annotations
import hashlib,json,os,re,uuid
from pathlib import Path
PURPOSE='existing_failed_worker_cleanup'
class RecoveryConflict(ValueError):
    pass
def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
def canonical(value) -> bytes:
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
def guard(path: Path, boundary: Path) -> Path:
    path=Path(path).absolute();boundary=Path(boundary).absolute()
    if not path.is_relative_to(boundary):raise RecoveryConflict('unsafe recovery boundary')
    for p in (path,*path.parents):
        if p.is_symlink() or p.is_junction():raise RecoveryConflict('linked recovery path')
        if p==boundary:break
    resolved=path.resolve()
    if not resolved.is_relative_to(boundary.resolve()):raise RecoveryConflict('unsafe recovery path')
    return resolved
def read_bytes(path: Path,boundary: Path,limit=2*1024*1024) -> bytes:
    p=guard(path,boundary)
    if not p.is_file() or p.stat().st_size>limit or p.stat().st_nlink!=1:raise RecoveryConflict('invalid recovery evidence file')
    return p.read_bytes()
def publish_once(path: Path,data: bytes,boundary: Path) -> None:
    p=guard(path,boundary);p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists():
        if read_bytes(p,boundary)==data:return
        raise RecoveryConflict('recovery evidence already exists')
    tmp=guard(p.parent/('.receipt-'+uuid.uuid4().hex[:12]+'.tmp'),boundary)
    try:
        with tmp.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        # Atomic no-overwrite publication, unlike replace. Same-volume local files only.
        os.link(tmp,p)
    finally:tmp.unlink(missing_ok=True)
def identity(value):
    if not isinstance(value,dict) or set(value)!={'pid','parent_pid','creation_filetime','image'}:raise RecoveryConflict('invalid process identity')
    if any(type(value.get(k)) is not int or value[k]<=0 for k in ('pid','parent_pid','creation_filetime')):raise RecoveryConflict('invalid process identity')
    if not isinstance(value['image'],str) or not value['image'] or len(value['image'])>512:raise RecoveryConflict('invalid process image')
    return canonical(value)
def load_bundle(job_root: Path,job_id: str,inside):
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',job_id):raise RecoveryConflict('invalid recovery job')
        boundary=inside(job_root,'recovery_root');owner=guard(boundary/job_id,boundary);private=guard(owner/'.recovery',boundary)
        before_raw=read_bytes(private/'before.json',boundary);intent_raw=read_bytes(private/'intent.json',boundary);exit_raw=read_bytes(private/'exit.json',boundary)
        before=json.loads(before_raw);intent=json.loads(intent_raw);exit=json.loads(exit_raw)
        if intent.get('version')!=1 or exit.get('version')!=1 or intent.get('purpose')!=PURPOSE:raise RecoveryConflict('invalid recovery purpose')
        if any(x.get('job_id')!=job_id for x in (intent,exit)) or before.get('id')!=job_id:raise RecoveryConflict('recovery job mismatch')
        if intent.get('before_sha256')!=digest(before_raw) or exit.get('before_sha256')!=digest(before_raw) or exit.get('intent_sha256')!=digest(intent_raw):raise RecoveryConflict('recovery binding mismatch')
        if before.get('state') not in {'failed','interrupted'} or intent.get('prior_state')!=before.get('state') or intent.get('prior_error_code')!=before.get('error_code'):raise RecoveryConflict('not a prior failed worker cleanup')
        if before.get('cancel_requested'):raise RecoveryConflict('prior user cancellation is not recoverable here')
        if exit.get('confirmed') is not True or exit.get('api_disk_equal') is not True:raise RecoveryConflict('exit not confirmed')
        server=identity(intent.get('server'))
        if identity(exit.get('server'))!=server or exit.get('listener_pid')!=intent['server']['pid']:raise RecoveryConflict('server identity mismatch')
        workers=intent.get('workers');scope=exit.get('cancel_scope');exited=exit.get('exited')
        if not isinstance(workers,list) or not workers or not isinstance(scope,list) or not isinstance(exited,list):raise RecoveryConflict('worker exit evidence missing')
        w={identity(x) for x in workers};c={identity(x) for x in scope};e={identity(x) for x in exited}
        if len(c)!=len(scope) or len(e)!=len(exited) or not w.issubset(e) or c!=e:raise RecoveryConflict('worker exit identities mismatch')
        if not re.fullmatch('[a-f0-9]{64}',str(exit.get('cancelled_sha256',''))):raise RecoveryConflict('invalid cancelled receipt')
        if not re.fullmatch('[a-f0-9]{64}',str(intent.get('source_sha256',''))):raise RecoveryConflict('invalid source receipt')
        if intent.get('source_path')!=before.get('source') or intent.get('output_root')!=before.get('output_root'):raise RecoveryConflict('source identity mismatch')
        if guard(Path(intent['output_root']),boundary)!=owner/'output':raise RecoveryConflict('output owner mismatch')
        # Walk every source ancestor inside the project boundary (not just resolve()).
        source=inside(Path(intent['source_path']),'recovery_source')
        for p in (Path(intent['source_path']),*Path(intent['source_path']).parents):
            if p.is_symlink() or p.is_junction():raise RecoveryConflict('linked source')
        with source.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=intent['source_sha256']:raise RecoveryConflict('source changed')
        return before,intent,exit,digest(exit_raw)
    except RecoveryConflict:raise
    except (OSError,ValueError,TypeError,KeyError,AttributeError) as exc:raise RecoveryConflict('recovery evidence unavailable or invalid') from exc

def validate_cancelled(job_root,job_id,record,before,intent,exit):
    current=read_bytes(Path(job_root)/job_id/'job.json',Path(job_root))
    if digest(current)!=exit['cancelled_sha256'] or json.loads(current)!=record:raise RecoveryConflict('stale cancelled snapshot')
    if record.get('state')!='cancelled' or record.get('cancel_requested') is not True or record.get('error_code')!='cancelled_by_user':raise RecoveryConflict('not a confirmed cleanup cancellation')
    # Only the original cancel API's terminal fields may differ.
    mutable={'state','stage','message','updated_at','cancel_requested','error_code','solution'}
    if {k:v for k,v in record.items() if k not in mutable}!={k:v for k,v in before.items() if k not in mutable}:raise RecoveryConflict('job changed beyond cancellation')
    old=before.get('solution') or {};new=record.get('solution') or {}
    if old.get('state') in {'queued','indexing','linking'}:
        if new!={**old,'state':'cancelled','message':'해설 연결이 취소되었습니다.'}:raise RecoveryConflict('solution cancellation mismatch')
    elif new!=old:raise RecoveryConflict('solution changed beyond cancellation')
