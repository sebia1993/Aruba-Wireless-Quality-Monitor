import argparse
from awqm.web import serve

def main():
    p=argparse.ArgumentParser(description="AWQM development-only demo")
    p.add_argument("--demo",action="store_true",help="explicitly enable synthetic localhost mode")
    p.add_argument("--port",type=int,default=8765)
    p.add_argument("--smoke-test",action="store_true",help="initialize demo data and exit")
    args=p.parse_args()
    if not args.demo:
        p.error("Production collection is not enabled. Use --demo for synthetic preview.")
    if not 1024<=args.port<=65535:
        p.error("port must be 1024..65535")
    serve(args.port,args.smoke_test)

if __name__=="__main__":
    main()
