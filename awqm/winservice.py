"""Windows SCM host for the explicitly authorized diagnostic service."""
import sys,threading
from .service_config import default_root
from .service_runtime import run_service

SERVICE_NAME="AWQM"
DISPLAY_NAME="Aruba Wireless Quality Monitor (diagnostic)"

def service_class():
    import win32serviceutil,win32service,servicemanager
    class AWQMService(win32serviceutil.ServiceFramework):
        _svc_name_=SERVICE_NAME
        _svc_display_name_=DISPLAY_NAME
        _svc_description_="Read-only, field-unvalidated Aruba diagnostic collection"
        def __init__(self,args):
            super().__init__(args)
            self.stop_event=threading.Event()
        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING,waitHint=120000)
            self.stop_event.set()
        def SvcShutdown(self):self.SvcStop()
        def SvcDoRun(self):
            # No device details, credential strings or untrusted exception text
            # are sent to the Windows Event Log.
            try:
                run_service(default_root(),self.stop_event)
            except Exception as exc:
                servicemanager.LogErrorMsg("AWQM diagnostic service stopped: "+type(exc).__name__)
                raise RuntimeError("AWQM diagnostic service failed") from None
    return AWQMService

def main():
    if sys.platform!="win32":raise SystemExit("Windows service only")
    if sys.argv[1:]==["--smoke-test"]:
        cls=service_class()
        print("AWQM Windows service host import passed:",cls._svc_name_,"(no service registration)")
        return
    if len(sys.argv)!=1:raise SystemExit("AWQM_Service.exe is started by the Windows SCM; use AWQM_Manager.exe")
    import servicemanager
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(service_class())
    servicemanager.StartServiceCtrlDispatcher()
