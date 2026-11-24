"""Offline package verification/staging. Never installs, executes or stops services."""
import argparse,hashlib,json,re,shutil,stat,tempfile,zipfile
from pathlib import Path
from .maintenance import write_json

REQUIRED={"awqm.exe","awqm_service.exe","awqm_manager.exe","version.txt","readme.md","build-dependencies.txt"}
RESERVED=re.compile(r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$",re.I)
MAX_FILES=12000
MAX_BYTES=2*1024**3

def digest(p):
    h=hashlib.sha256()
    with Path(p).open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()

def entries(z):
    seen={};spellings={};total=0;out=[]
    infos=z.infolist()
    if not 1<=len(infos)<=MAX_FILES:raise ValueError("invalid archive entry count")
    for i in infos:
        # ZipInfo.filename normalizes backslashes on Windows; validate the
        # original central-directory spelling before trusting normalized paths.
        raw=i.orig_filename
        if raw!=i.filename:raise ValueError("archive path was normalized")
        if "\\" in raw or ":" in raw or raw.startswith("/") or "\x00" in raw:raise ValueError("unsafe archive path")
        parts=raw.rstrip("/").split("/")
        if not parts or parts[0]!="AWQM" or any(not p or p in (".","..") or p.endswith((" ",".")) or RESERVED.fullmatch(p.split('.',1)[0].rstrip(' ')) or any(ord(c)<32 or c in '<>"|?*' for c in p) for p in parts):
            raise ValueError("unsafe Windows archive path")
        mode=(i.external_attr>>16)&0xffff
        if i.external_attr&0x400 or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR) or i.flag_bits&1:raise ValueError("unsupported archive member")
        if len(parts)==1 and not i.is_dir():raise ValueError("invalid archive root")
        rel="/".join(parts[1:]);key=rel.casefold()
        if key in seen or key=="stage-receipt.json" or key.startswith("stage-receipt.json/"):raise ValueError("duplicate or reserved archive member")
        for k in range(1,len(parts)):
            prefix="/".join(parts[1:k+1]);folded=prefix.casefold()
            if folded in spellings and spellings[folded]!=prefix:raise ValueError("case-colliding archive directory")
            spellings[folded]=prefix
        seen[key]=i.is_dir()
        total+=i.file_size
        if total>MAX_BYTES:raise ValueError("uncompressed archive too large")
        out.append((i,parts))
    for key in seen:
        for k in range(1,len(key.split("/"))):
            parent="/".join(key.split("/")[:k])
            if parent in seen and not seen[parent]:raise ValueError("file/directory collision")
    if not REQUIRED<=set(k for k,v in seen.items() if not v):raise ValueError("missing package components")
    if not any(k.startswith("_internal/") and not v for k,v in seen.items()) or not any(k.startswith("docs/") and not v for k,v in seen.items()):raise ValueError("missing runtime or documentation")
    return out

def stage(archive,expected_sha256,destination):
    archive=Path(archive).resolve(strict=True);dest=Path(destination).absolute()
    if any(p.is_symlink() or getattr(p,"is_junction",lambda:False)() for p in (dest,*dest.parents)):raise ValueError("reparse point destination prohibited")
    if dest.exists() or dest.is_symlink() or not dest.parent.is_dir():raise ValueError("new destination with existing parent required")
    if not isinstance(expected_sha256,str) or not re.fullmatch(r"[0-9a-fA-F]{64}",expected_sha256):raise ValueError("trusted SHA-256 required")
    # Copy and hash the exact bytes subsequently opened, avoiding source changes.
    with tempfile.TemporaryDirectory(prefix=".awqm-stage-",dir=dest.parent) as work:
        work=Path(work);local=work/"input.zip"
        if archive.stat().st_size>MAX_BYTES:raise ValueError("archive too large")
        with archive.open("rb") as src,local.open("xb") as dst:
            total=0
            while chunk:=src.read(1024*1024):
                total+=len(chunk)
                if total>MAX_BYTES:raise ValueError("archive too large")
                dst.write(chunk)
        actual=digest(local)
        if actual!=expected_sha256.lower():raise ValueError("package checksum mismatch")
        extracted=work/"unpacked";extracted.mkdir()
        with zipfile.ZipFile(local) as z:
            members=entries(z)
            for i,parts in members:
                target=extracted.joinpath(*parts)
                if i.is_dir():target.mkdir(parents=True,exist_ok=True);continue
                target.parent.mkdir(parents=True,exist_ok=True)
                with z.open(i) as src,target.open("xb") as dst:
                    shutil.copyfileobj(src,dst,1024*1024)
                if target.stat().st_size!=i.file_size:raise ValueError("archive length mismatch")
        root=extracted/"AWQM"
        version=(root/"VERSION.txt").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-dev)?",version):raise ValueError("invalid package version")
        result={"version":version,"sha256":actual,"status":"verified_staged_not_installed",
                "files":sum(not i.is_dir() for i,_ in members),"production_approved":False}
        write_json(root/"STAGE-RECEIPT.json",result)
        if dest.exists() or dest.is_symlink():raise ValueError("destination already exists")
        root.rename(dest)
        return result

def main():
    p=argparse.ArgumentParser(description="Verify trusted ZIP checksum and stage in a new offline directory; does not install")
    p.add_argument("archive",type=Path);p.add_argument("--sha256",required=True);p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(stage(a.archive,a.sha256,a.output),ensure_ascii=False,indent=2))
