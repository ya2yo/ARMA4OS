"""SSH/SCP command construction for native and WSL Windows transports."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import subprocess

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]

def powershell_command(program: str, args: list[str]) -> list[str]:
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    script = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); & " + program + " " + " ".join(map(quote, args)) + "; exit $LASTEXITCODE"
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode()]

class Connection:
    def __init__(self, host: str, transport: str):
        self.host = host
        self.transport = ("windows" if os.environ.get("WSL_DISTRO_NAME") and shutil.which("powershell.exe") else "native") if transport == "auto" else transport

    def command(self, program: str, args: list[str]) -> list[str]:
        return powershell_command(program, args) if self.transport == "windows" else [program, *args]

    def ssh(self, command: str) -> list[str]:
        return self.command("ssh", [*SSH_OPTIONS, self.host, command])

    def local_path(self, path: Path) -> str:
        if self.transport == "windows":
            return subprocess.check_output(["wslpath", "-w", str(path.resolve())], text=True).strip()
        return str(path.resolve())

    def copy(self, local: Path, remote: str, upload: bool) -> list[str]:
        pair = [self.local_path(local), self.host + ":" + remote]
        return self.command("scp", [*SSH_OPTIONS, *(pair if upload else pair[::-1])])
