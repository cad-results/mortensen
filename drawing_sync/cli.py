"""Command-Line Interface for Drawing Sync System.

Provides commands for:
- scan: Scan directories and extract all components
- check: Run mismatch detection
- report: Generate various reports
- watch: Monitor for live changes
- status: Show component sync status
- propagate: Show update propagation impact
- pipeline: Full end-to-end pipeline with structured output
"""

import os
import sys
import json
import shutil
import logging
import argparse
from datetime import datetime

from .sync_engine import SyncEngine
from .reports import (
    generate_scan_report,
    generate_mismatch_report,
    generate_component_report,
    generate_shared_components_report,
    generate_dependency_graph_report,
    generate_change_log_report,
    generate_propagation_report,
)
from .cable_export import CableListExporter
from .authority import AuthorityConfig
from .audit import AuditTrail


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _write_output(content: str, output_path: str | None, label: str = "Report"):
    """Write content to output file if specified, otherwise just print."""
    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w") as f:
            f.write(content)
        print(f"{label} saved to: {output_path}")


def cmd_scan(args):
    """Scan directories or individual files for drawings and extract components."""
    input_path = args.input
    engine = SyncEngine(args.db)

    if getattr(args, 'index', None):
        engine.load_drawing_index(args.index)

    print(f"Scanning: {input_path}")
    print(f"Database: {args.db}")
    print()

    if os.path.isfile(input_path):
        results = engine.scan_single_file_with_results(input_path, force=args.force)
    else:
        results = engine.scan_directory(input_path, force=args.force)

    report = generate_scan_report(engine, results)
    print(report)

    _write_output(report, args.output, "Scan report")
    engine.close()


def cmd_check(args):
    """Run mismatch detection checks."""
    engine = SyncEngine(args.db)

    # If -i provided, scan that directory first
    if args.input:
        print(f"Scanning: {args.input}")
        engine.scan_directory(args.input)
        print()

    print("Running mismatch detection...")
    print()

    mismatches = engine.check_mismatches()
    report = generate_mismatch_report(engine)
    print(report)

    _write_output(report, args.output, "Mismatch report")

    # Return non-zero if critical mismatches found
    critical = sum(1 for m in mismatches if m.severity.value == "CRITICAL")
    engine.close()
    return 1 if critical > 0 else 0


def cmd_status(args):
    """Show sync status for a component."""
    engine = SyncEngine(args.db)

    # If -i provided, scan that directory first
    if args.input:
        print(f"Scanning: {args.input}")
        engine.scan_directory(args.input)
        print()

    if args.component:
        report = generate_component_report(engine, args.component)
    else:
        report = generate_shared_components_report(engine)

    print(report)
    _write_output(report, args.output)
    engine.close()


def cmd_propagate(args):
    """Show update propagation impact, plan, or apply propagation."""
    engine = SyncEngine(args.db)

    # If -i provided, scan that directory first
    if args.input:
        print(f"Scanning: {args.input}")
        engine.scan_directory(args.input)
        print()

    # --- New propagation modes ---

    if getattr(args, 'log', False):
        # Show propagation log
        component_filter = getattr(args, 'component', None)
        log_entries = engine.db.get_propagation_log(
            component_id=component_filter, limit=100,
        )
        lines = []
        lines.append("=" * 80)
        lines.append("PROPAGATION LOG")
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("=" * 80)
        lines.append("")

        if not log_entries:
            lines.append("  No propagation log entries found.")
        else:
            for entry in log_entries:
                ts = entry["timestamp"][:19]
                lines.append(
                    f"  [{ts}] {entry['status']} | {entry['component_id']} / "
                    f"{entry['parameter']}"
                )
                lines.append(
                    f"    {entry['source_drawing_id']} -> "
                    f"{entry['target_drawing_id']}: "
                    f"{entry['old_value']} -> {entry['new_value']}"
                )
                if entry.get("authority_basis"):
                    lines.append(f"    Basis: {entry['authority_basis']}")
                lines.append("")

        output = "\n".join(lines)
        print(output)
        _write_output(output, args.output, "Propagation log")
        engine.close()
        return

    if getattr(args, 'plan', False):
        # Plan mode
        if getattr(args, 'all', False):
            actions = engine.plan_all_propagations()
            summary = engine.propagation.get_propagation_summary(actions)
            print(f"Plan all: {summary['total']} proposed actions")
            print(f"  Affected components: {len(summary['affected_components'])}")
            print(f"  Affected drawings:   {len(summary['affected_drawings'])}")
            print()
        else:
            # In plan/apply mode, the first positional (drawing) is the component ID
            component = getattr(args, 'component', None) or getattr(args, 'drawing', None)
            if not component:
                print("Error: component ID required (or use --all)")
                engine.close()
                return
            actions = engine.plan_propagation(component)
            print(f"Plan for {component}: {len(actions)} proposed actions")
            print()

        report = generate_propagation_report(actions)
        print(report)
        _write_output(report, args.output, "Propagation plan report")
        engine.close()
        return

    if getattr(args, 'apply', False):
        # Apply mode
        if getattr(args, 'all', False):
            actions = engine.plan_all_propagations()
        else:
            component = getattr(args, 'component', None) or getattr(args, 'drawing', None)
            if not component:
                print("Error: component ID required (or use --all)")
                engine.close()
                return
            actions = engine.plan_propagation(component)

        if not actions:
            print("No propagation actions needed.")
            engine.close()
            return

        # Confirmation unless --force
        if not getattr(args, 'force', False):
            summary = engine.propagation.get_propagation_summary(actions)
            print(f"About to apply {summary['total']} propagation actions:")
            print(f"  Components: {', '.join(summary['affected_components'][:10])}")
            print(f"  Drawings:   {len(summary['affected_drawings'])}")
            print()
            confirm = input("Proceed? [y/N] ").strip().lower()
            if confirm != "y":
                print("Aborted.")
                engine.close()
                return

        result = engine.propagation.apply_propagation(actions, dry_run=False)
        report = generate_propagation_report(result)
        print(report)
        _write_output(report, args.output, "Propagation apply report")
        engine.close()
        return

    # --- Backward-compatible mode: read-only propagation analysis ---
    drawing = getattr(args, 'drawing', None)
    component = getattr(args, 'component', None)

    if not drawing or not component:
        print("Error: provide both drawing and component, or use --plan/--apply/--log flags.")
        engine.close()
        return

    result = engine.propagate_update(drawing, component)

    lines = []
    lines.append(f"Propagation Analysis: {component} in {drawing}")
    lines.append("=" * 60)

    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    elif not result["affected_drawings"]:
        lines.append("No other drawings need updating.")
    else:
        lines.append(f"Affected drawings: {len(result['affected_drawings'])}")
        lines.append("")
        for change in result["changes_needed"]:
            lines.append(f"  {change['drawing_id']} ({change['file_path']}):")
            for diff in change["differences"]:
                lines.append(f"    {diff['parameter']}: {diff['target_value']} -> {diff['source_value']}")
            lines.append("")

    output = "\n".join(lines)
    print(output)
    _write_output(output, args.output, "Propagation report")
    engine.close()


def cmd_graph(args):
    """Show dependency graph."""
    engine = SyncEngine(args.db)

    # If -i provided, scan that directory first
    if args.input:
        print(f"Scanning: {args.input}")
        engine.scan_directory(args.input)
        print()

    report = generate_dependency_graph_report(engine)
    print(report)
    _write_output(report, args.output)
    engine.close()


def cmd_log(args):
    """Show change log."""
    engine = SyncEngine(args.db)

    # If -i provided, scan that directory first
    if args.input:
        print(f"Scanning: {args.input}")
        engine.scan_directory(args.input)
        print()

    report = generate_change_log_report(
        engine, drawing_id=args.drawing, limit=args.limit,
    )
    print(report)
    _write_output(report, args.output, "Change log")
    engine.close()


def cmd_watch(args):
    """Watch directories for live changes."""
    from .watcher import DrawingWatcher

    directory = args.input
    engine = SyncEngine(args.db)
    watcher = DrawingWatcher(engine, callback=_watch_callback)
    watcher.watch(directory)

    print(f"Watching: {directory}")
    print("Press Ctrl+C to stop")
    print()

    try:
        watcher.start()
    except KeyboardInterrupt:
        pass
    finally:
        engine.close()


def cmd_export(args):
    """Export component data as JSON."""
    engine = SyncEngine(args.db)

    # If -i provided, scan that directory first
    if args.input:
        print(f"Scanning: {args.input}")
        engine.scan_directory(args.input)
        print()

    if args.component:
        data = engine.get_component_sync_status(args.component)
    elif args.drawing:
        data = engine.get_sync_report(args.drawing)
    else:
        # Export everything
        data = {
            "statistics": engine.db.get_statistics(),
            "shared_components": engine.db.get_shared_components(),
            "dependency_graph": engine.get_dependency_graph(),
        }

    output = json.dumps(data, indent=2, default=str)

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Exported to: {args.output}")
    else:
        print(output)

    engine.close()


def cmd_report_all(args):
    """Generate all reports."""
    engine = SyncEngine(args.db)

    # If -i provided, scan that directory first
    if args.input:
        print(f"Scanning: {args.input}")
        engine.scan_directory(args.input)
        print()

    outdir = args.output or "reports"
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    reports = {
        "mismatch": generate_mismatch_report(engine),
        "shared_components": generate_shared_components_report(engine),
        "dependency_graph": generate_dependency_graph_report(engine),
        "change_log": generate_change_log_report(engine),
    }

    for name, content in reports.items():
        path = os.path.join(outdir, f"{name}_{ts}.txt")
        with open(path, "w") as f:
            f.write(content)
        print(f"Generated: {path}")

    engine.close()


def cmd_classify(args):
    """Classify drawing types for all drawings in the database."""
    engine = SyncEngine(args.db)

    if getattr(args, 'index', None):
        engine.load_drawing_index(args.index)

    results = engine.classifier.classify_all(engine.db)

    lines = []
    lines.append("Drawing Type Classification")
    lines.append("=" * 100)
    lines.append(f"{'Drawing ID':<30} {'Drawing Type':<20} {'Title':<48}")
    lines.append("-" * 100)

    for drawing_id in sorted(results.keys()):
        dtype = results[drawing_id]
        title = ""
        # Try index_entries first (loaded from XLSX), then DB
        if engine.classifier and hasattr(engine.classifier, '_index_entries'):
            entry = engine.classifier._index_entries.get(drawing_id)
            if entry:
                title = entry.drawing_title
        if not title:
            try:
                row = engine.db.get_drawing(drawing_id)
                if row:
                    meta = json.loads(row.get("index_metadata_json", "{}") or "{}")
                    title = meta.get("drawing_title", "")
            except Exception:
                pass
        if len(title) > 48:
            title = title[:46] + ".."
        lines.append(f"{drawing_id:<30} {dtype:<20} {title:<48}")

    lines.append("-" * 100)
    lines.append(f"Total: {len(results)} drawings classified")

    # Summary by type
    type_counts = {}
    for dtype in results.values():
        type_counts[dtype] = type_counts.get(dtype, 0) + 1
    lines.append("")
    lines.append("Summary by type:")
    for dtype in sorted(type_counts.keys()):
        lines.append(f"  {dtype:<25} {type_counts[dtype]:>4}")

    output = "\n".join(lines)
    print(output)
    _write_output(output, getattr(args, 'output', None), "Classification report")
    engine.close()


def cmd_cable_list(args):
    """Export cable list to Excel."""
    engine = SyncEngine(args.db)

    # If -i provided, scan that directory first
    if args.input:
        print(f"Scanning: {args.input}")
        engine.scan_directory(args.input)
        print()

    exporter = CableListExporter(engine.db)
    summary = exporter.export_cable_list(
        output_path=args.output,
        filter_drawing=args.drawing,
    )

    print("Cable List Export")
    print("=" * 50)
    print(f"  Total cables:      {summary['total_cables']}")
    print(f"  Unique specs:      {summary['unique_specs']}")
    print(f"  Drawings covered:  {summary['drawings_covered']}")
    print(f"  Output:            {summary['output_path']}")
    print()

    engine.close()


def cmd_authority(args):
    """Manage authority rules for source-of-truth hierarchy."""
    action = args.authority_action

    if action == "export":
        ac = AuthorityConfig()
        output = args.output or "authority_rules.json"
        ac.save_to_json(output)
        print(f"Authority rules exported to: {output}")

    elif action == "show":
        ac = AuthorityConfig()
        rules = ac.get_all_rules()

        lines = []
        lines.append("Authority Rules — Source-of-Truth Hierarchy")
        lines.append("=" * 80)
        lines.append(
            f"{'Parameter':<25} {'Component Types':<30} {'Authority Order':<25}"
        )
        lines.append("-" * 80)

        for rule in rules:
            comp_types = ", ".join(rule.component_types)
            order = " > ".join(rule.authority_order)
            lines.append(f"{rule.parameter:<25} {comp_types:<30} {order:<25}")
            if rule.description:
                lines.append(f"  {'':25} {rule.description}")

        lines.append("-" * 80)
        lines.append(f"Total: {len(rules)} rules")

        print("\n".join(lines))

    else:
        print(f"Unknown authority action: {action}")
        print("Use 'authority show' or 'authority export -o <file>'")


def cmd_audit(args):
    """Decision audit trail: show decision tree, export report, or view log."""
    engine = SyncEngine(args.db)

    if getattr(args, 'show', False):
        component = getattr(args, 'component', None)
        if not component:
            print("Error: --component / -c required for --show")
            engine.close()
            return

        from .reports import generate_decision_tree_report
        report = generate_decision_tree_report(engine.audit, component)
        print(report)
        _write_output(report, getattr(args, 'output', None), "Decision tree report")

    elif getattr(args, 'export', False):
        component = getattr(args, 'component', None)
        output = getattr(args, 'output', None)
        if not component:
            print("Error: --component / -c required for --export")
            engine.close()
            return
        if not output:
            output = f"audit_{component}.txt"

        engine.audit.export_audit_report(component, output)
        print(f"Audit report exported to: {output}")

    elif getattr(args, 'log', False):
        limit = getattr(args, 'limit', 50)
        dtype = getattr(args, 'type', None)

        from .reports import generate_audit_report
        report = generate_audit_report(engine.audit, decision_type=dtype, limit=limit)
        print(report)
        _write_output(report, getattr(args, 'output', None), "Audit log")

    else:
        # Default: show statistics
        stats = engine.audit.get_statistics()
        print("Decision Audit Trail — Statistics")
        print("=" * 50)
        print(f"  Total decisions:      {stats['total']}")
        print(f"  Average confidence:   {stats['average_confidence']:.0%}")
        if stats.get("earliest"):
            print(f"  Earliest decision:    {stats['earliest'][:19]}")
        if stats.get("latest"):
            print(f"  Latest decision:      {stats['latest'][:19]}")
        if stats.get("by_type"):
            print()
            print("  By type:")
            for dtype, count in sorted(stats["by_type"].items()):
                print(f"    {dtype:<25} {count:>5}")
        print()

    engine.close()


def cmd_pipeline(args):
    """Full pipeline: scan input path (file or directory), detect mismatches,
    generate structured output.

    Mirrors the input directory structure in the output directory and generates
    all reports, exports, and per-drawing data.
    """
    import time

    input_path = args.input
    output_dir = args.output or "output"
    is_single_file = os.path.isfile(input_path)

    if not is_single_file and not os.path.isdir(input_path):
        print(f"Error: input path does not exist: {input_path}")
        return 1

    print("=" * 80)
    print("DRAWING SYNC — FULL PIPELINE")
    print(f"Input:    {os.path.abspath(input_path)}")
    print(f"Output:   {os.path.abspath(output_dir)}")
    print(f"Database: {args.db}")
    print("=" * 80)
    print()

    # Create output structure
    os.makedirs(output_dir, exist_ok=True)
    reports_dir = os.path.join(output_dir, "reports")
    exports_dir = os.path.join(output_dir, "exports")
    drawings_dir = os.path.join(output_dir, "drawings")
    os.makedirs(reports_dir, exist_ok=True)
    os.makedirs(exports_dir, exist_ok=True)
    os.makedirs(drawings_dir, exist_ok=True)

    # Mirror subdirectory structure from input into drawings/ (only for directories)
    if not is_single_file:
        _mirror_subdirs(input_path, drawings_dir)

    # Place DB inside output dir
    db_path = args.db
    if db_path == "drawing_sync.db":
        db_path = os.path.join(output_dir, "drawing_sync.db")

    engine = SyncEngine(db_path)

    if getattr(args, 'index', None):
        engine.load_drawing_index(args.index)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Step 1: Scan ────────────────────────────────────────────────
    print("STEP 1: Scanning all drawings...")
    t0 = time.time()

    if is_single_file:
        all_results = engine.scan_single_file_with_results(input_path, force=args.force)
    else:
        # Walk input to find all scannable subdirectories (PDF, CAD, etc.)
        scan_dirs = _find_scan_dirs(input_path)
        all_results = {
            "scanned": 0, "skipped": 0, "errors": [],
            "new_drawings": [], "updated_drawings": [], "drawings": {},
        }

        if scan_dirs:
            for sd in scan_dirs:
                results = engine.scan_directory(sd, force=args.force)
                all_results["scanned"] += results["scanned"]
                all_results["skipped"] += results["skipped"]
                all_results["errors"].extend(results["errors"])
                all_results["new_drawings"].extend(results["new_drawings"])
                all_results["updated_drawings"].extend(results["updated_drawings"])
                all_results["drawings"].update(results["drawings"])
        else:
            # No recognized subdirs — scan the whole input dir
            all_results = engine.scan_directory(input_path, force=args.force)

    t1 = time.time()

    scan_report = generate_scan_report(engine, all_results)
    print(f"  Scanned {all_results['scanned']} drawings in {t1-t0:.1f}s")
    print(f"  Skipped {all_results['skipped']} unchanged")
    if all_results["errors"]:
        print(f"  Errors: {len(all_results['errors'])}")
    print()

    scan_path = os.path.join(reports_dir, f"scan_{ts}.txt")
    with open(scan_path, "w") as f:
        f.write(scan_report)

    # ── Step 2: Mismatch detection ──────────────────────────────────
    print("STEP 2: Running mismatch detection...")
    t0 = time.time()
    mismatches = engine.check_mismatches()
    t1 = time.time()

    mismatch_report = generate_mismatch_report(engine)
    critical = sum(1 for m in mismatches if m.severity.value == "CRITICAL")
    warning = sum(1 for m in mismatches if m.severity.value == "WARNING")
    info = sum(1 for m in mismatches if m.severity.value == "INFO")
    print(f"  {len(mismatches)} mismatches ({critical} critical, {warning} warning, {info} info) in {t1-t0:.1f}s")
    print()

    mismatch_path = os.path.join(reports_dir, f"mismatch_{ts}.txt")
    with open(mismatch_path, "w") as f:
        f.write(mismatch_report)

    # ── Step 3: Generate all reports ────────────────────────────────
    print("STEP 3: Generating reports...")
    shared_report = generate_shared_components_report(engine)
    graph_report = generate_dependency_graph_report(engine)
    log_report = generate_change_log_report(engine)

    for name, content in [
        ("shared_components", shared_report),
        ("dependency_graph", graph_report),
        ("change_log", log_report),
    ]:
        path = os.path.join(reports_dir, f"{name}_{ts}.txt")
        with open(path, "w") as f:
            f.write(content)
        print(f"  Generated: {path}")

    # ── Step 4: Per-drawing exports ─────────────────────────────────
    print()
    print("STEP 4: Exporting per-drawing data...")
    all_drawing_ids = engine.db.get_all_drawing_ids()
    for dwg_id in all_drawing_ids:
        dwg_data = engine.get_sync_report(dwg_id)
        dwg_path = os.path.join(drawings_dir, f"{dwg_id}.json")
        with open(dwg_path, "w") as f:
            json.dump(dwg_data, f, indent=2, default=str)
    print(f"  Exported {len(all_drawing_ids)} drawing files to {drawings_dir}/")

    # ── Step 5: Full JSON export ────────────────────────────────────
    print()
    print("STEP 5: Generating full export...")
    full_export = {
        "generated": datetime.now().isoformat(),
        "input_directory": os.path.abspath(input_path),
        "statistics": engine.db.get_statistics(),
        "shared_components": engine.db.get_shared_components(),
        "dependency_graph": engine.get_dependency_graph(),
    }
    export_path = os.path.join(exports_dir, f"full_export_{ts}.json")
    with open(export_path, "w") as f:
        json.dump(full_export, f, indent=2, default=str)
    print(f"  Exported: {export_path}")

    # ── Summary ─────────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("PIPELINE COMPLETE")
    print("=" * 80)
    stats = engine.db.get_statistics()
    print(f"  Drawings:         {stats['total_drawings']}")
    print(f"  Components:       {stats['total_components']}")
    print(f"  Shared (2+ dwgs): {stats['shared_components']}")
    print(f"  Connections:      {stats['total_connections']}")
    print(f"  Labels:           {stats['total_labels']}")
    print(f"  Mismatches:       {stats['active_mismatches']}")
    print()
    print(f"Output directory: {os.path.abspath(output_dir)}/")
    print(f"  reports/    — scan, mismatch, shared components, dependency graph, change log")
    print(f"  drawings/   — per-drawing JSON exports ({len(all_drawing_ids)} files)")
    print(f"  exports/    — full JSON export")
    print(f"  drawing_sync.db — SQLite database")
    print()

    engine.close()
    return 1 if critical > 0 else 0


def _mirror_subdirs(src: str, dst: str):
    """Mirror the subdirectory structure from src into dst (directories only)."""
    for root, dirs, _files in os.walk(src):
        rel = os.path.relpath(root, src)
        if rel == ".":
            continue
        if "backup" in rel.lower():
            continue
        target = os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)


def _find_scan_dirs(input_dir: str) -> list:
    """Find subdirectories that contain scannable files (PDF, DXF, DWG, XLSX).

    Returns a list of directories to scan individually, or empty list if
    the input_dir itself should be scanned as a flat directory.
    """
    scan_dirs = []
    has_files_at_root = False

    for entry in os.scandir(input_dir):
        if entry.is_dir() and "backup" not in entry.name.lower():
            # Check if this subdir has scannable files
            for _root, _dirs, files in os.walk(entry.path):
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in (".pdf", ".dxf", ".dwg", ".xlsx", ".xls"):
                        scan_dirs.append(entry.path)
                        break
                break  # only check top level of each subdir
        elif entry.is_file():
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in (".pdf", ".dxf", ".dwg", ".xlsx", ".xls"):
                has_files_at_root = True

    if has_files_at_root and not scan_dirs:
        return []  # signal: scan input_dir itself

    return scan_dirs


def _watch_callback(event: dict):
    """Callback for file watcher events."""
    ts = event["timestamp"][:19]
    dwg = event["drawing_id"]
    comps = event["components_extracted"]
    mismatches = event["mismatches"]

    print(f"[{ts}] {event['event'].upper()}: {dwg} — {comps} components extracted")

    if mismatches > 0:
        print(f"  *** {mismatches} MISMATCH(ES) DETECTED ***")

    if event["propagation"]:
        for comp_id, prop in event["propagation"].items():
            affected = prop["affected_drawings"]
            if affected:
                print(f"  {comp_id} -> update needed in: {', '.join(affected)}")


def main():
    parser = argparse.ArgumentParser(
        description="Drawing Synchronization System — "
        "Detect and synchronize component changes across electrical drawings",
    )
    parser.add_argument(
        "--db", default="drawing_sync.db",
        help="Path to SQLite database (default: drawing_sync.db)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # scan
    p = subparsers.add_parser("scan", help="Scan directories for drawings")
    p.add_argument("-i", "--input", required=True, help="File or directory to scan")
    p.add_argument("-o", "--output", help="Save scan report to file")
    p.add_argument("--force", action="store_true", help="Force re-scan all files")
    p.add_argument("--index", help="Drawing index XLSX for type classification")

    # check
    p = subparsers.add_parser("check", help="Run mismatch detection")
    p.add_argument("-i", "--input", help="Directory to scan before checking (optional)")
    p.add_argument("-o", "--output", help="Save mismatch report to file")

    # status
    p = subparsers.add_parser("status", help="Component sync status")
    p.add_argument("-i", "--input", help="Directory to scan before checking (optional)")
    p.add_argument("-o", "--output", help="Save report to file")
    p.add_argument("-c", "--component", help="Specific component ID")

    # propagate
    p = subparsers.add_parser("propagate", help="Show update propagation / plan / apply")
    p.add_argument("-i", "--input", help="Directory to scan before checking (optional)")
    p.add_argument("-o", "--output", help="Save propagation report to file")
    p.add_argument("drawing", nargs="?", default=None, help="Source drawing ID (for legacy mode)")
    p.add_argument("component", nargs="?", default=None, help="Component ID")
    p.add_argument("--plan", action="store_true", help="Plan propagation (dry-run)")
    p.add_argument("--apply", action="store_true", help="Apply propagation to database")
    p.add_argument("--all", action="store_true", help="Operate on ALL mismatched components")
    p.add_argument("--force", action="store_true", help="Skip confirmation when applying")
    p.add_argument("--log", action="store_true", help="Show propagation log")

    # graph
    p = subparsers.add_parser("graph", help="Show dependency graph")
    p.add_argument("-i", "--input", help="Directory to scan before checking (optional)")
    p.add_argument("-o", "--output", help="Save report to file")

    # log
    p = subparsers.add_parser("log", help="Show change log")
    p.add_argument("-i", "--input", help="Directory to scan before checking (optional)")
    p.add_argument("-o", "--output", help="Save change log to file")
    p.add_argument("-d", "--drawing", help="Filter by drawing ID")
    p.add_argument("-n", "--limit", type=int, default=50)

    # watch
    p = subparsers.add_parser("watch", help="Watch for live changes")
    p.add_argument("-i", "--input", required=True, help="Directory to watch")
    p.add_argument("-o", "--output", help="(unused, accepted for consistency)")

    # export
    p = subparsers.add_parser("export", help="Export data as JSON")
    p.add_argument("-i", "--input", help="Directory to scan before exporting (optional)")
    p.add_argument("-o", "--output", help="Output JSON file")
    p.add_argument("-c", "--component", help="Export specific component")
    p.add_argument("-d", "--drawing", help="Export specific drawing")

    # report-all
    p = subparsers.add_parser("report-all", help="Generate all reports")
    p.add_argument("-i", "--input", help="Directory to scan before reporting (optional)")
    p.add_argument("-o", "--output", help="Output directory for reports")

    # pipeline
    p = subparsers.add_parser("pipeline", help="Full pipeline: scan + detect + report with structured output")
    p.add_argument("-i", "--input", required=True, help="Input file or directory containing drawings (PDF/DXF/DWG)")
    p.add_argument("-o", "--output", help="Output directory for structured results (default: output/)")
    p.add_argument("--force", action="store_true", help="Force re-scan all files")
    p.add_argument("--index", help="Drawing index XLSX for type classification")

    # cable-list
    p = subparsers.add_parser("cable-list", help="Export cable list to Excel")
    p.add_argument("-i", "--input", help="Directory to scan before exporting (optional)")
    p.add_argument("-o", "--output", required=True, help="Output XLSX file path")
    p.add_argument("-d", "--drawing", help="Filter to specific drawing ID")

    # classify
    p = subparsers.add_parser("classify", help="Classify drawing types")
    p.add_argument("--index", help="Drawing index XLSX for type classification")
    p.add_argument("-o", "--output", help="Save classification report to file")

    # authority
    p = subparsers.add_parser("authority", help="Manage authority rules for source-of-truth hierarchy")
    p.add_argument("authority_action", choices=["show", "export"], help="Action: show rules or export to JSON")
    p.add_argument("-o", "--output", help="Output JSON file (for export action)")

    # audit
    p = subparsers.add_parser("audit", help="Decision audit trail")
    p.add_argument("-c", "--component", help="Component ID for decision tree")
    p.add_argument("--show", action="store_true", help="Show decision tree")
    p.add_argument("--export", action="store_true", help="Export audit report")
    p.add_argument("--log", action="store_true", help="Show recent decisions")
    p.add_argument("-o", "--output", help="Output file path")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("--type", help="Filter by decision type")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if not args.command:
        parser.print_help()
        return

    commands = {
        "scan": cmd_scan,
        "check": cmd_check,
        "status": cmd_status,
        "propagate": cmd_propagate,
        "graph": cmd_graph,
        "log": cmd_log,
        "watch": cmd_watch,
        "export": cmd_export,
        "report-all": cmd_report_all,
        "pipeline": cmd_pipeline,
        "cable-list": cmd_cable_list,
        "classify": cmd_classify,
        "authority": cmd_authority,
        "audit": cmd_audit,
    }

    func = commands.get(args.command)
    if func:
        result = func(args)
        if isinstance(result, int):
            sys.exit(result)


if __name__ == "__main__":
    main()
