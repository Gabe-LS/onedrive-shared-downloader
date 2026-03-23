#!/usr/bin/env python3
"""
run.py — run onedrive-shared-downloader.

Usage:
    macOS / Linux:  python3 run.py 'https://1drv.ms/f/...'
    Windows:        python run.py "https://1drv.ms/f/..."
"""

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR   = os.path.join(SCRIPT_DIR, ".venv")

IN_VENV = sys.prefix != sys.base_prefix

if IN_VENV:
    VENV_PYTHON = sys.executable
elif sys.platform == "win32":
    VENV_PYTHON = os.path.join(VENV_DIR, "Scripts", "python.exe")
else:
    VENV_PYTHON = os.path.join(VENV_DIR, "bin", "python")


def main():
    if not IN_VENV and not os.path.exists(VENV_PYTHON):
        print("Error: the tool is not installed yet.")
        print()
        print("Please run the installer first:")
        print()
        if sys.platform == "win32":
            print("    python install.py")
        else:
            print("    python3 install.py")
        print()
        sys.exit(1)

    if len(sys.argv) < 2:
        if sys.platform == "win32":
            print('Usage:   python run.py "https://1drv.ms/f/c/..." [destination_folder]')
            print()
            print('Example: python run.py "https://1drv.ms/f/c/abc123/..."')
            print('         python run.py "https://1drv.ms/f/c/abc123/..." C:\\Users\\you\\Desktop\\folder')
        else:
            print("Usage:   python3 run.py 'https://1drv.ms/f/c/...' [destination_folder]")
            print()
            print("Example: python3 run.py 'https://1drv.ms/f/c/abc123/...'")
            print("         python3 run.py 'https://1drv.ms/f/c/abc123/...' ~/Desktop/folder")
        print()
        sys.exit(1)

    downloader = os.path.join(SCRIPT_DIR, "downloader.py")
    result = subprocess.run([VENV_PYTHON, downloader] + sys.argv[1:])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
