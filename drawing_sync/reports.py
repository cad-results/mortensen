"""Report Generation.

Generates human-readable reports for:
- Full scan summaries
- Mismatch alerts (with RED FLAG formatting)
- Component sync status
- Dependency graphs
- Change logs
"""

import json
from datetime import datetime
from typing import Optional

from .sync_engine import SyncEngine
from .models import AlertSeverity


def generate_scan_report(engine: SyncEngine, scan_results: dict) -> str:
    """Generate a full scan summary report."""
    lines = []
    lines.append("=" * 80)
    lines.append("DRAWING SYNCHRONIZATION — SCAN REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")

    lines.append(f"Drawings scanned:  {scan_results['scanned']}")
    lines.append(f"Drawings skipped:  {scan_results['skipped']} (unchanged)")
    lines.append(f"New drawings:      {len(scan_results['new_drawings'])}")
    lines.append(f"Updated drawings:  {len(scan_results['updated_drawings'])}")
    lines.append(f"Errors:            {len(scan_results['errors'])}")
    lines.append("")

    if scan_results["errors"]:
        lines.append("--- ERRORS ---")
        for err in scan_results["errors"]:
            lines.append(f"  {err['file']}: {err['error']}")
        lines.append("")

    # Per-drawing summary
    lines.append("--- DRAWING DETAILS ---")
    lines.append(f"{'Drawing':<25} {'Type':<18} {'Components':>10} {'Connections':>12} {'CrossRefs':>10} {'Labels':>8} {'Cables':>8}")
    lines.append("-" * 100)
    for dwg_id, info in sorted(scan_results.get("drawings", {}).items()):
        # Look up drawing type from DB
        dwg_type = ""
        try:
            row = engine.db.get_drawing(dwg_id)
            if row:
                dwg_type = row.get("drawing_type", "")[:16]
        except Exception:
            pass
        lines.append(
            f"{dwg_id:<25} {dwg_type:<18} {info['components']:>10} {info['connections']:>12} "
            f"{info['cross_refs']:>10} {info['labels']:>8} {info['cables']:>8}"
        )
    lines.append("")

    # Per-drawing component inventory — detailed breakdown
    lines.append("--- COMPONENT INVENTORY (PER DRAWING) ---")
    for dwg_id in sorted(scan_results.get("drawings", {}).keys()):
        rows = engine.db.conn.execute(
            "SELECT component_id, component_type, values_json, attributes_json, connections_json "
            "FROM components WHERE drawing_id = ?",
            (dwg_id,),
        ).fetchall()
        if not rows:
            lines.append(f"\n  {dwg_id}: No components extracted")
            continue

        lines.append(f"\n  {dwg_id}: {len(rows)} component(s)")
        lines.append(f"  {'ID':<28} {'Type':<18} {'Values':<30} {'Attributes'}")
        lines.append("  " + "-" * 96)
        completeness_issues = []

        for r in rows:
            comp_id = r["component_id"]
            comp_type = r["component_type"]
            vals = json.loads(r["values_json"])
            attrs = json.loads(r["attributes_json"])
            conns = json.loads(r["connections_json"])

            # Format values summary
            val_strs = []
            for v in vals:
                val_strs.append(f"{v['parameter']}={v['value']}")
            val_summary = ", ".join(val_strs[:3]) if val_strs else "(none)"
            if len(val_strs) > 3:
                val_summary += f" +{len(val_strs)-3} more"

            # Format attributes summary
            attr_strs = []
            for k, v in attrs.items():
                if isinstance(v, str) and v.strip():
                    attr_strs.append(f"{k}={v}")
                elif isinstance(v, list):
                    attr_strs.append(f"{k}=[{len(v)} items]")
            attr_summary = ", ".join(attr_strs[:3]) if attr_strs else "(none)"
            if len(attr_strs) > 3:
                attr_summary += f" +{len(attr_strs)-3} more"

            lines.append(f"  {comp_id:<28} {comp_type:<18} {val_summary:<30} {attr_summary}")

            # Track completeness issues
            if not vals:
                completeness_issues.append(
                    f"  [MISSING DATA] {comp_id} ({comp_type}): no electrical values extracted"
                )
            if not conns and comp_type in (
                "52", "50", "51", "86", "87", "89", "RELAY",
                "RTAC", "CLOCK", "PDC", "NETSW", "RTR", "DFR", "LTC",
            ):
                completeness_issues.append(
                    f"  [MISSING DATA] {comp_id} ({comp_type}): no connections/terminals found"
                )

        if completeness_issues:
            lines.append("")
            lines.append("  Extraction completeness notes:")
            for issue in completeness_issues:
                lines.append(issue)

    lines.append("")

    # Drawing notes (unrecognized blocks, warnings, etc.)
    all_notes = []
    for dwg_id in sorted(scan_results.get("drawings", {}).keys()):
        row = engine.db.get_drawing(dwg_id)
        if row:
            notes = json.loads(row.get("notes_json", "[]") or "[]")
            for note in notes:
                if "Unrecognized" in note or "WARNING" in note or "error" in note.lower():
                    all_notes.append((dwg_id, note))
    if all_notes:
        lines.append("--- EXTRACTION WARNINGS ---")
        for dwg_id, note in all_notes:
            lines.append(f"  [{dwg_id}] {note}")
        lines.append("")

    # Voltage levels found per drawing
    lines.append("--- VOLTAGE LEVELS ---")
    for dwg_id in sorted(scan_results.get("drawings", {}).keys()):
        vlevels = scan_results["drawings"][dwg_id].get("voltage_levels", [])
        if vlevels:
            lines.append(f"  {dwg_id}: {', '.join(vlevels)}")
    lines.append("")

    # Database statistics
    stats = engine.db.get_statistics()
    lines.append("--- DATABASE STATISTICS ---")
    lines.append(f"Total drawings in DB:         {stats['total_drawings']}")
    lines.append(f"Unique components:            {stats['total_components']}")
    lines.append(f"Component instances:          {stats['total_component_instances']}")
    lines.append(f"Components in 2+ drawings:    {stats['shared_components']}")
    lines.append(f"Total connections:            {stats['total_connections']}")
    lines.append(f"Total labels:                 {stats['total_labels']}")
    lines.append(f"Active mismatches:            {stats['active_mismatches']}")
    lines.append("")

    return "\n".join(lines)


def generate_mismatch_report(engine: SyncEngine) -> str:
    """Generate a mismatch/alert report with RED FLAG formatting."""
    mismatches = engine.detector.get_mismatch_summary()
    lines = []
    lines.append("=" * 80)
    lines.append("DRAWING SYNCHRONIZATION — MISMATCH ALERT REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")

    total = mismatches["total"]
    critical = mismatches["critical"]
    warning = mismatches["warning"]
    info = mismatches["info"]

    if critical > 0:
        lines.append(f"  *** {critical} CRITICAL MISMATCH(ES) — IMMEDIATE ACTION REQUIRED ***")
    lines.append(f"  Total: {total} | Critical: {critical} | Warning: {warning} | Info: {info}")
    lines.append("")

    if total == 0:
        lines.append("  All components are consistent across drawings.")
        lines.append("")
        return "\n".join(lines)

    # Group by severity
    for severity_name, icon in [("CRITICAL", "[RED FLAG]"), ("WARNING", "[WARNING]"), ("INFO", "[INFO]")]:
        items = [m for m in mismatches["mismatches"] if m["severity"] == severity_name]
        if not items:
            continue

        lines.append(f"--- {severity_name} ({len(items)}) ---")
        lines.append("")

        for m in items:
            lines.append(f"  {icon} Component: {m['component_id']}")
            lines.append(f"       Parameter: {m['parameter']}")
            vals = json.loads(m["values_found_json"]) if isinstance(m["values_found_json"], str) else m["values_found_json"]
            lines.append(f"       Values found:")
            for dwg, val in vals.items():
                # Look up revision from DB for context
                rev_info = ""
                try:
                    row = engine.db.get_drawing(dwg)
                    if row:
                        meta = json.loads(row.get("index_metadata_json", "{}") or "{}")
                        rev = meta.get("current_revision", "")
                        if rev:
                            rev_info = f" (Rev {rev})"
                except Exception:
                    pass
                lines.append(f"         {dwg}{rev_info}: {val}")
            involved = json.loads(m["drawings_involved_json"]) if isinstance(m["drawings_involved_json"], str) else m["drawings_involved_json"]
            lines.append(f"       Drawings: {', '.join(involved)}")
            lines.append(f"       Message: {m['message']}")
            if m.get("recommendation"):
                lines.append(f"       Action: {m['recommendation']}")
            lines.append("")

    return "\n".join(lines)


def generate_component_report(engine: SyncEngine, component_id: str) -> str:
    """Generate a detailed report for a single component across all drawings."""
    status = engine.get_component_sync_status(component_id)
    lines = []
    lines.append("=" * 80)
    lines.append(f"COMPONENT SYNC STATUS: {component_id}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")

    lines.append(f"Appears in {status['total_drawings']} drawing(s)")
    lines.append(f"Consistent: {'YES' if status['is_consistent'] else 'NO — MISMATCHES DETECTED'}")
    lines.append("")

    if status["inconsistencies"]:
        lines.append("--- INCONSISTENCIES ---")
        for inc in status["inconsistencies"]:
            lines.append(f"  Parameter: {inc['parameter']}")
            for dwg, val in inc["values"].items():
                lines.append(f"    {dwg}: {val}")
            lines.append("")

    lines.append("--- DRAWING DETAILS ---")
    for dwg_id, data in sorted(status["drawings"].items()):
        lines.append(f"  {dwg_id}:")
        lines.append(f"    Type: {data['type']}")
        if data["values"]:
            for v in data["values"]:
                lines.append(f"    {v['parameter']}: {v['value']}")
        if data["attributes"]:
            for k, v in data["attributes"].items():
                lines.append(f"    [{k}]: {v}")
        lines.append("")

    return "\n".join(lines)


def generate_shared_components_report(engine: SyncEngine) -> str:
    """Generate a report of all shared components (appear in 2+ drawings)."""
    shared = engine.db.get_shared_components(min_drawings=2)
    lines = []
    lines.append("=" * 80)
    lines.append("SHARED COMPONENTS REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Components appearing in 2+ drawings: {len(shared)}")
    lines.append("=" * 80)
    lines.append("")

    lines.append(f"{'Component':<20} {'Drawings':>5}  Drawing List")
    lines.append("-" * 80)

    for comp_id, drawings in sorted(shared.items(), key=lambda x: -len(x[1])):
        dwg_list = ", ".join(sorted(drawings)[:8])
        if len(drawings) > 8:
            dwg_list += f" ... (+{len(drawings)-8} more)"
        lines.append(f"{comp_id:<20} {len(drawings):>5}  {dwg_list}")

    lines.append("")
    return "\n".join(lines)


def generate_dependency_graph_report(engine: SyncEngine) -> str:
    """Generate a text-based dependency graph report."""
    graph = engine.get_dependency_graph()
    lines = []
    lines.append("=" * 80)
    lines.append("DRAWING DEPENDENCY GRAPH")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Total drawings: {len(graph)}")
    lines.append("=" * 80)
    lines.append("")

    for dwg_id in sorted(graph.keys()):
        node = graph[dwg_id]
        refs_out = node["references"]
        refs_in = node["referenced_by"]
        shared = node["shared_components"]

        if not refs_out and not refs_in and not shared:
            continue

        lines.append(f"{dwg_id}:")
        if refs_out:
            lines.append(f"  References -> {', '.join(sorted(refs_out)[:10])}")
        if refs_in:
            lines.append(f"  Referenced by <- {', '.join(sorted(refs_in)[:10])}")
        if shared:
            lines.append(f"  Shared components ({len(shared)}):")
            for comp_id, others in sorted(shared.items())[:5]:
                lines.append(f"    {comp_id} (also in {', '.join(others[:4])})")
            if len(shared) > 5:
                lines.append(f"    ... and {len(shared)-5} more")
        lines.append("")

    return "\n".join(lines)


def generate_propagation_report(actions: list) -> str:
    """Generate a formatted propagation report from a list of PropagationAction objects.

    Accepts PropagationAction dataclass instances or plain dicts with the same keys.
    """
    lines = []
    lines.append("=" * 80)
    lines.append("ATTRIBUTE PROPAGATION REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")

    if not actions:
        lines.append("  No propagation actions.")
        lines.append("")
        return "\n".join(lines)

    # Normalize: accept both dataclass and dict
    def _get(obj, key):
        if isinstance(obj, dict):
            return obj.get(key, "")
        return getattr(obj, key, "")

    # Summary by status
    by_status = {}
    affected_components = set()
    affected_drawings = set()
    for a in actions:
        status = _get(a, "status")
        by_status[status] = by_status.get(status, 0) + 1
        affected_components.add(_get(a, "component_id"))
        affected_drawings.add(_get(a, "target_drawing_id"))

    lines.append("--- SUMMARY ---")
    lines.append(f"  Total actions:        {len(actions)}")
    for status, count in sorted(by_status.items()):
        lines.append(f"  {status}:  {count}")
    lines.append(f"  Affected components:  {len(affected_components)}")
    lines.append(f"  Affected drawings:    {len(affected_drawings)}")
    lines.append("")

    # Group by component
    by_component = {}
    for a in actions:
        comp = _get(a, "component_id")
        if comp not in by_component:
            by_component[comp] = []
        by_component[comp].append(a)

    lines.append("--- PER-COMPONENT BREAKDOWN ---")
    lines.append("")

    for comp_id in sorted(by_component.keys()):
        comp_actions = by_component[comp_id]
        lines.append(f"  Component: {comp_id} ({len(comp_actions)} action(s))")
        lines.append("-" * 80)

        for a in comp_actions:
            source = _get(a, "source_drawing_id")
            target = _get(a, "target_drawing_id")
            param = _get(a, "parameter")
            old_val = _get(a, "old_value")
            new_val = _get(a, "new_value")
            basis = _get(a, "authority_basis")
            status = _get(a, "status")

            lines.append(f"    {source} -> {target}")
            lines.append(f"      Parameter: {param}")
            lines.append(f"      Value:     {old_val} -> {new_val}")
            lines.append(f"      Basis:     {basis}")
            lines.append(f"      Status:    {status}")
            lines.append("")

    return "\n".join(lines)


def generate_audit_report(audit_trail, decision_type: Optional[str] = None, limit: int = 50) -> str:
    """Generate a formatted audit log report showing recent decisions.

    Args:
        audit_trail: AuditTrail instance.
        decision_type: Optional filter by decision type.
        limit: Maximum number of decisions to show.
    """
    decisions = audit_trail.get_decisions(decision_type=decision_type, limit=limit)
    lines = []
    lines.append("=" * 80)
    title = f"DECISION AUDIT LOG — {decision_type}" if decision_type else "DECISION AUDIT LOG (ALL TYPES)"
    lines.append(title)
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Showing last {limit} decisions")
    lines.append("=" * 80)
    lines.append("")

    if not decisions:
        lines.append("  No decisions recorded.")
        lines.append("")
        return "\n".join(lines)

    for d in decisions:
        ts = d.get("timestamp", "")[:19]
        dtype = d.get("decision_type", "")
        lines.append(f"  [{ts}] {dtype}")
        lines.append(f"    Drawing: {d.get('drawing_id', 'N/A')} | Component: {d.get('component_id', 'N/A') or 'N/A'}")
        lines.append(f"    Outcome: {d.get('outcome', '')}")
        lines.append(f"    Confidence: {d.get('confidence', 0.0):.0%}")
        if d.get("reasoning"):
            lines.append(f"    Reasoning: {d['reasoning']}")
        alternatives = d.get("alternatives", [])
        if alternatives:
            lines.append(f"    Alternatives: {json.dumps(alternatives)}")
        lines.append("")

    # Footer summary
    stats = audit_trail.get_statistics()
    lines.append("-" * 80)
    lines.append(f"  Total decisions in database: {stats['total']}")
    lines.append(f"  Average confidence: {stats['average_confidence']:.0%}")
    lines.append("")

    return "\n".join(lines)


def generate_decision_tree_report(audit_trail, component_id: str) -> str:
    """Generate a formatted decision tree report for a component.

    Args:
        audit_trail: AuditTrail instance.
        component_id: Component ID to build the tree for.
    """
    tree = audit_trail.generate_decision_tree(component_id)
    lines = []
    lines.append("=" * 80)
    lines.append(f"DECISION TREE: {component_id}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")

    # Classifications
    classifications = tree.get("classifications", [])
    lines.append(f"--- CLASSIFICATIONS ({len(classifications)}) ---")
    if classifications:
        for cls_dec in classifications:
            lines.append(f"  Drawing: {cls_dec['drawing_id']}")
            lines.append(f"    Type: {cls_dec['outcome']}  (confidence: {cls_dec['confidence']:.0%})")
            lines.append(f"    Reasoning: {cls_dec['reasoning']}")
            lines.append("")
    else:
        lines.append("  No classification decisions recorded.")
        lines.append("")

    # Authority determinations
    authority = tree.get("authority_determinations", [])
    lines.append(f"--- AUTHORITY DETERMINATIONS ({len(authority)}) ---")
    if authority:
        for auth_dec in authority:
            lines.append(f"  Parameter: {auth_dec['parameter']}")
            lines.append(f"    Authoritative: {auth_dec['authoritative_drawing']}")
            lines.append(f"    Reasoning: {auth_dec['reasoning']}")
            lines.append("")
    else:
        lines.append("  No authority determinations recorded.")
        lines.append("")

    # Propagation actions
    propagation = tree.get("propagation_actions", [])
    lines.append(f"--- PROPAGATION ACTIONS ({len(propagation)}) ---")
    if propagation:
        for prop_dec in propagation:
            lines.append(f"  {prop_dec['source']} -> {prop_dec['target']}")
            lines.append(f"    Parameter: {prop_dec['parameter']}")
            lines.append(f"    Outcome: {prop_dec['outcome']}")
            lines.append(f"    Reasoning: {prop_dec['reasoning']}")
            lines.append("")
    else:
        lines.append("  No propagation actions recorded.")
        lines.append("")

    # Mismatch detections
    mismatches = tree.get("mismatch_detections", [])
    lines.append(f"--- MISMATCH DETECTIONS ({len(mismatches)}) ---")
    if mismatches:
        for mm_dec in mismatches:
            lines.append(f"  Parameter: {mm_dec['parameter']}")
            lines.append(f"    Severity: {mm_dec['severity']}")
            lines.append(f"    Reasoning: {mm_dec['reasoning']}")
            lines.append("")
    else:
        lines.append("  No mismatches recorded.")
        lines.append("")

    return "\n".join(lines)


def generate_change_log_report(engine: SyncEngine, drawing_id: Optional[str] = None, limit: int = 50) -> str:
    """Generate a change log report."""
    changes = engine.db.get_change_log(drawing_id=drawing_id, limit=limit)
    lines = []
    lines.append("=" * 80)
    title = f"CHANGE LOG: {drawing_id}" if drawing_id else "CHANGE LOG (ALL DRAWINGS)"
    lines.append(title)
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Showing last {limit} changes")
    lines.append("=" * 80)
    lines.append("")

    if not changes:
        lines.append("  No changes recorded.")
        lines.append("")
        return "\n".join(lines)

    for c in changes:
        ts = c["timestamp"][:19]
        lines.append(f"  [{ts}] {c['change_type']}")
        lines.append(f"    Drawing: {c['drawing_id']} | Component: {c.get('component_id', 'N/A')}")
        if c.get("description"):
            lines.append(f"    {c['description']}")
        if c.get("old_value") and c.get("new_value"):
            lines.append(f"    Old: {c['old_value'][:100]}")
            lines.append(f"    New: {c['new_value'][:100]}")
        lines.append("")

    return "\n".join(lines)
