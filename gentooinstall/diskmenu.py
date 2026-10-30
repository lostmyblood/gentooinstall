"""Disk configuration UI modelled on archinstall's disk menus."""
from __future__ import annotations

from .config import FILESYSTEMS, Config
from .disklayout import (COLS, MOUNT_CHOICES, PART_FS, Part, default_layout, disk_size_mib,
                         free_regions, fmt_size, load_existing, parse_size, row_text,
                         table_text, validate_layout)
from .system import all_disks, human_size, is_uefi
from .tui import Tui

DISK_TYPES = [
    ("default", "Use a best-effort default partition layout"),
    ("manual", "Manual Partitioning"),
    ("premounted", "Pre-mounted configuration"),
]


def pick_disk(t: Tui, current: str = "") -> str | None:
    disks = all_disks()
    if not disks:
        t.message("No disks", "No writable disks were detected. A disk is required to install Gentoo.", level="error")
        return None
    opts = [(d["path"], f"{d['path']:<14} {human_size(d['size']):>10}  {d['model'] or '-'}") for d in disks]

    def preview(path) -> str:
        parts, label = load_existing(path)
        if not parts:
            return "No partitions found" + (f" ({label} table)" if label else "")
        return table_text(parts, path, disk_size_mib(path), False)

    return t.choose("Select disks for the installation", opts, current=current or None,
                    preview=preview, preview_header="Partitions")


def h_disk(t: Tui, c: Config) -> None:
    mode = t.choose("Select a disk configuration", DISK_TYPES, current=c.disk_mode)
    if mode is None:
        return
    if mode == "default":
        _default_wizard(t, c)
    elif mode == "manual":
        _manual(t, c)
    else:
        _premounted(t, c)


# ----------------------------------------------------------------------------- best-effort
def _default_wizard(t: Tui, c: Config) -> None:
    disk = pick_disk(t, c.disk)
    if not disk:
        return
    fs = t.choose("Select main filesystem", [(f, f) for f in FILESYSTEMS], current=c.filesystem)
    if not fs:
        return
    subvols = c.btrfs_subvolumes
    if fs == "btrfs":
        subvols = t.confirm("Btrfs", "Would you like to use BTRFS subvolumes with a default structure?\n"
                            "(@, @home, @var_log, @snapshots)", True)
    home = False
    if disk_size_mib(disk) >= 64 * 1024 and not c.encrypt:
        home = t.confirm("Separate /home", "Would you like to create a separate partition for /home?", False)
    c.disk_mode, c.disk, c.filesystem = "default", disk, fs
    c.btrfs_subvolumes, c.separate_home, c.partitions = subvols, home, []


# ----------------------------------------------------------------------------- pre-mounted
def _premounted(t: Tui, c: Config) -> None:
    if t.confirm("Pre-mounted configuration",
                 "You will use whatever drive-setup is mounted at /mnt/gentoo.\n"
                 "WARNING: gentooinstall won't check the suitability of this setup.\n"
                 "On UEFI the EFI system partition must be mounted at /mnt/gentoo/efi.\n\n"
                 "Open a shell now to partition and mount (type 'exit' to return)?"):
        t.suspend_run(["/bin/bash", "-l"])
    c.disk_mode, c.partitions = "premounted", []
    if not is_uefi():
        d = pick_disk(t, c.disk)
        if d:
            c.disk = d


# ----------------------------------------------------------------------------- manual
def _manual(t: Tui, c: Config) -> None:
    disk = pick_disk(t, c.disk)
    if not disk:
        return
    uefi = is_uefi()
    mib = disk_size_mib(disk) or 20 * 1024
    if c.disk_mode == "manual" and c.disk == disk and c.partitions:
        parts = [Part.from_dict(d) for d in c.partitions]
    else:
        parts, label = load_existing(disk)
        if label and label != "gpt":
            t.message("Partition table", f"{disk} uses a '{label}' table. Manual editing needs GPT, so the "
                      "disk will be wiped and re-labelled when you install.", level="warning")
            for p in parts:
                p.delete = True
    result = partition_manager(t, c, disk, mib, parts, uefi)
    if result is None:
        return
    c.disk_mode, c.disk = "manual", disk
    c.partitions = [p.to_dict() for p in result]
    root = next((p for p in result if p.mount == "/" and not p.delete), None)
    if root and root.fs in FILESYSTEMS:
        c.filesystem = root.fs
        if root.fs == "btrfs":
            c.btrfs_subvolumes = t.confirm("Btrfs", "Would you like to use BTRFS subvolumes with a default structure?", True)


def _mount_dialog(t: Tui, p: Part) -> None:
    opts = [(m, m) for m in MOUNT_CHOICES] + [("", "(no mountpoint)"), ("*", "Custom path...")]
    m = t.choose("Enter a mountpoint", opts, current=p.mount,
                 prompt="Partition mount-points are relative to inside the installation; "
                        "the ESP is /efi, boot would be /boot as an example.")
    if m is None:
        return
    if m == "*":
        m = t.ask("Custom mountpoint", "Absolute path, e.g. /srv", validator=lambda s: "" if s.startswith("/") and " " not in s else "Must start with / and contain no spaces")
        if m is None:
            return
    p.mount = m
    if m == "/efi" and p.fs == "fat32":
        p.esp = True


def _fs_dialog(t: Tui, p: Part, uefi: bool) -> None:
    opts = [(f, f) for f in PART_FS]
    if not uefi:
        opts.append(("bios_boot", "bios_boot (1 MiB, for GRUB on BIOS)"))
    f = t.choose("Change filesystem", opts, current=p.fs)
    if f:
        p.fs = f
        p.fmt = True
        if f in ("swap", "bios_boot"):
            p.mount = ""
        if f != "fat32":
            p.esp = False


def _partition_actions(t: Tui, c: Config, p: Part, parts: list[Part], uefi: bool) -> None:
    del_label = ("Restore partition" if p.delete else "Delete partition")
    acts = [("mount", "Assign mountpoint"),
            ("fmt", "Mark/Unmark to be formatted (wipes data)"),
            ("esp", "Mark/Unmark as ESP"),
            ("fs", "Change filesystem")]
    if p.mount == "/":
        acts.append(("luks", f"{'Disable' if c.encrypt else 'Enable'} disk encryption (LUKS2)"))
    acts.append(("del", del_label))
    a = t.choose(f"Partition: {p.node or 'new partition'}", acts,
                 prompt=f"{p.status}  {p.fs}  {fmt_size(p.size) if p.size else 'rest of disk'}  mount: {p.mount or '-'}")
    if a == "mount":
        _mount_dialog(t, p)
    elif a == "fmt":
        if p.existing:
            p.fmt = not p.fmt
        else:
            t.message("Already formatted", "New partitions are always formatted.", level="info")
    elif a == "esp":
        p.esp = not p.esp
        if p.esp:
            p.fs = "fat32"
    elif a == "fs":
        _fs_dialog(t, p, uefi)
    elif a == "luks":
        if c.encrypt:
            c.encrypt, c.luks_password = False, ""
        else:
            pw = t.ask_password("LUKS passphrase")
            if pw:
                c.encrypt, c.luks_password = True, pw
    elif a == "del":
        if p.existing:
            p.delete = not p.delete
            if p.delete:
                p.mount = ""
        else:
            parts.remove(p)


def _create_dialog(t: Tui, parts: list[Part], start: int, free: int, uefi: bool) -> None:
    prompt = (f"Selected free space segment: start {fmt_size(start)}, available {fmt_size(free)}.\n"
              "Values can be suffixed with a unit: %, MiB, GiB, TiB. Leave empty to use all of it.")
    txt = t.ask("Enter a size", prompt, "", validator=lambda s: "" if parse_size(s, free) is not None else "Invalid size")
    if txt is None:
        return
    size = parse_size(txt, free)
    if size == free:
        size = 0                                   # rest of the disk
    p = Part(start=start, size=size, fs="ext4")
    _fs_dialog(t, p, uefi)
    if p.fs in ("swap", "bios_boot"):
        if p.fs == "bios_boot" and size == 0:
            p.size = 1
    else:
        if not any(q.mount == "/" and not q.delete for q in parts):
            p.mount = "/"                  # first data partition: preselect the root mountpoint
        _mount_dialog(t, p)
    parts.append(p)


def partition_manager(t: Tui, c: Config, disk: str, mib: int, parts: list[Part], uefi: bool):
    """Interactive editor. Returns the edited list, or None if cancelled."""
    while True:
        live_existing = any(p.existing and not p.delete for p in parts)
        ordered = sorted(parts, key=lambda p: p.start)
        free = free_regions(parts, mib)
        rows: list[tuple] = []
        entries = [("p", p, p.start) for p in ordered] + [("f", r, r[0]) for r in free if r[1] >= 1]
        for kind, obj, _ in sorted(entries, key=lambda e: e[2]):
            if kind == "p":
                rows.append((("p", id(obj)), row_text(obj, disk, ordered.index(obj) + 1, mib, c.encrypt)))
            else:
                rows.append((("f", obj), f"{'free':<9}{'-':<16}{fmt_size(obj[0]):>11} {fmt_size(obj[1]):>10}"))
        rows += [(("a", "suggest"), "Suggest partition layout"),
                 (("a", "remove"), "Remove all newly added partitions"),
                 (("a", "wipe"), "Delete all partitions (wipe the disk)"),
                 (("a", "done"), "Confirm and exit")]
        header = (f"Partition management: {disk}\nTotal length: {fmt_size(mib)}\nWipe: {not live_existing}")
        choice = t.choose("Partitioning", rows, prompt=header, col_header=COLS, filterable=False)
        if choice is None:
            if t.confirm("Discard changes", "Leave the partition editor without saving your changes?"):
                return None
            continue
        kind, key = choice
        if kind == "p":
            p = next(x for x in parts if id(x) == key)
            _partition_actions(t, c, p, parts, uefi)
        elif kind == "f":
            _create_dialog(t, parts, key[0], key[1], uefi)
        elif key == "suggest":
            if t.confirm("Suggest partition layout", "This replaces the whole layout and wipes the disk. Continue?"):
                for p in parts:
                    if p.existing:
                        p.delete, p.mount = True, ""
                parts[:] = [p for p in parts if p.existing] + default_layout(
                    mib, uefi=uefi, bootloader=c.bootloader, encrypt=c.encrypt, swap_gib=c.swap_gib,
                    filesystem=c.filesystem, separate_home=c.separate_home and not c.encrypt)
        elif key == "remove":
            if t.confirm("Remove", "This will remove all newly added partitions, continue?"):
                parts[:] = [p for p in parts if p.existing]
        elif key == "wipe":
            if t.confirm("Wipe", f"Mark every partition on {disk} for deletion?"):
                for p in parts:
                    if p.existing:
                        p.delete, p.mount = True, ""
                parts[:] = [p for p in parts if p.existing]
        elif key == "done":
            errs = validate_layout(parts, uefi=uefi, bootloader=c.bootloader, encrypt=c.encrypt)
            if errs:
                t.message("Layout problems", "\n".join(f"- {e}" for e in errs), level="error")
                continue
            return parts
