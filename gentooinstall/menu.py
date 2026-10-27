"""The guided installer menu: one row per setting, current value on the right."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .config import (ATOM_RE, BOOTLOADERS, DESKTOPS, FILESYSTEMS, GRAPHICS, HOSTNAME_RE,
                     INIT_SYSTEMS, KERNELS, NETWORKS, PROFILE_TYPES, USERNAME_RE, Config,
                     profile_name, stage3_flavor, validate)
from .system import all_disks, human_size, is_uefi
from .tui import Tui

CONFIG_FILE = "gentooinstall_config.json"
CREDS_FILE = "gentooinstall_creds.json"
NOT_SET = "<not set>"


# --------------------------------------------------------------------------- data sources
def list_timezones() -> list[str]:
    zones = {"UTC"}
    for tab in ("/usr/share/zoneinfo/zone1970.tab", "/usr/share/zoneinfo/zone.tab"):
        try:
            for line in Path(tab).read_text().splitlines():
                if line and not line.startswith("#"):
                    zones.add(line.split("\t")[2])
            break
        except (OSError, IndexError):
            continue
    return sorted(zones)


def list_locales() -> list[str]:
    try:
        out = [ln.split()[0] for ln in Path("/usr/share/i18n/SUPPORTED").read_text().splitlines()
               if ln.strip().endswith(" UTF-8")]
        if out:
            return sorted(set(out))
    except (OSError, IndexError):
        pass
    return ["en_US.UTF-8", "en_GB.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8", "es_ES.UTF-8",
            "it_IT.UTF-8", "pt_BR.UTF-8", "ru_RU.UTF-8", "ja_JP.UTF-8", "zh_CN.UTF-8", "hi_IN.UTF-8", "ta_IN.UTF-8"]


def list_keymaps() -> list[str]:
    names: set[str] = set()
    for root in ("/usr/share/keymaps", "/usr/share/kbd/keymaps"):
        for _, _, files in os.walk(root):
            for f in files:
                if f.endswith((".map.gz", ".map")):
                    names.add(f.split(".map")[0])
    return sorted(names) or ["us", "uk", "de", "fr", "es", "it", "dvorak", "colemak", "br-abnt2", "ru", "jp106"]


# --------------------------------------------------------------------------- standalone API helper
def select_disk(disks: list[dict]) -> str | None:
    """Show a disk picker and return the chosen device path (for use as a library)."""
    with Tui() as t:
        return _pick_disk(t, disks)


def _pick_disk(t: Tui, disks: list[dict], current: str = "") -> str | None:
    opts = [(d["path"], f"{d['path']:<14} {human_size(d['size']):>10}   {d['model'] or '-'}") for d in disks]
    return t.choose("Select the target disk", opts, current=current or None,
                    prompt="Pick the disk to install to.")


# --------------------------------------------------------------------------- handlers
def h_init(t: Tui, c: Config) -> None:
    v = t.choose("Init system", list(INIT_SYSTEMS.items()), current=c.init_system,
                 prompt="Selects the stage3 flavour and Portage profile.")
    if v:
        c.init_system = v
        if v != "systemd" and c.network == "systemd-networkd":
            c.network = "dhcpcd"


def h_stage3(t: Tui, c: Config) -> None:
    v = t.choose("Stage3 tarball", [
        (False, f"Minimal      stage3-amd64-{c.init_system}"),
        (True, f"Desktop      stage3-amd64-desktop-{c.init_system}  (desktop-oriented USE flags)")],
        current=c.stage3_desktop, prompt="The init system is chosen under 'Init system'. "
        "The newest tarball is fetched and GPG-verified at install time.")
    if v is not None:
        c.stage3_desktop = v


def h_disk(t: Tui, c: Config) -> None:
    mode = t.choose("Disk configuration", [
        ("auto", "Automatic partitioning (wipes the selected disk)"),
        ("premounted", "Pre-mounted configuration (use what you mounted at /mnt/gentoo)")],
        current=c.disk_mode, prompt="For custom layouts or dual-boot, partition and mount manually, "
        "then choose pre-mounted. On UEFI, mount the ESP at /mnt/gentoo/efi.")
    if not mode:
        return
    c.disk_mode = mode
    if mode == "premounted":
        if t.confirm("Manual layout", "Open a shell now to partition (cfdisk/parted) and mount?\n"
                     "GPT with an EFI System Partition (type EF00, FAT32) is required on UEFI.\n"
                     "Mount root at /mnt/gentoo, the ESP at /mnt/gentoo/efi, then type 'exit'."):
            t.suspend_run(["/bin/bash", "-l"])
    if mode == "auto" or not is_uefi():
        disks = all_disks()
        if not disks:
            t.message("No disks", "No writable disks were found.", error=True)
            return
        d = _pick_disk(t, disks, c.disk)
        if d:
            c.disk = d


def h_fs(t: Tui, c: Config) -> None:
    v = t.choose("Filesystem", [(f, f) for f in FILESYSTEMS], current=c.filesystem)
    if v:
        c.filesystem = v
        if v == "btrfs":
            c.btrfs_subvolumes = t.confirm("Btrfs subvolumes",
                                           "Use the recommended layout (@, @home, @var_log, @snapshots)?", True)


def h_encrypt(t: Tui, c: Config) -> None:
    if not t.confirm("Disk encryption", "Encrypt the root partition with LUKS2?\n"
                     "A separate unencrypted /boot is created automatically when using GRUB.", c.encrypt):
        c.encrypt = False
        c.luks_password = ""
        return
    pw = t.ask_password("LUKS passphrase")
    if pw:
        c.encrypt, c.luks_password = True, pw


def h_swap(t: Tui, c: Config) -> None:
    v = t.ask("Swap partition", "Swap size in GiB (0 = no swap):", str(c.swap_gib),
              validator=lambda s: "" if s.isdigit() and int(s) <= 256 else "Enter a whole number (0-256)")
    if v is not None:
        c.swap_gib = int(v)


def h_boot(t: Tui, c: Config) -> None:
    v = t.choose("Bootloader", list(BOOTLOADERS.items()), current=c.bootloader)
    if v:
        c.bootloader = v
        if v == "grub" and is_uefi():
            c.removable_boot = t.confirm("Removable media path",
                                         "Also install to the fallback path (EFI/BOOT/BOOTX64.EFI)?\n"
                                         "Useful for USB installs or firmware that forgets boot entries.",
                                         c.removable_boot)


def h_kernel(t: Tui, c: Config) -> None:
    v = t.choose("Kernel", [(k, f"{k:<20} {d}") for k, (_, d) in KERNELS.items()], current=c.kernel,
                 prompt="All options use dracut + installkernel for the initramfs.")
    if v:
        c.kernel = v


def h_hostname(t: Tui, c: Config) -> None:
    v = t.ask("Hostname", "System hostname:", c.hostname,
              validator=lambda s: "" if HOSTNAME_RE.match(s) else "Letters, digits and hyphens only")
    if v:
        c.hostname = v


def h_root(t: Tui, c: Config) -> None:
    pw = t.ask_password("Root password")
    if pw:
        c.root_password = pw


def edit_list(t: Tui, title: str, items: list[str], prompt: str, validator=None) -> list[str]:
    items = list(items)
    while True:
        opts = [("__add", "+ Add an entry")] + [(f"i:{i}", x) for i, x in enumerate(items)] + [("__done", "Confirm and exit")]
        ch = t.choose(title, opts, prompt=prompt + "\nSelect an existing entry to remove it.", filterable=False)
        if ch in (None, "__done"):
            return items
        if ch == "__add":
            v = t.ask(title, "New entry:", validator=validator)
            if v:
                items.append(v.strip())
        else:
            i = int(ch[2:])
            if t.confirm("Remove entry", f"Remove '{items[i]}'?"):
                del items[i]


def h_users(t: Tui, c: Config) -> None:
    while True:
        opts = [("__add", "+ Add a user")]
        opts += [(f"u:{u['username']}", f"{u['username']}   (sudo: {'yes' if u.get('sudo') else 'no'})") for u in c.users]
        opts.append(("__done", "Confirm and exit"))
        ch = t.choose("User accounts", opts, prompt="Select an existing user to remove it.", filterable=False)
        if ch in (None, "__done"):
            return
        if ch == "__add":
            name = t.ask("New user", "Username:", validator=lambda s: "" if USERNAME_RE.match(s) and
                         s not in [u["username"] for u in c.users] else "Invalid or duplicate username")
            if not name:
                continue
            pw = t.ask_password(f"Password for {name}")
            if not pw:
                continue
            c.users.append({"username": name, "password": pw,
                            "sudo": t.confirm("Administrator", f"Give {name} sudo (wheel) rights?", True)})
        elif t.confirm("Remove user", f"Remove {ch[2:]}?"):
            c.users = [u for u in c.users if u["username"] != ch[2:]]


def h_profile(t: Tui, c: Config) -> None:
    v = t.choose("Profile", list(PROFILE_TYPES.items()), current=c.profile_type)
    if not v:
        return
    c.profile_type = v
    if v == "desktop":
        d = t.choose("Desktop environment", [(k, d["label"]) for k, d in DESKTOPS.items()],
                     current=c.desktop or None,
                     prompt="Desktop profiles pull in many packages; keep binary packages enabled "
                            "unless you want to compile for hours.")
        c.desktop = d or c.desktop or "plasma"
        if c.audio == "none":
            c.audio = "pipewire"


def h_graphics(t: Tui, c: Config) -> None:
    v = t.choose("Graphics driver", list(GRAPHICS.items()), current=c.graphics)
    if v:
        c.graphics = v


def h_audio(t: Tui, c: Config) -> None:
    v = t.choose("Audio", [("none", "None"), ("pipewire", "PipeWire + WirePlumber")], current=c.audio)
    if v:
        c.audio = v


def h_network(t: Tui, c: Config) -> None:
    opts = [(k, d) for k, d in NETWORKS.items() if k != "systemd-networkd" or c.init_system == "systemd"]
    v = t.choose("Network configuration", opts, current=c.network)
    if v:
        c.network = v


def h_tz(t: Tui, c: Config) -> None:
    v = t.choose("Timezone", [(z, z) for z in list_timezones()], current=c.timezone, prompt="Press / to filter.")
    if v:
        c.timezone = v


def h_locale(t: Tui, c: Config) -> None:
    v = t.choose("System locale", [(z, z) for z in list_locales()], current=c.locale, prompt="Press / to filter.")
    if v:
        c.locale = v


def h_keymap(t: Tui, c: Config) -> None:
    v = t.choose("Console keyboard layout", [(z, z) for z in list_keymaps()], current=c.keymap, prompt="Press / to filter.")
    if v:
        c.keymap = v


def h_ntp(t: Tui, c: Config) -> None:
    c.ntp = t.confirm("Time synchronization", "Enable automatic clock sync (NTP)?", c.ntp)


def h_binpkg(t: Tui, c: Config) -> None:
    c.binpkg = t.confirm("Binary packages",
                         "Use the official Gentoo binary package host where possible?\n"
                         "Much faster; custom USE flags may still trigger local compiles.", c.binpkg)


def h_testing(t: Tui, c: Config) -> None:
    c.testing = t.confirm("Testing branch (~amd64)",
                          "Use unstable keywords system-wide? Binary packages are built for the stable "
                          "branch, so expect a lot of compiling.", c.testing)


def h_mirror(t: Tui, c: Config) -> None:
    v = t.ask("Mirror", "Gentoo distfiles mirror (used for stage3 and sources):", c.mirror,
              validator=lambda s: "" if s.startswith(("http://", "https://")) else "Must start with http(s)://")
    if v:
        c.mirror = v.rstrip("/")


def h_packages(t: Tui, c: Config) -> None:
    c.packages = edit_list(t, "Additional packages", c.packages,
                           "Portage atoms to install at the end, e.g. app-editors/vim",
                           validator=lambda s: "" if ATOM_RE.match(s) else "Not a valid-looking atom (no spaces)")


def h_commands(t: Tui, c: Config) -> None:
    c.custom_commands = edit_list(t, "Custom commands", c.custom_commands,
                                  "Shell commands run inside the new system (chroot) as the last step.")


# --------------------------------------------------------------------------- menu table
def _mask(v: str) -> str:
    return "set" if v else NOT_SET


def entries(c: Config):
    disk = (f"auto: {c.disk}" if c.disk else NOT_SET) if c.disk_mode == "auto" else "pre-mounted (/mnt/gentoo)"
    prof = c.profile_type + (f": {DESKTOPS[c.desktop]['label']}" if c.profile_type == "desktop" and c.desktop in DESKTOPS else "")
    return [
        ("Init system", c.init_system, h_init),
        ("Stage3 tarball", stage3_flavor(c), h_stage3),
        ("Disk configuration", disk, h_disk),
        ("Filesystem", c.filesystem + (" (subvolumes)" if c.filesystem == "btrfs" and c.btrfs_subvolumes else ""), h_fs),
        ("Disk encryption (LUKS)", "enabled" if c.encrypt else "disabled", h_encrypt),
        ("Swap partition", f"{c.swap_gib} GiB" if c.swap_gib else "none", h_swap),
        ("Bootloader", c.bootloader, h_boot),
        ("Kernel", c.kernel, h_kernel),
        ("Hostname", c.hostname, h_hostname),
        ("Root password", _mask(c.root_password), h_root),
        ("User accounts", ", ".join(u["username"] for u in c.users) or "none", h_users),
        ("Profile", prof, h_profile),
        ("Graphics driver", c.graphics, h_graphics),
        ("Audio", c.audio, h_audio),
        ("Network configuration", c.network, h_network),
        ("Timezone", c.timezone, h_tz),
        ("System locale", c.locale, h_locale),
        ("Keyboard layout", c.keymap, h_keymap),
        ("Time synchronization (NTP)", "on" if c.ntp else "off", h_ntp),
        ("Binary packages", "on" if c.binpkg else "off", h_binpkg),
        ("Testing branch (~amd64)", "on" if c.testing else "off", h_testing),
        ("Mirror", c.mirror, h_mirror),
        ("Additional packages", f"{len(c.packages)} selected" if c.packages else "none", h_packages),
        ("Custom commands", f"{len(c.custom_commands)} defined" if c.custom_commands else "none", h_commands),
    ]


def summary(c: Config, uefi: bool) -> str:
    L = [f"Boot mode        : {'UEFI' if uefi else 'BIOS'}",
         f"Stage3           : {stage3_flavor(c)}",
         f"Init / profile   : {c.init_system} / {profile_name(c)}",
         f"Disk             : {'pre-mounted at /mnt/gentoo' if c.disk_mode == 'premounted' else c.disk + '  (WILL BE ERASED)'}",
         f"Filesystem       : {c.filesystem}{' + LUKS2' if c.encrypt else ''}, swap {c.swap_gib} GiB",
         f"Bootloader       : {c.bootloader}",
         f"Kernel           : {KERNELS[c.kernel][0]}",
         f"Hostname         : {c.hostname}",
         f"Locale / TZ / KB : {c.locale} / {c.timezone} / {c.keymap}",
         f"Network          : {c.network}   NTP: {c.ntp}",
         f"Graphics / audio : {c.graphics} / {c.audio}",
         f"Binary packages  : {c.binpkg}   Testing: {c.testing}",
         f"Users            : {', '.join(u['username'] for u in c.users) or 'only root'}",
         f"Extra packages   : {' '.join(c.packages) or '-'}",
         f"Custom commands  : {len(c.custom_commands)}", "",
         "The install downloads a stage3 and can take a long time (hours if packages must be compiled)."]
    return "\n".join(L)


def run_menu(t: Tui, c: Config) -> str:
    """Show the main menu. Returns 'install' or 'abort'."""
    uefi = is_uefi()
    start = None
    while True:
        rows = entries(c)
        opts = [(i, f"{label:<30} {value}") for i, (label, value, _) in enumerate(rows)]
        opts += [("save", "Save configuration"), ("install", "Install"), ("abort", "Abort")]
        ch = t.choose("Gentoo guided installer", opts, start=start, filterable=False,
                      prompt=f"Boot mode: {'UEFI' if uefi else 'BIOS'}   |   "
                             "Configure each entry, then choose Install.",
                      help_text="Up/Down move  Enter edit  Esc abort")
        start = ch
        if ch is None or ch == "abort":
            if t.confirm("Abort", "Quit without installing?"):
                return "abort"
        elif ch == "save":
            creds = t.confirm("Save configuration",
                              f"Write {CONFIG_FILE} to the current directory.\n"
                              f"Also save passwords to {CREDS_FILE} (plain text, mode 600)?")
            c.save(CONFIG_FILE, CREDS_FILE if creds else None)
            t.message("Saved", f"Wrote {os.path.abspath(CONFIG_FILE)}" + (f" and {CREDS_FILE}" if creds else ""))
        elif ch == "install":
            errs = validate(c, uefi)
            if errs:
                t.message("Cannot install yet", "\n".join(f"- {e}" for e in errs), error=True)
                continue
            if not t.pager("Installation summary", summary(c, uefi), "Enter = install   Esc = go back"):
                continue
            if c.disk_mode == "auto" and not t.confirm(
                    f"ERASE {c.disk}?", f"Every partition and all data on {c.disk} will be destroyed.\nContinue?"):
                continue
            return "install"
        else:
            rows[ch][2](t, c)
