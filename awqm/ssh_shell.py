"""Persistent bounded interactive CLI transport; not enabled by production launcher."""
import codecs
import re
import threading
import time
from .collector import PinnedSSH, command

PROMPT=re.compile(r"(?m)^\([^\r\n]+\)(?:\s*\*?\[[^\r\n]+\])?\s*#\s*$")
PAGER=re.compile(r"--More--",re.I)
ANSI=re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

class ShellError(RuntimeError):
    pass

class ShellSession:
    def __init__(self, transport: PinnedSSH, clock=time.monotonic, sleep=time.sleep, max_bytes=16_000_000):
        self.transport=transport; self.clock=clock; self.sleep=sleep
        self.max_bytes=max_bytes; self.channel=None; self.prompt=None
        self.lock=threading.Lock()
    def _receive(self,timeout,idle_timeout):
        start=self.clock(); last=start; size=0; output=""; pages=0
        decoder=codecs.getincrementaldecoder("utf-8")("strict")
        while self.clock()-start<timeout and self.clock()-last<idle_timeout:
            if self.channel.recv_ready():
                data=self.channel.recv(65536)
                if not data:
                    raise ShellError("SSH channel closed")
                last=self.clock(); size+=len(data)
                if size>self.max_bytes:
                    raise ShellError("CLI response size limit")
                output+=decoder.decode(data)
                # Handle only the known Aruba pager marker, never send arbitrary input.
                clean=ANSI.sub("",output).replace("\r","")
                if PAGER.search(clean):
                    count=len(PAGER.findall(clean))
                    pages+=count
                    if pages>1000:
                        raise ShellError("pager limit")
                    output=PAGER.sub("",output)
                    output=output.replace("\x08","")
                    self.channel.sendall(" ")
                    continue
                line=clean.rstrip().split("\n")[-1]
                if PROMPT.fullmatch(line):
                    if self.prompt is None:
                        self.prompt=line.strip()
                    elif line.strip()!=self.prompt:
                        raise ShellError("CLI prompt changed")
                    body=clean.rstrip().rsplit("\n",1)
                    return body[0] if len(body)==2 else ""
            elif self.channel.closed:
                raise ShellError("SSH channel closed")
            else:
                self.sleep(.02)
        raise ShellError("CLI response deadline exceeded")
    def open(self):
        if self.transport.client is None:
            raise ShellError("SSH connection not established")
        self.channel=self.transport.client.invoke_shell(term="vt100",width=512,height=1000)
        self.prompt=None
        try:
            self._receive(15,10)
        except Exception:
            self.close()
            raise
    def run(self,name,target=None,timeout=30,idle_timeout=10):
        cmd=command(name,target)
        if not self.lock.acquire(blocking=False):
            raise ShellError("another CLI command is running")
        try:
            if self.channel is None:
                raise ShellError("CLI session not initialized")
            self.channel.sendall(cmd+"\n")
            output=self._receive(timeout,idle_timeout)
            lines=output.splitlines()
            if lines and lines[0].strip()==cmd:
                lines=lines[1:]
            return "\n".join(lines)
        except Exception:
            self.close()
            raise
        finally:
            self.lock.release()
    def close(self):
        if self.channel is not None:
            self.channel.close()
            self.channel=None
        self.transport.close()
        self.prompt=None
