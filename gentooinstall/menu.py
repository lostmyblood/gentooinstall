"""The guided installer menu: archinstall-style list on the left, preview of the focused item on the right."""
from __future__ import annotations

import os
from pathlib import Path

from .config import (ATOM_RE, BOOTLOADERS, DESKTOPS, GREETERS, GRAPHICS, HOSTNAME_RE, INIT_SYSTEMS,
                     KERNELS, NETWORKS, PROFILE_TYPES, USERNAME_RE, Config, profile_name,
                     stage3_flavor, validate)
from .diskmenu import h_disk
from .disklayout import describe, disk_size_mib, layout_for, table_text
from .system import human_size, is_uefi
from .timezones import TIMEZONES
from .tui import Tui

CONFIG_FILE = "gentooinstall_config.json"
CREDS_FILE = "gentooinstall_creds.json"
NOT_SET = "<not set>"


# --------------------------------------------------------------------------- data sources
def list_timezones() -> list[str]:
    """All 'Region/City' zones. Reads the live system's tables, falls back to a bundled list."""
    zones: set[str] = set()
    for tab in ("/usr/share/zoneinfo/zone.tab", "/usr/share/zoneinfo/zone1970.tab"):
        try:
            for line in Path(tab).read_text().splitlines():
                if line and not line.startswith("#"):
                    zones.add(line.split("\t")[2])
        except (OSError, IndexError):
            continue
    if len(zones) < 50:
        root = "/usr/share/zoneinfo"
        skip = ("posix", "right", "Etc", "SystemV", "US", "Brazil", "Canada", "Chile", "Mexico")
        for base, dirs, files in os.walk(root):
            rel = os.path.relpath(base, root)
            if rel != "." and rel.split(os.sep)[0] in skip:
                dirs[:] = []
                continue
            if rel != ".":
                zones.update(f"{rel}/{f}" for f in files if f[0].isupper() and "." not in f)
    if len(zones) < 50:
        zones |= set(TIMEZONES)
    return sorted(zones)


def list_locales() -> list[str]:
    try:
        out = [ln.split()[0] for ln in Path("/usr/share/i18n/SUPPORTED").read_text().splitlines()
               if ln.strip().endswith(" UTF-8")]
        if out:
            return sorted(set(out))
    except (OSError, IndexError):
        pass
    langs = ["en_US", "en_GB", "en_AU", "en_CA", "en_IN", "de_DE", "de_AT", "de_CH", "fr_FR", "fr_CA", "es_ES",
             "es_MX", "es_AR", "it_IT", "pt_PT", "pt_BR", "nl_NL", "sv_SE", "nb_NO", "da_DK", "fi_FI", "pl_PL",
             "cs_CZ", "hu_HU", "ro_RO", "el_GR", "tr_TR", "ru_RU", "uk_UA", "he_IL", "ar_SA", "hi_IN", "ta_IN",
             "te_IN", "bn_BD", "th_TH", "vi_VN", "id_ID", "ja_JP", "ko_KR", "zh_CN", "zh_TW"]
    return [f"{x}.UTF-8" for x in langs]


def list_keymaps() -> list[str]:
    names: set[str] = set()
    for root in ("/usr/share/keymaps", "/usr/share/kbd/keymaps"):
        for _, _, files in os.walk(root):
            for f in files:
                if f.endswith((".map.gz", ".map")):
                    names.add(f.split(".map")[0])
    return sorted(names) or ["us", "uk", "de", "fr", "es", "it", "dvorak", "colemak", "br-abnt2", "ru", "jp106"]


def select_disk(disks: list[dict]) -> str | None:
    """Show a disk picker and return the chosen device path (for use as a library)."""
    opts = [(d["path"], f"{d['path']:<14} {human_size(d['size']):>10}   {d['model'] or '-'}") for d in disks]
    with Tui() as t:
        return t.choose("Select disks for the installation", opts)


# --------------------------------------------------------------------------- handlers
def h_init(t: Tui, c: Config) -> None:
    v = t.choose("Init system", list(INIT_SYSTEMS.items()), current=c.init_system)
    if v:
        c.init_system = v
        if v != "systemd" and c.network == "systemd-networkd":
            c.network = "dhcpcd"


def h_stage3(t: Tui, c: Config) -> None:
    v = t.choose("Stage3 tarball", [
        (False, f"Minimal   stage3-amd64-{c.init_system}"),
        (True, f"Desktop   stage3-amd64-desktop-{c.init_system}")], current=c.stage3_desktop,
        prompt="The newest tarball is fetched and GPG-verified at install time.")
    if v is not None:
        c.stage3_desktop = v


def h_encrypt(t: Tui, c: Config) -> None:
    if not t.confirm("Disk encryption", "Encrypt the root partition with LUKS2?\n"
                     "With GRUB a separate unencrypted /boot partition is required.", c.encrypt):
        c.encrypt, c.luks_password = False, ""
        return
    pw = t.ask_password("Encryption password")
    if pw:
        c.encrypt, c.luks_password = True, pw
        if c.disk_mode == "default":
            c.separate_home = False        # a single encrypted root keeps the default layout simple


def h_swap(t: Tui, c: Config) -> None:
    v = t.ask("Swap partition", "Swap size in GiB (0 = no swap).\nUsed by the default layout only.", str(c.swap_gib),
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
    v = t.choose("Kernels", [(k, f"{k:<20} {d}") for k, (_, d) in KERNELS.items()], current=c.kernel)
    if v:
        c.kernel = v


def h_hostname(t: Tui, c: Config) -> None:
    v = t.ask("Hostname", "Enter a hostname", c.hostname,
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
            v = t.ask(title, "New entry", validator=validator)
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
        ch = t.choose("User account", opts, prompt="Select an existing user to remove it.", filterable=False)
        if ch in (None, "__done"):
            return
        if ch == "__add":
            name = t.ask("Add a user", "Enter a username", validator=lambda s: "" if USERNAME_RE.match(s) and
                         s not in [u["username"] for u in c.users] else "Invalid or duplicate username")
            if not name:
                continue
            pw = t.ask_password(f"Password for {name}")
            if not pw:
                continue
            c.users.append({"username": name, "password": pw,
                            "sudo": t.confirm("Administrator", f"Should {name} be a superuser (sudo)?", True)})
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
                     preview=lambda k: "\n".join("  " + p for p in DESKTOPS[k]["pkgs"]),
                     preview_header="Packages")
        c.desktop = d or c.desktop or "i3"
        if c.audio == "none":
            c.audio = "pipewire"
        h_greeter(t, c)


def h_greeter(t: Tui, c: Config) -> None:
    notes = {"sddm": "Package: x11-misc/sddm (main Gentoo tree)",
             "ly": "Package: x11-misc/ly from the GURU overlay.\nTesting keyword (~amd64); built locally with Zig.\n"
                   "Runs on tty2 (the tty2 getty is disabled)."}
    v = t.choose("Greeter", list(GREETERS.items()), current=c.greeter,
                 preview=lambda k: notes[k], preview_header="Details")
    if v:
        c.greeter = v


def h_graphics(t: Tui, c: Config) -> None:
    v = t.choose("Graphics driver", list(GRAPHICS.items()), current=c.graphics)
    if v:
        c.graphics = v


def h_audio(t: Tui, c: Config) -> None:
    v = t.choose("Audio", [("none", "No audio server"), ("pipewire", "pipewire")], current=c.audio)
    if v:
        c.audio = v


def h_network(t: Tui, c: Config) -> None:
    opts = [(k, d) for k, d in NETWORKS.items() if k != "systemd-networkd" or c.init_system == "systemd"]
    v = t.choose("Network configuration", opts, current=c.network)
    if v:
        c.network = v


def h_tz(t: Tui, c: Config) -> None:
    """Pick a region first (Africa, America, Asia, Europe...), then a city."""
    zones = list_timezones()
    regions = sorted({z.split("/")[0] for z in zones if "/" in z})
    cur_region = c.timezone.split("/")[0] if "/" in c.timezone else c.timezone
    while True:
        opts = [("UTC", "UTC")] + [(r, f"{r}  ({sum(1 for z in zones if z.startswith(r + '/'))} zones)") for r in regions]
        r = t.choose("Select timezone region", opts, current=cur_region)
        if r is None:
            return
        if r == "UTC":
            c.timezone = "UTC"
            return
        cities = [(z, z[len(r) + 1:]) for z in zones if z.startswith(r + "/")]
        z = t.choose(f"Select timezone: {r}", cities, current=c.timezone, prompt="Press / to filter.")
        if z:
            c.timezone = z
            return


def h_locale(t: Tui, c: Config) -> None:
    v = t.choose("Locale language", [(z, z) for z in list_locales()], current=c.locale)
    if v:
        c.locale = v


def h_keymap(t: Tui, c: Config) -> None:
    v = t.choose("Keyboard layout", [(z, z) for z in list_keymaps()], current=c.keymap)
    if v:
        c.keymap = v


def h_ntp(t: Tui, c: Config) -> None:
    c.ntp = t.confirm("Automatic time sync (NTP)", "Enable automatic clock synchronization?", c.ntp)


def h_binpkg(t: Tui, c: Config) -> None:
    c.binpkg = t.confirm("Binary packages",
                         "Use the official Gentoo binary package host where possible?\n"
                         "Much faster; custom USE flags may still trigger local compiles.", c.binpkg)


def h_testing(t: Tui, c: Config) -> None:
    c.testing = t.confirm("Testing branch (~amd64)",
                          "Use unstable keywords system-wide? Binary packages are built for the stable "
                          "branch, so expect a lot of compiling.", c.testing)


def h_mirror(t: Tui, c: Config) -> None:
    v = t.ask("Mirror", "Gentoo distfiles mirror (used for stage3 and sources)", c.mirror,
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
    """[(label, preview_text, handler)] - the preview is shown for the focused row."""
    uefi = is_uefi()
    prof = f"Profile: {c.profile_type}"
    if c.profile_type == "desktop":
        prof += f"\nDesktop: {DESKTOPS.get(c.desktop, {}).get('label', NOT_SET)}\nGreeter: {c.greeter}"
        if c.desktop in DESKTOPS:
            prof += "\n\nPackages:\n" + "\n".join("  " + p for p in DESKTOPS[c.desktop]["pkgs"])
    users = "\n".join(f"  {u['username']}  (sudo: {'yes' if u.get('sudo') else 'no'})" for u in c.users) or "  none"
    return [
        ("Init system", f"Init system: {c.init_system}\nPortage profile: {profile_name(c)}", h_init),
        ("Stage3 tarball", f"Tarball: {stage3_flavor(c)}", h_stage3),
        ("Disk configuration", describe(c, uefi), h_disk),
        ("Disk encryption", f"Disk encryption: {'LUKS2 on /' if c.encrypt else 'disabled'}"
                            f"\nPassword: {_mask(c.luks_password) if c.encrypt else '-'}", h_encrypt),
        ("Swap", f"Swap partition: {str(c.swap_gib) + ' GiB' if c.swap_gib else 'none'}", h_swap),
        ("Bootloader", f"Bootloader: {c.bootloader}"
                       + (f"\nRemovable path: {c.removable_boot}" if c.bootloader == "grub" else ""), h_boot),
        ("Kernels", f"Kernel: {c.kernel}\nPackage: {KERNELS[c.kernel][0]}", h_kernel),
        ("Hostname", f"Hostname: {c.hostname}", h_hostname),
        ("Root password", f"Root password: {_mask(c.root_password)}", h_root),
        ("User account", "Users:\n" + users, h_users),
        ("Profile", prof, h_profile),
        ("Greeter", f"Greeter: {c.greeter} ({GREETERS.get(c.greeter, '')})\n"
                    + ("Applies to the desktop profile." if c.profile_type == "desktop"
                       else "Only used with a desktop profile."), h_greeter),
        ("Graphics driver", f"Graphics driver: {c.graphics}", h_graphics),
        ("Audio", f"Audio: {c.audio}", h_audio),
        ("Network configuration", f"Network: {c.network}", h_network),
        ("Timezone", f"Timezone: {c.timezone}", h_tz),
        ("Locale", f"Locale: {c.locale}", h_locale),
        ("Keyboard layout", f"Keyboard layout: {c.keymap}", h_keymap),
        ("Automatic time sync (NTP)", f"NTP: {'enabled' if c.ntp else 'disabled'}", h_ntp),
        ("Binary packages", f"Binary packages: {'on' if c.binpkg else 'off'}", h_binpkg),
        ("Testing branch (~amd64)", f"Testing branch: {'on' if c.testing else 'off'}", h_testing),
        ("Mirror", f"Mirror: {c.mirror}", h_mirror),
        ("Additional packages", "Packages:\n" + ("\n".join("  " + p for p in c.packages) or "  none"), h_packages),
        ("Custom commands", "Commands:\n" + ("\n".join("  " + p for p in c.custom_commands) or "  none"), h_commands),
    ]


def summary(c: Config, uefi: bool) -> str:
    mib = disk_size_mib(c.disk) if c.disk else 0
    L = [f"Boot mode        : {'UEFI' if uefi else 'BIOS'}",
         f"Stage3           : {stage3_flavor(c)}",
         f"Init / profile   : {c.init_system} / {profile_name(c)}",
         f"Disk             : {describe(c, uefi).splitlines()[0]}", ""]
    if c.disk_mode == "premounted":
        L.append("Using whatever is mounted at /mnt/gentoo")
    else:
        L.append(f"Device {c.disk}: rows marked 'create' or 'format' destroy data "
                 "(the whole disk is wiped if no existing partition is kept)")
        L += table_text(layout_for(c, uefi), c.disk, mib, c.encrypt).split("\n")
    L += ["", f"Bootloader       : {c.bootloader}",
          f"Kernel           : {KERNELS[c.kernel][0]}",
          f"Hostname         : {c.hostname}",
          f"Locale / TZ / KB : {c.locale} / {c.timezone} / {c.keymap}",
          f"Network          : {c.network}   NTP: {c.ntp}",
          f"Graphics / audio : {c.graphics} / {c.audio}",
          f"Desktop / greeter: {c.desktop + ' / ' + c.greeter if c.profile_type == 'desktop' else '-'}",
          f"Binary packages  : {c.binpkg}   Testing: {c.testing}",
          f"Users            : {', '.join(u['username'] for u in c.users) or 'only root'}",
          f"Extra packages   : {' '.join(c.packages) or '-'}",
          f"Custom commands  : {len(c.custom_commands)}", "",
          "The install downloads a stage3 and can take hours if packages must be compiled."]
    return "\n".join(L)


def run_menu(t: Tui, c: Config) -> str:
    """Show the main menu. Returns 'install' or 'abort'."""
    uefi = is_uefi()
    start = None
    while True:
        rows = entries(c)
        opts = [(i, label) for i, (label, _, _) in enumerate(rows)]
        opts += [("save", "Save configuration"), ("install", "Install"), ("abort", "Abort")]

        def preview(key):
            if isinstance(key, int):
                return rows[key][1]
            return {"save": f"Writes {CONFIG_FILE} (and optionally {CREDS_FILE})",
                    "install": "Validates the configuration and starts the installation",
                    "abort": "Quit without changing anything"}[key]

        ch = t.choose("Gentoo installer", opts, start=start, filterable=False, preview=preview)
        start = ch
        if ch is None or ch == "abort":
            if t.confirm("Abort", "Quit without installing?"):
                return "abort"
        elif ch == "save":
            creds = t.confirm("Save configuration",
                              f"Write {CONFIG_FILE} to the current directory.\n"
                              f"Also save passwords to {CREDS_FILE} (plain text, mode 600)?")
            c.save(CONFIG_FILE, CREDS_FILE if creds else None)
            t.message("Saved", f"Wrote {os.path.abspath(CONFIG_FILE)}" + (f" and {CREDS_FILE}" if creds else ""),
                      level="info")
        elif ch == "install":
            errs = validate(c, uefi)
            if errs:
                t.message("Cannot install yet", "\n".join(f"- {e}" for e in errs), level="error")
                continue
            if not t.pager("Installation summary", summary(c, uefi), "Install"):
                continue
            if c.disk_mode != "premounted" and not t.confirm(
                    f"Erase {c.disk}?", f"Partitions marked create/format on {c.disk} will be destroyed.\nContinue?"):
                continue
            return "install"
        else:
            rows[ch][2](t, c)
