"""Offline pre-update rollback bundle: program, config, secrets, data and logs.

Requires the real caller to hold the exclusive service guard and verify SCM
STOPPED. This module never starts/stops a service or replaces live paths.
No live SQLite copying: nonempty WAL/journals or pending spool block preparation.
A verified recovery copy can be extracted to a NEW protected directory.
"""
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
import hashlib,json,os,re,shutil,sqlite3,stat,time
from .package_stage import stage,digest,RESERVED,validated_program_version as _validate_program
from .maintenance import write_json

STATE_DIRS=("config","secrets","data","logs")
MAX_FILES=20000
MAX_BYTES=500*1024**3
HASH=re.compile(r"^[0-9a-f]{64}$")
FORMAT="awqm-offline-update-v1"

def _safe_part(name):
    return (isinstance(name,str) and bool(name) and name not in (".","..")
        and not name.endswith((" ",".")) and not RESERVED.fullmatch(name.split(".",1)[0].rstrip(" "))
        and not any(ord(c)<32 or c in '\\/:<>"|?*' for c in name))
def _path(path):
    path=Path(path).absolute()
    for p in (path,*path.parents):
        if p.is_symlink() or getattr(p,"is_junction",lambda:False)():
            raise ValueError("reparse point prohibited")
        if p.exists() and getattr(p.stat(),"st_file_attributes",0)&0x400:
            raise ValueError("reparse point prohibited")
    return path.resolve()
def _relative(root,path):
    rel=path.relative_to(root).as_posix()
    if not all(_safe_part(p) for p in rel.split("/")):raise ValueError("unsafe file name")
    return rel
def _check_entry(path):
    st=path.lstat()
    if stat.S_ISLNK(st.st_mode) or getattr(st,"st_file_attributes",0)&0x400 or getattr(path,"is_junction",lambda:False)():
        raise ValueError("reparse point prohibited")
    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        raise ValueError("unsupported file type")
    return st

def _walk(root,check=None):
    root=_path(root)
    if not root.is_dir():raise ValueError("directory required")
    if check:check(root)
    files={};dirs=[];seen=set();total=0;stack=[root]
    while stack:
        parent=stack.pop()
        for p in sorted(parent.iterdir()):
            st=_check_entry(p);name=_relative(root,p);key=name.casefold()
            if key in seen:raise ValueError("case-colliding file name")
            seen.add(key)
            if len(seen)>MAX_FILES:raise ValueError("snapshot entry limit exceeded")
            if check:check(p)
            if stat.S_ISDIR(st.st_mode):dirs.append(name);stack.append(p)
            else:
                total+=st.st_size
                if total>MAX_BYTES:raise ValueError("snapshot size limit exceeded")
                files[name]=(st.st_size,st.st_mtime_ns)
    return root,files,sorted(dirs)

def _state_inventory(root,include_association,check):
    all_files={};all_dirs=[]
    for name in STATE_DIRS:
        p=root/name
        base,files,dirs=_walk(p,check)
        all_dirs.append(name)
        for rel in dirs:all_dirs.append(name+"/"+rel)
        for rel,value in files.items():
            full=name+"/"+rel
            # Only cleanly stopped DB files may be copied. A nonempty
            # WAL/rollback journal requires recovery with the original version.
            if rel.casefold().endswith(("-wal","-journal")):
                suffix="-wal" if rel.casefold().endswith("-wal") else "-journal"
                if value[0] or rel[:-len(suffix)] not in files:
                    raise RuntimeError("unclean or orphan SQLite sidecar")
                continue
            if rel.casefold().endswith("-shm"):
                if rel[:-4] not in files:raise RuntimeError("orphan SQLite sidecar")
                continue
            if rel.casefold().endswith("association.db") and not include_association:
                raise ValueError("explicit association-history backup authorization required")
            all_files[full]=value
    if len(all_files)+len(all_dirs)>MAX_FILES or sum(v[0] for v in all_files.values())>MAX_BYTES:
        raise ValueError("state snapshot limit exceeded")
    return all_files,sorted(all_dirs)

def _quick_check(path):
    c=sqlite3.connect(path.resolve().as_uri()+"?mode=ro&immutable=1",uri=True,timeout=2)
    try:
        if c.execute("PRAGMA quick_check").fetchall()!=[("ok",)]:raise ValueError("SQLite integrity check failed")
        if path.name.casefold()=="core.db":
            if c.execute("SELECT value FROM metadata WHERE key='mode'").fetchone()!=("diagnostic",):
                raise ValueError("diagnostic database required")
            if c.execute("SELECT COUNT(*) FROM pending").fetchone()[0]:
                raise RuntimeError("pending writes must be recovered before update")
        return int(c.execute("PRAGMA user_version").fetchone()[0])
    finally:c.close()

def _copy(source,destination,files,dirs,protect=None,check_db=False):
    destination.mkdir()
    if protect:protect(destination)
    for rel in sorted(dirs,key=lambda x:(x.count("/"),x)):
        p=destination/rel;p.mkdir(exist_ok=False)
        if protect:protect(p)
    records={}
    for rel,(size,mtime) in sorted(files.items()):
        a=source/rel;b=destination/rel
        st=_check_entry(a)
        if (st.st_size,st.st_mtime_ns)!=(size,mtime):raise RuntimeError("source changed during backup")
        with a.open("rb") as inp,b.open("xb") as out:
            shutil.copyfileobj(inp,out,1024*1024)
            out.flush();os.fsync(out.fileno())
        if protect:protect(b)
        value=digest(b)
        st=_check_entry(a)
        if b.stat().st_size!=size or (st.st_size,st.st_mtime_ns)!=(size,mtime) or digest(a)!=value:
            raise RuntimeError("source changed during backup")
        record={"size":size,"sha256":value}
        if check_db and rel.lower().endswith(".db"):
            record["sqlite_user_version"]=_quick_check(b)
        records[rel]=record
    return records

def _inventory_matches(root,files,dirs,state=False,include_association=False,check=None):
    if state:current,current_dirs=_state_inventory(root,include_association,check)
    else:_,current,current_dirs=_walk(root,check)
    if current!=files or current_dirs!=dirs:raise RuntimeError("source inventory changed during preparation")

def _file_manifest(root,protect=None):
    _,files,dirs=_walk(root,protect)
    return {"directories":dirs,"files":{rel:{"size":v[0],"sha256":digest(root/rel)} for rel,v in sorted(files.items())}}

def _check_manifest_records(root,manifest,protect=None,check_db=False):
    if not isinstance(manifest,dict) or set(manifest)!={"directories","files"}:
        raise ValueError("invalid file manifest")
    _,files,dirs=_walk(root,protect)
    wanted=manifest["files"]
    if not isinstance(wanted,dict) or set(wanted)!=set(files) or dirs!=manifest["directories"]:
        raise ValueError("snapshot file set mismatch")
    for rel,record in wanted.items():
        if not isinstance(record,dict) or set(record)-{"size","sha256","sqlite_user_version"} or "size" not in record or "sha256" not in record:
            raise ValueError("invalid manifest record")
        if type(record["size"]) is not int or record["size"]!=files[rel][0] or not isinstance(record["sha256"],str) or not HASH.fullmatch(record["sha256"]) or digest(root/rel)!=record["sha256"]:
            raise ValueError("snapshot checksum mismatch")
        if check_db and rel.lower().endswith(".db"):
            if _quick_check(root/rel)!=record.get("sqlite_user_version"):raise ValueError("snapshot schema mismatch")
    return True

def verify(bundle,protect=None):
    root=_path(bundle)
    if protect:protect(root)
    mf=root/"update-manifest.json"
    _check_entry(mf)
    if protect:protect(mf)
    if mf.stat().st_size>16*1024*1024:raise ValueError("manifest too large")
    m=json.loads(mf.read_text(encoding="utf-8"))
    keys={"format","created_at","old_version","new_version","archive_sha256","include_association_history",
          "candidate","old_program","old_state","status"}
    if (not isinstance(m,dict) or set(m)!=keys or m["format"]!=FORMAT or m["status"]!="prepared_not_installed"
        or not isinstance(m["archive_sha256"],str) or not HASH.fullmatch(m["archive_sha256"])
        or type(m["include_association_history"]) is not bool):
        raise ValueError("invalid update manifest")
    if {x.name for x in root.iterdir()}!={"candidate","old-program","old-state","update-manifest.json"}:
        raise ValueError("unexpected bundle member")
    _check_manifest_records(root/"candidate",m["candidate"],protect)
    _check_manifest_records(root/"old-program",m["old_program"],protect)
    _check_manifest_records(root/"old-state",m["old_state"],protect,check_db=True)
    if _validate_program(root/"old-program")!=m["old_version"] or _validate_program(root/"candidate")!=m["new_version"]:
        raise ValueError("version mismatch")
    if set(p.name for p in (root/"old-state").iterdir())!=set(STATE_DIRS):raise ValueError("rollback state incomplete")
    if not (root/"old-state/config/service.json").is_file() or not (root/"old-state/data/core.db").is_file():raise ValueError("rollback core configuration missing")
    receipt=json.loads((root/"candidate"/"STAGE-RECEIPT.json").read_text(encoding="utf-8"))
    if receipt.get("sha256")!=m["archive_sha256"] or receipt.get("version")!=m["new_version"]:
        raise ValueError("candidate provenance mismatch")
    return m

def prepare(installed,state_root,archive,expected_sha256,destination,guard,protect=None,include_association=False):
    installed=_path(installed);state_root=_path(state_root);dest=_path(destination)
    if dest.exists() or not dest.parent.is_dir():raise ValueError("new destination required")
    if installed==state_root or installed in state_root.parents or state_root in installed.parents:
        raise ValueError("program and operational root must be separate")
    if dest==installed or installed in dest.parents or dest in installed.parents or dest==state_root or dest in state_root.parents:
        raise ValueError("snapshot destination overlaps source")
    if any(state_root/k==dest or state_root/k in dest.parents for k in STATE_DIRS):raise ValueError("snapshot destination overlaps operational state")
    if protect:protect(dest.parent)
    guard()
    old_version=_validate_program(installed)
    _,program_files,program_dirs=_walk(installed)
    state_files,state_dirs=_state_inventory(state_root,include_association,protect)
    if "config/service.json" not in state_files or "data/core.db" not in state_files:
        raise ValueError("configured service and core database required")
    needed=sum(x[0] for x in program_files.values())+sum(x[0] for x in state_files.values())
    # Include archive and unpacked candidate headroom; stage has its own
    # independent upper limit. No existing backup deletion is attempted.
    if shutil.disk_usage(dest.parent).free<needed+5*1024**3:
        raise RuntimeError("insufficient snapshot disk space")
    temp=dest.parent/(".awqm-update-"+uuid4().hex)
    temp.mkdir()
    try:
        if protect:protect(temp)
        result=stage(archive,expected_sha256,temp/"candidate")
        candidate=_file_manifest(temp/"candidate",protect)
        if result["version"]==old_version:raise ValueError("candidate version equals installed version")
        guard()
        old_program=_copy(installed,temp/"old-program",program_files,program_dirs,protect)
        old_state=_copy(state_root,temp/"old-state",state_files,state_dirs,protect,check_db=True)
        guard()
        _inventory_matches(installed,program_files,program_dirs)
        _inventory_matches(state_root,state_files,state_dirs,True,include_association,protect)
        # All source bytes are checked again, not just mtime. This is a
        # stopped-service snapshot, not atomic cross-filesystem live backup.
        for rel,r in old_program.items():
            if digest(installed/rel)!=r["sha256"]:raise RuntimeError("installed program changed")
        for rel,r in old_state.items():
            if digest(state_root/rel)!=r["sha256"]:raise RuntimeError("operational state changed")
        manifest={"format":FORMAT,"created_at":int(time.time()),"old_version":old_version,"new_version":result["version"],
            "archive_sha256":result["sha256"],"include_association_history":bool(include_association),
            "candidate":candidate,"old_program":{"directories":program_dirs,"files":old_program},
            "old_state":{"directories":state_dirs,"files":old_state},"status":"prepared_not_installed"}
        write_json(temp/"update-manifest.json",manifest)
        verify(temp,protect)
        guard()
        _inventory_matches(installed,program_files,program_dirs)
        _inventory_matches(state_root,state_files,state_dirs,True,include_association,protect)
        if dest.exists():raise FileExistsError(dest)
        temp.rename(dest)
    finally:
        if temp.exists():shutil.rmtree(temp,ignore_errors=True)
    return {"status":"prepared_not_installed","old_version":old_version,"new_version":result["version"],
            "archive_sha256":result["sha256"],"rollback_files":len(old_program)+len(old_state),
            "includes_association_history":bool(include_association)}

def restore_copy(bundle,destination,protect=None):
    """A separate recovery copy only. Never replace or activate a live path."""
    root=_path(bundle);dest=_path(destination)
    if dest.exists() or not dest.parent.is_dir() or root==dest or root in dest.parents or dest in root.parents:
        raise ValueError("new separate recovery destination required")
    if protect:protect(dest.parent)
    manifest=verify(root,protect)
    temp=dest.parent/(".awqm-recovery-"+uuid4().hex)
    temp.mkdir()
    try:
        if protect:protect(temp)
        for name,field in (("old-program","old_program"),("old-state","old_state")):
            src=root/name
            _,files,dirs=_walk(src,protect)
            _copy(src,temp/name,files,dirs,protect,check_db=(name=="old-state"))
            _check_manifest_records(temp/name,manifest[field],protect,check_db=(name=="old-state"))
        write_json(temp/"RECOVERY-RECEIPT.json",{"status":"verified_recovery_copy_not_applied","version":manifest["old_version"]})
        if protect:protect(temp/"RECOVERY-RECEIPT.json")
        if dest.exists():raise FileExistsError(dest)
        temp.rename(dest)
    finally:
        if temp.exists():shutil.rmtree(temp,ignore_errors=True)
    return {"status":"verified_recovery_copy_not_applied","version":manifest["old_version"]}
