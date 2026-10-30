"""Configuration model, option tables and validation for gentooinstall."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

TARGET = "/mnt/gentoo"
PROFILE_BASE = "default/linux/amd64/23.0"

INIT_SYSTEMS = {
    "openrc": "OpenRC (Gentoo default)",
    "systemd": "systemd",
}
FILESYSTEMS = ["ext4", "btrfs", "xfs", "f2fs"]
BOOTLOADERS = {
    "grub": "GRUB (UEFI and BIOS)",
    "systemd-boot": "systemd-boot (UEFI only)",
}
# key -> (package atom, description)
KERNELS = {
    "gentoo-kernel-bin": ("sys-kernel/gentoo-kernel-bin", "Distribution kernel, prebuilt (fastest, recommended)"),
    "gentoo-kernel": ("sys-kernel/gentoo-kernel", "Distribution kernel, compiled locally"),
    "vanilla-kernel": ("sys-kernel/vanilla-kernel", "Mainline kernel.org stable, compiled locally"),
    "gentoo-sources": ("sys-kernel/gentoo-sources", "Custom sources: defconfig build, tune it afterwards (advanced)"),
}
NETWORKS = {
    "dhcpcd": "dhcpcd (simple wired DHCP)",
    "NetworkManager": "NetworkManager (wired + Wi-Fi)",
    "systemd-networkd": "systemd-networkd (systemd only)",
    "none": "None / configure manually",
}
GRAPHICS = {
    "auto": "Auto-detect",
    "intel": "Intel (open source)",
    "amd": "AMD (amdgpu, open source)",
    "nvidia": "NVIDIA (proprietary)",
    "nouveau": "NVIDIA (nouveau, open source)",
    "none": "None",
}
VIDEO_CARDS = {
    "intel": "intel",
    "amd": "amdgpu radeonsi",
    "nvidia": "nvidia",
    "nouveau": "nouveau",
    "vmware": "vmware",
    "virtualbox": "virtualbox",
    "virtio": "virgl",
}
# Shared by every X11 desktop (Arch: xorg-server, xorg-xinit, xorg-xrandr, libx11, libxcb)
XORG_COMMON = ["x11-base/xorg-server", "x11-apps/xinit", "x11-apps/xrandr",
               "x11-libs/libX11", "x11-libs/libxcb"]
# key -> label and Gentoo atoms for the packages archinstall would install
DESKTOPS = {
    "i3": dict(label="i3-wm", pkgs=[
        "x11-wm/i3", "x11-misc/i3status", "x11-misc/dmenu", "x11-misc/i3lock"] + XORG_COMMON),
    "xfce": dict(label="Xfce", pkgs=[
        "xfce-base/xfce4-meta", "xfce-base/xfce4-panel", "xfce-base/xfce4-session",
        "xfce-base/xfce4-settings", "xfce-base/xfdesktop", "xfce-base/xfwm4", "xfce-base/thunar",
        "x11-terms/xfce4-terminal", "app-editors/mousepad"] + XORG_COMMON),
}
GREETERS = {
    "sddm": "SDDM",
    "ly": "Ly (TUI greeter; from the GURU overlay, built locally)",
}
PROFILE_TYPES = {
    "minimal": "Minimal (base system only)",
    "server": "Server (headless: sshd, cron)",
    "desktop": "Desktop environment",
}

SECRET_KEYS = ("root_password", "luks_password")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
ATOM_RE = re.compile(r"^[A-Za-z0-9_@+./:=<>~*\[\],-]+$")


def stage3_flavor(cfg: "Config") -> str:
    return f"stage3-amd64-{'desktop-' if cfg.stage3_desktop else ''}{cfg.init_system}"


def profile_name(cfg: "Config") -> str:
    parts = [PROFILE_BASE]
    if cfg.profile_type == "desktop":
        parts.append("desktop")
    if cfg.init_system == "systemd":
        parts.append("systemd")
    return "/".join(parts)


@dataclass
class Config:
    init_system: str = "openrc"
    stage3_desktop: bool = False     # use the stage3-amd64-desktop-* tarball
    disk_mode: str = "default"       # default | manual | premounted
    disk: str = ""
    filesystem: str = "ext4"
    btrfs_subvolumes: bool = True
    encrypt: bool = False
    swap_gib: int = 0
    separate_home: bool = False
    partitions: list = field(default_factory=list)   # manual layout (disklayout.Part dicts)
    bootloader: str = "grub"
    removable_boot: bool = False
    kernel: str = "gentoo-kernel-bin"
    hostname: str = "gentoo"
    timezone: str = "UTC"
    locale: str = "en_US.UTF-8"
    keymap: str = "us"
    profile_type: str = "minimal"    # minimal | server | desktop
    desktop: str = ""
    greeter: str = "sddm"
    graphics: str = "auto"
    audio: str = "none"              # none | pipewire
    network: str = "dhcpcd"
    ntp: bool = True
    binpkg: bool = True
    testing: bool = False            # ACCEPT_KEYWORDS="~amd64"
    mirror: str = "https://distfiles.gentoo.org"
    verify_stage3: bool = True
    users: list = field(default_factory=list)     # {"username","sudo","password"}
    packages: list = field(default_factory=list)
    custom_commands: list = field(default_factory=list)
    root_password: str = ""
    luks_password: str = ""

    # ---- (de)serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        for k in SECRET_KEYS:
            d.pop(k, None)
        d["users"] = [{k: v for k, v in u.items() if k != "password"} for u in self.users]
        return d

    def creds_dict(self) -> dict:
        return {
            "root_password": self.root_password,
            "luks_password": self.luks_password,
            "users": [{"username": u["username"], "password": u.get("password", "")} for u in self.users],
        }

    def save(self, cfg_path: str, creds_path: str | None = None) -> None:
        Path(cfg_path).write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        if creds_path:
            fd = os.open(creds_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(self.creds_dict(), fh, indent=2)
                fh.write("\n")

    @classmethod
    def load(cls, cfg_path: str, creds_path: str | None = None) -> "Config":
        data = json.loads(Path(cfg_path).read_text())
        known = {f.name for f in fields(cls)} - set(SECRET_KEYS)
        if data.get("disk_mode") == "auto":      # name used by older configs
            data["disk_mode"] = "default"
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg.users = [dict(u, password=u.get("password", "")) for u in cfg.users]
        if creds_path:
            creds = json.loads(Path(creds_path).read_text())
            cfg.root_password = creds.get("root_password", "")
            cfg.luks_password = creds.get("luks_password", "")
            pw = {u["username"]: u.get("password", "") for u in creds.get("users", [])}
            for u in cfg.users:
                u["password"] = pw.get(u["username"], u.get("password", ""))
        return cfg


def validate(cfg: Config, uefi: bool) -> list[str]:
    errs: list[str] = []
    if cfg.init_system not in INIT_SYSTEMS:
        errs.append(f"Unknown init system '{cfg.init_system}'")
    if cfg.disk_mode in ("default", "manual") and not cfg.disk:
        errs.append("No target disk selected")
    if cfg.disk_mode == "manual":
        from .disklayout import Part, validate_layout
        errs += validate_layout([Part.from_dict(d) for d in cfg.partitions], uefi=uefi,
                                bootloader=cfg.bootloader, encrypt=cfg.encrypt)
    if cfg.disk_mode == "premounted" and not uefi and not cfg.disk:
        errs.append("BIOS boot with a pre-mounted layout needs a disk for the bootloader")
    if cfg.filesystem not in FILESYSTEMS:
        errs.append(f"Unsupported filesystem '{cfg.filesystem}'")
    if cfg.bootloader not in BOOTLOADERS:
        errs.append(f"Unknown bootloader '{cfg.bootloader}'")
    if cfg.bootloader == "systemd-boot" and not uefi:
        errs.append("systemd-boot requires UEFI, but this system booted in BIOS mode")
    if cfg.kernel not in KERNELS:
        errs.append(f"Unknown kernel '{cfg.kernel}'")
    if cfg.network == "systemd-networkd" and cfg.init_system != "systemd":
        errs.append("systemd-networkd requires the systemd init system")
    if cfg.encrypt and cfg.disk_mode != "premounted" and not cfg.luks_password:
        errs.append("Disk encryption is enabled but no passphrase is set")
    if not cfg.root_password:
        errs.append("Root password is not set")
    if not HOSTNAME_RE.match(cfg.hostname):
        errs.append("Invalid hostname")
    if cfg.profile_type == "desktop" and cfg.desktop not in DESKTOPS:
        errs.append("Desktop profile selected but no desktop environment chosen")
    if cfg.profile_type == "desktop" and cfg.greeter not in GREETERS:
        errs.append("Desktop profile selected but no greeter chosen")
    for u in cfg.users:
        if not USERNAME_RE.match(u.get("username", "")):
            errs.append(f"Invalid username '{u.get('username')}'")
        if not u.get("password"):
            errs.append(f"User '{u.get('username')}' has no password")
    for p in cfg.packages:
        if not ATOM_RE.match(p):
            errs.append(f"Suspicious package atom '{p}'")
    return errs
