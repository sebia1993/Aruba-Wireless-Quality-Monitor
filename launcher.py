import argparse
from awqm.web import serve
from awqm.probe import main as probe_main

def main():
    import sys
    if len(sys.argv)>1 and sys.argv[1]=="--validate-cli":
        sys.argv.pop(1)
        probe_main()
        return
    p=argparse.ArgumentParser(description="AWQM development-only demo")
    p.add_argument("--demo",action="store_true",help="explicitly enable synthetic localhost mode")
    p.add_argument("--port",type=int,default=8765)
    p.add_argument("--smoke-test",action="store_true",help="initialize demo data and exit")
    args=p.parse_args()
    if not args.demo:
        p.error("Production collection is not enabled. Use --demo or --validate-cli.")
    if not 1024<=args.port<=65535:
        p.error("port must be 1024..65535")
    serve(args.port,args.smoke_test)

if __name__=="__main__":
    main()
