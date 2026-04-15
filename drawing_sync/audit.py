"""Decision audit trail for compliance and traceability.

Records WHY each decision was made — not just WHAT changed — to support
signing engineer compliance requirements (Phase 3).

Decision types:
- CLASSIFICATION: How a drawing was typed (ONE_LINE, AC_SCHEMATIC, etc.)
- AUTHORITY: Which drawing is source-of-truth for a parameter
- PROPAGATION: What was changed, from where, to where, and why
- MISMATCH_DETECTION: What inconsistencies were found
- MISMATCH_RESOLUTION: How a mismatch was resolved
"""

import json
import hashlib
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional


@dataclass
class DecisionRecord:
    """A single recorded decision with full traceability."""
    decision_id: str            # Deterministic hash
    timestamp: str              # ISO format
    decision_type: str          # CLASSIFICATION, AUTHORITY, PROPAGATION, etc.
    component_id: str           # Component this relates to (empty for drawing-level)
    drawing_id: str             # Drawing this relates to
    input_data: dict            # What data was considered (the evidence)
    reasoning: str              # Human-readable explanation of the decision logic
    confidence: float           # 0.0-1.0 confidence score
    outcome: str                # What was decided
    alternatives: list = field(default_factory=list)  # Other options considered

    def to_dict(self) -> dict:
        return asdict(self)


class AuditTrail:
    """Manages the decision audit trail database and reporting."""

    def __init__(self, db):
        """Initialise with a ComponentDatabase instance.

        Args:
            db: ComponentDatabase — must have a .conn sqlite3 connection.
        """
        self.db = db
        self._ensure_table()

    # ── Table setup ──────────────────────────────────────────────────

    def _ensure_table(self):
        """Create the decisions table if it does not exist (idempotent)."""
        self.db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT UNIQUE NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                decision_type TEXT NOT NULL,
                component_id TEXT DEFAULT '',
                drawing_id TEXT DEFAULT '',
                input_data_json TEXT NOT NULL DEFAULT '{}',
                reasoning TEXT NOT NULL DEFAULT '',
                confidence REAL DEFAULT 1.0,
                outcome TEXT NOT NULL DEFAULT '',
                alternatives_json TEXT DEFAULT '[]'
            );

            CREATE INDEX IF NOT EXISTS idx_decisions_component
                ON decisions(component_id);
            CREATE INDEX IF NOT EXISTS idx_decisions_type
                ON decisions(decision_type);
            CREATE INDEX IF NOT EXISTS idx_decisions_drawing
                ON decisions(drawing_id);
        """)
        self.db.conn.commit()

    # ── Record / query ───────────────────────────────────────────────

    def record_decision(self, record: DecisionRecord) -> None:
        """Persist a decision record to the database.

        Uses INSERT OR REPLACE so re-running the same decision (same
        decision_id) overwrites cleanly.
        """
        try:
            self.db.conn.execute("""
                INSERT OR REPLACE INTO decisions
                (decision_id, timestamp, decision_type, component_id,
                 drawing_id, input_data_json, reasoning, confidence,
                 outcome, alternatives_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.decision_id,
                record.timestamp,
                record.decision_type,
                record.component_id,
                record.drawing_id,
                json.dumps(record.input_data),
                record.reasoning,
                record.confidence,
                record.outcome,
                json.dumps(record.alternatives),
            ))
            self.db.conn.commit()
        except Exception:
            # Audit recording must never fail the main operation
            pass

    def get_decisions(
        self,
        component_id: Optional[str] = None,
        drawing_id: Optional[str] = None,
        decision_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        """Query decisions with optional filters.

        Returns list of dicts with parsed JSON fields.
        """
        clauses: List[str] = []
        params: list = []

        if component_id:
            clauses.append("component_id = ?")
            params.append(component_id)
        if drawing_id:
            clauses.append("drawing_id = ?")
            params.append(drawing_id)
        if decision_type:
            clauses.append("decision_type = ?")
            params.append(decision_type)

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        params.append(limit)
        rows = self.db.conn.execute(f"""
            SELECT * FROM decisions
            {where}
            ORDER BY timestamp DESC
            LIMIT ?
        """, params).fetchall()

        results = []
        for row in rows:
            d = dict(row)
            # Parse JSON fields
            try:
                d["input_data"] = json.loads(d.pop("input_data_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                d["input_data"] = {}
            try:
                d["alternatives"] = json.loads(d.pop("alternatives_json", "[]"))
            except (json.JSONDecodeError, TypeError):
                d["alternatives"] = []
            results.append(d)

        return results

    # ── Decision tree ────────────────────────────────────────────────

    def generate_decision_tree(self, component_id: str) -> dict:
        """Build a nested decision tree for a component.

        Collects all decisions related to this component (or the drawings
        it appears in) and organises them by type.
        """
        tree: Dict = {
            "component_id": component_id,
            "generated_at": datetime.now().isoformat(),
            "classifications": [],
            "authority_determinations": [],
            "propagation_actions": [],
            "mismatch_detections": [],
        }

        # Get all decisions for this component directly
        comp_decisions = self.get_decisions(component_id=component_id, limit=500)

        # Also get CLASSIFICATION decisions for drawings containing this component
        drawing_ids = self._get_drawing_ids_for_component(component_id)
        classification_decisions = []
        for dwg_id in drawing_ids:
            classification_decisions.extend(
                self.get_decisions(
                    drawing_id=dwg_id,
                    decision_type="CLASSIFICATION",
                    limit=50,
                )
            )

        # Populate tree sections
        for dec in classification_decisions:
            tree["classifications"].append({
                "drawing_id": dec.get("drawing_id", ""),
                "outcome": dec.get("outcome", ""),
                "confidence": dec.get("confidence", 0.0),
                "reasoning": dec.get("reasoning", ""),
            })

        for dec in comp_decisions:
            dtype = dec.get("decision_type", "")
            if dtype == "AUTHORITY":
                input_data = dec.get("input_data", {})
                tree["authority_determinations"].append({
                    "parameter": input_data.get("parameter", ""),
                    "authoritative_drawing": dec.get("drawing_id", ""),
                    "reasoning": dec.get("reasoning", ""),
                })
            elif dtype == "PROPAGATION":
                input_data = dec.get("input_data", {})
                tree["propagation_actions"].append({
                    "source": input_data.get("source_drawing", ""),
                    "target": input_data.get("target_drawing", ""),
                    "parameter": input_data.get("parameter", ""),
                    "outcome": dec.get("outcome", ""),
                    "reasoning": dec.get("reasoning", ""),
                })
            elif dtype == "MISMATCH_DETECTION":
                input_data = dec.get("input_data", {})
                tree["mismatch_detections"].append({
                    "parameter": input_data.get("parameter", ""),
                    "severity": input_data.get("severity", ""),
                    "reasoning": dec.get("reasoning", ""),
                })

        return tree

    # ── Audit report export ──────────────────────────────────────────

    def export_audit_report(self, component_id: str, output_path: str) -> None:
        """Generate a formal text report suitable for legal/compliance review.

        Writes a structured document to *output_path* covering:
        1. Component Overview
        2. Classification Decisions
        3. Authority Determinations
        4. Propagation Actions
        5. Mismatch History
        """
        import os
        tree = self.generate_decision_tree(component_id)
        lines: List[str] = []

        # Header
        lines.append("=" * 80)
        lines.append("DECISION AUDIT REPORT")
        lines.append(f"Component: {component_id}")
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"System version: drawing_sync 1.0.0")
        lines.append("=" * 80)
        lines.append("")

        # Section 1: Component Overview
        lines.append("SECTION 1: COMPONENT OVERVIEW")
        lines.append("-" * 40)
        drawing_ids = self._get_drawing_ids_for_component(component_id)
        if drawing_ids:
            lines.append(f"  Appears in {len(drawing_ids)} drawing(s):")
            for dwg_id in sorted(drawing_ids):
                dtype = self._get_drawing_type(dwg_id)
                lines.append(f"    {dwg_id} ({dtype})")
        else:
            lines.append("  No drawings found for this component.")
        lines.append("")

        # Section 2: Classification Decisions
        lines.append("SECTION 2: CLASSIFICATION DECISIONS")
        lines.append("-" * 40)
        if tree["classifications"]:
            for cls_dec in tree["classifications"]:
                lines.append(f"  Drawing: {cls_dec['drawing_id']}")
                lines.append(f"    Type: {cls_dec['outcome']}")
                lines.append(f"    Confidence: {cls_dec['confidence']:.0%}")
                lines.append(f"    Reasoning: {cls_dec['reasoning']}")
                lines.append("")
        else:
            lines.append("  No classification decisions recorded.")
            lines.append("")

        # Section 3: Authority Determinations
        lines.append("SECTION 3: AUTHORITY DETERMINATIONS")
        lines.append("-" * 40)
        if tree["authority_determinations"]:
            for auth_dec in tree["authority_determinations"]:
                lines.append(f"  Parameter: {auth_dec['parameter']}")
                lines.append(f"    Authoritative drawing: {auth_dec['authoritative_drawing']}")
                lines.append(f"    Reasoning: {auth_dec['reasoning']}")
                lines.append("")
        else:
            lines.append("  No authority determinations recorded.")
            lines.append("")

        # Section 4: Propagation Actions
        lines.append("SECTION 4: PROPAGATION ACTIONS")
        lines.append("-" * 40)
        if tree["propagation_actions"]:
            for prop_dec in tree["propagation_actions"]:
                lines.append(f"  {prop_dec['source']} -> {prop_dec['target']}")
                lines.append(f"    Parameter: {prop_dec['parameter']}")
                lines.append(f"    Outcome: {prop_dec['outcome']}")
                lines.append(f"    Reasoning: {prop_dec['reasoning']}")
                lines.append("")
        else:
            lines.append("  No propagation actions recorded.")
            lines.append("")

        # Section 5: Mismatch History
        lines.append("SECTION 5: MISMATCH HISTORY")
        lines.append("-" * 40)
        if tree["mismatch_detections"]:
            for mm_dec in tree["mismatch_detections"]:
                lines.append(f"  Parameter: {mm_dec['parameter']}")
                lines.append(f"    Severity: {mm_dec['severity']}")
                lines.append(f"    Reasoning: {mm_dec['reasoning']}")
                lines.append("")
        else:
            lines.append("  No mismatches recorded.")
            lines.append("")

        # Footer
        all_decisions = self.get_decisions(component_id=component_id, limit=1000)
        total = len(all_decisions)
        if total > 0:
            avg_conf = sum(d.get("confidence", 0.0) for d in all_decisions) / total
        else:
            avg_conf = 0.0

        lines.append("=" * 80)
        lines.append("SUMMARY")
        lines.append(f"  Total decisions recorded: {total}")
        lines.append(f"  Average confidence: {avg_conf:.0%}")
        lines.append(f"  Classifications: {len(tree['classifications'])}")
        lines.append(f"  Authority determinations: {len(tree['authority_determinations'])}")
        lines.append(f"  Propagation actions: {len(tree['propagation_actions'])}")
        lines.append(f"  Mismatch detections: {len(tree['mismatch_detections'])}")
        lines.append("=" * 80)
        lines.append("")

        # Write out
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w") as f:
            f.write("\n".join(lines))

    # ── Statistics ───────────────────────────────────────────────────

    def get_statistics(self) -> dict:
        """Return aggregate statistics about the audit trail.

        Returns dict with: total, by_type counts, average_confidence,
        earliest/latest timestamps.
        """
        row = self.db.conn.execute(
            "SELECT COUNT(*) as cnt FROM decisions"
        ).fetchone()
        total = row["cnt"] if row else 0

        # By type
        type_rows = self.db.conn.execute("""
            SELECT decision_type, COUNT(*) as cnt
            FROM decisions
            GROUP BY decision_type
        """).fetchall()
        by_type = {r["decision_type"]: r["cnt"] for r in type_rows}

        # Average confidence
        conf_row = self.db.conn.execute(
            "SELECT AVG(confidence) as avg_conf FROM decisions"
        ).fetchone()
        avg_confidence = conf_row["avg_conf"] if conf_row and conf_row["avg_conf"] is not None else 0.0

        # Date range
        range_row = self.db.conn.execute(
            "SELECT MIN(timestamp) as earliest, MAX(timestamp) as latest FROM decisions"
        ).fetchone()
        earliest = range_row["earliest"] if range_row else None
        latest = range_row["latest"] if range_row else None

        return {
            "total": total,
            "by_type": by_type,
            "average_confidence": round(avg_confidence, 3),
            "earliest": earliest,
            "latest": latest,
        }

    # ── Helpers ──────────────────────────────────────────────────────

    def _make_decision_id(self, *parts) -> str:
        """SHA-256 hash (24 chars) of concatenated parts."""
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def _get_drawing_ids_for_component(self, component_id: str) -> List[str]:
        """Get all drawing IDs that contain a component."""
        rows = self.db.conn.execute(
            "SELECT DISTINCT drawing_id FROM components WHERE component_id = ?",
            (component_id,),
        ).fetchall()
        return [r["drawing_id"] for r in rows]

    def _get_drawing_type(self, drawing_id: str) -> str:
        """Get the drawing type for a drawing ID."""
        row = self.db.conn.execute(
            "SELECT drawing_type FROM drawings WHERE drawing_id = ?",
            (drawing_id,),
        ).fetchone()
        return row["drawing_type"] if row and row["drawing_type"] else "UNKNOWN"
