from dataclasses import dataclass
from math import isfinite

def number(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if isfinite(value) and lo <= value <= hi else None

@dataclass(frozen=True)
class Radio:
    radio_id: str
    ap_id: str
    ap_name: str
    controller: str
    band: str
    channel: int
    observed_at: int
    utilization: float | None
    interference: float | None
    noise: float | None
    clients: int | None = None
    source: str = "synthetic"
    epoch: str = "1"
    complete: bool = True

    @property
    def valid(self):
        return (self.complete and number(self.utilization,0,100) is not None
                and number(self.interference,0,100) is not None
                and number(self.noise,-130,0) is not None)

def delta(previous, current, previous_epoch, current_epoch):
    if previous_epoch != current_epoch or previous is None or current is None:
        return None
    if not all(isinstance(v,int) and not isinstance(v,bool) and v >= 0 for v in (previous,current)):
        return None
    return current - previous if current >= previous else None

def select_active(rows):
    """Return unique active observation only; duplicates with inconsistent values are conflicts."""
    active = [row for role,row in rows if role == "A"]
    if not active:
        return None, "Unknown"
    # Distinct controllers claiming active ownership must not be silently resolved.
    if len({row.controller for row in active}) != 1:
        return None, "Conflict"
    newest = max(row.observed_at for row in active)
    candidates = [row for row in active if row.observed_at == newest]
    if any(row != candidates[0] for row in candidates):
        return None, "Conflict"
    return candidates[0], "Single Source"
