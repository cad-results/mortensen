"""Synchronization Engine.

Core engine that:
1. Scans all drawings (PDF/DXF/XLSX) in configured directories
2. Extracts components and stores in the registry database
3. Detects changes when drawings are updated
4. Propagates change notifications across related drawings
5. Generates sync reports showing what needs updating
"""

import os
import json
from datetime import datetime
from typing import Optional

from .models import DrawingData, Mismatch, AlertSeverity
from .db import ComponentDatabase
from .extractors.pdf_extractor import PDFExtractor
from .extractors.dxf_extractor import DXFExtractor
from .extractors.xlsx_extractor import XLSXExtractor
from .mismatch_detector import MismatchDetector
from .authority import AuthorityConfig
from .propagation_engine import PropagationEngine
from .audit import AuditTrail
try:
    from .drawing_classifier import DrawingClassifier
except ImportError:
    DrawingClassifier = None


class SyncEngine:
    """Main synchronization engine for drawing management."""

    def __init__(self, db_path: str = "drawing_sync.db"):
        self.db = ComponentDatabase(db_path)
        self.pdf_extractor = PDFExtractor()
        self.dxf_extractor = DXFExtractor()
        self.xlsx_extractor = XLSXExtractor()
        self.detector = MismatchDetector(self.db)
        self.authority = AuthorityConfig()
        self.audit = AuditTrail(self.db)
        self.classifier = DrawingClassifier(audit=self.audit) if DrawingClassifier else None
        self.propagation = PropagationEngine(self.db, self.authority, audit=self.audit)

    def load_drawing_index(self, xlsx_path: str):
        """Load a drawing index XLSX for type classification."""
        if self.classifier:
            self.classifier._load_drawing_index(xlsx_path)

    def _auto_discover_index(self, directory: str) -> str | None:
        """Search for a drawing index XLSX in common locations relative to the input dir.

        Checks global_reference/ directories by walking up from the input path.
        Returns the first match, or None.
        """
        import glob

        # Walk up from the input directory looking for global_reference/
        current = os.path.abspath(directory)
        for _ in range(4):  # Up to 4 levels up
            parent = os.path.dirname(current)
            if parent == current:
                break
            candidate_dir = os.path.join(parent, "global_reference")
            if os.path.isdir(candidate_dir):
                matches = glob.glob(os.path.join(candidate_dir, "*DRAWING INDEX*.xlsx"))
                if matches:
                    return matches[0]
            current = parent

        return None

    def scan_directory(self, directory: str, force: bool = False) -> dict:
        """Scan a directory tree for all drawing files and extract components.

        Args:
            directory: Root directory to scan
            force: If True, re-scan even if files haven't changed

        Returns:
            dict with scan results summary
        """
        results = {
            "scanned": 0,
            "skipped": 0,
            "errors": [],
            "new_drawings": [],
            "updated_drawings": [],
            "drawings": {},
        }

        # Auto-discover drawing index if not already loaded
        if self.classifier and not self.classifier._index_map:
            index_path = self._auto_discover_index(directory)
            if index_path:
                self.load_drawing_index(index_path)

        for root, dirs, files in os.walk(directory, followlinks=False):
            # Skip backup directories
            if "backup" in root.lower():
                continue

            for fname in sorted(files):
                # Skip hidden/temp files
                if fname.startswith(".") or fname.startswith("~"):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext not in (".pdf", ".dxf", ".dwg", ".xlsx", ".xls"):
                    continue

                file_path = os.path.join(root, fname)
                drawing_id = os.path.splitext(fname)[0]

                # Check if file has changed
                if not force and not self.db.has_drawing_changed(drawing_id, file_path):
                    results["skipped"] += 1
                    continue

                try:
                    drawing = self._extract_file(file_path, ext)
                    if drawing:
                        # Classify drawing type
                        if self.classifier:
                            drawing.drawing_type = self.classifier.classify(drawing)
                            self.classifier.enrich_from_index(drawing)

                        # Check if this is new or updated
                        existing = self.db.get_drawing(drawing_id)
                        if existing:
                            results["updated_drawings"].append(drawing_id)
                        else:
                            results["new_drawings"].append(drawing_id)

                        self.db.store_drawing(drawing)
                        results["drawings"][drawing_id] = {
                            "components": len(drawing.components),
                            "connections": len(drawing.connections),
                            "cross_refs": len(drawing.cross_references),
                            "labels": len(drawing.all_labels),
                            "cables": len(drawing.cable_schedule),
                            "voltage_levels": drawing.voltage_levels,
                        }
                        results["scanned"] += 1
                except Exception as e:
                    results["errors"].append({
                        "file": file_path,
                        "error": str(e),
                    })

        return results

    def scan_single_file(self, file_path: str) -> Optional[DrawingData]:
        """Scan a single file and update the database."""
        ext = os.path.splitext(file_path)[1].lower()
        drawing = self._extract_file(file_path, ext)
        if drawing:
            if self.classifier:
                drawing.drawing_type = self.classifier.classify(drawing)
                self.classifier.enrich_from_index(drawing)
            self.db.store_drawing(drawing)
        return drawing

    def scan_single_file_with_results(self, file_path: str, force: bool = False) -> dict:
        """Scan a single file and return results dict matching scan_directory format."""
        results = {
            "scanned": 0,
            "skipped": 0,
            "errors": [],
            "new_drawings": [],
            "updated_drawings": [],
            "drawings": {},
        }

        if not os.path.isfile(file_path):
            results["errors"].append({"file": file_path, "error": "File not found"})
            return results

        fname = os.path.basename(file_path)
        ext = os.path.splitext(fname)[1].lower()
        if ext not in (".pdf", ".dxf", ".dwg", ".xlsx", ".xls"):
            results["errors"].append({"file": file_path, "error": f"Unsupported file type: {ext}"})
            return results

        drawing_id = os.path.splitext(fname)[0]

        # Auto-discover drawing index
        if self.classifier and not self.classifier._index_map:
            index_path = self._auto_discover_index(os.path.dirname(file_path))
            if index_path:
                self.load_drawing_index(index_path)

        # Check if file has changed
        if not force and not self.db.has_drawing_changed(drawing_id, file_path):
            results["skipped"] += 1
            return results

        try:
            drawing = self._extract_file(file_path, ext)
            if drawing:
                if self.classifier:
                    drawing.drawing_type = self.classifier.classify(drawing)
                    self.classifier.enrich_from_index(drawing)

                existing = self.db.get_drawing(drawing_id)
                if existing:
                    results["updated_drawings"].append(drawing_id)
                else:
                    results["new_drawings"].append(drawing_id)

                self.db.store_drawing(drawing)
                results["drawings"][drawing_id] = {
                    "components": len(drawing.components),
                    "connections": len(drawing.connections),
                    "cross_refs": len(drawing.cross_references),
                    "labels": len(drawing.all_labels),
                    "cables": len(drawing.cable_schedule),
                    "voltage_levels": drawing.voltage_levels,
                }
                results["scanned"] += 1
        except Exception as e:
            results["errors"].append({"file": file_path, "error": str(e)})

        return results

    def check_mismatches(self) -> list:
        """Run all mismatch detection checks."""
        return self.detector.run_all_checks()

    def get_sync_report(self, drawing_id: str) -> dict:
        """Generate a synchronization report for a specific drawing.

        Shows:
        - All components in this drawing
        - Which other drawings share these components
        - Any mismatches detected
        - Recommended actions
        """
        report = {
            "drawing_id": drawing_id,
            "timestamp": datetime.now().isoformat(),
            "components": {},
            "cross_references": [],
            "mismatches": [],
            "recommendations": [],
        }

        # Get drawing data
        drawing = self.db.get_drawing(drawing_id)
        if not drawing:
            report["error"] = f"Drawing {drawing_id} not found in database"
            return report

        report["cross_references"] = json.loads(
            drawing["cross_references_json"]
        )

        # Get all components in this drawing
        rows = self.db.conn.execute(
            "SELECT * FROM components WHERE drawing_id = ?",
            (drawing_id,),
        ).fetchall()

        for r in rows:
            comp_id = r["component_id"]

            # Find this component in other drawings
            other_drawings = self.db.get_component_across_drawings(comp_id)
            other_dwgs = [
                d["drawing_id"] for d in other_drawings
                if d["drawing_id"] != drawing_id
            ]

            report["components"][comp_id] = {
                "type": r["component_type"],
                "values": json.loads(r["values_json"]),
                "also_in_drawings": other_dwgs,
                "connections": json.loads(r["connections_json"]),
                "attributes": json.loads(r["attributes_json"]),
            }

        # Get mismatches involving this drawing's components
        active_mismatches = self.db.get_active_mismatches()
        for m in active_mismatches:
            involved = json.loads(m["drawings_involved_json"])
            if drawing_id in involved:
                report["mismatches"].append(m)

        # Generate recommendations
        report["recommendations"] = self._generate_recommendations(report)

        return report

    def get_component_sync_status(self, component_id: str) -> dict:
        """Get the synchronization status of a component across all drawings.

        Returns detailed comparison showing values in each drawing.
        """
        instances = self.db.get_component_across_drawings(component_id)

        status = {
            "component_id": component_id,
            "total_drawings": len(instances),
            "drawings": {},
            "is_consistent": True,
            "inconsistencies": [],
        }

        # Compare values across drawings
        all_values = {}
        for inst in instances:
            dwg_id = inst["drawing_id"]
            vals = json.loads(inst["values_json"])
            status["drawings"][dwg_id] = {
                "type": inst["component_type"],
                "values": vals,
                "attributes": json.loads(inst["attributes_json"]),
            }

            for v in vals:
                param = v["parameter"]
                if param not in all_values:
                    all_values[param] = {}
                all_values[param][dwg_id] = v["value"]

        # Check consistency
        for param, dwg_vals in all_values.items():
            unique = set(dwg_vals.values())
            if len(unique) > 1:
                status["is_consistent"] = False
                status["inconsistencies"].append({
                    "parameter": param,
                    "values": dwg_vals,
                })

        return status

    def propagate_update(self, source_drawing_id: str, component_id: str) -> dict:
        """When a component is updated in one drawing, find all other
        drawings that need updating.

        Returns a report of which drawings are affected and what needs changing.
        """
        result = {
            "source": source_drawing_id,
            "component": component_id,
            "affected_drawings": [],
            "changes_needed": [],
        }

        # Get the component's current values in the source drawing
        source_data = self.db.conn.execute("""
            SELECT values_json, attributes_json, component_type
            FROM components
            WHERE component_id = ? AND drawing_id = ?
        """, (component_id, source_drawing_id)).fetchone()

        if not source_data:
            result["error"] = f"Component {component_id} not found in {source_drawing_id}"
            return result

        source_values = json.loads(source_data["values_json"])
        source_attrs = json.loads(source_data["attributes_json"])

        # Find all other drawings containing this component
        all_instances = self.db.get_component_across_drawings(component_id)

        for inst in all_instances:
            dwg_id = inst["drawing_id"]
            if dwg_id == source_drawing_id:
                continue

            target_values = json.loads(inst["values_json"])

            # Compare
            differences = []
            source_val_map = {v["parameter"]: v["value"] for v in source_values}
            target_val_map = {v["parameter"]: v["value"] for v in target_values}

            for param, src_val in source_val_map.items():
                tgt_val = target_val_map.get(param)
                if tgt_val and tgt_val != src_val:
                    differences.append({
                        "parameter": param,
                        "source_value": src_val,
                        "target_value": tgt_val,
                    })

            if differences:
                result["affected_drawings"].append(dwg_id)
                result["changes_needed"].append({
                    "drawing_id": dwg_id,
                    "file_path": inst["file_path"],
                    "differences": differences,
                })

        return result

    def plan_propagation(self, component_id: str, source_drawing_id: Optional[str] = None) -> list:
        """Plan attribute propagation for a component using authority rules.

        Delegates to PropagationEngine.plan_propagation().
        """
        return self.propagation.plan_propagation(component_id, source_drawing_id)

    def apply_propagation(self, component_id: str, source_drawing_id: Optional[str] = None, dry_run: bool = True) -> list:
        """Plan and apply attribute propagation for a component.

        Defaults to dry_run=True for safety.
        Delegates to PropagationEngine.
        """
        actions = self.propagation.plan_propagation(component_id, source_drawing_id)
        return self.propagation.apply_propagation(actions, dry_run=dry_run)

    def plan_all_propagations(self) -> list:
        """Plan propagation for all shared components.

        Delegates to PropagationEngine.plan_all_propagations().
        """
        return self.propagation.plan_all_propagations()

    def get_dependency_graph(self) -> dict:
        """Build a dependency graph showing how drawings are related.

        Returns dict: drawing_id -> {
            "references": [drawing_ids this drawing points to],
            "referenced_by": [drawing_ids that point to this drawing],
            "shared_components": {component_id: [other_drawing_ids]}
        }
        """
        graph = {}
        all_drawings = self.db.get_all_drawing_ids()

        for dwg_id in all_drawings:
            graph[dwg_id] = {
                "references": [],
                "referenced_by": [],
                "shared_components": {},
            }

        # Build cross-reference edges
        for dwg_id in all_drawings:
            refs = self.db.get_drawing_cross_references(dwg_id)
            for ref in refs:
                full_ref = f"NRE-{ref}" if not ref.startswith("NRE-") else ref
                graph[dwg_id]["references"].append(full_ref)
                if full_ref in graph:
                    graph[full_ref]["referenced_by"].append(dwg_id)

        # Build shared component edges
        shared = self.db.get_shared_components(min_drawings=2)
        for comp_id, drawings in shared.items():
            for dwg_id in drawings:
                if dwg_id in graph:
                    other = [d for d in drawings if d != dwg_id]
                    graph[dwg_id]["shared_components"][comp_id] = other

        return graph

    def _extract_file(self, file_path: str, ext: str) -> Optional[DrawingData]:
        """Extract data from a file based on its extension."""
        if ext == ".pdf":
            return self.pdf_extractor.extract(file_path)
        elif ext in (".dxf", ".dwg"):
            return self.dxf_extractor.extract(file_path)
        elif ext in (".xlsx", ".xls"):
            return self.xlsx_extractor.extract(file_path)
        return None

    def _generate_recommendations(self, report: dict) -> list:
        """Generate actionable recommendations from report data."""
        recs = []

        # Flag mismatches
        for m in report["mismatches"]:
            severity = m["severity"]
            if severity == "CRITICAL":
                recs.append(
                    f"[RED FLAG] {m['message']} — {m.get('recommendation', '')}"
                )
            elif severity == "WARNING":
                recs.append(
                    f"[WARNING] {m['message']} — {m.get('recommendation', '')}"
                )

        # Flag components in many drawings that might need coordinated updates
        for comp_id, data in report["components"].items():
            other = data["also_in_drawings"]
            if len(other) > 5:
                recs.append(
                    f"Component {comp_id} appears in {len(other)+1} drawings. "
                    f"Any changes require coordinated update across: "
                    f"{', '.join(other[:5])}{'...' if len(other) > 5 else ''}"
                )

        return recs

    def close(self):
        """Close database connection."""
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
