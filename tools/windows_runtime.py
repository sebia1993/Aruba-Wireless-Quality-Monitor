"""Prepare a private Windows build interpreter and verify the frozen runtime."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from urllib.request import urlopen
from zipfile import ZipFile


SQLITE_URL = "https://www.sqlite.org/2026/sqlite-dll-win-x64-3540100.zip"
SQLITE_ARCHIVE_DIGEST = "c2d09c8babb9399277055c057c97a53f1ffd7c5f11d0c1f2118ffdfa39f2daa4"
SQLITE_DLL_SHA256 = "4b59d3cded9dc1608544160f3b4b10ac974c6ac0d6ffc23064e5f4c8dbd05aea"
EXPECTED = {
    "python": "3.14.10",
    "sqlite": "3.54.1",
    "openssl": "OpenSSL 3.5.9 29 Sep 2026",
    "cryptography_openssl": "OpenSSL 4.0.3 29 Sep 2026",
}


def sqlite_dll(data):
    if hashlib.sha3_256(data).hexdigest() != SQLITE_ARCHIVE_DIGEST:
        raise ValueError("SQLite vendor archive digest mismatch")
    with ZipFile(io.BytesIO(data)) as archive:
        return archive.read("sqlite3.dll")


def prepare(destination):
    if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.version.split()[0] != EXPECTED["python"]:
        raise ValueError("Run preparation with Windows x64 CPython " + EXPECTED["python"])
    source = Path(sys.base_prefix).resolve()
    destination = destination.resolve()
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError("Choose a new private directory outside the Python installation")
    installed = source / "DLLs" / "sqlite3.dll"
    if not installed.is_file():
        raise ValueError("Missing Windows CPython SQLite DLL")
    with urlopen(SQLITE_URL, timeout=45) as response:
        dll = sqlite_dll(response.read(2 * 1024 * 1024))
    shutil.copytree(source, destination)
    (destination / installed.relative_to(source)).write_bytes(dll)
    observed = subprocess.check_output(
        [destination / "python.exe", "-I", "-c", "import sqlite3; print(sqlite3.sqlite_version)"],
        text=True,
    ).strip()
    if observed != EXPECTED["sqlite"]:
        raise ValueError("Private interpreter loaded an unexpected SQLite version: " + observed)


def verify_report(report):
    for name, expected in EXPECTED.items():
        if report.get(name) != expected:
            raise ValueError(f"Unexpected {name}: {report.get(name)!r}, expected {expected!r}")

def embedded_sqlite_digest(executable):
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    matches = [name for name in archive.toc if name.replace("\\", "/").rsplit("/", 1)[-1].lower() == "sqlite3.dll"]
    if len(matches) != 1:
        raise ValueError("Expected one embedded SQLite DLL")
    digest = hashlib.sha256(archive.extract(matches[0])).hexdigest()
    if digest != SQLITE_DLL_SHA256:
        raise ValueError("Embedded SQLite DLL differs from the verified vendor build")
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("prepare").add_argument("destination", type=Path)
    verify = actions.add_parser("verify")
    verify.add_argument("executable", type=Path)
    verify.add_argument("report", type=Path)
    verify.add_argument("--onefile", action="store_true", help="Verify the SQLite DLL embedded in the portable EXE")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.destination)
        return
    payload = subprocess.check_output([args.executable.resolve(), "--runtime-info"], text=True, timeout=90)
    report = json.loads(payload)
    verify_report(report)
    if args.onefile:
        report["embedded_sqlite3_sha256"] = embedded_sqlite_digest(args.executable)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
