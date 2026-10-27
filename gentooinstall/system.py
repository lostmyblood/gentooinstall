"""Low-level helpers: logging, command execution, hardware probing, downloads."""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path

log = logging.getLogger("gentooinstall")
log.addHandler(logging.NullHandler())
log.propagate = False
LOG_DIR = Path("/var/log/gentooinstall")


class InstallError(RuntimeError):
    pass


# --- colour-coded status output (muted blue accents, white text, no harsh colours) ---
BLUE, WHITE, DIM, BOLD, RST = "\033[38;5;67m", "\033[97m", "\033[2m", "\033[1m", "\033[0m"


def say(kind: str, msg: str) -> None:
    """kind: step | ok | info | warn | err"""
    tag = {"step": f"{BLUE}{BOLD}==>{RST}", "ok": f"{BLUE}[ ok ]{RST}", "info": f"{BLUE}[ .. ]{RST}",
           "warn": f"{BOLD}{WHITE}[warn]{RST}", "err": f"{BOLD}{WHITE}[FAIL]{RST}"}[kind]
    print(f"{tag} {BOLD + WHITE if kind in ('step', 'err') else WHITE}{msg}{RST}", flush=True)
    log.log({"err": logging.ERROR, "warn": logging.WARNING}.get(kind, logging.INFO), "%s: %s", kind, msg)


def check_environment() -> list[tuple[str, bool, str]]:
    """Safety checks for the live environment: [(name, passed, detail)].

    Only 'passed' False on a hard blocker; BIOS boot is reported as a warning detail but passes.
    """
    import platform
    import socket
    res = []
    arch = platform.machine()
    res.append(("Architecture is x86_64", arch == "x86_64", arch))
    res.append(("Running as root", os.geteuid() == 0, f"uid {os.geteuid()}"))
    uefi = is_uefi()
    res.append(("Boot mode", True, "UEFI" if uefi else "BIOS/legacy (GRUB + GPT bios_boot only; systemd-boot unavailable)"))
    try:
        socket.create_connection(("distfiles.gentoo.org", 443), timeout=6).close()
        res.append(("Internet connectivity", True, "distfiles.gentoo.org reachable"))
    except OSError as e:
        res.append(("Internet connectivity", False, f"{e} - configure networking first (net-setup / nmtui / dhcpcd)"))
    return res


def setup_logging() -> Path | None:
    """Log to /var/log/gentooinstall/install.log (falls back to /tmp when not root)."""
    log.setLevel(logging.DEBUG)
    for d in (LOG_DIR, Path("/tmp/gentooinstall")):
        try:
            d.mkdir(parents=True, exist_ok=True)
            path = d / "install.log"
            handler = logging.FileHandler(path)
        except OSError:
            continue
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        return path
    return None


# --------------------------------------------------------------------------- hardware
def human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def all_disks() -> list[dict]:
    """Return writable whole disks: [{'path','size','model'}]."""
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-d", "-b", "-o", "NAME,SIZE,MODEL,TYPE,RO"],
            capture_output=True, text=True, check=True).stdout
        devices = json.loads(out)["blockdevices"]
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError):
        return []
    disks = []
    for d in devices:
        if d.get("type") != "disk" or d.get("ro") in (True, 1, "1"):
            continue
        if d["name"].startswith(("zram", "ram", "loop", "sr", "fd")):
            continue
        disks.append({"path": "/dev/" + d["name"], "size": int(d.get("size") or 0),
                      "model": (d.get("model") or "").strip()})
    return disks


def detect_gpus() -> set[str]:
    found: set[str] = set()
    try:
        out = subprocess.run(["lspci", "-mm"], capture_output=True, text=True).stdout
    except OSError:
        return found
    for line in out.splitlines():
        low = line.lower()
        if not any(k in low for k in ("vga", "3d controller", "display controller")):
            continue
        if "nvidia" in low:
            found.add("nvidia")
        elif "advanced micro" in low or "amd" in low or "ati " in low:
            found.add("amd")
        elif "intel" in low:
            found.add("intel")
    return found


def mem_gib() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 2.0


def is_uefi() -> bool:
    return os.path.isdir("/sys/firmware/efi")


# --------------------------------------------------------------------------- network
def fetch_text(url: str) -> str:
    log.info("GET %s", url)
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def download(url: str, dest: str) -> None:
    log.info("download %s -> %s", url, dest)
    with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                sys.stdout.write(f"\r  {done * 100 // total:3d}%  {done >> 20}/{total >> 20} MiB")
                sys.stdout.flush()
    print()


# --------------------------------------------------------------------------- runner
class Runner:
    """Runs commands (optionally inside the target chroot) and writes target files.

    In dry-run mode nothing is executed or written; actions are only printed/logged.
    """

    def __init__(self, target: str, dry_run: bool = False):
        self.target = target
        self.dry = dry_run

    def run(self, cmd, *, chroot: bool = False, check: bool = True, stdin: str | None = None,
            quiet: bool = False, env: dict | None = None) -> str:
        if isinstance(cmd, (list, tuple)):
            display = shlex.join(cmd)
            inner = display
            argv = list(cmd)
        else:
            display = inner = cmd
            argv = ["bash", "-c", cmd]
        if chroot:
            argv = ["chroot", self.target, "/bin/bash", "-lc", inner]
            display = f"(chroot) {display}"
        log.info("$ %s%s", display, "  [stdin hidden]" if stdin is not None else "")
        if self.dry:
            print(f"\033[2m[dry-run] $ {display}\033[0m")
            return ""
        full_env = dict(os.environ, **(env or {}))
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=full_env)
        if stdin is not None:
            assert proc.stdin
            proc.stdin.write(stdin)
            proc.stdin.close()
        lines = []
        assert proc.stdout
        for line in proc.stdout:
            lines.append(line)
            log.debug(line.rstrip())
            if not quiet:
                sys.stdout.write(line)
                sys.stdout.flush()
        rc = proc.wait()
        if rc != 0 and check:
            raise InstallError(f"Command failed ({rc}): {display}")
        return "".join(lines)

    def write(self, path: str, content: str, mode: int = 0o644, append: bool = False) -> None:
        """Write `path` (absolute, relative to the target root)."""
        real = os.path.join(self.target, path.lstrip("/"))
        log.info("%s %s", "append" if append else "write", real)
        if self.dry:
            print(f"\033[2m[dry-run] {'append' if append else 'write'} {real}\033[0m")
            return
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "a" if append else "w") as fh:
            fh.write(content)
        os.chmod(real, mode)
