"""Shared local AP-name target contract for read-only detail investigations.

An AP display name is not a physical identity. Validate the queued physical
binding against unique current-connection AAC and inventory observations
before any AP-scoped detail CLI. Fresh positive peer evidence can veto the local binding. Missing peer
observations cannot prove cluster-wide ownership or what happened after
collection.
"""
from .collector import AP
from .manual_jobs import bind_ap


def binding_from_physical_id(ap_id):
    """Convert a canonical site:wired-MAC identity to the existing binding."""
    if not isinstance(ap_id, str):
        raise ValueError("physical AP identity required")
    parts = ap_id.rsplit(":", 6)
    if len(parts) != 7 or not parts[0]:
        raise ValueError("physical AP identity required")
    return bind_ap(":".join(parts[1:]))


def peer_identity_conflicts(worker, target, ip, mac, now):
    """Veto positive peer contradictions without taking another worker lock.

    Each peer publishes a pair of immutable, independently timed identity
    frames. A missing, failed, stale or future frame is not corroboration.
    This is not a cluster transaction or a guarantee of complete coverage.
    """
    for peer in getattr(worker, "identity_peers", ()):
        if peer.maintenance_requested:
            continue
        inventory, active = peer.identity_frame
        if inventory is not None and 0 <= now-inventory[0] <= 660:
            for name, addr, wired in inventory[1]:
                if wired == mac and (name != target or (addr and addr != ip)):
                    return True
                if wired != mac and (name == target or addr == ip):
                    return True
        if active is not None and 0 <= now-active[0] <= 150:
            if any(aac and (name == target or addr == ip)
                   for name, addr, aac in active[1]):
                return True
    return False


def current_ap_binding(worker, target, now):
    """Return an opaque binding or None; never infer ownership from absence."""
    if (not isinstance(target, str) or not AP.fullmatch(target)
            or worker.session is None or worker.blocked
            or worker.maintenance_requested or worker.schedule.paused):
        return None
    readings = []
    for command, max_age in (("show ap active details", 150),
                             ("show ap database long", 660)):
        rd = worker.latest.get(command)
        outcome = worker.outcomes.get(command)
        if (rd is None or outcome is not rd or rd.status != "success"
                or rd.generation != worker.generation
                or not 0 <= now - rd.observed_at <= max_age):
            return None
        readings.append(rd)
    active, inventory = (rd.data for rd in readings)
    named = [a for a in active if a["name"] == target]
    if len(named) != 1 or "A" not in named[0]["roles"]:
        return None
    ip = named[0]["ip"]
    if not ip or sum(a["ip"] == ip for a in active) != 1:
        return None
    # A duplicate name, IP or wired MAC in the same inventory is ambiguous,
    # even if one row happens to match the active table.
    claims = [a for a in inventory if a["name"] == target or a["ip"] == ip]
    if len(claims) != 1 or claims[0]["name"] != target or claims[0]["ip"] != ip:
        return None
    mac = claims[0]["mac"]
    if sum(a["mac"] == mac for a in inventory) != 1:
        return None
    if peer_identity_conflicts(worker, target, ip, mac, now):
        return None
    try:
        return bind_ap(mac)
    except ValueError:
        return None
