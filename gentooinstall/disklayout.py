"""Disk layout model shared by the TUI (preview / partition editor) and the installer.

All offsets and sizes are in MiB (1 MiB aligned). size == 0 means "use the rest of the disk".
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field

GUID_ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
GUID_BIOS = "21686148-6449-6e6f-744e-656564454649"
GUID_SWAP = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"

FILESYSTEMS = ["ext4", "btrfs", "xfs", "f2fs"]
PART_FS = ["ext4", "btrfs", "xfs", "f2fs", "fat32", "swap"]
MOUNT_CHOICES = ["/", "/boot", "/efi", "/home", "/var", "/opt"]
MIB = 1024 * 1024


@dataclass
class Part:
    start: int = 0              # MiB
    size: int = 0               # MiB, 0 = rest of the disk
    fs: str = "ext4"            # ext4 btrfs xfs f2fs fat32 swap bios_boot
    mount: str = ""
    esp: bool = False
    existing: bool = False      # already on disk
    num: int = 0                # partition number if existing
    node: str = ""              # /dev node (existing, or assigned after creation)
    fmt: bool = True            # existing partitions: wipe and re-create the filesystem?
    delete: bool = False        # existing partitions: remove from the table
    old_fs: str = ""            # existing partitions: filesystem found on disk

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Part":
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def status(self) -> str:
        if self.delete:
            return "delete"
        if not self.existing:
            return "create"
        return "format" if self.fmt else "existing"

    @property
    def end(self) -> int:
        return self.start + self.size


# ----------------------------------------------------------------------------- probing
def disk_size_mib(disk: str) -> int:
    try:
        out = subprocess.run(["lsblk", "-b", "-d", "-n", "-o", "SIZE", disk],
                             capture_output=True, text=True, check=True).stdout.strip()
        return int(out) // MIB
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0


def load_existing(disk: str) -> tuple[list[Part], str]:
    """Read the partition table. Returns (partitions, label) ; label '' if none/unreadable."""
    try:
        out = subprocess.run(["sfdisk", "-J", disk], capture_output=True, text=True)
        if out.returncode != 0:
            return [], ""
        table = json.loads(out.stdout)["partitiontable"]
    except (OSError, ValueError, KeyError):
        return [], ""
    ss = int(table.get("sectorsize", 512))
    fstypes: dict[str, str] = {}
    try:
        lb = json.loads(subprocess.run(["lsblk", "-J", "-o", "PATH,FSTYPE", disk],
                                       capture_output=True, text=True).stdout)
        for dev in lb.get("blockdevices", []):
            for ch in dev.get("children", []) or []:
                fstypes[ch["path"]] = ch.get("fstype") or ""
    except (OSError, ValueError, KeyError):
        pass
    parts = []
    for p in table.get("partitions", []):
        node = p["node"]
        guid = str(p.get("type", "")).lower()
        fs = fstypes.get(node, "")
        fs = {"vfat": "fat32", "swap": "swap"}.get(fs, fs)
        if guid == GUID_BIOS:
            fs = "bios_boot"
        num = int(re.search(r"(\d+)$", node).group(1))
        start = p["start"] * ss // MIB
        size = max(1, -(-p["size"] * ss // MIB))
        parts.append(Part(start=start, size=size, fs=fs or "unknown", existing=True, num=num,
                          node=node, fmt=False, esp=(guid == GUID_ESP), old_fs=fs))
    return parts, table.get("label", "")


# ----------------------------------------------------------------------------- default layout
def default_layout(disk_mib: int, *, uefi: bool, bootloader: str, encrypt: bool, swap_gib: int,
                   filesystem: str, separate_home: bool) -> list[Part]:
    """archinstall-style best-effort layout, adapted to Gentoo (ESP mounted at /efi)."""
    parts: list[Part] = []
    pos = 1

    def add(size: int, fs: str, mount: str = "", **kw) -> None:
        nonlocal pos
        parts.append(Part(start=pos, size=size, fs=fs, mount=mount, **kw))
        pos += size

    if uefi:
        add(1024, "fat32", "/efi", esp=True)
    elif bootloader == "grub":
        add(1, "bios_boot")
    if encrypt and bootloader == "grub":
        add(1024, "ext4", "/boot")           # GRUB cannot unlock LUKS2: keep /boot plain
    if swap_gib > 0:
        add(swap_gib * 1024, "swap")
    if separate_home and not encrypt and disk_mib >= 64 * 1024:
        gib = disk_mib // 1024
        root = 50 if gib > 500 else 32 if gib < 320 else gib // 10      # archinstall's rule
        add(root * 1024, filesystem, "/")
        add(0, filesystem, "/home")
    else:
        add(0, filesystem, "/")
    return parts


# ----------------------------------------------------------------------------- free space
def free_regions(parts: list[Part], disk_mib: int) -> list[tuple[int, int]]:
    """Free (start, size) regions in MiB, keeping 1 MiB at both ends for GPT metadata."""
    limit = max(1, disk_mib - 1)
    # size 0 means "rest of the disk", so such a partition uses everything after its start
    used = sorted((p.start, p.end if p.size else limit) for p in parts if not p.delete)
    regions, cursor = [], 1
    for s, e in used:
        if s > cursor:
            regions.append((cursor, s - cursor))
        cursor = max(cursor, e)
    if limit > cursor:
        regions.append((cursor, limit - cursor))
    return regions


def parse_size(text: str, max_mib: int) -> int | None:
    """'20GiB', '512M', '50%', 'rest' -> MiB (0 = rest). None if invalid or too big."""
    t = text.strip().lower().replace(" ", "")
    if t in ("", "rest", "max", "100%"):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(%|b|k|kb|kib|m|mb|mib|g|gb|gib|t|tb|tib)?", t)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2) or "mib"
    mult = {"%": max_mib / 100, "b": 1 / MIB, "k": 1 / 1024, "kb": 1 / 1024, "kib": 1 / 1024, "m": 1,
            "mb": 1, "mib": 1, "g": 1024, "gb": 1024, "gib": 1024, "t": 1024 ** 2, "tb": 1024 ** 2,
            "tib": 1024 ** 2}[unit]
    mib = int(n * mult)
    return mib if 1 <= mib <= max_mib else None


def fmt_size(mib: int) -> str:
    if mib >= 1024 * 1024:
        return f"{mib / 1024 / 1024:.1f} TiB"
    if mib >= 1024:
        return f"{mib / 1024:.1f} GiB"
    return f"{mib} MiB"


# ----------------------------------------------------------------------------- rendering
COLS = f"{'Status':<9}{'Device':<16}{'Start':>11} {'Size':>10}  {'FS':<9}{'Mount':<7}Flags"


def row_text(p: Part, disk: str, idx: int, disk_mib: int, encrypt: bool) -> str:
    dev = p.node or (f"{disk}*{idx}" if not p.existing else "?")
    size = p.size or max(0, disk_mib - 1 - p.start)
    flags = []
    if p.esp:
        flags.append("boot,esp")
    if p.fs == "bios_boot":
        flags.append("bios_grub")
    if encrypt and p.mount == "/" and not p.delete:
        flags.append("luks")
    fs = p.fs if (p.fmt or not p.existing) else f"{p.fs}"
    return (f"{p.status:<9}{dev:<16}{fmt_size(p.start):>11} {fmt_size(size):>10}  "
            f"{fs:<9}{p.mount or '-':<7}{','.join(flags)}")


def table_text(parts: list[Part], disk: str, disk_mib: int, encrypt: bool) -> str:
    if not parts:
        return "(no partitions)"
    ordered = sorted(parts, key=lambda p: p.start)
    return "\n".join([COLS] + [row_text(p, disk, i + 1, disk_mib, encrypt) for i, p in enumerate(ordered)])


# ----------------------------------------------------------------------------- validation
def validate_layout(parts: list[Part], *, uefi: bool, bootloader: str, encrypt: bool) -> list[str]:
    live = [p for p in parts if not p.delete]
    errs: list[str] = []
    mounts = [p.mount for p in live if p.mount]
    for m in set(mounts):
        if mounts.count(m) > 1:
            errs.append(f"Mountpoint {m} is assigned to more than one partition")
    roots = [p for p in live if p.mount == "/"]
    if len(roots) != 1:
        errs.append("Exactly one partition must be mounted at /")
    elif roots[0].fs in ("swap", "fat32", "bios_boot", "unknown"):
        errs.append(f"The root partition cannot use '{roots[0].fs}'")
    for p in live:
        if p.mount and p.fs in ("swap", "bios_boot"):
            errs.append(f"{p.fs} partitions cannot have a mountpoint")
        if p.mount and p.fs == "unknown" and not p.fmt:
            errs.append(f"{p.node}: unknown filesystem - mark it to be formatted or pick a filesystem")
        if p.mount and not p.mount.startswith("/"):
            errs.append(f"Invalid mountpoint '{p.mount}'")
    if uefi:
        esp = [p for p in live if p.esp and p.mount == "/efi" and p.fs == "fat32"]
        if not esp:
            errs.append("UEFI needs a FAT32 EFI system partition (ESP) mounted at /efi")
    elif bootloader == "grub" and not any(p.fs == "bios_boot" for p in live):
        errs.append("BIOS boot with GRUB on GPT needs a 1 MiB bios_boot partition")
    if encrypt and bootloader == "grub" and not any(p.mount == "/boot" for p in live):
        errs.append("LUKS root with GRUB needs a separate unencrypted /boot partition")
    if encrypt and roots and roots[0].existing and not roots[0].fmt:
        errs.append("An encrypted root partition must be formatted")
    return errs


# ----------------------------------------------------------------------------- config glue
def layout_for(cfg, uefi: bool) -> list[Part]:
    """The partition list implied by a Config (default layout is generated, manual is stored)."""
    if cfg.disk_mode == "manual":
        return [Part.from_dict(d) for d in cfg.partitions]
    if cfg.disk_mode == "default" and cfg.disk:
        return default_layout(disk_size_mib(cfg.disk), uefi=uefi, bootloader=cfg.bootloader,
                              encrypt=cfg.encrypt, swap_gib=cfg.swap_gib, filesystem=cfg.filesystem,
                              separate_home=cfg.separate_home)
    return []


def describe(cfg, uefi: bool) -> str:
    """Preview text for the main menu's right-hand pane (like archinstall's disk preview)."""
    names = {"default": "Use a best-effort default partition layout",
             "manual": "Manual Partitioning", "premounted": "Pre-mounted configuration"}
    out = [f"Configuration type: {names.get(cfg.disk_mode, cfg.disk_mode)}"]
    if cfg.disk_mode == "premounted":
        out.append("Mountpoint: /mnt/gentoo")
        out.append("You will use whatever drive-setup is mounted at that directory.")
        return "\n".join(out)
    if not cfg.disk:
        return "\n".join(out + ["", "No disk selected"])
    mib = disk_size_mib(cfg.disk)
    parts = layout_for(cfg, uefi)
    wipe = not any(p.existing and not p.delete for p in parts)
    out += [f"Device: {cfg.disk}" + (f"  ({fmt_size(mib)})" if mib else ""), f"Wipe: {wipe}",
            f"Disk encryption: {'LUKS2 on /' if cfg.encrypt else 'none'}"]
    root = next((p for p in parts if p.mount == "/" and not p.delete), None)
    if root and root.fs == "btrfs" and cfg.btrfs_subvolumes:
        out.append("Btrfs subvolumes: @, @home, @var_log, @snapshots")
    out += ["", table_text(parts, cfg.disk, mib, cfg.encrypt)]
    return "\n".join(out)
