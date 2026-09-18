"""
P-drive folder watcher using PollingObserver (reliable on Windows network shares).
Uses a settle pattern: files are only ingested after their size has been
stable for SETTLE_SECONDS to avoid ingesting partial writes from RayStation.

Dispatch is per FOLDER, not per file: a folder's ingest callback fires exactly
once, when it contains at least one settled file and no sibling file is still
pending/growing. (The previous per-file dispatch fired the callback once per
settled file -- a multi-file drop triggered the pipeline N times, and files
settling on different poll cycles re-fired it again.)

STARTUPSWEEP_V1: on start(), any .dcm files already present in the watch tree
are seeded into the pending set as if they had just arrived. Previously the
watcher only reacted to on_created events, so files that landed while the
service was down (or whose ingest failed before cleanup) sat in the watch
folder forever until an unrelated new drop re-triggered a folder scan.
"""
from __future__ import annotations
import logging
import threading
import time
from pathlib import Path
from watchdog.events import FileCreatedEvent, FileSystemEventHandler
from watchdog.observers.polling import PollingObserver
logger = logging.getLogger(__name__)
SETTLE_SECONDS = 5
POLL_INTERVAL = 3


class DicomArrivalHandler(FileSystemEventHandler):
    """
    Watches for new .dcm files. Holds each file in a pending dict until its
    size has been stable for SETTLE_SECONDS; a folder is dispatched once ALL
    of its pending files have settled.
    """
    def __init__(self, ingest_callback):
        super().__init__()
        self.ingest_callback = ingest_callback
        # path -> (last_known_size, timestamp_of_last_change)
        self._pending: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def on_created(self, event: FileCreatedEvent):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() == ".dcm":
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            with self._lock:
                self._pending[str(path)] = (size, time.time())
            logger.info(f"New DICOM file detected: {path.name}")

    def seed_path(self, path: Path) -> bool:
        """
        STARTUPSWEEP_V1: enqueue an already-existing file into the pending set
        so it flows through the normal settle -> dispatch machinery. Returns
        True if seeded. Safe to call for files already pending (no-op refresh).
        """
        try:
            size = path.stat().st_size
        except OSError:
            return False
        with self._lock:
            if str(path) not in self._pending:
                self._pending[str(path)] = (size, time.time())
                return True
        return False

    def check_settled(self):
        """
        Called periodically by the watcher thread. A folder is ready when it
        has settled files and NO still-growing/unsettled siblings; each ready
        folder is dispatched exactly once and its files leave the pending set.
        """
        now = time.time()
        ready_parents: list[str] = []
        settled_by_parent: dict[str, list[str]] = {}
        with self._lock:
            # Refresh sizes; drop vanished files; reset timers on growth.
            for path_str, (last_size, last_changed) in list(self._pending.items()):
                path = Path(path_str)
                if not path.exists():
                    del self._pending[path_str]
                    continue
                try:
                    current_size = path.stat().st_size
                except OSError:
                    continue
                if current_size != last_size:
                    self._pending[path_str] = (current_size, now)

            # Group by parent: settled vs still-pending siblings.
            unsettled_parents: set[str] = set()
            for path_str, (_size, changed) in self._pending.items():
                parent = str(Path(path_str).parent)
                if now - changed >= SETTLE_SECONDS:
                    settled_by_parent.setdefault(parent, []).append(path_str)
                else:
                    unsettled_parents.add(parent)

            for parent, files in list(settled_by_parent.items()):
                if parent in unsettled_parents:
                    # A sibling in this folder is still being written -- hold
                    # the whole folder until the batch completes.
                    continue
                for f in files:
                    del self._pending[f]
                ready_parents.append(parent)

        for parent in sorted(set(ready_parents)):
            self._dispatch(parent, n_files=len(settled_by_parent.get(parent, [])))

    def _dispatch(self, parent: str, n_files: int = 0):
        logger.info(
            f"Folder settled ({n_files} file(s)), scheduling ingestion from: {parent}"
        )
        try:
            self.ingest_callback(parent)
        except Exception as exc:
            logger.error(f"Ingest callback failed for {parent}: {exc}")


class DicomFolderWatcher:
    """
    Starts a PollingObserver on the configured watch folder.
    Call start() once at app startup -- runs in a daemon thread.
    """
    def __init__(self, watch_path: str, ingest_callback):
        self.watch_path = watch_path
        self.handler = DicomArrivalHandler(ingest_callback)
        self.observer = PollingObserver(timeout=POLL_INTERVAL)
        self.observer.schedule(self.handler, watch_path, recursive=True)
        self._settle_thread: threading.Thread | None = None

    def _seed_existing(self):
        """
        STARTUPSWEEP_V1: queue any .dcm files already sitting in the watch
        tree at service start. They go through the same settle window as new
        arrivals, so a file mid-copy at startup is still handled safely.
        """
        seeded = 0
        try:
            for path in Path(self.watch_path).rglob("*.dcm"):
                if self.handler.seed_path(path):
                    seeded += 1
        except OSError as exc:
            logger.warning(f"Startup sweep failed on {self.watch_path}: {exc}")
        if seeded:
            logger.info(
                f"Startup sweep: queued {seeded} pre-existing DICOM file(s) "
                f"for ingestion from {self.watch_path}"
            )

    def start(self):
        self._seed_existing()
        self.observer.start()
        logger.info(f"DICOM folder watcher started on: {self.watch_path}")
        self._settle_thread = threading.Thread(target=self._settle_loop, daemon=True)
        self._settle_thread.start()

    def stop(self):
        self.observer.stop()
        self.observer.join()

    def _settle_loop(self):
        while self.observer.is_alive():
            self.handler.check_settled()
            time.sleep(POLL_INTERVAL)
