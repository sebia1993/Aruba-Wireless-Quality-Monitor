"""Invented CLI fixtures only. Never opens a socket or reads credentials."""
from .worker import ControllerWorker

def table(title,fields,rows,footer):
    widths=[max(len(k),*(len(str(r[i])) for r in rows),1)+3 for i,k in enumerate(fields)]
    def line(values):return "".join(str(v).ljust(w) for v,w in zip(values,widths)).rstrip()
    return "\n".join([title,"-"*len(title),line(fields),line(["-"*(w-3) for w in widths]),*[line(r) for r in rows],footer])

def fixtures(index):
    name=f"DEMO-AP-{index:03d}"
    ip=f"192.0.2.{index+10}"
    mac=f"02:00:00:00:00:{index:02x}"
    bssid=f"02:ff:00:{index:02x}:00:01"
    bssid2=f"02:ff:00:{index:02x}:01:01"
    inv=table("AP Database",["Name","Group","AP Type","IP Address","Status","Switch IP","Standby IP","Wired MAC Address","Serial #"],
         [[name,"DEMO","535",ip,"Up 1d",f"198.51.100.{index}","198.51.100.9",mac,"DEMO"]],"Total APs:1")
    act=table("Active AP Table",["Name","IP Address","Cluster Role","Radio 0 Band Ch/EIRP/MaxEIRP/Clients","Radio 1 Band Ch/EIRP/MaxEIRP/Clients"],
         [[name,ip,"A","AP:5GHz-HE:44+/18/24/4","AP:2.4GHz-HE:6/12/20/4"]],"Num APs:1")
    busy=index in (1,3)
    rf=table("APs Radios information",["Name","Group","AP Type","IP Address","Band","Mode","EIRP/MaxEIRP","NF/U/I","TD","TM","TC"],
         [[name,"DEMO","535",ip,"5","AP:44+","18/24","-94/ 94/ 5" if busy else "-94/ 24/ 5","1/2/3/4/5/6","0/0/0/0/0/0","0/0/0/0/0/0"],
          [name,"DEMO","535",ip,"2.4","AP:6","12/20","-96/ 24/ 2","1/2/3/4/5/6","0/0/0/0/0/0","0/0/0/0/0/0"]],"Total Radios:2")
    assocrows=[]
    detailrows=[]
    for radio,bss in enumerate((bssid,bssid2)):
        for client in range(1,5):
            addr=f"02:00:00:{index:02x}:{radio:02x}:{client:02x}"
            ssid="DEMO-GUEST" if client==1 else "DEMO-STAFF"
            assocrows.append([name,bss,addr,"y","y",ssid,"WAB"])
            detailrows.append([addr,ssid,bss,"Associated",1000,500,200,24,54,13,15,0,"22/88"])
    assoc=table("Association Table",["Name","bssid","mac","auth","assoc","essid","Flags"],assocrows,"Num Clients:8")
    detail=table("Client Table",["MAC","ESSID","BSSID","Assoc_State","Tx_Pkts","Rx_Pkts","Tx_Retries","Tx_Rate","Rx_Rate","Last_ACK_SNR","Last_Rx_SNR","Idle time","Client health (C/R)"],detailrows,
                 "UAPSD:(VO,VI,BK,BE,Max SP,Q Len)\nHT_State shows client's original capabilities (not operational capabilities)")
    return name,{"show cpuload":"user 10%, system 10%, idle 80%","show ap radio-summary":rf,
        "show ap active details":act,"show ap database long":inv,"show ap association":assoc,
        "show ap debug client-table":detail,
        "show ap debug client-stats":"Station Stats\nParameter Value\nFrames Transmitted 200\nSuccess With Retry 70"}

class FixtureSession:
    def __init__(self,index):
        self.ap,self.responses=fixtures(index);self.closed=False
    def open(self):self.closed=False
    def run(self,name,target=None,**kwargs):
        if self.closed:raise RuntimeError("fixture session closed")
        if name=="show ap debug client-table" and target!=self.ap:raise ValueError("unmapped fixture target")
        return self.responses[name]
    def close(self):self.closed=True

def workers(clock,wall,offsets=True):
    return [ControllerWorker(f"WLC-{i}",lambda i=i:FixtureSession(i),clock,wall,
            mode="standard",offset=(i-1)*15 if offsets else 0) for i in range(1,5)]
