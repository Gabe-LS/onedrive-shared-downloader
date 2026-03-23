#!/usr/bin/env python3
"""
uninstall.py — remove the virtual environment.

Your downloaded files are NOT affected.

Usage:
    macOS / Linux:  python3 uninstall.py
    Windows:        python uninstall.py
"""

import os
import shutil
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR   = os.path.join(SCRIPT_DIR, ".venv")


def main():
    print()
    print("onedrive-shared-downloader — uninstaller")
    print("=" * 41)
    print()
    print("This will remove:")
    print("  \u2022 .venv/           (virtual environment and installed packages)")
    print()
    print("This will NOT remove:")
    print("  \u2022 downloader.py    (the script itself)")
    print("  \u2022 Your downloaded files")
    print()

    response = input("Continue? [y/N] ").strip().lower()
    if response != "y":
        print("Cancelled.")
        sys.exit(0)

    print()

    if os.path.exists(VENV_DIR):
        shutil.rmtree(VENV_DIR)
        print("  \u2713 Removed .venv/")
    else:
        print("  .venv/ not found, skipping")

    cmd = "python" if sys.platform == "win32" else "python3"
    print()
    print(f"Done. To reinstall, run:  {cmd} install.py")
    print()


if __name__ == "__main__":
    main()
