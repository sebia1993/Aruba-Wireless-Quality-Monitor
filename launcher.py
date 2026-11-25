import argparse
import sys
from pathlib import Path

from awqm import __version__


COMMAND_HELP = """Commands with their own --help
  --validate-cli    Parse saved controller output offline
  --validate-scale  Run a synthetic capacity, storage, HTTPS or soak trial
  --stage-package   Verify and stage an offline folder-package ZIP
  --manager         Windows service management
  --collect-once    Perform one authorized controller collection
  --auth-preview    Run the local HTTPS authentication preview

Standalone commands
  --service --smoke-test  Check Windows service imports
  --validate-ssh      Exercise SSH against a local fixture
  --dpapi-smoke-test  Test DPAPI under the current Windows user
  --desktop-smoke-test  Check Windows controls and a local dashboard
  --version           Print the AWQM version
  --runtime-info      Print the loaded Python, SQLite and OpenSSL versions as JSON"""


def portable_runtime():
    """Distinguish PyInstaller onefile from the legacy installed onedir layout."""
    if not getattr(sys,"frozen",False):return False
    exe_dir=Path(sys.executable).resolve().parent
    unpacked=Path(sys._MEIPASS).resolve()
    return unpacked not in (exe_dir,(exe_dir/"_internal").resolve())

def main():
    if sys.argv[1:] == ["--version"]:
        print(__version__)
        return
    if sys.argv[1:] == ["--runtime-info"]:
        import json
        import sqlite3
        import ssl
        from cryptography.hazmat.backends.openssl.backend import backend

        print(json.dumps({
            "python": ".".join(map(str, sys.version_info[:3])),
            "sqlite": sqlite3.sqlite_version,
            "openssl": ssl.OPENSSL_VERSION,
            "cryptography_openssl": backend.openssl_version_text(),
        }))
        return
    portable = portable_runtime()
    name = Path(sys.executable).name.lower() if getattr(sys,"frozen",False) else ""
    service_entry=name=="awqm_service.exe" or sys.argv[1:2]==["--service"]
    manager_entry=name=="awqm_manager.exe" or sys.argv[1:2]==["--manager"]
    if service_entry:
        if sys.argv[1:2]==["--service"]:sys.argv.pop(1)
        if portable and sys.argv[1:]!=["--smoke-test"]:
            raise SystemExit("Use the machine-wide AWQM installation to host a Windows service.")
        from awqm.winservice import main as service_main
        service_main();return
    if manager_entry:
        if sys.argv[1:2]==["--manager"]:sys.argv.pop(1)
        if portable and sys.argv[1:] not in (["smoke-test"],["--help"],["-h"]):
            raise SystemExit("Use the machine-wide AWQM installation for service management.")
        from awqm.manager import main as manager_main
        manager_main();return
    if sys.argv[1:] == ["--desktop-smoke-test"]:
        import json
        from awqm.desktop_app import desktop_smoke
        print(json.dumps(desktop_smoke()))
        return
    if len(sys.argv) == 1 and sys.platform == "win32":
        from awqm.desktop_app import run_desktop
        run_desktop(portable=portable)
        return
    if len(sys.argv)>1 and sys.argv[1]=="--stage-package":
        sys.argv.pop(1)
        from awqm.package_stage import main as stage_main
        stage_main();return
    if len(sys.argv)>1 and sys.argv[1]=="--validate-scale":
        sys.argv.pop(1)
        from awqm.scale_validation import main as scale_main
        scale_main();return
    if len(sys.argv)>1 and sys.argv[1]=="--validate-cli":
        sys.argv.pop(1)
        from awqm.probe import main as probe_main
        probe_main()
        return
    if sys.argv[1:]==["--validate-ssh"]:
        from awqm.ssh_validation import main as ssh_main
        ssh_main();return
    if sys.argv[1:]==["--dpapi-smoke-test"]:
        from awqm.dpapi import smoke as dpapi_smoke
        dpapi_smoke()
        return
    if len(sys.argv)>1 and sys.argv[1]=="--auth-preview":
        sys.argv.pop(1)
        from awqm.secure_preview import main as auth_main
        auth_main()
        return
    if len(sys.argv)>1 and sys.argv[1]=="--collect-once":
        sys.argv.pop(1)
        from awqm.diagnostic import main as diagnostic_main
        diagnostic_main()
        return
    p = argparse.ArgumentParser(
        description="AWQM development diagnostics and local demos",
        epilog=COMMAND_HELP, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    modes=p.add_mutually_exclusive_group()
    modes.add_argument("--demo",action="store_true",help="explicitly enable synthetic localhost mode")
    modes.add_argument("--engine-demo",action="store_true",help="run fixture workers and durable jobs on localhost")
    p.add_argument("--port",type=int,default=8765)
    p.add_argument("--smoke-test",action="store_true",help="initialize demo data and exit")
    args=p.parse_args()
    if not (args.demo or args.engine_demo):
        p.error("Production collection is not enabled. Use --demo, --validate-cli or the authorized local --collect-once trial.")
    if not 1024<=args.port<=65535:
        p.error("port must be 1024..65535")
    if args.engine_demo:
        from awqm.engine_demo import serve_engine as run_demo
    else:
        from awqm.web import serve as run_demo
    try:
        run_demo(args.port, args.smoke_test, open_browser=False)
    except KeyboardInterrupt:
        return

if __name__=="__main__":
    main()
