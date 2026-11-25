"""Windows SCM host for the explicitly authorized diagnostic service."""
import sys,threading
from .package_stage import uses_unified_launcher, validated_program_version
from .service_config import default_root
from .service_runtime import run_service

SERVICE_NAME="AWQM"
DISPLAY_NAME="Aruba Wireless Quality Monitor (diagnostic)"


def unified_command(installed):
    return f'"{installed / "AWQM.exe"}" --service'


def installed_command(installed):
    if uses_unified_launcher(validated_program_version(installed)):
        return unified_command(installed)
    return f'"{installed / "AWQM_Service.exe"}"'


def registration_command(installed, command):
    """Recognize only the two supported ImagePaths. Arguments are case-sensitive."""
    if isinstance(command, str):
        suffix = " --service"
        if command.endswith(suffix) and command.removesuffix(suffix).casefold() == f'"{installed / "AWQM.exe"}"'.casefold():
            return unified_command(installed)
        legacy = str(installed / "AWQM_Service.exe")
        if command.casefold() == f'"{legacy}"'.casefold():
            return f'"{legacy}"'
        if command.casefold() == legacy.casefold():
            # Recognize old registrations during offline migration. A path with
            # spaces has to be re-quoted before it may be activated.
            return legacy if any(c.isspace() for c in legacy) else f'"{legacy}"'
    raise ValueError("unexpected AWQM service executable or arguments")

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
    if len(sys.argv)!=1:raise SystemExit("Windows SCM starts AWQM.exe --service. Use AWQM.exe --manager for service management.")
    import servicemanager
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(service_class())
    servicemanager.StartServiceCtrlDispatcher()
