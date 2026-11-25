"""Machine-wide cooperative exclusion of AWQM's two live collection paths.

Use kernel object *lifetime*, not thread-owned mutex acquisition: a desktop
worker may release the last lease from a different thread. Never wait on or
take ownership of an existing object, and never change its ACL. A service
account denied access to an existing object must also fail closed.
"""
from contextlib import contextmanager
from threading import Lock
import sys

NAME = r"Global\AWQM.CollectionOwner.v1"
_process_guard = Lock()


class CollectionBusy(RuntimeError):
    """No new live collection is allowed while another owner may exist."""

    def __init__(self, message="collection ownership unavailable", *, reason="unknown"):
        super().__init__(message)
        # Fixed categorical reason only. Never return native errors or arbitrary
        # exception text to the browser.
        self.reason = reason if reason in {
            "owner_active", "owner_unavailable", "service_active",
            "service_unknown", "close_unconfirmed",
        } else "unknown"


@contextmanager
def _windows_lease(name):
    import ctypes
    from ctypes import wintypes

    # Capture the Win32 last-error value at the actual native call boundary.
    # Python wrapper/handle construction and unrelated calls must not change
    # the already-exists decision. Explicitly clear the previous thread error.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = create(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        # Access denied or object-type collision: no second name or ACL bypass.
        raise CollectionBusy("collection ownership unavailable", reason="owner_unavailable")
    try:
        if error == 183:  # ERROR_ALREADY_EXISTS
            raise CollectionBusy("another collection owner is active", reason="owner_active")
        if error:
            raise CollectionBusy("unexpected collection ownership result", reason="owner_unavailable")
        yield
    finally:
        if not close(handle):
            raise CollectionBusy("collection ownership release unconfirmed", reason="close_unconfirmed")


@contextmanager
def collection_lease():
    if sys.platform == "win32":
        with _windows_lease(NAME):
            yield
    else:
        # Non-Windows fixture runtime only. The SCM service is Windows-only.
        if not _process_guard.acquire(blocking=False):
            raise CollectionBusy("another collection owner is active", reason="owner_active")
        try:
            yield
        finally:
            _process_guard.release()


@contextmanager
def local_collection_lease():
    # Take the same kernel guard as the service before checking SCM. This
    # also blocks other desktop accounts and a service startup racing this
    # check. Unknown SCM status is not permission to connect.
    with collection_lease():
        if sys.platform == "win32":
            from .desktop_app import service_state
            try:
                state = service_state()
            except Exception:
                raise CollectionBusy("SCM state unavailable", reason="service_unknown") from None
            if state not in ("missing", "stopped"):
                reason = "service_unknown" if state == "unknown" else "service_active"
                raise CollectionBusy("service may be active", reason=reason)
        yield
