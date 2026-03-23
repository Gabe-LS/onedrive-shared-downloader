#!/usr/bin/env python3
# MIT License — Copyright (c) 2026 Gabriele Lo Surdo
"""
OneDrive Shared Folder Downloader
Usage: python downloader.py <shared_url> [destination_folder]

Requirements:
    pip install requests playwright
    playwright install chromium

Python 3.10 or later is required.

IMPORTANT — How this works:
  This tool works by opening the shared OneDrive link in a headless Chromium
  browser, intercepting the temporary auth token and API endpoints that the
  page uses, and then reusing them directly. It relies on undocumented
  Microsoft internals that may change at any time. If it stops working,
  re-run — the token is refreshed on every invocation.
"""

import asyncio
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# Python version check — must be before any 3.10+ syntax is parsed
if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or later is required.")

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from playwright.async_api import async_playwright

# ── Constants ──────────────────────────────────────────────────────────────────

API_BASE         = "https://my.microsoftpersonalcontent.com/_api/v2.0"
MAX_WORKERS      = 4
LOG_FILENAME     = "downloader.log"
STATE_FILE       = "onedrive_download_state.json"
DISPLAY_INTERVAL = 0.2          # seconds between display redraws
CONNECT_TIMEOUT  = 15           # seconds to establish a connection
READ_TIMEOUT     = 120          # seconds allowed between consecutive chunks
CHUNK_SIZE       = 1024 * 1024  # 1 MB per read

# ── Windows ANSI support ───────────────────────────────────────────────────────

if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # Enable ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) on stdout
        handle = kernel32.GetStdHandle(-11)
        mode   = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass  # best-effort; progress bars may look garbled on old Windows

# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging(log_path: str) -> logging.Logger:
    """
    Two handlers:
    - File (DEBUG): full detail with timestamps and thread names.
    - Stderr (WARNING): only warnings/errors, keeping the terminal clean.
    Guards against duplicate handlers if called more than once.
    """
    log = logging.getLogger("downloader")
    log.setLevel(logging.DEBUG)
    log.propagate = False

    # Remove any existing handlers (e.g. if called again with a new path)
    log.handlers.clear()

    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(threadName)-20s] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    log.addHandler(fh)
    log.addHandler(ch)
    return log

# Initialised in main() once the destination folder is known
log: logging.Logger = logging.getLogger("downloader")

# ── State store ────────────────────────────────────────────────────────────────

class StateStore:
    """
    Persists completed download records to a JSON sidecar file.

    Each record: { relative_path -> {size, item_id} }

    A file is skipped if and only if all three conditions hold:
      1. The file exists on disk.
      2. Its size matches the remote size.
      3. The sidecar has an entry for it.

    Written only after a successful, complete download.
    Interrupted downloads leave no sidecar entry and are always resumed.
    Writes are atomic (tmp + os.replace) so a crash never corrupts the file.
    All public methods are thread-safe.
    """

    def __init__(self, destination: str):
        self._path        = os.path.join(destination, STATE_FILE)
        self._destination = destination
        self._lock        = threading.Lock()
        self._data: dict  = {}
        self._load()

    def _rel(self, file_path: str) -> str:
        """Stable relative key, independent of the working directory."""
        return os.path.relpath(file_path, self._destination)

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            log.info("StateStore: loaded %d entries from %s", len(self._data), self._path)
        except Exception as e:
            log.warning("StateStore: load failed (%s) — starting fresh", e)
            self._data = {}

    def _save(self):
        """Atomic write: write to .tmp then rename."""
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            log.error("StateStore: save failed: %s", e)

    def is_recorded(self, file_path: str, size: int) -> bool:
        with self._lock:
            entry = self._data.get(self._rel(file_path))
            return bool(entry and entry.get("size") == size)

    def record(self, file_path: str, size: int, item_id: str):
        key = self._rel(file_path)
        with self._lock:
            self._data[key] = {"size": size, "item_id": item_id}
            self._save()
        log.info("StateStore: recorded %s", key)

    def remove(self, file_path: str):
        key = self._rel(file_path)
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._save()
        log.info("StateStore: removed %s", key)


# ── Terminal display ───────────────────────────────────────────────────────────

def terminal_width() -> int:
    try:
        return os.get_terminal_size().columns
    except Exception:
        return 80

def hide_cursor():
    if sys.stdout.isatty():
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

def show_cursor():
    if sys.stdout.isatty():
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

def format_bar(pct: float, width: int = 16) -> str:
    pct    = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100)
    return "\u2588" * filled + "\u2591" * (width - filled)

def format_size(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f}GB"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.0f}MB"
    return f"{b / 1024:.0f}KB"

def fmt_row(label: str, bar: str, pct: float, left: str, right: str,
            speed: Optional[float] = None) -> str:
    """Format one progress row with consistent column widths."""
    size_col  = f"{left:>6}/{right:<6}" if right else f"{'':>13}"
    speed_col = f"{speed:5.1f} MB/s"   if speed is not None else f"{'':>10}"
    return (
        f"  {label:<32} |{bar}| "
        f"{pct:3.0f}%  "
        f"{size_col}  "
        f"{speed_col}"
    )


class DisplayManager:
    """
    Owns a fixed block of N lines at the bottom of the terminal.
    A background thread redraws the block at DISPLAY_INTERVAL.
    Workers call log_message() for scrolling output and update_slot() for bars.
    All public methods are thread-safe.
    """

    def __init__(self, n_slots: int):
        self.n_slots         = n_slots
        self.lock            = threading.Lock()
        self.slots: dict     = {}
        self.messages: list  = []
        self.total_files     = 0
        self.completed_files = 0
        self._stop           = threading.Event()
        self._thread         = threading.Thread(
            target=self._run, name="display", daemon=True
        )
        self._lines_drawn    = 0

    def start(self):
        hide_cursor()
        sys.stdout.write("\n" * self.n_slots)
        sys.stdout.flush()
        self._lines_drawn = self.n_slots
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1)
        self._erase_block()
        show_cursor()

    def log_message(self, msg: str):
        """Queue a message to be printed above the progress block."""
        with self.lock:
            self.messages.append(msg)

    def update_slot(self, slot_id: int, **kwargs):
        with self.lock:
            if slot_id not in self.slots:
                self.slots[slot_id] = {}
            self.slots[slot_id].update(kwargs)

    def clear_slot(self, slot_id: int):
        with self.lock:
            self.slots.pop(slot_id, None)

    def _erase_block(self):
        if not self._lines_drawn:
            return
        sys.stdout.write(f"\033[{self._lines_drawn}A")
        for _ in range(self._lines_drawn):
            sys.stdout.write("\033[2K\n")
        sys.stdout.write(f"\033[{self._lines_drawn}A")
        sys.stdout.flush()
        self._lines_drawn = 0

    def _run(self):
        while not self._stop.is_set():
            self._redraw()
            time.sleep(DISPLAY_INTERVAL)
        self._redraw()  # final pass to flush any queued messages

    def _redraw(self):
        with self.lock:
            messages  = self.messages[:]
            self.messages.clear()
            slots     = dict(self.slots)
            completed = self.completed_files
            total     = self.total_files

        w = terminal_width()

        if self._lines_drawn:
            sys.stdout.write(f"\033[{self._lines_drawn}A")

        for msg in messages:
            sys.stdout.write(f"\033[2K{msg[:w]}\n")

        lines = []

        overall_pct = 100 * completed / total if total else 0
        lines.append(fmt_row(
            "Overall", format_bar(overall_pct), overall_pct,
            str(completed), str(total),
        ))

        for slot_id in range(self.n_slots - 1):
            s = slots.get(slot_id)
            if s:
                pct = s.get("pct", 0)
                lines.append(fmt_row(
                    s.get("name", "")[:32],
                    format_bar(pct),
                    pct,
                    format_size(s.get("done",  0)),
                    format_size(s.get("total", 0)),
                    speed=s.get("speed", 0),
                ))
            else:
                lines.append("")

        for line in lines:
            sys.stdout.write(f"\033[2K{line[:w]}\n")

        self._lines_drawn = len(lines)
        sys.stdout.flush()


# ── Slot pool ──────────────────────────────────────────────────────────────────

class SlotPool:
    """
    Thread-safe pool of display slot indices (0 .. n-1).
    claim() blocks until a slot is available.
    release() returns a slot to the pool.
    """

    def __init__(self, n: int):
        self._q: queue.Queue = queue.Queue()
        for i in range(n):
            self._q.put(i)

    def claim(self) -> int:
        return self._q.get()

    def release(self, slot_id: int):
        self._q.put(slot_id)

_slot_pool: Optional[SlotPool] = None


# ── Token acquisition ──────────────────────────────────────────────────────────

async def get_badger_token(shared_url: str) -> tuple[str, str, str]:
    """
    Launch a headless Chromium browser, navigate to the shared URL, and
    intercept the Badger auth token + drive/item IDs from outgoing API requests.

    Returns (token, drive_id, root_item_id).
    Raises RuntimeError if anything cannot be captured.
    """
    print("» Acquiring auth token...")
    log.info("Token acquisition start: %s", shared_url)

    token        = None
    drive_id     = None
    root_item_id = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                page    = await context.new_page()

                def on_request(request):
                    nonlocal token, drive_id, root_item_id
                    hdrs = request.headers
                    if ("authorization" in hdrs
                            and hdrs["authorization"].startswith("Badger ")):
                        token = hdrs["authorization"]
                    url = request.url
                    if ("microsoftpersonalcontent.com/_api/v2.0/drives/" in url
                            and "/children" in url):
                        parts = url.split("/drives/")[1].split("/")
                        if drive_id is None:
                            drive_id = parts[0]
                            log.info("Drive ID: %s", drive_id)
                        if root_item_id is None:
                            root_item_id = parts[2]
                            log.info("Root item ID: %s", root_item_id)

                page.on("request", on_request)
                await page.goto(shared_url)
                await page.wait_for_timeout(6000)
            finally:
                await browser.close()

    except Exception as e:
        log.error("Browser error: %s", e, exc_info=True)
        raise RuntimeError(f"Could not launch browser: {e}") from e

    if not token:
        raise RuntimeError(
            "Auth token not captured. The shared link may have expired, "
            "or OneDrive changed its auth flow."
        )
    if not drive_id or not root_item_id:
        raise RuntimeError(
            "Drive ID or root item ID not captured. "
            "Try opening the link in a browser to confirm it still works."
        )

    print(f"\u2713 Token acquired  drive={drive_id}")
    log.info("Token acquisition complete.")
    return token, drive_id, root_item_id


# ── API helpers ────────────────────────────────────────────────────────────────

def make_session(token: str) -> requests.Session:
    """
    Create an API session with auth headers and an automatic retry policy.
    Retries up to 3 times with exponential backoff on server errors and
    rate-limiting responses.
    """
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update({
        "Authorization": token,
        "Accept":        "application/json",
        "User-Agent":    (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Referer": "https://onedrive.live.com/",
    })
    return session


def get_children(session: requests.Session, drive_id: str,
                 item_id: str) -> list:
    """Fetch all children of a drive item, following pagination."""
    url       = f"{API_BASE}/drives/{drive_id}/items/{item_id}/children?$top=100"
    all_items = []
    while url:
        r = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        r.raise_for_status()
        data = r.json()
        all_items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return all_items


def get_item_name(session: requests.Session, drive_id: str,
                  item_id: str) -> Optional[str]:
    """Fetch just the name of a drive item. Returns None on failure."""
    try:
        r = session.get(
            f"{API_BASE}/drives/{drive_id}/items/{item_id}"
            f"?$select=id,name",
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        r.raise_for_status()
        return r.json().get("name")
    except Exception:
        return None


def get_item_details(session: requests.Session, drive_id: str,
                     item_id: str) -> dict:
    """Fetch the download URL for a single item."""
    r = session.get(
        f"{API_BASE}/drives/{drive_id}/items/{item_id}"
        f"?$select=id,name,@content.downloadUrl",
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    r.raise_for_status()
    return r.json()


# ── Folder traversal ───────────────────────────────────────────────────────────

def collect_files(session: requests.Session, drive_id: str,
                  item_id: str, dest_path: str) -> list[tuple[dict, str]]:
    """
    Recursively collect all (item, dest_path) tuples beneath item_id.
    A failed folder listing is logged and skipped rather than crashing.
    """
    os.makedirs(dest_path, exist_ok=True)
    try:
        items = get_children(session, drive_id, item_id)
    except requests.RequestException as e:
        log.error("Could not list folder %s: %s", dest_path, e)
        print(f"  \u2717 Could not list folder: {dest_path} — {e}")
        return []

    print(f"  \u25b8 {dest_path} ({len(items)} items)")
    result = []
    for item in items:
        if "folder" in item:
            result.extend(collect_files(
                session, drive_id, item["id"],
                os.path.join(dest_path, item["name"]),
            ))
        elif "file" in item:
            result.append((item, dest_path))
    return result


def prefetch_details(session: requests.Session, drive_id: str,
                     all_files: list[tuple[dict, str]]) -> dict[str, dict]:
    """
    Fetch download URLs for all files concurrently (8 threads).
    Returns {item_id -> details_dict}. Missing items are logged and excluded.
    """
    log.info("Prefetch start: %d files", len(all_files))
    total     = len(all_files)
    results: dict = {}
    errors    = 0
    completed = 0   # only incremented in the main thread — no lock needed
    lock      = threading.Lock()

    hide_cursor()
    sys.stdout.write("\n")
    sys.stdout.flush()

    def redraw():
        pct  = 100 * completed / total if total else 0
        w    = terminal_width()
        line = fmt_row("Fetching metadata", format_bar(pct), pct,
                       str(completed), str(total))
        sys.stdout.write(f"\033[1A\033[2K{line[:w]}\n")
        sys.stdout.flush()

    def fetch(item):
        return item["id"], get_item_details(session, drive_id, item["id"])

    redraw()
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="prefetch") as executor:
        futures = {
            executor.submit(fetch, item): item["id"]
            for item, _ in all_files
        }
        for future in as_completed(futures):
            try:
                item_id, details = future.result()
                with lock:
                    results[item_id] = details
            except Exception as e:
                errors += 1
                log.error("Prefetch failed for %s: %s", futures[future], e)
            completed += 1
            redraw()

    show_cursor()
    log.info("Prefetch complete: %d ok, %d errors", len(results), errors)
    if errors:
        print(f"  \u25b3 {errors} file(s) could not be fetched — they will be skipped")
    return results


# ── Download ───────────────────────────────────────────────────────────────────

def download_file(item: dict, dest_path: str,
                  details: Optional[dict], shutdown_flag: dict,
                  display: DisplayManager, state: StateStore,
                  session: requests.Session, drive_id: str):
    """
    Download a single file with full lifecycle handling:
      - Skip if already complete and recorded in the sidecar.
      - Resume from a .part file if available.
      - Refresh expired pre-signed URLs (404/410) and retry once.
      - Pause gracefully on shutdown signal.
    """
    if shutdown_flag["stop"]:
        return

    name      = item["name"]
    size      = item.get("size", 0)
    item_id   = item.get("id", "")
    file_path = os.path.join(dest_path, name)
    tmp_path  = file_path + ".part"

    if not details:
        log.warning("No metadata for %s — skipping", name)
        display.log_message(f"  \u25b3 No metadata, skipping: {name}")
        return

    download_url = details.get("@content.downloadUrl")

    # ── Skip ───────────────────────────────────────────────────────────────────
    if os.path.exists(file_path):
        try:
            actual = os.path.getsize(file_path)
        except OSError as e:
            log.error("Cannot stat %s: %s", file_path, e)
            display.log_message(f"  \u2717 Cannot read: {name}")
            return

        if actual == size and state.is_recorded(file_path, size):
            log.info("SKIP %s", name)
            return  # silent skip — completed_files counter still increments

        if actual != size:
            log.warning("WRONG SIZE %s: expected=%d got=%d", name, size, actual)
            display.log_message(f"  \u25b3 Wrong size, re-downloading: {name}")
        else:
            log.info("NO SIDECAR %s — re-downloading", name)
            display.log_message(f"  \u25b9 Re-downloading (not in sidecar): {name}")

        state.remove(file_path)
        try:
            os.remove(file_path)
        except OSError as e:
            log.error("Could not remove %s: %s", file_path, e)
            display.log_message(f"  \u2717 Cannot remove file: {name} — {e}")
            return

    if not download_url:
        log.error("No download URL for %s", name)
        display.log_message(f"  \u2717 No download URL: {name}")
        return

    # ── Resume ─────────────────────────────────────────────────────────────────
    resume_from = 0
    if os.path.exists(tmp_path):
        try:
            resume_from = os.path.getsize(tmp_path)
        except OSError:
            resume_from = 0

        if resume_from >= size:
            try:
                os.replace(tmp_path, file_path)
                state.record(file_path, size, item_id)
                display.log_message(f"  \u2713 Done (resumed complete): {name}")
            except OSError as e:
                log.error("Could not promote .part for %s: %s", name, e)
                display.log_message(f"  \u2717 Could not finalise: {name} — {e}")
            return

    req_headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}

    # ── Download ───────────────────────────────────────────────────────────────
    if _slot_pool is None:
        raise RuntimeError("SlotPool not initialised — this is a bug, please report it")
    slot = _slot_pool.claim()
    try:
        log.info("START %s size=%d resume=%d slot=%d", name, size, resume_from, slot)

        r = requests.get(
            download_url, stream=True, headers=req_headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )

        # Refresh an expired pre-signed URL and retry once
        if r.status_code in (404, 410):
            log.warning("URL expired for %s (status=%d) — refreshing", name, r.status_code)
            display.log_message(f"  \u21ba Refreshing URL: {name}")
            try:
                fresh        = get_item_details(session, drive_id, item_id)
                download_url = fresh.get("@content.downloadUrl")
            except requests.RequestException as e:
                log.error("URL refresh failed for %s: %s", name, e)
                display.log_message(f"  \u2717 URL refresh failed: {name}")
                return
            if not download_url:
                log.error("No URL after refresh for %s", name)
                display.log_message(f"  \u2717 No URL after refresh: {name}")
                return
            r = requests.get(
                download_url, stream=True, headers=req_headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )

        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            log.error("HTTP error for %s: %s", name, e)
            display.log_message(f"  \u2717 HTTP {r.status_code}: {name}")
            return

        bytes_written = 0
        t_start       = time.monotonic()

        try:
            with open(tmp_path, "ab" if resume_from else "wb") as f:
                for chunk in r.iter_content(CHUNK_SIZE):
                    if shutdown_flag["stop"]:
                        log.info("PAUSED %s at %d bytes",
                                 name, resume_from + bytes_written)
                        display.log_message(f"  \u2016 Paused: {name}")
                        return
                    f.write(chunk)
                    bytes_written += len(chunk)
                    elapsed = time.monotonic() - t_start
                    speed   = bytes_written / elapsed / 1024 / 1024 if elapsed > 0 else 0
                    done    = resume_from + bytes_written
                    pct     = 100 * done / size if size > 0 else 100.0
                    display.update_slot(slot, name=name, pct=pct,
                                        done=done, total=size, speed=speed)
        except (requests.RequestException, OSError) as e:
            log.error("Download interrupted for %s: %s", name, e)
            display.log_message(f"  \u2717 Download error: {name} — {e}")
            return

        try:
            os.replace(tmp_path, file_path)
        except OSError as e:
            log.error("Could not promote .part for %s: %s", name, e)
            display.log_message(f"  \u2717 Could not finalise: {name} — {e}")
            return

        elapsed   = time.monotonic() - t_start
        speed_avg = bytes_written / elapsed / 1024 / 1024 if elapsed > 0 else 0
        log.info("DONE %s — %.1f MB/s", name, speed_avg)
        display.log_message(f"  \u2713 Done  {name}  ({speed_avg:.1f} MB/s avg)")
        state.record(file_path, size, item_id)

    except Exception as e:
        log.error("Unexpected error for %s: %s", name, e, exc_info=True)
        display.log_message(f"  \u2717 Unexpected error: {name} — {e}")
    finally:
        display.clear_slot(slot)
        _slot_pool.release(slot)


# ── Orchestrator ───────────────────────────────────────────────────────────────

def download_all(session: requests.Session, drive_id: str,
                 root_item_id: str, destination: str,
                 shutdown_flag: dict):
    global _slot_pool
    _slot_pool = SlotPool(MAX_WORKERS)

    log.info("=== download_all start ===")
    print("\n» Scanning folders...")

    all_files = collect_files(session, drive_id, root_item_id, destination)
    total     = len(all_files)
    log.info("Collected %d files", total)

    if total == 0:
        print("  No files found.")
        return

    metadata = prefetch_details(session, drive_id, all_files)
    state    = StateStore(destination)

    # Count skippable files without risking OSError between exists+getsize
    skip_count = 0
    for item, dest in all_files:
        fp = os.path.join(dest, item["name"])
        try:
            if (os.path.exists(fp)
                    and os.path.getsize(fp) == item.get("size", 0)
                    and state.is_recorded(fp, item.get("size", 0))):
                skip_count += 1
        except OSError:
            pass

    if skip_count:
        print(f"  \u2713 {skip_count}/{total} already complete, skipping")

    print(f"\n» {total} files — starting {MAX_WORKERS} workers\n")

    display             = DisplayManager(n_slots=MAX_WORKERS + 1)
    display.total_files = total
    display.start()

    def _sigint_handler(sig, frame):
        if shutdown_flag["stop"]:
            log.info("Second SIGINT — forcing exit")
            display.stop()
            logging.shutdown()
            os._exit(1)
        shutdown_flag["stop"] = True
        display.log_message("  \u25b3 Interrupted — finishing current chunks...")
        log.info("SIGINT — shutdown flag set")

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS, thread_name_prefix="worker"
        ) as executor:
            futures = {
                executor.submit(
                    download_file,
                    item, dest, metadata.get(item["id"]),
                    shutdown_flag, display, state, session, drive_id,
                ): item["name"]
                for item, dest in all_files
            }
            completed = 0
            for future in as_completed(futures):
                if shutdown_flag["stop"]:
                    break
                name = futures[future]
                try:
                    future.result()
                except Exception as e:
                    log.error("Future raised for %s: %s", name, e, exc_info=True)
                    display.log_message(f"  \u2717 Failed: {name} — {e}")
                completed += 1
                display.completed_files = completed
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        display.stop()
        log.info("=== download_all end ===")

    if shutdown_flag["stop"]:
        print("\n\u2713 Paused cleanly. Re-run to resume.")
        logging.shutdown()
        os._exit(0)


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) < 2:
        print("Usage:   python downloader.py <onedrive_shared_url> [destination]")
        print("Example: python downloader.py 'https://1drv.ms/f/...' ~/Downloads/footage")
        sys.exit(1)

    shared_url    = sys.argv[1]
    shutdown_flag = {"stop": False}

    try:
        token, drive_id, root_item_id = await get_badger_token(shared_url)
    except RuntimeError as e:
        print(f"\u2717 {e}")
        sys.exit(1)

    session = make_session(token)

    # Derive default destination from the root folder name
    if len(sys.argv) > 2:
        destination = os.path.expanduser(sys.argv[2])
    else:
        root_name   = get_item_name(session, drive_id, root_item_id)
        folder_name = root_name if root_name else "onedrive_download"
        destination = os.path.expanduser(f"~/Downloads/{folder_name}")

    # Create destination folder early so the log file can live there
    os.makedirs(destination, exist_ok=True)

    global log
    log = setup_logging(os.path.join(destination, LOG_FILENAME))
    log.info("=== Session start === url=%s dest=%s", shared_url, destination)

    download_all(session, drive_id, root_item_id, destination, shutdown_flag)

    if not shutdown_flag["stop"]:
        log.info("=== All done ===")
        print(f"\n\u2713 All done! Files saved to: {destination}")


if __name__ == "__main__":
    asyncio.run(main())
