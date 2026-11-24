import unittest
from awqm.scheduler import Scheduler,Protection,Poll
from awqm.ssh_shell import ShellSession,ShellError

class Clock:
    def __init__(self):self.t=0
    def now(self):return self.t
    def sleep(self,n):self.t+=n

class Channel:
    def __init__(self,chunks):
        self.chunks=list(chunks);self.sent=[];self.closed=False
    def recv_ready(self):return bool(self.chunks)
    def recv(self,n):return self.chunks.pop(0)
    def sendall(self,x):self.sent.append(x)
    def close(self):self.closed=True

class Transport:
    def __init__(self,c):self.client=self;self.c=c
    def invoke_shell(self,**kw):return self.c
    def close(self):pass

class SchedulerTests(unittest.TestCase):
    def test_priority_no_overlap(self):
        s=Scheduler(mode="standard")
        self.assertEqual(s.next(0).name,"show cpuload")
        self.assertIsNone(s.next(1))
        s.protection.cpu(20,1);s.finish(1)
        self.assertEqual(s.next(1).name,"show ap radio-summary")
        s.finish(80)
        self.assertGreater(s.due["show ap radio-summary"],80)
    def test_cpu_protection(self):
        p=Protection();d=Poll("detail",60,30,4,True)
        p.cpu(20,0);self.assertTrue(p.allowed(d,0))
        p.cpu(75,30);p.cpu(75,60);self.assertFalse(p.allowed(d,60))
        p.cpu(88,70);self.assertEqual(p.effective(70),"critical")
        p.cpu(20,80);p.cpu(20,90);p.cpu(20,100);self.assertTrue(p.allowed(d,100))
        self.assertFalse(p.allowed(d,200))
    def test_budget(self):
        p=Protection();p.cpu(20,0)
        p.account(0,11,True)
        self.assertFalse(p.allowed(Poll("detail",60,30,4,True),12))
    def test_shell(self):
        c=Channel([b"(demo) [node] # "])
        clock=Clock();s=ShellSession(Transport(c),clock.now,clock.sleep)
        s.open();c.chunks=[b"show cpuload\r\nuser 1%, system 2%, idle 97%\r\n(demo) [node] # "]
        self.assertEqual(s.run("show cpuload"),"user 1%, system 2%, idle 97%")
        self.assertEqual(c.sent,["show cpuload\n"])
    def test_timeout_closes(self):
        c=Channel([b"(demo) # "]);clock=Clock()
        s=ShellSession(Transport(c),clock.now,clock.sleep);s.open()
        with self.assertRaises(ShellError):s.run("show version",timeout=.1,idle_timeout=.1)
        self.assertTrue(c.closed)
    def test_unapproved_cli(self):
        c=Channel([b"(demo) # "]);clock=Clock()
        s=ShellSession(Transport(c),clock.now,clock.sleep);s.open()
        with self.assertRaises(ValueError):s.run("reload")
        self.assertEqual(c.sent,[])
