"""Windows user-scope DPAPI primitive.

No credential files are created here. The eventual service must enforce NTFS
ACLs and call this under its own Windows identity and loaded profile. This
module does not enable machine-scope protection or a password database.
"""
import ctypes,os,sys,secrets
from ctypes import wintypes

MAX_BYTES=65536
CRYPTPROTECT_UI_FORBIDDEN=0x1

class DPAPIError(RuntimeError):pass

class DATA_BLOB(ctypes.Structure):
    _fields_=[("cbData",wintypes.DWORD),("pbData",ctypes.POINTER(ctypes.c_ubyte))]

def _api():
    if sys.platform!="win32":raise DPAPIError("Windows DPAPI is unavailable")
    crypt=ctypes.WinDLL("crypt32",use_last_error=True)
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    p=ctypes.POINTER(DATA_BLOB)
    crypt.CryptProtectData.argtypes=[p,wintypes.LPCWSTR,p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,p]
    crypt.CryptProtectData.restype=wintypes.BOOL
    crypt.CryptUnprotectData.argtypes=[p,ctypes.POINTER(wintypes.LPWSTR),p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,p]
    crypt.CryptUnprotectData.restype=wintypes.BOOL
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    kernel.LocalFree.restype=ctypes.c_void_p
    return crypt,kernel

def _transform(value,encrypt):
    if not isinstance(value,bytes) or not 1<=len(value)<=MAX_BYTES:
        raise ValueError("invalid DPAPI input length")
    crypt,kernel=_api()
    buf=(ctypes.c_ubyte*len(value)).from_buffer_copy(value)
    src=DATA_BLOB(len(value),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)))
    out=DATA_BLOB()
    try:
        if encrypt:
            ok=crypt.CryptProtectData(ctypes.byref(src),"AWQM collection credential",None,None,None,CRYPTPROTECT_UI_FORBIDDEN,ctypes.byref(out))
        else:
            ok=crypt.CryptUnprotectData(ctypes.byref(src),None,None,None,None,CRYPTPROTECT_UI_FORBIDDEN,ctypes.byref(out))
        if not ok:
            raise DPAPIError("DPAPI operation failed (Windows error "+str(ctypes.get_last_error())+")")
        if not out.pbData or not 1<=out.cbData<=MAX_BYTES:
            raise DPAPIError("invalid DPAPI output")
        return ctypes.string_at(out.pbData,out.cbData)
    finally:
        # Wipe native buffers where possible. Python immutable byte copies are
        # not guaranteed to be zeroized; do not claim memory erasure.
        ctypes.memset(buf,0,len(value))
        if out.pbData:
            ctypes.memset(out.pbData,0,out.cbData)
            kernel.LocalFree(ctypes.cast(out.pbData,ctypes.c_void_p))

def protect(value):return _transform(value,True)
def unprotect(value):return _transform(value,False)

def smoke():
    data=secrets.token_bytes(32)
    encrypted=protect(data)
    if encrypted==data or unprotect(encrypted)!=data:raise DPAPIError("round trip failed")
    print("DPAPI user-scope round trip passed (synthetic value; current process identity only)")
