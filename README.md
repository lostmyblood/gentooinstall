# gentooinstall

A guided, archinstall-style TUI installer for Gentoo (amd64), following the
[AMD64 Handbook](https://wiki.gentoo.org/wiki/Handbook:AMD64). Python standard library only
(`curses`), so it runs on the stock Gentoo minimal ISO with nothing to install.

## Run it from the live ISO

1. Boot the Gentoo minimal install ISO (UEFI recommended) and get online (`net-setup`, `nmtui`, or Ethernet).
2. Copy this folder over (USB stick, or `wget`/`scp`) and unpack: `tar xf gentooinstall.tar.gz && cd gentooinstall_pkg`
3. Try it harmlessly first, then run it for real:
   ```
   sh gentooinstall.sh --dry-run     # prints every command, changes nothing
   sh gentooinstall.sh               # as root
   ```

## Look and feel

Colors and layout are copied from archinstall's own Textual CSS (read from its source): transparent
background with white text, a blue bold header bar, the focused row in white-on-blue bold, a bordered
input that turns blue when focused, red/yellow/green for error/warning/info, black key chips in the
footer, and the archinstall main-menu layout: **labels on the left, a live preview of the focused item
on the right**. Only the 8 basic ANSI colours are used, so your terminal's own palette applies.

## Disk configuration (like archinstall)

| Mode | What it does |
|---|---|
| **Use a best-effort default partition layout** | Pick a disk (the preview pane shows its current partitions), a filesystem (ext4, btrfs, xfs, f2fs), btrfs subvolumes (`@`, `@home`, `@var_log`, `@snapshots`), and optionally a separate `/home` (disks of 64 GiB or more). Creates: 1 GiB ESP at `/efi` (UEFI) or 1 MiB `bios_boot` (BIOS+GRUB), optional swap, root. With LUKS+GRUB a separate `/boot` is added. |
| **Manual Partitioning** | The partition editor: a table (Status / Device / Start / Size / FS / Mount / Flags) with free-space rows. Select free space to **create**, a partition to **assign mountpoint**, **mark for format**, **mark as ESP**, **change filesystem**, **enable LUKS** (on `/`), **delete/restore**. Also *Suggest partition layout*, *Remove all newly added partitions*, *Delete all partitions*. Existing partitions can be kept (no format) and reused. Sizes accept `%`, `MiB`, `GiB`, `TiB`. The layout is validated (one `/`, ESP on UEFI, bios_boot on BIOS+GRUB, `/boot` for LUKS+GRUB). |
| **Pre-mounted configuration** | Use whatever is mounted at `/mnt/gentoo` (ESP at `/mnt/gentoo/efi` on UEFI). Offers a shell to prepare it. Not validated. |

The main menu's preview pane always shows the resulting table, and the install summary shows it again before anything is erased.

## Desktops and greeters

Profiles: minimal, server (sshd + cron), desktop. Desktop environments (Gentoo atoms for the packages you listed):

* **i3-wm**: `x11-wm/i3 x11-misc/i3status x11-misc/dmenu x11-misc/i3lock x11-base/xorg-server x11-apps/xinit x11-apps/xrandr x11-libs/libX11 x11-libs/libxcb`
* **Xfce**: `xfce-base/xfce4-meta xfce-base/xfce4-panel xfce-base/xfce4-session xfce-base/xfce4-settings xfce-base/xfdesktop xfce-base/xfwm4 xfce-base/thunar x11-terms/xfce4-terminal app-editors/mousepad` + the same Xorg set

Every desktop also gets `x11-base/xorg-drivers` (input and video drivers), and on OpenRC `elogind` and `dbus`.

Greeters:

* **SDDM**: `x11-misc/sddm` (main tree). OpenRC uses `display-manager-init`; systemd enables `sddm`.
* **Ly**: not in the main tree. The installer enables the **GURU overlay**, accepts `~amd64` for `x11-misc/ly`
  and its dependencies (Zig) and builds it locally. Ly runs on tty2, so the tty2 getty is disabled.

## Everything else

Stage3 (OpenRC or systemd, minimal or `desktop`; fetched fresh and GPG-verified), kernels
(`gentoo-kernel-bin` default, `gentoo-kernel`, `vanilla-kernel`, `gentoo-sources` defconfig build),
GRUB (UEFI+BIOS, optional removable path) or systemd-boot (UEFI), NetworkManager / systemd-networkd /
dhcpcd, users with sudo, NTP, graphics driver (auto-detects Intel/AMD/NVIDIA and common VMs; falls back
to `fbdev vesa` on desktops), PipeWire, region-then-city timezone picker, locale, keymap, binary packages
from the official binhost, testing branch, mirror, extra packages, custom chroot commands.
Initramfs is built with dracut through `installkernel`.

## Safety and failure handling

* Pre-flight: x86_64, root, internet, UEFI/BIOS (shown up front).
* Summary screen plus an explicit erase confirmation before any disk is touched.
* Colour-coded step log and a full log in `/var/log/gentooinstall/install.log`.
* If a step fails: **retry**, **shell** (inside the chroot once it exists), **continue anyway**, or
  **abort and roll back** (unmount, close LUKS, swapoff; an already-repartitioned disk is not restored).

## Automation

```
# "Save configuration" in the menu, then:
python3 -m gentooinstall --config gentooinstall_config.json --creds gentooinstall_creds.json --silent
```
`--script my.py` runs your Python with `cfg` and `Installer` in scope. Credentials are in a separate
mode-600 file; treat it as plain-text secrets.

## Known limits

* amd64 only; no translations. Plasma, GNOME and Sway were removed from the desktop list.
* Encryption covers the root partition only (LUKS2). Other partitions stay plain.
* Binary packages are for the stable generic x86-64 branch; custom USE flags or the testing branch mean compiling.
  GURU packages (Ly) are always compiled locally.
* OpenRC + PipeWire: session autostart is left for you to wire up.
* `gentoo-sources` is a defconfig build plus the options this installer needs.
* Tested with dry-runs, real `sfdisk` runs against a disk image, and a terminal-emulator test of the TUI.
  **Not tested with a full install on real hardware or in a VM.** Try a VM first.
