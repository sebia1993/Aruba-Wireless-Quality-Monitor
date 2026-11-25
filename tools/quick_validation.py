"""Change-aware developer checks; never a substitute for full release CI."""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SHA=re.compile(r"[0-9a-f]{40}",re.ASCII)
# Explicit groups for bounded leaf changes. Broad/shared or unknown code
# deliberately falls back to all tests. Full candidate workflows always run
# everything, independently of this selector.
GROUPS={
    "awqm/client_info_airtime.py": ("test_client_info_airtime","test_client","test_station_validation"),
    "awqm/airtime_contract.py": ("test_client","test_client_info_airtime","test_station_validation","test_client_correlation"),
    "awqm/controller_resources.py": ("test_controller_resources","test_worker","test_cluster"),
    "awqm/ap_anchors.py": ("test_ap_anchors","test_worker","test_cluster","test_diagnostic_web"),
    "awqm/rf_neighbors.py": ("test_rf_neighbors","test_live_neighbors","test_incidents"),
    "awqm/selection.py": ("test_selection","test_worker","test_jobs_engine"),
    "awqm/reporting.py": ("test_reporting","test_diagnostic_history","test_diagnostic_web"),
    "awqm/incident_reports.py": ("test_incident_reports","test_diagnostic_web","test_rf03_incident_evidence"),
    "awqm/retained_validation.py": ("test_retained_validation",),
    "awqm/lifecycle_validation.py": ("test_lifecycle_validation","test_storage_lifecycle","test_maintenance"),
    "awqm/https_validation.py": ("test_https_validation","test_http_bounds","test_notifications"),
    "awqm/diagnostic_web.py": ("test_diagnostic_web","test_http_bounds","test_notifications","test_https_validation","test_auth","test_manual_jobs","test_operations"),
    "awqm/secure_preview.py": ("test_secure_preview","test_diagnostic_web","test_http_bounds","test_auth"),
    "awqm/http_bounds.py": ("test_http_bounds","test_diagnostic_web","test_notifications"),
    "awqm/web.py": ("test_foundation","test_demo_clients","test_engine_demo","test_secure_preview","test_diagnostic_web"),
    "awqm/engine_demo.py": ("test_engine_demo","test_foundation"),
    "tools/browser_validation.py": ("test_diagnostic_web","test_secure_preview"),
    "tools/check_dependency_audit.py": ("test_dependency_audit",),
    "tools/quick_validation.py": ("test_quick_validation",),
}
BASELINE=("test_foundation",)
PORTABLE=("test_portable_launcher",)


def git(*args):
    return subprocess.check_output(["git",*args],cwd=ROOT,stderr=subprocess.DEVNULL)


def changed_paths(before,head,ref):
    if not SHA.fullmatch(head or ""):
        return None
    try:
        parents=git("rev-list","--parents","-n","1",head).decode("ascii").split()[1:]
        if ref=="refs/heads/develop/awqm-portable" and len(parents)==2:
            # Only recognize a two-parent sync if its second parent is
            # actually on the fetched foundation history. Test the remaining
            # portable delta, not every already-tested foundation change.
            subprocess.run(["git","merge-base","--is-ancestor",parents[1],
                            "refs/remotes/origin/develop/awqm-foundation"],cwd=ROOT,
                           check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            before=parents[1]
        if not SHA.fullmatch(before or "") or before=="0"*40:
            return None
        raw=git("diff","--name-only","-z",before,head)
        return [p.decode("utf-8") for p in raw.split(b"\0") if p]
    except (OSError,subprocess.CalledProcessError,UnicodeError):
        return None


def select(paths,available):
    available=set(available)
    if paths is None:
        return sorted(available),"all: missing or untrusted change range"
    chosen=set(BASELINE)&available
    for p in paths:
        if p.startswith("docs/") or p in ("README.md","VERSION.txt","awqm/__init__.py",".gitignore"):
            continue
        if p.startswith(".github/workflows/"):
            chosen.update({"test_quick_validation"}&available)
            continue
        if p.startswith("awqm/static/"):
            chosen.update({"test_diagnostic_web","test_secure_preview","test_engine_demo"}&available)
            continue
        if p=="launcher.py":
            chosen.update(set(PORTABLE)&available)
            chosen.update({"test_engine_demo","test_scale_validation","test_ssh_validation"}&available)
            continue
        if p.startswith("tests/test_") and p.endswith(".py") and "/" not in p[6:]:
            name=Path(p).stem
            if name not in available:
                return sorted(available),"all: removed/renamed test"
            chosen.add(name)
            continue
        if p in GROUPS:
            group=set(GROUPS[p])
            if not group<=available:
                return sorted(available),"all: stale test group for "+p
            chosen.update(group)
            continue
        return sorted(available),"all: broad or unknown change "+p
    if "test_portable_launcher" in available:
        chosen.add("test_portable_launcher")
    return sorted(chosen),"targeted developer checks"


def main():
    names={p.stem for p in (ROOT/"tests").glob("test_*.py")}
    paths=changed_paths(os.environ.get("AWQM_BEFORE"),os.environ.get("AWQM_HEAD"),
                        os.environ.get("AWQM_REF"))
    chosen,reason=select(paths,names)
    print(reason,flush=True)
    print("Selected:",", ".join(chosen),flush=True)
    sys.path.insert(0,str(ROOT/"tests"))
    loader=unittest.TestLoader()
    suite=unittest.TestSuite(loader.loadTestsFromName(name) for name in chosen)
    if not suite.countTestCases():
        raise SystemExit("no tests selected")
    if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
        raise SystemExit(1)


if __name__=="__main__":
    main()
