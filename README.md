# gentooinstall

A guided, archinstall-style TUI installer for Gentoo (amd64), following the
[AMD64 Handbook](https://wiki.gentoo.org/wiki/Handbook:AMD64). Python standard library only
(`curses`), so it runs on the stock Gentoo minimal ISO with nothing to install.

Palette: pure black background, white text, muted steel-blue accents (256-colour terminals;
plain blue on the 8-colour Linux console).

## Run it from the live ISO

1. Boot the Gentoo minimal install ISO (UEFI recommended) and get online
   (`net-setup`, `nmtui`, or plug in Ethernet). Check with `ping -c1 gentoo.org`.
2. Copy this folder over (USB stick, or `wget`/`scp` from wherever you host the tarball):
   ```
   tar xf gentooinstall.tar.gz && cd gentooinstall_pkg
   ```
3. Try it harmlessly first, then run it for real:
   ```
   sh gentooinstall.sh --dry-run     # prints every command, changes nothing
   sh gentooinstall.sh               # as root
   ```

## Wizard

Each menu row shows its current value; edit rows in any order, then **Install**.

| Row | Options |
|---|---|
| Init system / Stage3 tarball | OpenRC or systemd; minimal or `desktop` stage3 (fetched fresh and GPG-verified) |
| Disk configuration | Automatic GPT (ESP + optional swap + root; `bios_boot` on BIOS) or **pre-mounted** for manual/dual-boot layouts (offers a shell; root at `/mnt/gentoo`, ESP at `/mnt/gentoo/efi`) |
| Filesystem / encryption / swap | ext4, btrfs (subvolumes), xfs, f2fs; optional LUKS2 root |
| Bootloader | GRUB (UEFI+BIOS, optional removable path) or systemd-boot (UEFI) |
| Kernel | `gentoo-kernel-bin` (default), `gentoo-kernel`, `vanilla-kernel`, or `gentoo-sources` (defconfig build) |
| Network, hostname | NetworkManager, systemd-networkd, dhcpcd, or none |
| Users | root + any number of users (optional sudo) |
| Profile | minimal / server (sshd, cron) / desktop (Plasma, GNOME, Xfce, i3, Sway) |
| Extras | graphics driver, PipeWire, timezone, locale, keymap, NTP, binary packages, mirror, extra atoms, custom chroot commands |

Initramfs is built with dracut through `installkernel` (Gentoo's replacement for `mkinitcpio` hooks).

## Safety and failure handling

* Pre-flight: x86_64, root, internet, UEFI/BIOS mode (shown up front; systemd-boot is refused on BIOS).
* Disk wipe needs an explicit "ERASE /dev/..." confirmation after a summary screen.
* Colour-coded step log (`==>` step, `[ ok ]`, `[warn]`, `[FAIL]`) and a full log in
  `/var/log/gentooinstall/install.log`.
* If a step fails you get: **retry**, **shell** (inside the chroot once it exists), **continue anyway**,
  or **abort and roll back** (unmount, close LUKS, swapoff). Rollback cannot restore a disk that was
  already repartitioned.

## Automation

```
# save from the menu ("Save configuration"), then on another machine:
python3 -m gentooinstall --config gentooinstall_config.json --creds gentooinstall_creds.json --silent
```
`--script my.py` runs your Python with `cfg` and `Installer` in scope. Credentials live in a separate
mode-600 file; treat it as plain-text secrets.

## Known limits

* amd64 only. No translations / accessibility tooling like archinstall.
* Binary packages come from the official binhost (stable, generic x86-64); custom USE flags or the
  testing branch mean local compiling. Desktop profiles can take hours without binaries.
* OpenRC + PipeWire: session autostart (`gentoo-pipewire-launcher`) is left for you to wire up.
* The `gentoo-sources` option is a defconfig build plus the options this installer needs; tune it for your hardware.
* This was validated with dry-runs and a pty smoke test of the TUI, **not a full install on real
  hardware**. Try it in a VM first (UEFI and BIOS, with and without LUKS).
