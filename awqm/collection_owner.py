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


@contextmanager
def _windows_lease(name):
    import pywintypes
    import win32api
    import win32event
    import winerror

    try:
        handle = win32event.CreateMutex(None, False, name)
        already_exists = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    except pywintypes.error:
        # Includes access denied across service/user identities or an invalid
        # object of another type. Do not create a second name as a fallback.
        raise CollectionBusy("collection ownership unavailable") from None
    try:
        if already_exists:
            raise CollectionBusy("another collection owner is active")
        yield
    finally:
        handle.Close()


@contextmanager
def collection_lease():
    if sys.platform == "win32":
        with _windows_lease(NAME):
            yield
    else:
        # Non-Windows fixture runtime only. The SCM service is Windows-only.
        if not _process_guard.acquire(blocking=False):
            raise CollectionBusy("another collection owner is active")
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
                raise CollectionBusy("SCM state unavailable") from None
            if state not in ("missing", "stopped"):
                raise CollectionBusy("service may be active")
        yield
