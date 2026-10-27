#!/bin/sh
# Launcher for the Gentoo live ISO. Usage:  sh gentooinstall.sh [--dry-run] [--config F --creds F [--silent]]
set -eu
cd "$(dirname "$0")"
command -v python3 >/dev/null 2>&1 || { echo "python3 not found on this live image" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || [ "${1:-}" = "--dry-run" ] || { echo "Run as root (sudo -i)" >&2; exit 1; }
exec python3 -m gentooinstall "$@"
