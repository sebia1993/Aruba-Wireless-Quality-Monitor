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
            self.stop_event=threading.Event()
            self.status_lock=threading.Lock()
            super().__init__(args)
        def SvcStop(self):
            with self.status_lock:
                self.stop_event.set()
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING,waitHint=120000)
        def SvcShutdown(self):self.SvcStop()
        def SvcInterrogate(self):
            # SCM already has the last transition. The framework default sends RUNNING.
            return 0
        def SvcRun(self):
            # pywin32's default SvcRun publishes RUNNING before SvcDoRun.
            self.ReportServiceStatus(win32service.SERVICE_START_PENDING,waitHint=120000)
            self.SvcDoRun()
            # The native host reports the final STOPPED status and error code.
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        def report_ready(self):
            with self.status_lock:
                if not self.stop_event.is_set():
                    self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        def SvcDoRun(self):
            # No device details, credential strings or untrusted exception text
            # are sent to the Windows Event Log.
            try:
                run_service(default_root(),self.stop_event,self.report_ready)
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
