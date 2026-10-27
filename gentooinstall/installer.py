"""Installation engine: follows the Gentoo handbook, driven by a Config."""
from __future__ import annotations

import os
import shlex
import shutil
import tempfile

import subprocess
import sys
import time

from .config import (DESKTOPS, KERNELS, TARGET, VIDEO_CARDS, Config, profile_name, stage3_flavor)
from .system import (InstallError, Runner, check_environment, detect_gpus, download, fetch_text,
                     is_uefi, log, mem_gib, say)

GENTOO_KEY = "/usr/share/openpgp-keys/gentoo-release.asc"
BLUE, BOLD, RST = "\033[38;5;67m", "\033[1m", "\033[0m"
BIOS_BOOT_GUID = "21686148-6449-6E6F-744E-656564454649"
CRYPT_NAME = "cryptroot"


class Installer:
    def __init__(self, cfg: Config, dry_run: bool = False, target: str = TARGET):
        self.cfg = cfg
        self.dry = dry_run
        self.target = target
        self.r = Runner(target, dry_run)
        self.uefi = is_uefi()
        self.fstab: list[tuple[str, str, str, str, int]] = []
        self.root_uuid = ""
        self.luks_uuid = ""
        self.root_flags = ""
        self.swap_dev = ""
        self.encrypted = False
        self.chroot_ready = False
        self.partitioned = False

    # ------------------------------------------------------------------ helpers
    def c(self, cmd, **kw) -> str:
        """Run a command inside the chroot."""
        return self.r.run(cmd, chroot=True, **kw)

    def emerge(self, args: str) -> None:
        self.c(f"emerge --verbose --quiet-build=y {args}")

    def enable(self, service: str, runlevel: str = "default") -> None:
        if self.cfg.init_system == "systemd":
            self.c(f"systemctl enable {service}")
        else:
            self.c(f"rc-update add {service} {runlevel}")

    def uuid(self, dev: str) -> str:
        if self.dry:
            return f"UUID-OF-{os.path.basename(dev)}"
        return self.r.run(["blkid", "-s", "UUID", "-o", "value", dev], quiet=True).strip()

    def step(self, title: str) -> None:
        print()
        say("step", title)

    @staticmethod
    def part(disk: str, n: int) -> str:
        return f"{disk}p{n}" if disk[-1].isdigit() else f"{disk}{n}"

    def add_fstab(self, dev: str, mnt: str, fstype: str, opts: str, passno: int) -> None:
        self.fstab.append((f"UUID={self.uuid(dev)}", mnt, fstype, opts, passno))

    # ------------------------------------------------------------------ driver
    def run(self, interactive: bool = True) -> None:
        """Run all steps. On failure: retry / shell / continue / roll back (interactive only)."""
        steps = [
            ("Partitioning and mounting", self.prepare_disk),
            ("Downloading and unpacking stage3", self.stage3),
            ("Preparing chroot", self.chroot_prep),
            ("Configuring Portage", self.portage),
            ("Syncing the Portage tree and updating @world", self.sync_world),
            ("Localization and hostname", self.localization),
            ("Writing /etc/fstab", self.write_fstab),
            ("Installing kernel and bootloader", self.kernel_boot),
            ("Network, time and services", self.network_services),
            ("Users", self.users),
            ("Graphics, desktop and audio", self.desktop),
            ("Extra packages and custom commands", self.extras),
            ("Finalizing", self.finalize),
        ]
        self.preflight()
        for title, fn in steps:
            while True:
                self.step(title)
                t0 = time.time()
                try:
                    fn()
                    say("ok", f"{title} ({time.time() - t0:.0f}s)")
                    break
                except (InstallError, OSError, KeyboardInterrupt) as e:
                    say("err", f"{title}: {e}")
                    choice = self.failure_prompt() if interactive else "a"
                    if choice == "r":
                        continue
                    if choice == "c":
                        say("warn", "Continuing after a failed step; the result may be incomplete.")
                        break
                    self.rollback()
                    raise InstallError(f"Installation aborted at: {title}") from e
        self.cleanup()
        print()
        say("ok", "Installation complete. You can reboot into your new system.")

    def failure_prompt(self) -> str:
        """Ask what to do after a failed step. Returns r / c / a."""
        print(f"\n  {BOLD}[r]{RST} retry this step   {BOLD}[s]{RST} open a shell (exit to return)   "
              f"{BOLD}[c]{RST} continue anyway   {BOLD}[a]{RST} abort and roll back")
        while True:
            try:
                ans = input("  choice [r/s/c/a]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "a"
            if ans == "s":
                cmd = ["chroot", self.target, "/bin/bash", "-l"] if self.chroot_ready else ["/bin/bash", "-l"]
                subprocess.call(cmd)
            elif ans in ("r", "c", "a"):
                return ans

    def rollback(self) -> None:
        """Unmount everything, close LUKS, swapoff. Partition changes already made are NOT undone."""
        say("warn", "Rolling back: unmounting target and closing encrypted volumes...")
        self.cleanup(quiet=True)
        if self.cfg.disk_mode == "auto" and self.partitioned:
            say("warn", f"{self.cfg.disk} was already repartitioned; its old contents are gone. "
                        f"Re-run the installer to start over.")
        say("info", "Log: /var/log/gentooinstall/install.log")

    def preflight(self) -> None:
        """Safety checks: arch, root, internet, required live-ISO tools."""
        for name, ok, detail in check_environment():
            say("ok" if ok else "err", f"{name}: {detail}")
            if not ok and not self.dry:
                raise InstallError(f"Pre-flight check failed: {name}")
        if self.cfg.bootloader == "systemd-boot" and not self.uefi:
            raise InstallError("systemd-boot needs UEFI but this system booted in BIOS mode")
        if self.dry:
            return
        need = ["chroot", "tar", "blkid", "mount", "umount"]
        if self.cfg.disk_mode == "auto":
            need += ["sfdisk", "wipefs", "udevadm"]
        if self.cfg.encrypt:
            need.append("cryptsetup")
        missing = [b for b in need if not shutil.which(b)]
        if missing:
            raise InstallError("Missing tools on the live system: " + ", ".join(missing))

    # ------------------------------------------------------------------ disks
    def prepare_disk(self) -> None:
        if self.cfg.disk_mode == "auto":
            self.cleanup(quiet=True)      # makes retries safe (never touches user mounts in premounted mode)
        self.fstab, self.swap_dev, self.encrypted, self.root_flags = [], "", False, ""
        if self.cfg.disk_mode == "premounted":
            self.detect_premounted()
        else:
            self.partition_format_mount()

    def partition_format_mount(self) -> None:
        c, r = self.cfg, self.r
        disk = c.disk
        sep_boot = c.encrypt and (c.bootloader == "grub")
        lines = ["label: gpt"]
        roles: list[str] = []
        if self.uefi:
            lines.append("size=1GiB, type=U"); roles.append("esp")
        elif c.bootloader == "grub":
            lines.append(f"size=1MiB, type={BIOS_BOOT_GUID}"); roles.append("bios")
        if sep_boot:
            lines.append("size=1GiB, type=L"); roles.append("boot")
        if c.swap_gib > 0:
            lines.append(f"size={c.swap_gib}GiB, type=S"); roles.append("swap")
        lines.append("type=L"); roles.append("root")
        dev = {role: self.part(disk, i + 1) for i, role in enumerate(roles)}

        r.run(["wipefs", "--all", "--force", disk])
        self.partitioned = True
        r.run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", disk],
              stdin="\n".join(lines) + "\n")
        r.run(["udevadm", "settle"])

        if "esp" in dev:
            r.run(["mkfs.vfat", "-F", "32", dev["esp"]])
        if "boot" in dev:
            r.run(["mkfs.ext4", "-F", dev["boot"]])
        if "swap" in dev:
            r.run(["mkswap", dev["swap"]])
            r.run(["swapon", dev["swap"]])
            self.swap_dev = dev["swap"]

        root_dev = dev["root"]
        if c.encrypt:
            pw = c.luks_password
            r.run(["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2",
                   "--key-file", "-", root_dev], stdin=pw)
            r.run(["cryptsetup", "open", "--key-file", "-", root_dev, CRYPT_NAME], stdin=pw)
            self.luks_uuid = self.uuid(root_dev)
            self.encrypted = True
            root_dev = f"/dev/mapper/{CRYPT_NAME}"

        mkfs = {"ext4": ["mkfs.ext4", "-F"], "xfs": ["mkfs.xfs", "-f"],
                "btrfs": ["mkfs.btrfs", "-f"], "f2fs": ["mkfs.f2fs", "-f"]}[c.filesystem]
        r.run(mkfs + [root_dev])
        self.root_uuid = self.uuid(root_dev)
        t = self.target
        r.run(["mkdir", "-p", t])

        if c.filesystem == "btrfs" and c.btrfs_subvolumes:
            r.run(["mount", root_dev, t])
            subvols = [("@", "/"), ("@home", "/home"), ("@var_log", "/var/log"), ("@snapshots", "/.snapshots")]
            for name, _ in subvols:
                r.run(["btrfs", "subvolume", "create", f"{t}/{name}"])
            r.run(["umount", t])
            base = "noatime,compress=zstd"
            for name, mnt in subvols:
                dest = t + (mnt if mnt != "/" else "")
                r.run(["mkdir", "-p", dest])
                r.run(["mount", "-o", f"{base},subvol={name}", root_dev, dest])
                self.add_fstab(root_dev, mnt, "btrfs", f"{base},subvol={name}", 0)
            self.root_flags = "rootflags=subvol=@"
        else:
            r.run(["mount", root_dev, t])
            self.add_fstab(root_dev, "/", c.filesystem, "defaults,noatime", 0 if c.filesystem in ("btrfs", "xfs", "f2fs") else 1)
        if "boot" in dev:
            r.run(["mkdir", "-p", f"{t}/boot"])
            r.run(["mount", dev["boot"], f"{t}/boot"])
            self.add_fstab(dev["boot"], "/boot", "ext4", "defaults,noatime", 2)
        if "esp" in dev:
            r.run(["mkdir", "-p", f"{t}/efi"])
            r.run(["mount", dev["esp"], f"{t}/efi"])
            self.add_fstab(dev["esp"], "/efi", "vfat", "umask=0077", 2)
        if self.swap_dev:
            self.fstab.append((f"UUID={self.uuid(self.swap_dev)}", "none", "swap", "sw", 0))

    def detect_premounted(self) -> None:
        """Use whatever is mounted under /mnt/gentoo (root, /boot, /efi, /home ...)."""
        r, t = self.r, self.target
        if self.dry:
            self.root_uuid = "UUID-OF-ROOT"
            return
        if not os.path.ismount(t):
            raise InstallError(f"Nothing is mounted at {t}. Mount your root filesystem there first "
                               f"(and the ESP at {t}/efi on UEFI systems).")
        out = r.run(["findmnt", "-R", "-n", "-r", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", t], quiet=True)
        have_esp = False
        for line in out.splitlines():
            parts = line.split(" ")
            if len(parts) < 4 or not parts[0].startswith("/dev/"):
                continue
            src, tgt, fstype, opts = parts[0].split("[")[0], parts[1], parts[2], parts[3]
            mnt = tgt[len(t):] or "/"
            if mnt.startswith(("/dev", "/sys", "/proc", "/run")) and mnt != "/":
                continue
            keep = ["defaults"] + [o for o in opts.split(",") if o.startswith(("subvol=", "compress"))]
            if fstype == "vfat":
                keep = ["umask=0077"]
            self.add_fstab(src, mnt, fstype, ",".join(keep), 1 if mnt == "/" else 2)
            have_esp = have_esp or mnt == "/efi"
            if mnt == "/":
                self.root_uuid = self.uuid(src)
                for o in opts.split(","):
                    if o.startswith("subvol="):
                        self.root_flags = f"rootflags={o}"
                if src.startswith("/dev/mapper/"):
                    status = r.run(["cryptsetup", "status", os.path.basename(src)], quiet=True, check=False)
                    for sl in status.splitlines():
                        if sl.strip().startswith("device:"):
                            parent = sl.split(":", 1)[1].strip()
                            self.luks_uuid = r.run(["cryptsetup", "luksUUID", parent], quiet=True).strip()
                            self.encrypted = True
        if not self.root_uuid:
            raise InstallError(f"Could not determine the root device mounted at {t}")
        if self.uefi and not have_esp:
            raise InstallError(f"UEFI system: mount the EFI system partition at {t}/efi")

    # ------------------------------------------------------------------ stage3
    def stage3(self) -> None:
        c, t = self.cfg, self.target
        base = f"{c.mirror.rstrip('/')}/releases/amd64/autobuilds"
        flavor = stage3_flavor(c)
        tarball = f"{t}/stage3.tar.xz"
        if self.dry:
            print(f"[dry-run] would download latest {flavor} from {base}")
            return
        rel = None
        for line in fetch_text(f"{base}/latest-{flavor}.txt").splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "-----", "Hash:")) and line.split()[0].endswith(".tar.xz"):
                rel = line.split()[0]
                break
        if not rel:
            raise InstallError(f"Could not find a {flavor} tarball at {base}")
        print(f"  {rel}")
        download(f"{base}/{rel}", tarball)
        if c.verify_stage3:
            download(f"{base}/{rel}.asc", tarball + ".asc")
            if not shutil.which("gpg") or not os.path.exists(GENTOO_KEY):
                raise InstallError("Cannot verify stage3 (gpg or the Gentoo release key is missing). "
                                   "Set verify_stage3 to false to skip at your own risk.")
            gh = tempfile.mkdtemp(prefix="gentooinstall-gpg-")
            env = {"GNUPGHOME": gh}
            self.r.run(["gpg", "--import", GENTOO_KEY], quiet=True, env=env)
            self.r.run(["gpg", "--verify", tarball + ".asc", tarball], env=env)
            shutil.rmtree(gh, ignore_errors=True)
        self.r.run(["tar", "xpf", tarball, "--xattrs-include=*.*", "--numeric-owner", "-C", t])
        for f in (tarball, tarball + ".asc"):
            if os.path.exists(f):
                os.remove(f)

    def chroot_prep(self) -> None:
        r, t = self.r, self.target
        for sub in ("dev", "sys", "proc", "run"):       # idempotent on retry
            r.run(["umount", "-R", f"{t}/{sub}"], check=False, quiet=True)
        r.run(["cp", "--dereference", "/etc/resolv.conf", f"{t}/etc/resolv.conf"])
        r.run(["mount", "--types", "proc", "/proc", f"{t}/proc"])
        for sub in ("sys", "dev"):
            r.run(["mount", "--rbind", f"/{sub}", f"{t}/{sub}"])
            r.run(["mount", "--make-rslave", f"{t}/{sub}"])
        r.run(["mount", "--bind", "/run", f"{t}/run"])
        r.run(["mount", "--make-slave", f"{t}/run"])
        self.chroot_ready = True

    # ------------------------------------------------------------------ portage
    def video_cards(self) -> str:
        c = self.cfg
        if c.graphics == "none":
            return ""
        keys = detect_gpus() if c.graphics == "auto" else {c.graphics}
        if self.dry and c.graphics == "auto":
            keys = set()
        cards: list[str] = []
        for k in sorted(keys):
            cards += VIDEO_CARDS.get(k, "").split()
        return " ".join(dict.fromkeys(cards))

    def portage(self) -> None:
        c, r = self.cfg, self.r
        # Some stage3 tarballs ship package.use & co as plain files; make them directories.
        self.c("for d in package.use package.license package.accept_keywords; do p=/etc/portage/$d; "
               "if [ -f $p ]; then mv $p $p.tmp && mkdir $p && mv $p.tmp $p/00-stage3; else mkdir -p $p; fi; done; "
               "mkdir -p /etc/portage/repos.conf /etc/portage/binrepos.conf; "
               "[ -e /etc/portage/repos.conf/gentoo.conf ] || cp /usr/share/portage/config/repos.conf /etc/portage/repos.conf/gentoo.conf")
        if c.binpkg:
            self.c("[ -e /etc/portage/binrepos.conf/gentoo.conf ] || "
                   "cp /usr/share/portage/config/binrepos.conf /etc/portage/binrepos.conf/gentoo.conf")

        jobs = max(1, min(os.cpu_count() or 1, int(mem_gib() // 2) or 1))
        vc = self.video_cards()
        mk = ["", "# --- added by gentooinstall ---", f'MAKEOPTS="-j{jobs}"',
              'ACCEPT_LICENSE="-* @FREE @BINARY-REDISTRIBUTABLE"',
              f'GENTOO_MIRRORS="{c.mirror.rstrip("/")}"']
        if vc and (c.profile_type == "desktop" or c.graphics != "auto"):
            mk.append(f'VIDEO_CARDS="{vc}"')
        if c.bootloader == "grub":
            mk.append(f'GRUB_PLATFORMS="{"efi-64" if self.uefi else "pc"}"')
        if c.testing:
            mk.append('ACCEPT_KEYWORDS="~amd64"')
        if c.binpkg:
            mk.append('FEATURES="${FEATURES} getbinpkg binpkg-request-signature"')
            mk.append('EMERGE_DEFAULT_OPTS="${EMERGE_DEFAULT_OPTS} --getbinpkg --binpkg-respect-use=y"')
        r.write("/etc/portage/make.conf", "\n".join(mk) + "\n", append=True)

        lic = ["sys-kernel/linux-firmware linux-fw-redistributable no-source-code"]
        if "nvidia" in vc.split():
            lic.append("x11-drivers/nvidia-drivers NVIDIA-r2")
        r.write("/etc/portage/package.license/gentooinstall", "\n".join(lic) + "\n")

        use = [f"sys-kernel/installkernel dracut {'grub' if c.bootloader == 'grub' else 'systemd-boot'}"]
        if c.bootloader == "systemd-boot":
            use.append("sys-apps/systemd boot kernel-install" if c.init_system == "systemd"
                       else "sys-apps/systemd-utils boot kernel-install")
        if c.audio == "pipewire":
            use.append("media-video/pipewire sound-server")
        r.write("/etc/portage/package.use/gentooinstall", "\n".join(use) + "\n")

    def sync_world(self) -> None:
        c = self.cfg
        self.c("emerge-webrsync")
        if c.binpkg:
            self.c("getuto")
        self.c(["eselect", "profile", "set", profile_name(c)])
        self.c("emerge --verbose --quiet-build=y --update --deep --newuse @world")

    # ------------------------------------------------------------------ system config
    def localization(self) -> None:
        c, r = self.cfg, self.r
        r.write("/etc/timezone", c.timezone + "\n")
        self.c(f"ln -sf /usr/share/zoneinfo/{shlex.quote(c.timezone)} /etc/localtime")
        locales = dict.fromkeys([f"{c.locale} UTF-8", "en_US.UTF-8 UTF-8"])
        r.write("/etc/locale.gen", "\n".join(locales) + "\n")
        self.c("locale-gen")
        r.write("/etc/env.d/02locale", f'LANG="{c.locale}"\nLC_COLLATE="C.UTF-8"\n')
        r.write("/etc/locale.conf", f"LANG={c.locale}\n")
        self.c("env-update")
        r.write("/etc/vconsole.conf", f"KEYMAP={c.keymap}\n")
        self.c(f"[ -f /etc/conf.d/keymaps ] && sed -i 's/^keymap=.*/keymap=\"{c.keymap}\"/' /etc/conf.d/keymaps || true")
        r.write("/etc/hostname", c.hostname + "\n")
        r.write("/etc/conf.d/hostname", f'hostname="{c.hostname}"\n')
        r.write("/etc/hosts", f"127.0.0.1 localhost\n::1 localhost\n127.0.1.1 {c.hostname}.localdomain {c.hostname}\n")

    def write_fstab(self) -> None:
        rows = ["# Generated by gentooinstall", "# <fs> <mountpoint> <type> <opts> <dump> <pass>"]
        rows += [f"{s}\t{m}\t{t}\t{o}\t0 {p}" for s, m, t, o, p in self.fstab]
        self.r.write("/etc/fstab", "\n".join(rows) + "\n")

    # ------------------------------------------------------------------ kernel + bootloader
    def cmdline(self) -> str:
        parts = [f"root=UUID={self.root_uuid}", "rw"]
        if self.root_flags:
            parts.append(self.root_flags)
        if self.encrypted:
            parts.append(f"rd.luks.uuid={self.luks_uuid}")
        return " ".join(parts)

    def kernel_boot(self) -> None:
        c, r = self.cfg, self.r
        cmd = self.cmdline()
        r.write("/etc/kernel/cmdline", cmd + "\n")
        dracut = ['hostonly="no"']
        mods = []
        if self.encrypted:
            mods += ["crypt", "dm"]
        if c.filesystem == "btrfs":
            mods.append("btrfs")
        if mods:
            dracut.append(f'add_dracutmodules+=" {" ".join(mods)} "')
        r.write("/etc/dracut.conf.d/10-gentooinstall.conf", "\n".join(dracut) + "\n")

        tools = ["sys-kernel/linux-firmware", "sys-kernel/installkernel"]
        if self.uefi:
            tools.append("sys-fs/dosfstools")
        fs_tools = {"btrfs": "sys-fs/btrfs-progs", "xfs": "sys-fs/xfsprogs", "f2fs": "sys-fs/f2fs-tools"}
        if c.filesystem in fs_tools:
            tools.append(fs_tools[c.filesystem])
        if self.encrypted:
            tools.append("sys-fs/cryptsetup")
        self.emerge("--noreplace " + " ".join(tools))

        # Bootloader first, so installkernel hooks have something to talk to.
        if c.bootloader == "grub":
            self.emerge("--noreplace sys-boot/grub" + (" sys-boot/efibootmgr" if self.uefi else ""))
            r.write("/etc/default/grub", f'GRUB_CMDLINE_LINUX="{cmd}"\n', append=True)
            if self.uefi:
                extra = " --removable" if c.removable_boot else ""
                self.c(f"grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo{extra}")
            else:
                self.c(f"grub-install --target=i386-pc {shlex.quote(c.disk)}")
        else:
            if c.init_system != "systemd":
                self.emerge("--noreplace sys-apps/systemd-utils")
            self.c("systemd-machine-id-setup || true")
            self.c("bootctl install --esp-path=/efi || bootctl install --esp-path=/efi --no-variables")

        if c.kernel == "gentoo-sources":
            self.build_custom_kernel()
        else:
            self.emerge("--noreplace " + KERNELS[c.kernel][0])
        if c.bootloader == "grub":
            self.c("grub-mkconfig -o /boot/grub/grub.cfg")

    def build_custom_kernel(self) -> None:
        """gentoo-sources: defconfig + the options this install needs, then make install
        (installkernel then runs dracut and updates the bootloader)."""
        c = self.cfg
        self.emerge("--noreplace sys-kernel/gentoo-sources sys-devel/bc sys-devel/flex "
                    "sys-devel/bison dev-libs/openssl dev-libs/elfutils")
        self.c("eselect kernel set 1")
        enable = ["EXT4_FS", "VFAT_FS", "BLK_DEV_NVME", "SATA_AHCI", "EFI_STUB", "DM_CRYPT",
                  "BTRFS_FS", "XFS_FS", "F2FS_FS", "DEVTMPFS", "DEVTMPFS_MOUNT", "TMPFS"]
        flags = " ".join(f"-e {o}" for o in enable)
        self.c(f"cd /usr/src/linux && make defconfig && scripts/config {flags} && make olddefconfig")
        self.c("cd /usr/src/linux && make -j$(nproc) && make modules_install && make install")
        say("warn", "Custom kernel built from defconfig; review /usr/src/linux/.config for your hardware.")

    # ------------------------------------------------------------------ network & services
    def network_services(self) -> None:
        c, r = self.cfg, self.r
        if c.init_system == "openrc":
            self.emerge("--noreplace app-admin/sysklogd")
            self.enable("sysklogd")
        if c.network == "dhcpcd":
            self.emerge("--noreplace net-misc/dhcpcd")
            self.enable("dhcpcd")
        elif c.network == "NetworkManager":
            self.emerge("--noreplace net-misc/networkmanager")
            self.enable("NetworkManager")
        elif c.network == "systemd-networkd":
            r.write("/etc/systemd/network/20-wired.network",
                    "[Match]\nName=en* eth*\n\n[Network]\nDHCP=yes\n")
            self.c("systemctl enable systemd-networkd systemd-resolved")
        if c.ntp:
            if c.init_system == "systemd":
                self.c("systemctl enable systemd-timesyncd")
            else:
                self.emerge("--noreplace net-misc/chrony")
                self.enable("chronyd")
        if c.profile_type == "server":
            self.emerge("--noreplace sys-process/cronie")
            self.enable("sshd")
            self.enable("cronie")

    # ------------------------------------------------------------------ users
    def users(self) -> None:
        c, r = self.cfg, self.r
        self.c("chpasswd", stdin=f"root:{c.root_password}\n")
        if any(u.get("sudo") for u in c.users):
            self.emerge("--noreplace app-admin/sudo")
            r.write("/etc/sudoers.d/10-wheel", "%wheel ALL=(ALL:ALL) ALL\n", mode=0o440)
        for u in c.users:
            name = shlex.quote(u["username"])
            groups = "users,audio,video,input" + (",wheel" if u.get("sudo") else "")
            self.c(f"useradd -m -G {groups} -s /bin/bash {name}")
            self.c("chpasswd", stdin=f"{u['username']}:{u['password']}\n")

    # ------------------------------------------------------------------ desktop
    def desktop(self) -> None:
        c = self.cfg
        if "nvidia" in self.video_cards().split():
            self.emerge("--noreplace x11-drivers/nvidia-drivers")
        if c.audio == "pipewire":
            self.emerge("--noreplace media-video/pipewire media-video/wireplumber")
            if c.init_system == "systemd":
                self.c("systemctl --global enable pipewire.socket pipewire-pulse.socket wireplumber.service", check=False)
        if c.profile_type != "desktop":
            return
        de = DESKTOPS[c.desktop]
        pkgs = list(de["pkgs"])
        dm = de["dm"]
        if dm:
            pkgs.append(dm[1])
            if dm[0] == "lightdm":
                pkgs.append("x11-misc/lightdm-gtk-greeter")
        if c.init_system == "openrc" and dm:
            pkgs.append("gui-libs/display-manager-init")
        self.emerge("--noreplace " + " ".join(pkgs))
        if c.init_system == "openrc":
            self.enable("elogind", "boot")
            self.enable("dbus")
            if dm:
                self.r.write("/etc/conf.d/display-manager", f'DISPLAYMANAGER="{dm[0]}"\n')
                self.enable("display-manager")
        elif dm:
            self.c(f"systemctl enable {dm[0]}")

    # ------------------------------------------------------------------ extras
    def extras(self) -> None:
        if self.cfg.packages:
            self.emerge("--noreplace " + " ".join(shlex.quote(p) for p in self.cfg.packages))
        for cmd in self.cfg.custom_commands:
            self.c(cmd)

    def finalize(self) -> None:
        if self.cfg.network == "systemd-networkd":
            self.c("rm -f /etc/resolv.conf && ln -s ../run/systemd/resolve/stub-resolv.conf /etc/resolv.conf")

    def cleanup(self, quiet: bool = False) -> None:
        r, t = self.r, self.target
        r.run("sync", check=False, quiet=quiet)
        for sub in ("dev", "sys", "proc", "run"):
            r.run(["umount", "-R", f"{t}/{sub}"], check=False, quiet=True)
        r.run(["umount", "-R", t], check=False, quiet=True)
        if self.swap_dev:
            r.run(["swapoff", self.swap_dev], check=False, quiet=True)
        if self.encrypted and self.cfg.disk_mode == "auto":
            r.run(["cryptsetup", "close", CRYPT_NAME], check=False, quiet=True)
        self.chroot_ready = False
