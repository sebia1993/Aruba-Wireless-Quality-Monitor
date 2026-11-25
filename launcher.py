import argparse
from awqm.web import serve
from awqm.probe import main as probe_main
from awqm.diagnostic import main as diagnostic_main
from awqm.engine_demo import serve_engine
from awqm.secure_preview import main as auth_main
from awqm.dpapi import smoke as dpapi_smoke

def main():
    import sys
    from pathlib import Path
    name=Path(sys.executable).name.lower() if getattr(sys,"frozen",False) else ""
    if name=="awqm_service.exe":
        from awqm.winservice import main as service_main
        service_main();return
    if name=="awqm_manager.exe" or sys.argv[1:2]==["--manager"]:
        if name!="awqm_manager.exe":sys.argv.pop(1)
        from awqm.manager import main as manager_main
        manager_main();return
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
        probe_main()
        return
    if sys.argv[1:]==["--validate-ssh"]:
        from awqm.ssh_validation import main as ssh_main
        ssh_main();return
    if sys.argv[1:]==["--dpapi-smoke-test"]:
        dpapi_smoke()
        return
    if len(sys.argv)>1 and sys.argv[1]=="--auth-preview":
        sys.argv.pop(1)
        auth_main()
        return
    if len(sys.argv)>1 and sys.argv[1]=="--collect-once":
        sys.argv.pop(1)
        diagnostic_main()
        return
    p=argparse.ArgumentParser(description="AWQM development-only demo")
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
    (serve_engine if args.engine_demo else serve)(args.port,args.smoke_test)

if __name__=="__main__":
    main()
