"""Component Registry Database.

SQLite-backed database that stores:
- All drawings and their metadata
- All components with properties and values
- Component-to-drawing mapping (which drawings contain which components)
- Connection/wiring data
- Historical snapshots for change tracking
- Mismatch/alert records
"""

import sqlite3
import json
import os
import hashlib
from datetime import datetime
from typing import Optional

from .models import (
    Component, ComponentType, ComponentValue, Connection,
    DrawingData, Mismatch, AlertSeverity,
)


class ComponentDatabase:
    """SQLite database for the component registry."""

    def __init__(self, db_path: str = "drawing_sync.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_tables()

    def _create_tables(self):
        """Create all database tables."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS drawings (
                drawing_id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_hash TEXT,
                title_block_json TEXT DEFAULT '{}',
                raw_text TEXT DEFAULT '',
                notes_json TEXT DEFAULT '[]',
                cable_schedule_json TEXT DEFAULT '[]',
                terminal_blocks_json TEXT DEFAULT '{}',
                voltage_levels_json TEXT DEFAULT '[]',
                cross_references_json TEXT DEFAULT '[]',
                last_scanned TIMESTAMP,
                last_modified TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS components (
                component_id TEXT NOT NULL,
                drawing_id TEXT NOT NULL,
                component_type TEXT NOT NULL,
                description TEXT DEFAULT '',
                values_json TEXT DEFAULT '[]',
                connections_json TEXT DEFAULT '[]',
                labels_json TEXT DEFAULT '[]',
                drawing_refs_json TEXT DEFAULT '[]',
                attributes_json TEXT DEFAULT '{}',
                PRIMARY KEY (component_id, drawing_id),
                FOREIGN KEY (drawing_id) REFERENCES drawings(drawing_id)
            );

            CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drawing_id TEXT NOT NULL,
                from_component TEXT NOT NULL,
                from_terminal TEXT DEFAULT '',
                to_component TEXT NOT NULL,
                to_terminal TEXT DEFAULT '',
                cable_spec TEXT DEFAULT '',
                wire_label TEXT DEFAULT '',
                signal_type TEXT DEFAULT '',
                FOREIGN KEY (drawing_id) REFERENCES drawings(drawing_id)
            );

            CREATE TABLE IF NOT EXISTS labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drawing_id TEXT NOT NULL,
                text TEXT NOT NULL,
                x REAL DEFAULT 0,
                y REAL DEFAULT 0,
                category TEXT DEFAULT 'text',
                FOREIGN KEY (drawing_id) REFERENCES drawings(drawing_id)
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drawing_id TEXT NOT NULL,
                snapshot_time TIMESTAMP NOT NULL,
                components_json TEXT NOT NULL,
                file_hash TEXT,
                FOREIGN KEY (drawing_id) REFERENCES drawings(drawing_id)
            );

            CREATE TABLE IF NOT EXISTS mismatches (
                mismatch_id TEXT PRIMARY KEY,
                severity TEXT NOT NULL,
                component_id TEXT NOT NULL,
                parameter TEXT NOT NULL,
                drawings_involved_json TEXT NOT NULL,
                values_found_json TEXT NOT NULL,
                message TEXT NOT NULL,
                recommendation TEXT DEFAULT '',
                detected_at TIMESTAMP NOT NULL,
                resolved_at TIMESTAMP,
                resolved BOOLEAN DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL,
                drawing_id TEXT NOT NULL,
                component_id TEXT,
                change_type TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                description TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_components_drawing
                ON components(drawing_id);
            CREATE INDEX IF NOT EXISTS idx_components_id
                ON components(component_id);
            CREATE INDEX IF NOT EXISTS idx_connections_drawing
                ON connections(drawing_id);
            CREATE INDEX IF NOT EXISTS idx_labels_drawing
                ON labels(drawing_id);
            CREATE INDEX IF NOT EXISTS idx_mismatches_component
                ON mismatches(component_id);
            CREATE INDEX IF NOT EXISTS idx_change_log_drawing
                ON change_log(drawing_id);
            CREATE INDEX IF NOT EXISTS idx_change_log_component
                ON change_log(component_id);

            CREATE TABLE IF NOT EXISTS propagation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                source_drawing_id TEXT NOT NULL,
                target_drawing_id TEXT NOT NULL,
                component_id TEXT NOT NULL,
                parameter TEXT NOT NULL,
                old_value TEXT DEFAULT '',
                new_value TEXT DEFAULT '',
                authority_basis TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'PROPOSED',
                approved_by TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_propagation_component
                ON propagation_log(component_id);
            CREATE INDEX IF NOT EXISTS idx_propagation_status
                ON propagation_log(status);
        """)
        self.conn.commit()

        # Idempotent migration: add drawing_type column
        try:
            self.conn.execute("ALTER TABLE drawings ADD COLUMN drawing_type TEXT DEFAULT ''")
            self.conn.commit()
        except Exception:
            pass  # Column already exists

        # Idempotent migration: add index_metadata_json column
        try:
            self.conn.execute(
                "ALTER TABLE drawings ADD COLUMN index_metadata_json TEXT DEFAULT '{}'"
            )
            self.conn.commit()
        except Exception:
            pass  # Column already exists

        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_drawings_type ON drawings(drawing_type);")
        self.conn.commit()

    def store_drawing(self, drawing: DrawingData):
        """Store or update a complete drawing and all its components.

        Wrapped in a single transaction — either everything is stored or nothing.
        """
        now = datetime.now().isoformat()

        # Compute file hash for change detection
        file_hash = self._compute_file_hash(drawing.file_path)

        try:
            # Check if drawing already exists — snapshot old data first
            existing = self.get_drawing(drawing.drawing_id)
            if existing:
                self._snapshot_drawing(drawing.drawing_id)
                self._detect_changes(existing, drawing)

            # Upsert drawing
            self.conn.execute("""
                INSERT OR REPLACE INTO drawings
                (drawing_id, file_path, file_type, file_hash, title_block_json,
                 raw_text, notes_json, cable_schedule_json, terminal_blocks_json,
                 voltage_levels_json, cross_references_json, last_scanned, last_modified,
                 drawing_type, index_metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                drawing.drawing_id, drawing.file_path, drawing.file_type, file_hash,
                json.dumps(drawing.title_block.to_dict()),
                drawing.raw_text,
                json.dumps(drawing.notes),
                json.dumps(drawing.cable_schedule),
                json.dumps(drawing.terminal_blocks),
                json.dumps(drawing.voltage_levels),
                json.dumps(drawing.cross_references),
                now, now,
                getattr(drawing, 'drawing_type', ''),
                json.dumps(getattr(drawing, 'index_metadata', {})),
            ))

            # Clear old component/connection/label data for this drawing
            self.conn.execute(
                "DELETE FROM components WHERE drawing_id = ?",
                (drawing.drawing_id,),
            )
            self.conn.execute(
                "DELETE FROM connections WHERE drawing_id = ?",
                (drawing.drawing_id,),
            )
            self.conn.execute(
                "DELETE FROM labels WHERE drawing_id = ?",
                (drawing.drawing_id,),
            )

            # Insert components
            for comp_id, comp in drawing.components.items():
                self.conn.execute("""
                    INSERT INTO components
                    (component_id, drawing_id, component_type, description,
                     values_json, connections_json, labels_json,
                     drawing_refs_json, attributes_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    comp_id, drawing.drawing_id, comp.component_type.value,
                    comp.description,
                    json.dumps([v.to_dict() for v in comp.values]),
                    json.dumps([c.to_dict() for c in comp.connections]),
                    json.dumps([l.to_dict() for l in comp.labels]),
                    json.dumps(comp.drawing_refs),
                    json.dumps(comp.attributes),
                ))

            # Insert connections
            for conn in drawing.connections:
                self.conn.execute("""
                    INSERT INTO connections
                    (drawing_id, from_component, from_terminal, to_component,
                     to_terminal, cable_spec, wire_label, signal_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    drawing.drawing_id, conn.from_component, conn.from_terminal,
                    conn.to_component, conn.to_terminal,
                    conn.cable_spec, conn.wire_label, conn.signal_type,
                ))

            # Insert labels (batch insert for performance)
            label_data = [
                (drawing.drawing_id, l.text, l.x, l.y, l.category)
                for l in drawing.all_labels
            ]
            self.conn.executemany("""
                INSERT INTO labels (drawing_id, text, x, y, category)
                VALUES (?, ?, ?, ?, ?)
            """, label_data)

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_drawing(self, drawing_id: str) -> Optional[dict]:
        """Get a drawing's data from the database."""
        row = self.conn.execute(
            "SELECT * FROM drawings WHERE drawing_id = ?",
            (drawing_id,),
        ).fetchone()

        if not row:
            return None

        return dict(row)

    def get_component_across_drawings(self, component_id: str) -> list:
        """Get all instances of a component across all drawings.

        Returns list of dicts with drawing_id and component data.
        """
        rows = self.conn.execute("""
            SELECT c.*, d.file_path, d.file_type
            FROM components c
            JOIN drawings d ON c.drawing_id = d.drawing_id
            WHERE c.component_id = ?
            ORDER BY c.drawing_id
        """, (component_id,)).fetchall()

        return [dict(r) for r in rows]

    def get_all_components(self) -> dict:
        """Get all unique components and their drawing appearances.

        Returns dict: component_id -> list of drawing_ids
        """
        rows = self.conn.execute("""
            SELECT component_id, GROUP_CONCAT(drawing_id, '||') as drawings
            FROM components
            GROUP BY component_id
            ORDER BY component_id
        """).fetchall()

        return {
            row["component_id"]: row["drawings"].split("||")
            for row in rows
        }

    def get_shared_components(self, min_drawings: int = 2) -> dict:
        """Get components that appear in multiple drawings.

        Returns dict: component_id -> list of drawing_ids
        """
        rows = self.conn.execute("""
            SELECT component_id, GROUP_CONCAT(drawing_id, '||') as drawings,
                   COUNT(drawing_id) as count
            FROM components
            GROUP BY component_id
            HAVING count >= ?
            ORDER BY count DESC
        """, (min_drawings,)).fetchall()

        return {
            row["component_id"]: row["drawings"].split("||")
            for row in rows
        }

    def get_drawing_cross_references(self, drawing_id: str) -> list:
        """Get cross-references from a drawing to other drawings."""
        row = self.conn.execute(
            "SELECT cross_references_json FROM drawings WHERE drawing_id = ?",
            (drawing_id,),
        ).fetchone()

        if row:
            return json.loads(row["cross_references_json"])
        return []

    def get_component_values(self, component_id: str) -> dict:
        """Get all values for a component across all drawings.

        Returns dict: drawing_id -> list of ComponentValue dicts
        """
        rows = self.conn.execute("""
            SELECT drawing_id, values_json
            FROM components
            WHERE component_id = ?
        """, (component_id,)).fetchall()

        return {
            row["drawing_id"]: json.loads(row["values_json"])
            for row in rows
        }

    def get_connections_for_component(self, component_id: str) -> list:
        """Get all connections involving a component."""
        rows = self.conn.execute("""
            SELECT * FROM connections
            WHERE from_component = ? OR to_component = ?
        """, (component_id, component_id)).fetchall()

        return [dict(r) for r in rows]

    def store_mismatch(self, mismatch: Mismatch):
        """Store a detected mismatch."""
        self.conn.execute("""
            INSERT OR REPLACE INTO mismatches
            (mismatch_id, severity, component_id, parameter,
             drawings_involved_json, values_found_json,
             message, recommendation, detected_at, resolved)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            mismatch.mismatch_id, mismatch.severity.value,
            mismatch.component_id, mismatch.parameter,
            json.dumps(mismatch.drawings_involved),
            json.dumps(mismatch.values_found),
            mismatch.message, mismatch.recommendation,
            datetime.now().isoformat(),
        ))
        self.conn.commit()

    def get_active_mismatches(self) -> list:
        """Get all unresolved mismatches."""
        rows = self.conn.execute("""
            SELECT * FROM mismatches
            WHERE resolved = 0
            ORDER BY
                CASE severity
                    WHEN 'CRITICAL' THEN 1
                    WHEN 'WARNING' THEN 2
                    WHEN 'INFO' THEN 3
                END,
                detected_at DESC
        """).fetchall()

        return [dict(r) for r in rows]

    def resolve_mismatch(self, mismatch_id: str):
        """Mark a mismatch as resolved."""
        self.conn.execute("""
            UPDATE mismatches
            SET resolved = 1, resolved_at = ?
            WHERE mismatch_id = ?
        """, (datetime.now().isoformat(), mismatch_id))
        self.conn.commit()

    def get_change_log(self, drawing_id: Optional[str] = None, limit: int = 100) -> list:
        """Get recent changes."""
        if drawing_id:
            rows = self.conn.execute("""
                SELECT * FROM change_log
                WHERE drawing_id = ?
                ORDER BY timestamp DESC LIMIT ?
            """, (drawing_id, limit)).fetchall()
        else:
            rows = self.conn.execute("""
                SELECT * FROM change_log
                ORDER BY timestamp DESC LIMIT ?
            """, (limit,)).fetchall()

        return [dict(r) for r in rows]

    def log_change(self, drawing_id: str, component_id: str,
                   change_type: str, old_value: str, new_value: str,
                   description: str = ""):
        """Log a change for audit trail."""
        self.conn.execute("""
            INSERT INTO change_log
            (timestamp, drawing_id, component_id, change_type,
             old_value, new_value, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(), drawing_id, component_id,
            change_type, old_value, new_value, description,
        ))
        self.conn.commit()

    def log_propagation(
        self,
        action_id: str,
        timestamp: str,
        source: str,
        target: str,
        component_id: str,
        parameter: str,
        old_val: str,
        new_val: str,
        authority_basis: str,
        status: str,
        approved_by: str = "",
    ):
        """Insert a record into propagation_log."""
        self.conn.execute("""
            INSERT INTO propagation_log
            (action_id, timestamp, source_drawing_id, target_drawing_id,
             component_id, parameter, old_value, new_value,
             authority_basis, status, approved_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            action_id, timestamp, source, target,
            component_id, parameter, old_val, new_val,
            authority_basis, status, approved_by,
        ))
        self.conn.commit()

    def get_propagation_log(
        self,
        component_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        """Query propagation_log with optional filters.

        Returns list of dicts.
        """
        clauses = []
        params = []

        if component_id:
            clauses.append("component_id = ?")
            params.append(component_id)
        if status:
            clauses.append("status = ?")
            params.append(status)

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        params.append(limit)
        rows = self.conn.execute(f"""
            SELECT * FROM propagation_log
            {where}
            ORDER BY timestamp DESC
            LIMIT ?
        """, params).fetchall()

        return [dict(r) for r in rows]

    def update_component_value(
        self,
        drawing_id: str,
        component_id: str,
        parameter: str,
        new_value: str,
    ) -> bool:
        """Update a single parameter value for a component in a drawing.

        1. Read current values_json from components table
        2. Parse it as a list of value dicts
        3. Find the dict where parameter matches and update its value
        4. Write back the JSON
        5. Return True on success, False if component or parameter not found
        """
        row = self.conn.execute(
            "SELECT values_json FROM components WHERE component_id = ? AND drawing_id = ?",
            (component_id, drawing_id),
        ).fetchone()

        if not row:
            return False

        values = json.loads(row["values_json"])
        found = False

        for v in values:
            if v.get("parameter") == parameter:
                v["value"] = new_value
                found = True
                break  # Update first matching parameter entry

        if not found:
            return False

        self.conn.execute(
            "UPDATE components SET values_json = ? WHERE component_id = ? AND drawing_id = ?",
            (json.dumps(values), component_id, drawing_id),
        )
        self.conn.commit()
        return True

    def has_drawing_changed(self, drawing_id: str, file_path: str) -> bool:
        """Check if a drawing file has changed since last scan."""
        row = self.conn.execute(
            "SELECT file_hash FROM drawings WHERE drawing_id = ?",
            (drawing_id,),
        ).fetchone()

        if not row:
            return True  # New drawing

        current_hash = self._compute_file_hash(file_path)
        return current_hash != row["file_hash"]

    def get_all_drawing_ids(self) -> list:
        """Get all drawing IDs in the database."""
        rows = self.conn.execute(
            "SELECT drawing_id FROM drawings ORDER BY drawing_id",
        ).fetchall()
        return [row["drawing_id"] for row in rows]

    def get_statistics(self) -> dict:
        """Get database statistics."""
        stats = {}
        stats["total_drawings"] = self.conn.execute(
            "SELECT COUNT(*) FROM drawings",
        ).fetchone()[0]
        stats["total_components"] = self.conn.execute(
            "SELECT COUNT(DISTINCT component_id) FROM components",
        ).fetchone()[0]
        stats["total_component_instances"] = self.conn.execute(
            "SELECT COUNT(*) FROM components",
        ).fetchone()[0]
        stats["total_connections"] = self.conn.execute(
            "SELECT COUNT(*) FROM connections",
        ).fetchone()[0]
        stats["total_labels"] = self.conn.execute(
            "SELECT COUNT(*) FROM labels",
        ).fetchone()[0]
        stats["active_mismatches"] = self.conn.execute(
            "SELECT COUNT(*) FROM mismatches WHERE resolved = 0",
        ).fetchone()[0]
        stats["shared_components"] = self.conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT component_id FROM components
                GROUP BY component_id HAVING COUNT(drawing_id) >= 2
            )
        """).fetchone()[0]
        return stats

    def _snapshot_drawing(self, drawing_id: str):
        """Create a snapshot of current drawing data before update."""
        rows = self.conn.execute("""
            SELECT component_id, values_json, attributes_json
            FROM components WHERE drawing_id = ?
        """, (drawing_id,)).fetchall()

        if rows:
            components_data = {
                r["component_id"]: {
                    "values": r["values_json"],
                    "attributes": r["attributes_json"],
                }
                for r in rows
            }

            drawing_row = self.conn.execute(
                "SELECT file_hash FROM drawings WHERE drawing_id = ?",
                (drawing_id,),
            ).fetchone()

            self.conn.execute("""
                INSERT INTO snapshots
                (drawing_id, snapshot_time, components_json, file_hash)
                VALUES (?, ?, ?, ?)
            """, (
                drawing_id, datetime.now().isoformat(),
                json.dumps(components_data),
                drawing_row["file_hash"] if drawing_row else None,
            ))

    def _detect_changes(self, old_drawing: dict, new_drawing: DrawingData):
        """Detect and log changes between old and new drawing data."""
        drawing_id = new_drawing.drawing_id

        # Get old components
        old_components = {}
        rows = self.conn.execute(
            "SELECT * FROM components WHERE drawing_id = ?",
            (drawing_id,),
        ).fetchall()
        for r in rows:
            old_components[r["component_id"]] = dict(r)

        # Detect new components
        for comp_id in new_drawing.components:
            if comp_id not in old_components:
                self.log_change(
                    drawing_id, comp_id, "COMPONENT_ADDED",
                    "", comp_id,
                    f"New component {comp_id} detected in {drawing_id}",
                )

        # Detect removed components
        for comp_id in old_components:
            if comp_id not in new_drawing.components:
                self.log_change(
                    drawing_id, comp_id, "COMPONENT_REMOVED",
                    comp_id, "",
                    f"Component {comp_id} removed from {drawing_id}",
                )

        # Detect value changes
        for comp_id, comp in new_drawing.components.items():
            if comp_id in old_components:
                old_vals = json.loads(old_components[comp_id]["values_json"])
                new_vals = [v.to_dict() for v in comp.values]

                old_val_set = {(v["parameter"], v["value"]) for v in old_vals}
                new_val_set = {(v["parameter"], v["value"]) for v in new_vals}

                if old_val_set != new_val_set:
                    self.log_change(
                        drawing_id, comp_id, "VALUE_CHANGED",
                        json.dumps(old_vals), json.dumps(new_vals),
                        f"Component {comp_id} values changed in {drawing_id}",
                    )

    @staticmethod
    def _compute_file_hash(file_path: str) -> str:
        """Compute SHA256 hash of a file."""
        if not os.path.isfile(file_path):
            return ""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def close(self):
        """Close the database connection."""
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
