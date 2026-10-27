"""CLI entry point:  python3 -m gentooinstall [--config F] [--creds F] [--silent] [--dry-run]"""
from __future__ import annotations

import argparse
import os
import runpy
import sys

from .config import Config, validate
from .installer import Installer
from .system import InstallError, check_environment, is_uefi, say, setup_logging


def env_screen(t) -> bool:
    """Show pre-flight results in the TUI; return False if a hard check failed."""
    checks = check_environment()
    ok = all(p for _, p, _ in checks)
    text = "\n".join(f"[{'ok' if p else 'FAIL'}] {n}: {d}" for n, p, d in checks)
    text += "\n\n" + ("Everything needed is in place." if ok else
                      "Fix the failing checks (usually: get online) and run again.")
    t.message("Environment checks", text, error=not ok)
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gentooinstall", description="Guided Gentoo installer")
    ap.add_argument("--config", help="JSON configuration to load")
    ap.add_argument("--creds", help="JSON credentials (passwords) to load")
    ap.add_argument("--silent", action="store_true", help="no TUI: install straight from --config")
    ap.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    ap.add_argument("--script", help="python file executed with `cfg` and `Installer` in scope")
    args = ap.parse_args(argv)

    log_path = setup_logging()
    cfg = Config.load(args.config, args.creds) if args.config else Config()

    if args.script:
        runpy.run_path(args.script, init_globals={"cfg": cfg, "Installer": Installer, "dry_run": args.dry_run},
                       run_name="__main__")
        return 0

    if args.silent:
        errs = validate(cfg, is_uefi())
        if errs:
            for e in errs:
                say("err", e)
            return 2
        try:
            Installer(cfg, dry_run=args.dry_run).run(interactive=False)
        except InstallError as e:
            say("err", str(e))
            return 1
        return 0

    if not sys.stdin.isatty():
        print("A terminal is required for the guided installer (use --silent with --config otherwise).")
        return 2
    if not args.dry_run and os.geteuid() != 0:
        print("Run as root from the Gentoo live ISO.")
        return 2

    from .menu import run_menu
    from .tui import Tui
    with Tui() as t:
        if not env_screen(t) and not args.dry_run:
            return 1
        action = run_menu(t, cfg)
    if action != "install":
        print("Aborted; nothing was changed.")
        return 1
    try:
        Installer(cfg, dry_run=args.dry_run).run()
    except InstallError as e:
        say("err", str(e))
        return 1
    if not args.dry_run:
        try:
            if input("\nReboot now? [y/N] ").strip().lower() == "y":
                os.system("reboot")
        except EOFError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
