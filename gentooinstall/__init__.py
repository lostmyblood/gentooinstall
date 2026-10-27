"""gentooinstall - a guided, TUI-driven Gentoo installer for the live ISO."""
from .config import Config, validate
from .installer import Installer
from .menu import select_disk
from .system import all_disks

__version__ = "0.1.0"
__all__ = ["Config", "Installer", "validate", "all_disks", "select_disk"]
