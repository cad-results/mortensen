# Drawing Synchronization System — Scripts Guide

## Prerequisites

```bash
# Activate the conda environment
conda activate drawing-sync
```

All commands below assume you are in `/home/user/mortensen/` and the `drawing-sync` conda env is active.

All commands use `-i` for input (file or directory) and `-o` for output (file or directory).

---

## 1. Full Pipeline (`pipeline`)

Scans drawings from a directory or a single file, extracts components, detects mismatches, and generates a structured output directory with all reports, per-drawing exports, and a full JSON export.

```bash
# Run full pipeline on PDF drawings (directory)
python -m drawing_sync.cli pipeline -i "data/NRE P&C/P&C_PDF/" -o output/

# Run full pipeline on a single file
python -m drawing_sync.cli pipeline -i "data/test_files/NRE-EC-320.1_in_autocad_2026.dxf" -o output_single/

# Run on CAD drawings
python -m drawing_sync.cli pipeline -i "data/NRE P&C/P&C_CAD/" -o output_cad/

# Force re-scan all files (ignore file hashes)
python -m drawing_sync.cli pipeline -i "data/NRE P&C/P&C_PDF/" -o output/ --force

# Drawing index is auto-discovered from data/global_reference/ — no --index needed
# To use a specific index file instead:
python -m drawing_sync.cli pipeline -i "data/NRE P&C/P&C_PDF/" -o output/ --index "data/NRE P&C/P&C_CAD/SCHEDULES/NRE P&C DRAWING INDEX.xlsx"

# Verbose mode (debug logging)
python -m drawing_sync.cli -v pipeline -i "data/NRE P&C/P&C_PDF/" -o output/
```

**Output directory structure:**

```
output/
├── drawing_sync.db                          # SQLite database — all extracted data (9 tables)
├── reports/
│   ├── scan_YYYYMMDD_HHMMSS.txt             # Extraction summary: components, connections, labels per drawing
│   ├── mismatch_YYYYMMDD_HHMMSS.txt         # All mismatches grouped by severity (CRITICAL/WARNING/INFO)
│   ├── shared_components_YYYYMMDD_HHMMSS.txt # Components appearing in 2+ drawings with drawing lists
│   ├── dependency_graph_YYYYMMDD_HHMMSS.txt  # Cross-reference relationships between drawings
│   └── change_log_YYYYMMDD_HHMMSS.txt        # Chronological change audit trail
├── drawings/
│   ├── NRE-EC-001.0.json                     # Per-drawing JSON: components, mismatches, recommendations
│   ├── NRE-EC-001.1.json
│   └── ... (one JSON per scanned drawing)
└── exports/
    └── full_export_YYYYMMDD_HHMMSS.json      # Full cross-drawing export: stats, shared components, dependency graph
```

See [README.md](README.md#pipeline-output--what-gets-produced) for detailed descriptions of each output file and example contents.

---

## 2. CLI Commands

The CLI is the primary interface. All commands use `python -m drawing_sync.cli`.

Every command supports `-i` (input directory) and `-o` (output file/directory). For commands that primarily work on the database (`check`, `status`, `graph`, `log`, `export`, `report-all`), `-i` optionally specifies a directory to scan before running the command.

### 2a. Scan Drawings

Scans a directory or single file for PDF/DXF/DWG files, extracts components, and stores in the database.

```bash
# Scan all PDFs in a directory
python -m drawing_sync.cli scan -i "data/NRE P&C/P&C_PDF/"

# Scan a single file
python -m drawing_sync.cli scan -i "data/test_files/NRE-EC-320.1_in_autocad_2026.dxf"

# Scan CAD files (requires DXF or ODA File Converter for DWG)
python -m drawing_sync.cli scan -i "data/NRE P&C/P&C_CAD/"

# Force re-scan (ignore file hashes, re-extract everything)
python -m drawing_sync.cli scan -i "data/NRE P&C/P&C_PDF/" --force

# Drawing index is auto-discovered from data/global_reference/ — no --index needed
# To use a specific index file:
python -m drawing_sync.cli scan -i "data/NRE P&C/P&C_PDF/" --index "data/NRE P&C/P&C_CAD/SCHEDULES/NRE P&C DRAWING INDEX.xlsx"

# Save scan report to file
python -m drawing_sync.cli scan -i "data/NRE P&C/P&C_PDF/" -o reports/scan_report.txt

# Verbose mode (debug logging)
python -m drawing_sync.cli -v scan -i "data/NRE P&C/P&C_PDF/"
```

### 2b. Run Mismatch Detection

Runs all 12 mismatch checks: value mismatches, naming inconsistencies, broken cross-references, cable spec conflicts, terminal block issues, voltage level consistency, relay assignments, orphan components, relay-breaker trip paths, lockout relay completeness, CT-relay associations, and DC supply completeness.

```bash
# Run all checks (requires prior scan)
python -m drawing_sync.cli check

# Scan and check in one command
python -m drawing_sync.cli check -i "data/NRE P&C/P&C_PDF/"

# Save mismatch report
python -m drawing_sync.cli check -o reports/mismatch_report.txt
```

Alert severity levels:
- **[RED FLAG] CRITICAL** — Voltage/current/power rating mismatch (safety issue)
- **[WARNING]** — Naming inconsistency, broken cross-reference, cable spec mismatch
- **[INFO]** — Terminal block count variation, minor discrepancies

### 2c. Component Sync Status

Check if a specific component is consistent across all drawings, or list all shared components.

```bash
# Show all shared components (appear in 2+ drawings)
python -m drawing_sync.cli status

# Deep-dive on a specific component
python -m drawing_sync.cli status -c "52-L1"
python -m drawing_sync.cli status -c "SEL-451"
python -m drawing_sync.cli status -c "87-BH"
python -m drawing_sync.cli status -c "86MP"

# Save status report to file
python -m drawing_sync.cli status -c "52-L1" -o reports/52-L1_status.txt

# Scan first, then show status
python -m drawing_sync.cli status -i "data/NRE P&C/P&C_PDF/" -c "52-L1"
```

### 2d. Propagation Analysis & Execution

Analyze propagation impact, plan authority-based updates, or apply changes to the database.

**Legacy mode** — Show which drawings need updating when a component changes:

```bash
# Show impact of changing 52-L1 in NRE-EC-301.0
python -m drawing_sync.cli propagate NRE-EC-301.0 52-L1

# Show impact of changing SEL-451 in NRE-EC-200.0
python -m drawing_sync.cli propagate NRE-EC-200.0 SEL-451

# Save propagation report to file
python -m drawing_sync.cli propagate NRE-EC-301.0 52-L1 -o reports/propagation.txt

# Scan first, then analyze propagation
python -m drawing_sync.cli propagate NRE-EC-301.0 52-L1 -i "data/NRE P&C/P&C_PDF/"
```

**Plan mode** — Generate proposed propagation actions using authority rules (dry-run):

```bash
# Plan propagation for a single component
python -m drawing_sync.cli propagate --plan 52-L1

# Plan propagation for ALL shared components with mismatches
python -m drawing_sync.cli propagate --plan --all

# Save plan to file
python -m drawing_sync.cli propagate --plan --all -o reports/propagation_plan.txt
```

**Apply mode** — Execute propagation actions (updates component values in the database):

```bash
# Apply propagation for a single component (with confirmation prompt)
python -m drawing_sync.cli propagate --apply 52-L1

# Apply propagation for ALL shared components
python -m drawing_sync.cli propagate --apply --all

# Skip confirmation prompt
python -m drawing_sync.cli propagate --apply --all --force

# Save applied actions report
python -m drawing_sync.cli propagate --apply --all --force -o reports/propagation_applied.txt
```

**Propagation log** — View history of applied propagation actions:

```bash
# Show full propagation log
python -m drawing_sync.cli propagate --log

# Save log to file
python -m drawing_sync.cli propagate --log -o reports/propagation_log.txt
```

### 2e. Dependency Graph

Shows how drawings are related through cross-references and shared components.

```bash
python -m drawing_sync.cli graph
python -m drawing_sync.cli graph -o reports/dependency_graph.txt
python -m drawing_sync.cli graph -i "data/NRE P&C/P&C_PDF/" -o reports/dependency_graph.txt
```

### 2f. Change Log

Shows history of detected changes across drawing updates.

```bash
# Show all changes
python -m drawing_sync.cli log

# Filter by drawing
python -m drawing_sync.cli log -d NRE-EC-301.0

# Limit results
python -m drawing_sync.cli log -n 20

# Save to file
python -m drawing_sync.cli log -o reports/change_log.txt
```

### 2g. Live File Watcher

Monitors a directory for file changes and automatically re-extracts and checks for mismatches.

```bash
# Watch PDF directory
python -m drawing_sync.cli watch -i "data/NRE P&C/P&C_PDF/"

# Watch CAD directory
python -m drawing_sync.cli watch -i "data/NRE P&C/P&C_CAD/"
```

When a file changes, the watcher will:
1. Re-extract all components from the changed file
2. Run mismatch detection on affected components
3. Show which other drawings need updating
4. Print alerts for any RED FLAG mismatches

Press `Ctrl+C` to stop.

### 2h. Export as JSON

Export component data for integration with other tools.

```bash
# Export everything (stats, shared components, dependency graph)
python -m drawing_sync.cli export -o reports/full_export.json

# Export a specific component
python -m drawing_sync.cli export -c "52-L1" -o reports/52-L1.json

# Export a specific drawing
python -m drawing_sync.cli export -d NRE-EC-301.0 -o reports/NRE-EC-301.0.json

# Scan first, then export
python -m drawing_sync.cli export -i "data/NRE P&C/P&C_PDF/" -o reports/full_export.json
```

### 2i. Generate All Reports at Once

```bash
python -m drawing_sync.cli report-all -o reports/

# Scan first, then generate all reports
python -m drawing_sync.cli report-all -i "data/NRE P&C/P&C_PDF/" -o reports/
```

Generates: `mismatch_*.txt`, `shared_components_*.txt`, `dependency_graph_*.txt`, `change_log_*.txt`

### 2j. Classify Drawing Types

Classify all drawings in the database by type (ONE_LINE, AC_SCHEMATIC, DC_SCHEMATIC, etc.) using three strategies: Drawing Index XLSX lookup, title block DWGTYPE attribute, and drawing number series inference.

```bash
# Classify all drawings in the database
python -m drawing_sync.cli classify

# Drawing index is auto-discovered — classify also shows drawing titles
python -m drawing_sync.cli classify

# Or specify an index file explicitly:
python -m drawing_sync.cli classify --index "data/NRE P&C/P&C_CAD/SCHEDULES/NRE P&C DRAWING INDEX.xlsx"

# Save classification report to file
python -m drawing_sync.cli classify -o reports/classification.txt
```

### 2k. Authority Rules

View or export the source-of-truth authority rules that determine which drawing type is authoritative for each parameter.

```bash
# Display all authority rules
python -m drawing_sync.cli authority show

# Export rules to JSON (for editing or external tools)
python -m drawing_sync.cli authority export -o authority_rules.json
```

### 2l. Cable List Export

Export all cable/connection data from the database to a formatted Excel workbook with three sheets: Cable List, Cable Schedule Summary, and By Drawing.

```bash
# Export all cables to Excel
python -m drawing_sync.cli cable-list -o reports/cable_list.xlsx

# Export cables from a single drawing
python -m drawing_sync.cli cable-list -o reports/cable_list_301.xlsx -d NRE-EC-301.0

# Scan first, then export
python -m drawing_sync.cli cable-list -i "data/NRE P&C/P&C_PDF/" -o reports/cable_list.xlsx
```

### 2m. BOM Extraction

BOM files are automatically detected and fully extracted during `scan` and `pipeline` commands — no separate command is needed. Any XLSX/XLS file with "BOM" in its name gets full line-item extraction in addition to the standard regex-based component detection.

```bash
# Scan all files including BOMs (automatic detection)
python -m drawing_sync.cli scan -i "data/NRE P&C/P&C_CAD/SCHEDULES/"

# Scan a single BOM file
python -m drawing_sync.cli scan -i "data/NRE P&C/P&C_CAD/SCHEDULES/CT-B1M JUNCTION BOX BOM.xlsx"

# Full pipeline (BOMs are included automatically)
python -m drawing_sync.cli pipeline -i "data/NRE P&C/" -o output/
```

**What gets extracted from BOM files:**
- Every MATERIAL row: catalog number, quantity, material description, vendor, specifications
- Every NAMEPLATE row: sub-component designations (TB1, FU1, etc.), sizes, text
- Every DEVICE LIST row (IRIG-B style): device names, quantities, catalog numbers
- Embedded electrical values from descriptions (600VAC, 30A, etc.)
- All standard regex patterns (same as PDF/DXF extraction)

**Supported BOM files:** CT-B1M, CT-B2M, VT-B1, 89-BT1, CCVT, YARD LIGHT, TERM CAB junction box BOMs; IRIG-B & TIME DISTRIBUTION BOM; NRE FIBER BOM (.xls via xlrd).

### 2n. Decision Audit Trail

View decision statistics, generate decision trees for specific components, export formal audit reports, or browse recent decisions.

```bash
# Show audit trail statistics (default)
python -m drawing_sync.cli audit

# Show decision tree for a specific component
python -m drawing_sync.cli audit --show -c "52-L1"

# Export formal audit report for a component (for compliance review)
python -m drawing_sync.cli audit --export -c "86MP" -o reports/audit_86MP.txt

# Browse recent decisions
python -m drawing_sync.cli audit --log

# Filter decisions by type
python -m drawing_sync.cli audit --log --type CLASSIFICATION

# Limit number of results
python -m drawing_sync.cli audit --log -n 20

# Save audit log to file
python -m drawing_sync.cli audit --log -o reports/audit_log.txt
```

---

## 3. Command Reference

| Command | `-i` (input) | `-o` (output) | Other flags |
|---|---|---|---|
| `scan` | **Required.** File or directory to scan | Report file | `--force`, `--index` |
| `check` | Optional. Scan dir first | Report file | |
| `status` | Optional. Scan dir first | Report file | `-c COMPONENT` |
| `propagate` | Optional. Scan dir first | Report file | `DRAWING COMPONENT` (positional), `--plan`, `--apply`, `--all`, `--force`, `--log` |
| `classify` | (not used) | Report file | `--index` |
| `authority` | (not used) | JSON file (export) | `show` or `export` (positional action) |
| `cable-list` | Optional. Scan dir first | **Required.** XLSX file | `-d DRAWING` |
| `audit` | (not used) | Report file | `--show`, `--export`, `--log`, `-c COMPONENT`, `-n LIMIT`, `--type` |
| `graph` | Optional. Scan dir first | Report file | |
| `log` | Optional. Scan dir first | Report file | `-d DRAWING`, `-n LIMIT` |
| `watch` | **Required.** Directory to watch | (unused) | |
| `export` | Optional. Scan dir first | JSON file | `-c COMPONENT`, `-d DRAWING` |
| `report-all` | Optional. Scan dir first | Output directory | |
| `pipeline` | **Required.** File or directory | Output directory | `--force`, `--index` |

Global flags: `--db PATH` (database path), `-v` (verbose/debug logging).

---

## 4. Python API Usage

For integration into other scripts or notebooks:

```python
from drawing_sync.sync_engine import SyncEngine
from drawing_sync.reports import generate_mismatch_report

# Initialize
engine = SyncEngine("drawing_sync.db")

# Scan a directory
results = engine.scan_directory("data/NRE P&C/P&C_PDF/", force=True)

# Or scan a single file
results = engine.scan_single_file_with_results("data/test_files/NRE-EC-320.1_in_autocad_2026.dxf")

# Detect mismatches (all 12 checks)
mismatches = engine.check_mismatches()

# Get component status across all drawings
status = engine.get_component_sync_status("52-L1")
print(f"Consistent: {status['is_consistent']}")
print(f"Appears in {status['total_drawings']} drawings")

# Show propagation impact (legacy mode)
prop = engine.propagate_update("NRE-EC-301.0", "52-L1")
print(f"Affected drawings: {prop['affected_drawings']}")

# Get dependency graph
graph = engine.get_dependency_graph()

# Get shared components
shared = engine.db.get_shared_components(min_drawings=2)

# Generate reports
report = generate_mismatch_report(engine)
print(report)

engine.close()
```

### Drawing Classification API

```python
from drawing_sync.sync_engine import SyncEngine

engine = SyncEngine("drawing_sync.db")

# Classify all drawings by type
results = engine.classifier.classify_all(engine.db)
for drawing_id, dtype in sorted(results.items()):
    print(f"{drawing_id}: {dtype}")

# Or classify with a drawing index for highest confidence
engine.load_drawing_index("data/NRE P&C/P&C_CAD/SCHEDULES/NRE P&C DRAWING INDEX.xlsx")
results = engine.classifier.classify_all(engine.db)

engine.close()
```

### Authority Rules API

```python
from drawing_sync.authority import AuthorityConfig

# Load default rules
ac = AuthorityConfig()

# Get authority order for a parameter
order = ac.get_authority("voltage_rating")
print(f"Voltage authority: {' > '.join(order)}")
# Output: Voltage authority: ONE_LINE > AC_SCHEMATIC > DC_SCHEMATIC > PANEL_WIRING

# Find the authoritative drawing from a set of candidates
candidates = {"NRE-EC-100.0": "ONE_LINE", "NRE-EC-301.0": "DC_SCHEMATIC"}
auth = ac.get_authoritative_drawing("voltage_rating", "*", candidates)
print(f"Authoritative drawing: {auth}")  # NRE-EC-100.0

# Get explanation
basis = ac.get_authority_basis("voltage_rating", "*", "ONE_LINE")
print(basis)  # ONE_LINE is authoritative for voltage_rating (priority 1 of 4)

# Export/import rules as JSON
ac.save_to_json("authority_rules.json")
ac2 = AuthorityConfig("authority_rules.json")
```

### Propagation Engine API

```python
from drawing_sync.sync_engine import SyncEngine

engine = SyncEngine("drawing_sync.db")

# Plan propagation for a single component
actions = engine.plan_propagation("52-L1")
for a in actions:
    print(f"  {a.source_drawing_id} -> {a.target_drawing_id}: "
          f"{a.parameter} = {a.old_value} -> {a.new_value}")
    print(f"    Authority: {a.authority_basis}")

# Plan propagation for ALL shared components
all_actions = engine.plan_all_propagations()
summary = engine.propagation.get_propagation_summary(all_actions)
print(f"Total actions: {summary['total']}")
print(f"Affected components: {len(summary['affected_components'])}")
print(f"Affected drawings: {len(summary['affected_drawings'])}")

# Apply propagation (updates database)
applied = engine.propagation.apply_propagation(all_actions, dry_run=False)

engine.close()
```

### Cable List Export API

```python
from drawing_sync.sync_engine import SyncEngine
from drawing_sync.cable_export import CableListExporter

engine = SyncEngine("drawing_sync.db")
exporter = CableListExporter(engine.db)

# Export all cables to Excel
summary = exporter.export_cable_list("reports/cable_list.xlsx")
print(f"Total cables: {summary['total_cables']}")
print(f"Unique specs: {summary['unique_specs']}")
print(f"Drawings: {summary['drawings_covered']}")

# Export cables from a single drawing
summary = exporter.export_cable_list(
    "reports/cable_301.xlsx",
    filter_drawing="NRE-EC-301.0"
)

engine.close()
```

### Audit Trail API

```python
from drawing_sync.sync_engine import SyncEngine

engine = SyncEngine("drawing_sync.db")
audit = engine.audit

# Get audit statistics
stats = audit.get_statistics()
print(f"Total decisions: {stats['total']}")
print(f"By type: {stats['by_type']}")
print(f"Average confidence: {stats['average_confidence']:.0%}")

# Query decisions
decisions = audit.get_decisions(component_id="52-L1", limit=10)
for d in decisions:
    print(f"  [{d['decision_type']}] {d['reasoning']}")

# Generate decision tree for a component
tree = audit.generate_decision_tree("86MP")
print(f"Classifications: {len(tree['classifications'])}")
print(f"Propagation actions: {len(tree['propagation_actions'])}")

# Export formal audit report (for compliance review)
audit.export_audit_report("86MP", "reports/audit_86MP.txt")

engine.close()
```

---

## 5. Database Direct Access

The SQLite database (`drawing_sync.db`) can be queried directly:

```bash
# Open database
sqlite3 drawing_sync.db

# Key tables:
#   drawings        — all scanned drawings with metadata + drawing_type
#   components      — all components with values, per drawing
#   connections     — wiring connections between components
#   labels          — all text labels with positions
#   mismatches      — detected mismatches/alerts + resolution_options
#   change_log      — history of changes
#   snapshots       — historical snapshots for rollback
#   propagation_log — propagation action history (source, target, authority basis)
#   decisions       — decision audit trail (classification, authority, propagation)

# Example queries:

# Components in most drawings
SELECT component_id, COUNT(drawing_id) as n
FROM components GROUP BY component_id ORDER BY n DESC LIMIT 20;

# Active critical mismatches
SELECT component_id, parameter, message
FROM mismatches WHERE severity='CRITICAL' AND resolved=0;

# Cross-references for a drawing
SELECT cross_references_json FROM drawings WHERE drawing_id='NRE-EC-301.0';

# All values for a component
SELECT drawing_id, values_json FROM components WHERE component_id='52-L1';

# Drawing type classification
SELECT drawing_id, drawing_type FROM drawings ORDER BY drawing_type, drawing_id;

# Classification summary
SELECT drawing_type, COUNT(*) as n FROM drawings GROUP BY drawing_type ORDER BY n DESC;

# Propagation log
SELECT timestamp, component_id, parameter, source_drawing_id, target_drawing_id,
       old_value, new_value, status
FROM propagation_log ORDER BY timestamp DESC LIMIT 20;

# Audit trail decisions
SELECT decision_type, COUNT(*) as n FROM decisions GROUP BY decision_type;

# Decision tree for a component
SELECT decision_type, reasoning, outcome, confidence
FROM decisions WHERE component_id='52-L1' ORDER BY timestamp;
```

---

## 6. DWG Support

DWG files require conversion to DXF. Install ODA File Converter:

```bash
# Download from https://www.opendesign.com/guestfiles/oda_file_converter
# Install and ensure ODAFileConverter is in PATH

# Then scan CAD directory — DWG files will be auto-converted
python -m drawing_sync.cli scan -i "data/NRE P&C/P&C_CAD/"
```

Without ODA, the system automatically falls back to the corresponding PDF in `P&C_PDF/`.

---

## Summary of Key Results (from test runs)

### Full PDF scan (177 drawings)

| Metric                               | Value  |
|--------------------------------------|--------|
| Drawings scanned                     | 177    |
| Drawings classified                  | 177    |
| Unique components                    | 717    |
| Component instances                  | 3,957  |
| Shared components (2+ dwgs)          | 482    |
| Total connections                    | 2,813  |
| Total text labels                    | 81,119 |
| Cables exported to Excel             | 2,813  |
| **Active mismatches (12 checks)**    |  *511* |
| Critical (RED FLAG)                  | 65     |
| Warning                              | 19     |
| Info                                 | 427    |
| Mismatches with authority resolution | 67     |
| Propagation actions planned          | 109    |
| Audit decisions recorded             | 177    |
| Scan time                            | ~66s   |
| Mismatch detection time              | ~0.2s  |

### Single DXF file scan (AutoCAD Electrical)

| Metric                               | Value                                  |
|--------------------------------------|----------------------------------------|
| File                                 | `NRE-EC-320.1_in_autocad_2026.dxf`    |
| Components extracted                 | 20 (up from 4 with old extractor)      |
| Connections extracted                | 7                                      |
| Voltage levels detected              | 125V DC                                |
| Mismatches detected                  | 12                                     |
