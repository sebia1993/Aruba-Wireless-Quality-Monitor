import argparse
from awqm.web import serve
from awqm.probe import main as probe_main
from awqm.diagnostic import main as diagnostic_main
from awqm.engine_demo import serve_engine

def main():
    import sys
    if len(sys.argv)>1 and sys.argv[1]=="--validate-cli":
        sys.argv.pop(1)
        probe_main()
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
