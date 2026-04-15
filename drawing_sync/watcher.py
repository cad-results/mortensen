"""File System Watcher.

Monitors drawing directories for file changes and triggers
re-extraction and mismatch detection automatically.

Uses watchdog library for cross-platform file system events.
"""

import os
import time
import logging
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .sync_engine import SyncEngine

logger = logging.getLogger("drawing_sync.watcher")


class DrawingChangeHandler(FileSystemEventHandler):
    """Handles file system events for drawing files."""

    WATCHED_EXTENSIONS = {".pdf", ".dxf", ".dwg", ".xlsx", ".xls"}

    def __init__(self, engine: SyncEngine, callback=None):
        super().__init__()
        self.engine = engine
        self.callback = callback
        self._debounce = {}
        self._debounce_seconds = 2  # Wait 2s after last change before processing

    def on_modified(self, event):
        if event.is_directory:
            return
        self._handle_change(event.src_path, "modified")

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle_change(event.src_path, "created")

    def _handle_change(self, file_path: str, change_type: str):
        """Handle a file change event with debouncing."""
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in self.WATCHED_EXTENSIONS:
            return

        # Skip backup/temp files
        basename = os.path.basename(file_path)
        if basename.startswith(".") or basename.startswith("~") or ext == ".bak":
            return

        # Debounce: many editors save files multiple times
        now = time.time()
        last = self._debounce.get(file_path, 0)
        if now - last < self._debounce_seconds:
            return
        self._debounce[file_path] = now

        drawing_id = os.path.splitext(basename)[0]
        logger.info(f"Change detected: {file_path} ({change_type})")

        try:
            # Re-extract the changed file
            drawing = self.engine.scan_single_file(file_path)
            if not drawing:
                return

            logger.info(
                f"  Re-extracted {drawing_id}: "
                f"{len(drawing.components)} components, "
                f"{len(drawing.connections)} connections"
            )

            # Check for mismatches involving this drawing's components
            mismatches = self.engine.check_mismatches()
            relevant = [
                m for m in mismatches
                if drawing_id in m.drawings_involved
            ]

            if relevant:
                logger.warning(
                    f"  {len(relevant)} mismatch(es) detected after update!"
                )
                for m in relevant:
                    severity_icon = {
                        "CRITICAL": "[!!!]",
                        "WARNING": "[!]",
                        "INFO": "[i]",
                    }.get(m.severity.value, "[?]")
                    logger.warning(f"    {severity_icon} {m.message}")

            # Determine which other drawings are affected
            propagation = {}
            for comp_id in drawing.components:
                result = self.engine.propagate_update(drawing_id, comp_id)
                if result.get("affected_drawings"):
                    propagation[comp_id] = result

            if propagation:
                logger.info(
                    f"  Update propagation needed for "
                    f"{len(propagation)} component(s):"
                )
                for comp_id, prop in propagation.items():
                    affected = prop["affected_drawings"]
                    logger.info(
                        f"    {comp_id} -> affects {len(affected)} drawing(s): "
                        f"{', '.join(affected[:5])}"
                    )

            # Notify callback if provided
            if self.callback:
                self.callback({
                    "event": change_type,
                    "drawing_id": drawing_id,
                    "file_path": file_path,
                    "components_extracted": len(drawing.components),
                    "mismatches": len(relevant),
                    "propagation": propagation,
                    "timestamp": datetime.now().isoformat(),
                })

        except Exception as e:
            logger.error(f"  Error processing {file_path}: {e}")


class DrawingWatcher:
    """Watches directories for drawing file changes."""

    def __init__(self, engine: SyncEngine, callback=None):
        self.engine = engine
        self.callback = callback
        self.observer = Observer()
        self.handler = DrawingChangeHandler(engine, callback)
        self._watched_paths = []

    def watch(self, directory: str, recursive: bool = True):
        """Add a directory to watch."""
        if not os.path.isdir(directory):
            raise ValueError(f"Directory not found: {directory}")

        self.observer.schedule(self.handler, directory, recursive=recursive)
        self._watched_paths.append(directory)
        logger.info(f"Watching: {directory} (recursive={recursive})")

    def start(self):
        """Start watching for changes."""
        if not self._watched_paths:
            raise RuntimeError("No directories to watch. Call watch() first.")

        logger.info("Starting file watcher...")
        self.observer.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def start_background(self):
        """Start watching in the background (non-blocking)."""
        self.observer.start()
        logger.info("File watcher started in background")

    def stop(self):
        """Stop watching."""
        logger.info("Stopping file watcher...")
        self.observer.stop()
        self.observer.join()
        logger.info("File watcher stopped")
