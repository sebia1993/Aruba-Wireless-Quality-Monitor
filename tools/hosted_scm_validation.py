"""Exercise the frozen AWQM service on a disposable GitHub-hosted Windows VM."""
import argparse
import json
import os
import platform
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from awqm.maintenance import write_json
from awqm.package_stage import digest, stage
from awqm.service_config import default_root, envelope, load_config


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def wait_for(description, probe, seconds=80):
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if probe():
            return round(time.monotonic() - started, 3)
        time.sleep(0.25)
    raise RuntimeError("Timed out waiting for " + description)


def scm_info():
    import win32serviceutil
    import win32service as scm
    try:
        return win32serviceutil.QueryServiceStatus("AWQM")
    except scm.error as exc:
        if exc.winerror == 1060:
            return None
        raise


def interactive_install(argv, password, timeout=90):
    # Drive the actual no-echo Windows console prompt without logging its content.
    from winpty import PtyProcess
    proc = PtyProcess.spawn(argv)
    prompt = "Windows dedicated service account password:"
    deadline = time.monotonic() + timeout
    try:
        output = ""
        while prompt not in output:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "Timed out waiting for the installation prompt")
            # pywinpty 3.1.2 exposes the console reader as a loopback socket.
            proc.fileobj.settimeout(remaining)
            try:
                output = (output + proc.read())[-2048:]
            except (TimeoutError, EOFError) as exc:
                raise RuntimeError("Installation prompt unavailable: " + type(exc).__name__) from None
        proc.write(password + "\r")
        wait_for("interactive registration to exit", lambda: not proc.isalive(), deadline - time.monotonic())
        require(proc.exitstatus == 0, "Frozen interactive service registration failed")
    finally:
        proc.close(force=True)


def provision_account(name, password):
    import win32net
    import win32netcon
    import win32security as security
    win32net.NetUserAdd(None, 1, {
        "name": name, "password": password, "priv": win32netcon.USER_PRIV_USER,
        "flags": win32netcon.UF_SCRIPT | win32netcon.UF_NORMAL_ACCOUNT,
    })
    sid = security.LookupAccountName(None, name)[0]
    policy = security.LsaOpenPolicy(None, security.POLICY_LOOKUP_NAMES | security.POLICY_CREATE_ACCOUNT)
    try:
        security.LsaAddAccountRights(policy, sid, ("SeServiceLogonRight",))
    finally:
        security.LsaClose(policy)
    return security.ConvertSidToStringSid(sid)


def provision_fixture(root, principal, sid, password):
    import win32con
    import win32profile
    import win32security as security
    import paramiko
    from awqm.operations import configured_ops

    root.mkdir()
    subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r",
                    "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
                    "*" + sid + ":(OI)(CI)M"], check=True)
    for part in ("config", "secrets", "data", "backup", "logs"):
        (root / part).mkdir()
    address = "192.0.2.1"
    write_json(root / "config/service.json", {
        "version": 1, "mode": "diagnostic", "authorized": True, "site": "hosted-fixture",
        "windows_account": principal, "known_hosts": "secrets/known_hosts",
        "controllers": [{"id": "fixture", "ip": address, "username": "fixture-readonly",
                         "credential": "secrets/fixture.dpapi"}]})
    write_json(root / "config/collection.json", {"version": 1, "profile": "safe"})
    key = paramiko.RSAKey.generate(2048)
    (root / "secrets/known_hosts").write_text(f"{address} {key.get_name()} {key.get_base64()}\n", encoding="ascii")
    cfg = load_config(root)
    # Make the collection pause durable before any service start.
    configured_ops(cfg).set("hosted-qualification", "all", True, 0)
    username = principal.split("\\")[-1]
    # LoadUserProfile requires the runner's backup/restore privileges to be
    # present. It handles enabling them. PyHANDLE uses explicit Close().
    token = security.LogonUser(username, ".", password, win32con.LOGON32_LOGON_SERVICE,
                               win32con.LOGON32_PROVIDER_DEFAULT)
    try:
        profile = win32profile.LoadUserProfile(token, {"UserName": username, "Flags": 1})
        try:
            security.ImpersonateLoggedOnUser(token)
            try:
                payload = envelope(cfg.targets[0], secrets.token_urlsafe(24))
            finally:
                security.RevertToSelf()
        finally:
            win32profile.UnloadUserProfile(token, profile)
    finally:
        token.Close()
    cfg.targets[0].credential.write_bytes(payload)
    subprocess.run(["icacls", str(root), "/setowner", "*S-1-5-32-544", "/T"], check=True)


def run(args, report):
    import win32net
    import win32service as scm
    from awqm.windows_update import require_admin
    require(os.environ.get("GITHUB_ACTIONS") == "true" and
            os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted" and
            os.environ.get("GITHUB_JOB") == "service-lifecycle",
            "A disposable GitHub-hosted service-lifecycle job is required")
    require_admin()
    root = default_root()
    programs = Path(os.environ["ProgramFiles"])
    installed = programs / "AWQM"
    candidate = programs / "AWQM-Qualification-Manager"
    require(scm_info() is None and all(not p.exists() for p in (root, installed, candidate)),
            "Existing service or installation")
    require(digest(args.archive) == args.sha256, "Candidate archive digest mismatch")
    receipt = stage(args.archive, args.sha256, candidate)
    shutil.copytree(args.previous, installed)
    prior = (installed / "VERSION.txt").read_text(encoding="utf-8").strip()
    require(prior != receipt["version"], "A distinct upgrade baseline is required")
    report.update(previous_version=prior, candidate_version=receipt["version"],
                  candidate_zip_sha256=args.sha256, platform=platform.platform())

    account = "AwqmCi" + secrets.token_hex(4)
    password = secrets.token_urlsafe(24) + "aA1!"
    principal = os.environ["COMPUTERNAME"] + "\\" + account
    sid = provision_account(account, password)
    status_file = root / "logs/service-status.json"
    phases = report["verified"]

    def manager_command(root):
        legacy = root / "AWQM_Manager.exe"
        return [str(legacy)] if legacy.is_file() else [str(root / "AWQM.exe"), "--manager"]

    def manager_run(*argv, outside=False):
        subprocess.run([*manager_command(candidate if outside else installed), *argv], check=True, timeout=150)

    def verify_registered_image(binary, *, unified):
        from awqm.windows_security import account_sid
        handle = scm.OpenSCManager(None, None, scm.SC_MANAGER_CONNECT)
        try:
            service = scm.OpenService(handle, "AWQM", scm.SERVICE_QUERY_CONFIG)
            try:
                configuration = scm.QueryServiceConfig(service)
            finally:
                scm.CloseServiceHandle(service)
        finally:
            scm.CloseServiceHandle(handle)
        command = f'"{installed / binary}"' + (" --service" if unified else "")
        require(os.path.normcase(configuration[3]) == os.path.normcase(command),
                "SCM command does not match the installed package")
        require(account_sid(configuration[7]) == account_sid(principal), "SCM service account changed")

    def heartbeat(started_at):
        if not status_file.exists():
            return False
        status = json.loads(status_file.read_text(encoding="utf-8"))
        entries = (status.get("operations") or {}).get("controllers", [])
        return (status.get("state") == "running" and status.get("at", 0) >= started_at and len(entries) == 1 and
                entries[0]["controller"] == "fixture" and entries[0]["phase"] == "paused" and
                not entries[0]["inflight"] and scm_info()[1] == scm.SERVICE_RUNNING)

    def start(description):
        started_at = int(time.time())
        manager_run("start")
        wait_for(description, lambda: heartbeat(started_at))

    def stop():
        manager_run("stop")
        wait_for("clean SCM stop", lambda: scm_info()[1] == scm.SERVICE_STOPPED)
        status = json.loads(status_file.read_text(encoding="utf-8"))
        require(status["state"] == "stopped", "Missing clean-stop heartbeat")

    try:
        provision_fixture(root, principal, sid, password)
        report["phase"] = "baseline registration"
        interactive_install([*manager_command(installed), "install"], password)
        verify_registered_image("AWQM_Service.exe", unified=False)
        require(scm_info()[1] == scm.SERVICE_STOPPED, "Registration started the service")
        start("baseline running with collection paused")
        stop()
        phases.append("baseline_registered_started_stopped")
        manager_run("disable-startup")
        report["phase"] = "offline upgrade"
        bundle = ["--id", "fixture-upgrade"]
        archive = ["--archive", str(args.archive), "--sha256", args.sha256]
        try:
            manager_run("prepare-update", *bundle, *archive, outside=True)
        except subprocess.CalledProcessError:
            # Collect a traceback on a second synthetic snapshot; the frozen
            # manager's result remains the acceptance result for this trial.
            from types import SimpleNamespace
            import traceback
            from awqm.windows_update import manage_update
            probe = SimpleNamespace(action="prepare-update", id="diagnostic-snapshot",
                                    archive=args.archive, sha256=args.sha256,
                                    include_association_history=False)
            try:
                manage_update(load_config(root), probe)
            except Exception as exc:
                report["snapshot_diagnostic"] = {
                    "exception": type(exc).__name__, "winerror": getattr(exc, "winerror", None),
                    "api": getattr(exc, "funcname", None),
                    "stack": [Path(f.filename).name + "/" + f.name + "/" + str(f.lineno)
                              for f in traceback.extract_tb(exc.__traceback__)]}
            raise
        manager_run("verify-update", *bundle, outside=True)
        manager_run("switch-program", *bundle, *archive, outside=True)
        require((installed / "VERSION.txt").read_text(encoding="utf-8").strip() == receipt["version"],
                "Unexpected installed version")
        verify_registered_image("AWQM.exe", unified=True)
        phases.append("offline_upgrade")
        # Exercise the migrated registration before testing a fresh registration.
        manager_run("enable-manual")
        start("upgraded service running with its existing registration")
        stop()
        phases.append("upgraded_existing_service_started_stopped")
        # Exercise the candidate's own interactive installer and persisted SCM settings.
        manager_run("remove")
        wait_for("service removal", lambda: scm_info() is None)
        report["phase"] = "candidate registration"
        interactive_install([*manager_command(installed), "install"], password)
        verify_registered_image("AWQM.exe", unified=True)
        manager_run("enable-auto")
        start("candidate running with collection paused")
        stop()
        phases.append("candidate_registered_started_stopped")
        # One failed preflight exercises the real configured SCM restart action.
        report["phase"] = "failure recovery"
        field = root / "config/collection.json"
        original = field.read_bytes()
        try:
            # Edit the existing protected file in place to preserve its owner/ACL.
            field.write_bytes(b'{"version":1,"profile":"invalid-fixture"}')
            manager_run("start")
            wait_for("failed candidate preflight", lambda: scm_info()[1] == scm.SERVICE_STOPPED and
                     json.loads(status_file.read_text(encoding="utf-8")).get("state") == "failed")
        finally:
            field.write_bytes(original)
        restored_at = int(time.time())
        report["observed_recovery_seconds"] = wait_for("SCM automatic recovery", lambda: heartbeat(restored_at), 105)
        stop()
        phases.append("automatic_failure_recovery")
        manager_run("disable-startup")
        report["phase"] = "post-start rollback"
        manager_run("rollback-data", *bundle, "--acknowledge-data-displacement", outside=True)
        require((installed / "VERSION.txt").read_text(encoding="utf-8").strip() == prior,
                "Rollback did not restore the original version")
        verify_registered_image("AWQM_Service.exe", unified=False)
        manager_run("enable-manual")
        start("restored version running")
        stop()
        manager_run("remove")
        wait_for("final removal", lambda: scm_info() is None)
        phases.extend(["post_start_rollback", "restored_service_started_stopped_removed"])
    finally:
        # Fixture paths and partial services belong to the short-lived hosted VM.
        # Never remove or stop a service that wasn't successfully identified above.
        if scm_info() is None:
            win32net.NetUserDel(None, account)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"result": "incomplete", "phase": "preflight", "verified": [],
              "build_sha": os.environ.get("AWQM_BUILD_SHA"), "build_run": os.environ.get("AWQM_BUILD_RUN"),
              "harness_sha": os.environ.get("GITHUB_SHA"),
              "host_reboot": False, "field_equipment": False, "scope": "disposable Windows Server SCM"}
    try:
        run(args, report)
    except BaseException:
        write_json(args.output, report)
        print(json.dumps(report), flush=True)
        raise
    report.update(result="pass", phase="complete")
    write_json(args.output, report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
