#!/usr/bin/env python3
"""
install.py — set up onedrive-shared-downloader.

Run this once before using the tool:
    macOS / Linux:  python3 install.py
    Windows:        python install.py
"""

import os
import subprocess
import sys

MIN_PYTHON = (3, 10)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR   = os.path.join(SCRIPT_DIR, ".venv")

# If already running inside a virtual environment, use it directly
IN_VENV = sys.prefix != sys.base_prefix

if IN_VENV:
    _bin = os.path.dirname(sys.executable)
    if sys.platform == "win32":
        VENV_PYTHON     = sys.executable
        VENV_PIP        = os.path.join(_bin, "pip.exe")
        VENV_PLAYWRIGHT = os.path.join(_bin, "playwright.exe")
    else:
        VENV_PYTHON     = sys.executable
        VENV_PIP        = os.path.join(_bin, "pip")
        VENV_PLAYWRIGHT = os.path.join(_bin, "playwright")
elif sys.platform == "win32":
    VENV_PYTHON     = os.path.join(VENV_DIR, "Scripts", "python.exe")
    VENV_PIP        = os.path.join(VENV_DIR, "Scripts", "pip.exe")
    VENV_PLAYWRIGHT = os.path.join(VENV_DIR, "Scripts", "playwright.exe")
else:
    VENV_PYTHON     = os.path.join(VENV_DIR, "bin", "python")
    VENV_PIP        = os.path.join(VENV_DIR, "bin", "pip")
    VENV_PLAYWRIGHT = os.path.join(VENV_DIR, "bin", "playwright")


def header():
    print()
    print("onedrive-shared-downloader — installer")
    print("=" * 39)
    print()


def check_python():
    v = sys.version_info
    if (v.major, v.minor) < MIN_PYTHON:
        print(f"Error: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or later is required.")
        print(f"       You have Python {v.major}.{v.minor}.")
        print()
        print("Please download a newer version from https://www.python.org/downloads/")
        sys.exit(1)
    print(f"  \u2713 Python {v.major}.{v.minor}.{v.micro} found")


def create_venv():
    if IN_VENV:
        return  # already inside a venv — nothing to create
    if os.path.exists(VENV_PYTHON):
        print("  \u2713 Virtual environment already exists, skipping creation")
        return
    print("  Creating virtual environment...")
    try:
        subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)
    except subprocess.CalledProcessError:
        print()
        print("Error: could not create virtual environment.")
        print("Make sure Python is installed correctly and try again.")
        sys.exit(1)
    print("  \u2713 Virtual environment created")


def install_dependencies():
    print("  Installing dependencies (requests, playwright)...")
    try:
        subprocess.run([VENV_PIP, "install", "--quiet", "--upgrade", "pip"], check=True)
        subprocess.run([VENV_PIP, "install", "--quiet", "requests", "playwright"], check=True)
    except subprocess.CalledProcessError:
        print()
        print("Error: could not install dependencies.")
        print("Check your internet connection and try again.")
        sys.exit(1)
    print("  \u2713 Dependencies installed")


def install_chromium():
    print("  Installing Chromium browser (required for authentication)...")
    try:
        subprocess.run([VENV_PLAYWRIGHT, "install", "chromium"], check=True)
    except subprocess.CalledProcessError:
        print()
        print("Error: could not install Chromium.")
        print("Check your internet connection and try again.")
        sys.exit(1)
    print("  \u2713 Chromium installed")


def done():
    if sys.platform == "win32":
        cmd   = "python"
        quote = '"'
    else:
        cmd   = "python3"
        quote = "'"
    print()
    print("Installation complete!")
    print()
    print("To download a shared OneDrive folder, run:")
    print()
    print(f"    {cmd} run.py {quote}https://1drv.ms/f/...{quote}")
    print()
    print("You can optionally specify a destination folder:")
    print()
    print(f"    {cmd} run.py {quote}https://1drv.ms/f/...{quote} /path/to/destination")
    print()


def confirm():
    if IN_VENV:
        print(f"  \u2713 Using active virtual environment: {sys.prefix}")
        print()
        print("This will:")
        print("  \u2022 Install requests and playwright into the active environment")
        print("  \u2022 Download and install the Chromium browser (~170 MB)")
    else:
        print("This will:")
        print("  \u2022 Create a .venv/ folder in the current directory")
        print("  \u2022 Install requests and playwright into it")
        print("  \u2022 Download and install the Chromium browser (~170 MB)")
    print()
    response = input("Continue? [y/N] ").strip().lower()
    if response != "y":
        print("Cancelled.")
        sys.exit(0)
    print()


if __name__ == "__main__":
    header()
    check_python()
    confirm()
    create_venv()
    install_dependencies()
    install_chromium()
    done()
