# Drawing Synchronization System

A Python system that monitors electrical engineering drawings (PDF and CAD/DWG/DXF) for component changes, detects mismatches across drawings, and enables synchronous updates. Built for the NRE (Nomadic Red Egret) 138/34.5kV substation protection & control drawing set by Mortenson Engineering Services.

---

## The Problem

Electrical substation design involves hundreds of interconnected drawings: one-line diagrams, AC/DC schematics, panel wiring, relay settings, cable schedules, and more. The same component (e.g., breaker `52-L1`, relay `SEL-451`, lockout `86MP`) appears across many of these drawings. When an engineer updates a component's value in one drawing — say, a voltage rating or current setting — every other drawing referencing that component must also be updated. In practice:

- **Components fall out of sync.** A relay shown as `SEL-451` in the schematic might have a different voltage rating in the panel wiring drawing.
- **Cross-references break.** A drawing references `EC-307.0` but the data there doesn't match.
- **There is no built-in netlist in PDFs.** Unlike a CAD model, a PDF drawing is flat — it has no structured component database, no connection list, no way to query "which other drawings use this breaker?"
- **Manual checking is error-prone and slow.** With 177+ drawings and 717+ components, human review misses things.

This system solves all of that by extracting, indexing, cross-referencing, and continuously monitoring every component across every drawing.

---

## How It Works — End to End

### Step 1: Extraction

The system reads every drawing file and extracts all electrical data using spatial text analysis:

```
PDF/DXF file
    |
    v
[Extractor] ──> Components (52-L1, SEL-451, 87-BH, CT-B1M, ...)
             ──> Electrical values (138kV, 2000A, 14.95%Z, 105/140/175MVA, ...)
             ──> Connections (component A terminal X -> component B terminal Y)
             ──> Cable specs (2/C#10, 12/C #12SH, CAT5E, MM FIBER, ...)
             ──> Terminal blocks (TB6-71, TS3-8, ...)
             ──> Cross-references (EC-301.1, EC-307.0, EC-308.0, ...)
             ──> Signal types (TRIP, CLOSE, SCADA, BF TRIP, LOCKOUT, ...)
             ──> Title block (project, revision, drawn by, ...)
             ──> All text labels with X/Y positions
```

For PDFs, the extractor uses `pdfplumber` to get every word with its exact page coordinates. It then uses IEEE/ANSI device function number patterns (52 = breaker, 87 = differential relay, 89 = disconnect, etc.) and relay model patterns (SEL-451, SEL-487, GE-L90, etc.) to identify components. Electrical values are associated with the nearest component using spatial proximity — the voltage label closest to a component symbol is that component's voltage rating.

For DXF/DWG files, the extractor uses `ezdxf` to read block references (component symbols with attributes), text entities, dimension entities, and line/polyline entities (wires). DWG files are auto-converted to DXF via ODA File Converter (run headless under `xvfb-run` to suppress the GUI window). The DXF extractor iterates over both modelspace and all paper space layouts, since DWG files converted via ODA typically place drawing entities in paper space rather than modelspace. Multiline MTEXT entities are split into individual lines before pattern matching to prevent cross-line false matches. Title block metadata (drawing number, type, title, revision, author) is extracted from block reference attributes on INSERT entities whose block names contain "TITLEBLOCK". If ODA File Converter is not installed, the extractor falls back to the corresponding PDF in `P&C_PDF/`.

### Step 2: Storage

All extracted data goes into a SQLite database (`drawing_sync.db`). The schema tracks:

| Table | Purpose |
|---|---|
| `drawings` | Every scanned drawing with file hash, title block, cross-refs, voltage levels, drawing_type, index_metadata |
| `components` | Every component instance per drawing, with values, connections, attributes |
| `connections` | Wiring connections between components (from/to terminals, cable specs) |
| `labels` | All 81,000+ text labels with X/Y positions and categories |
| `mismatches` | Detected mismatches with severity, affected drawings, recommendations, and resolution_options |
| `change_log` | Audit trail of every detected change (component added/removed/modified) |
| `snapshots` | Historical snapshots of component data before each update |
| `propagation_log` | Full audit trail of propagation actions (source, target, parameter, old/new values, authority basis, status) |
| `decisions` | Decision audit trail for compliance (classification, authority, propagation, mismatch decisions with reasoning, confidence, and alternatives) |

The database uses file hashing (SHA-256) to detect which files have actually changed, so incremental scans skip unchanged drawings.

### Step 3: Drawing Classification

After extraction, the system classifies each drawing into a type using three strategies in priority order:

1. **Drawing Index XLSX lookup** (highest confidence) — reads the project drawing index spreadsheet to map drawing numbers to types.
2. **Title block DWGTYPE attribute** — extracts the drawing type from the DXF title block's DWGTYPE field.
3. **Drawing number series inference** (fallback) — maps drawing number ranges to types (e.g., 100-104 = ONE_LINE, 200-261 = AC_SCHEMATIC, 300-380 = DC_SCHEMATIC).

When the drawing index is loaded, the system also extracts drawing titles, revision history, and design phase data. The index XLSX may contain multiple sheets for different design phases (30%, 60%, 90%); the system prefers the highest phase when a drawing appears in multiple sheets. This enriched metadata is stored in the database (`index_metadata_json` column) and used in reports.

The drawing index XLSX is auto-discovered: when scanning a directory, the system walks up the directory tree looking for a `global_reference/` folder containing a `*DRAWING INDEX*.xlsx` file. The explicit `--index` flag still works and takes priority.

Drawing types include: `ONE_LINE`, `AC_SCHEMATIC`, `DC_SCHEMATIC`, `PANEL_WIRING`, `CABLE_WIRING`, `PANEL_LAYOUT`, `SYSTEM_DIAGRAM`, `DRAWING_INDEX`, `RELAY_FUNCTIONAL`, `LEGEND`, `COMMUNICATION`, and `UNKNOWN`.

Classification enables authority-based conflict resolution — knowing that a drawing is a ONE_LINE means its voltage ratings take precedence over the same value in a PANEL_WIRING drawing.

### Step 4: Source-of-Truth & Authority Rules

The system applies a configurable hierarchy of authority rules that define which drawing type is the source of truth for each parameter. For example:

- **voltage_rating** — ONE_LINE > AC_SCHEMATIC > DC_SCHEMATIC > PANEL_WIRING
- **current_rating** — ONE_LINE > AC_SCHEMATIC > DC_SCHEMATIC
- **cable_specification** — DC_SCHEMATIC > CABLE_WIRING > PANEL_WIRING
- **relay_settings** — DC_SCHEMATIC > AC_SCHEMATIC

There are 8 default authority rules. When a mismatch is detected, the system uses these rules to determine which drawing's value should be treated as correct and enriches mismatch reports with authority-based resolution guidance.

### Step 5: Mismatch Detection

Once all drawings are indexed and classified, the system runs 12 automated checks:

1. **Value mismatches** — Same component has different voltage/current/impedance/ratio across drawings. E.g., `52-L1` shown as `34.5kV` in one drawing but `125V DC` in another. These are flagged as **CRITICAL (RED FLAG)**.

2. **Component type consistency** — Same component ID classified as different types in different drawings. Flagged as **WARNING**.

3. **Cross-reference integrity** — Drawing references another drawing that doesn't exist in the scanned set. Flagged as **WARNING**.

4. **Cable spec consistency** — Same cable run between two components has different specs in different drawings. Flagged as **WARNING**.

5. **Terminal block conflicts** — Same terminal block has significantly different terminal counts across drawings (may indicate incomplete wiring schedule). Flagged as **INFO**.

6. **Voltage level consistency** — Component voltage ratings compared across all appearances. Flagged as **CRITICAL** when mismatched.

7. **Relay assignment consistency** — Relay models checked for consistent device associations. Flagged as **WARNING**.

8. **Orphan component detection** — Components appearing in only one drawing that may be missing from related drawings.

9. **Relay-breaker trip path verification** — Verifies that each protective relay (50, 51, 87, 21, 67, 81) has a connection path to its corresponding breaker (52), either directly or via a lockout relay (86). Flagged as **WARNING**.

10. **Lockout relay completeness** — Verifies that each lockout relay (86) has both input connections (from protective relays) and output connections (to breakers). Flagged as **WARNING**.

11. **CT-relay association** — Verifies that each current transformer (CT) has at least one connection to a protective relay. Flagged as **INFO**.

12. **DC supply completeness** — Verifies that relays and breakers have identifiable DC power supply connections. Flagged as **INFO**.

Mismatches with value conflicts are enriched with authority-based resolution options, identifying which drawing holds the authoritative value and why.

### Step 6: Attribute Propagation

The propagation engine uses authority rules to plan and apply attribute updates across drawings:

1. For each shared component with conflicting values, it identifies the authoritative drawing for each parameter.
2. It generates a set of proposed `PropagationAction` records — each specifying the source drawing, target drawing, parameter, old value, new value, and authority basis.
3. Actions can be reviewed in plan mode (`--plan`) before being applied (`--apply`).
4. All applied propagations are logged to the `propagation_log` database table for full traceability.

### Step 7: Synchronization & Change Detection

When a drawing is updated, the system:

1. Re-extracts the changed file automatically (via file watcher or manual re-scan)
2. Compares new data against the previous snapshot stored in the database
3. Logs all changes (component added, removed, or value changed)
4. Identifies every other drawing that shares the same components
5. Reports exactly which drawings need updating and what values differ
6. Re-runs mismatch detection and raises alerts for any new conflicts

### Step 8: Decision Audit Trail

Every significant decision made by the system is recorded to the `decisions` database table for compliance and traceability. Decision types include:

- **CLASSIFICATION** — How each drawing was typed and which strategy was used
- **AUTHORITY** — Which drawing was determined to be the source of truth for a parameter
- **PROPAGATION** — What values were changed, from where, to where, and why
- **MISMATCH_DETECTION** — What inconsistencies were found

The audit trail supports the signing engineer compliance requirement by generating formal decision tree reports showing how the system arrived at its conclusions.

### Step 9: Cable List Extraction

The system can extract all wiring connections from the database and export them as a formatted Excel workbook with three sheets:

- **Cable List** — Every cable/connection with from/to components, terminals, cable spec, wire label, signal type, and source drawing.
- **Cable Schedule Summary** — Aggregated by cable spec with counts and associated drawings/components.
- **By Drawing** — Same data as the Cable List, grouped by source drawing with separator rows.

### Step 10: Continuous Monitoring

The file watcher (`watchdog`) monitors drawing directories in real time. When a PDF or DWG file is saved, the system automatically triggers Steps 1-7 within seconds, printing alerts to the console.

---

## Project Structure

```
mortensen/
├── drawing_sync/                  # Main Python package
│   ├── __init__.py                # Package init, version
│   ├── models.py                  # Data models (Component, Connection, Drawing, Mismatch, DrawingType)
│   ├── extractors/                # File format extractors
│   │   ├── __init__.py
│   │   ├── pdf_extractor.py       # PDF extraction via pdfplumber
│   │   ├── dxf_extractor.py       # DXF/DWG extraction via ezdxf
│   │   └── xlsx_extractor.py      # Excel schedule/BOM extraction via openpyxl
│   ├── db.py                      # SQLite component registry database
│   ├── sync_engine.py             # Core scan/sync/propagation orchestrator
│   ├── drawing_classifier.py      # Drawing type detection & classification (3 strategies)
│   ├── authority.py               # Source-of-truth hierarchy & authority rules
│   ├── propagation_engine.py      # Authority-based attribute propagation engine
│   ├── mismatch_detector.py       # 12 automated mismatch detection checks
│   ├── cable_export.py            # Cable list extraction & Excel export
│   ├── audit.py                   # Decision audit trail for compliance
│   ├── watcher.py                 # Real-time file system monitoring
│   ├── reports.py                 # Human-readable report generation
│   └── cli.py                     # Command-line interface (14 commands, all with -i/-o)
├── data/                          # Drawing files
│   ├── global_reference/          # Master drawing index XLSX (auto-discovered)
│   └── NRE P&C/
│       ├── P&C_PDF/               # 177 PDF drawings
│       ├── P&C_CAD/               # 177 DWG drawings + XREF, PANELS, SCHEDULES
│       └── ...
├── reports/                       # Generated reports (after running)
├── install_oda.sh                 # ODA File Converter installer (DWG support)
├── environment.yml                # Conda environment definition
├── drawing_sync.db                # SQLite database (created after first scan)
├── SCRIPTS.md                     # All runnable commands and scripts
└── README.md                      # This file
```

---

## Module Descriptions

### `models.py` — Data Models

Defines the core data structures used throughout the system:

- **`ComponentType`** — Enum of 52 IEEE/ANSI device function numbers and component categories: `52` (breaker), `50` (overcurrent), `87` (differential), `89` (disconnect), `86` (lockout), plus `CT`, `PT`, `VT`, `CCVT`, `RELAY`, `FUSE`, `PANEL`, `SWITCH`, `CABLE`, `TERMINAL_BLOCK`, `DFR`, `HAND_SWITCH`, `POWER_SUPPLY`, `LTC`, `CUSTODY_METER`, `FIBER_PATCH`, `GROUND`, `RTAC`, `GPS_CLOCK`, `PDC`, `NETWORK_SWITCH`, `ROUTER`, `UPS`, `ATS`, `MCC`, `SWGR`, `VFD`, `PLC`, `RECTIFIER`, `MOV`, `SPD`, `REGULATOR`, `GENERATOR`, etc.
- **`DrawingType`** — Enum of drawing types: `ONE_LINE`, `AC_SCHEMATIC`, `DC_SCHEMATIC`, `PANEL_WIRING`, `CABLE_WIRING`, `PANEL_LAYOUT`, `SYSTEM_DIAGRAM`, `DRAWING_INDEX`, `RELAY_FUNCTIONAL`, `LEGEND`, `COMMUNICATION`, `UNKNOWN`. Used by the classification system and authority rules.
- **`Component`** — A single electrical component with its ID, type, description, electrical values, connections, associated text labels, cross-reference links, and arbitrary attributes.
- **`ComponentValue`** — An electrical parameter (e.g., `voltage_rating = 138kV`, `current_rating = 2000A`, `impedance = 14.95%Z`).
- **`Connection`** — A wiring connection between two component terminals, with cable spec, wire label, and signal type.
- **`TextLabel`** — A text string extracted from a drawing with X/Y position and category (component, value, cable, reference, terminal, signal, note, title).
- **`TitleBlock`** — Drawing title block metadata (number, revision, project, drawn by, drawing_type, etc.).
- **`DrawingData`** — Complete extraction result for one drawing: all components, connections, labels, cross-references, cables, terminal blocks, voltage levels, notes, drawing_type, and raw text. `to_dict()` / `from_dict()` serialize all fields including `raw_text` for full round-trip fidelity.
- **`Mismatch`** — A detected inconsistency: severity (CRITICAL/WARNING/INFO), affected component, parameter, which drawings are involved, the conflicting values, a recommendation, and `resolution_options` (authority-based guidance on which value to use).
- **`AlertSeverity`** — CRITICAL (safety-relevant value mismatch), WARNING (naming/reference issue), INFO (minor discrepancy).

### `extractors/pdf_extractor.py` — PDF Component Extraction

The heaviest module. Uses `pdfplumber` to extract every word from each PDF page with exact X/Y coordinates, then applies pattern matching to identify:

- **Device function components** using IEEE/ANSI patterns: `52-L1`, `50-AUX1`, `87-BH`, `89-M1`, `86MP`, `21-CQ`, etc.
- **Protective relay models**: `SEL-451`, `SEL-487`, `SEL-2505`, `GE-L90`, etc.
- **Instrument transformers**: `CT-B1M`, `VT-B1`, `CCVT-HV`, `PT-xxx`
- **Fuses, switches, panels**: `FU4`, `SW-1`, `DC PANEL DC2`
- **DPAC controllers**: `DPAC-1`, `DPAC-2` (normalized from `DPAC1`/`DPAC-1` to canonical `DPAC-N` form)
- **Communication cables**: `SEL-C805`, `SEL-C662` (SEL communication cables)
- **Output/input contacts**: `OUT01`, `IN001` (relay I/O points)
- **Circuit identifiers**: `CIRCUIT 1`, `CIRCUIT 2`
- **Electrical values**: voltages (`138kV`, `125V DC`), currents (`2000A`, `63kA`), impedances (`14.95%Z`), MVA ratings (`105/140/175MVA`), CT/PT ratios (`700/1200:1:1`)
- **Cable specs**: `2/C#10`, `12/C #12SH`, `CAT5E`, `MM FIBER`
- **Terminal blocks**: `TB6-71`, `TS3-8`, `TS2-4`
- **Cross-references**: `EC-301.1`, `EC-307.0`, etc.
- **Signal keywords**: `TRIP`, `CLOSE`, `SCADA`, `BF TRIP`, `LOCKOUT`, `ALARM`, `INTERLOCK`

Values are associated with components using **spatial proximity** — the system finds the nearest component label to each value label using Euclidean distance on the page coordinates. To avoid false matches (e.g., "kV" inside "kVA"), value-to-word position lookups use numeric-prefix matching rather than substring search. Signal keyword categorization uses regex word boundaries (`\b`) so that words like "CASCADE" are not falsely matched against "SCADA". The impedance pattern matches case-insensitively (`ohm`, `OHM`, `Ohm`). Component IDs are validated during deduplication — empty strings, whitespace-only, and pure-numeric IDs are filtered out while preserving legitimate short IDs like `CT` and `VT`. All pdfplumber word dictionary access uses `.get()` with defaults to prevent `KeyError` on malformed PDFs. Connections between components are inferred from labels on the same horizontal row (same Y coordinate = same circuit path in a schematic).

### `extractors/dxf_extractor.py` — DXF/DWG Component Extraction

Handles structured CAD data via `ezdxf`. DXF files contain richer data than PDFs:

- **Block references** (INSERT entities) = component symbols. The extractor uses an **11-strategy priority pipeline** to extract components from block references, designed to handle the full range of AutoCAD Electrical block naming and attribute conventions:
  1. **Title block skip** — Blocks with names containing "TITLEBLOCK" are processed for metadata only, not as components.
  2. **WD_M metadata skip** — AutoCAD Electrical wire diagram metadata blocks (WD_M) are skipped.
  3. **DEVICE attribute** — Blocks with a `DEVICE`, `RELAY_MODEL`, or `RELAY_TYPE` attribute are recognized as relay/device components (e.g., `HDV1`).
  4. **FUSE_NUM attribute** — Blocks with `FUSE_NUM` or `FUSE_ID` attributes are extracted as fuses with their ratings and terminal assignments.
  5. **OUTPUT-# attribute** — Blocks with `OUTPUT-#` or `OUTPUT_NUM` attributes are extracted as relay output contacts.
  6. **INPUT-#N attribute** — Blocks with attributes matching `INPUT-#` prefix are extracted as relay input contacts.
  7. **PWR_SUP attribute** — Blocks with `PWR_SUP`, `POWER_SUPPLY`, or `PWR` attributes are extracted as power supply components.
  8. **Block name keyword matching** — Block names are matched against known electrical keywords (`FUSE`, `POWER SUPPLY`, `GROUND`, `GND`, `SERIAL`, `ETHERNET`, `FIBER`) to identify components by their symbol type.
  9. **PNL_TERM / RELAY_TERM blocks** — Terminal blocks on panels and relays are extracted with their terminal numbering.
  10. **TAG/NAME fallback** — If no structured attribute matched, the block's TAG or NAME attribute is used as a component ID, with IEEE pattern matching applied.
  11. **Unrecognized block logging** — Blocks that don't match any strategy are logged as extraction warnings for manual review.

  The extractor tracks **relay device context** (`relay_device_id`) so sub-components (outputs, inputs, fuses, terminals) are associated with their parent relay device (e.g., output `OUT01` under relay `HDV1`). Component values are **deduplicated** — when multiple block instances represent the same physical component (e.g., two fuse terminals for `FU12`), duplicate values are merged rather than repeated. Attribute text values from non-titleblock blocks are also added to the raw text stream for regex pattern matching, ensuring fuses (`FU4`), switches (`SW-1`), panels (`DC PANEL`), NGRs, lockout relays (`86XX`), and `BREAKER XX` format breakers are all detected.
- **TEXT / MTEXT entities** = all text with exact coordinates. MTEXT entities are split line-by-line before categorization to prevent regex patterns (e.g., `PANEL\s+[A-Z0-9]+`) from matching across unrelated lines. Text parsing recognizes **DPAC controllers** (normalized to canonical `DPAC-N` form), **SEL-C communication cables**, **output/input contacts** (`OUT01`, `IN001`), **circuit identifiers**, and **DC voltage levels** (e.g., `125V DC`).
- **LINE / LWPOLYLINE entities** = wires. The system traces wire endpoints to nearby component positions to build the actual connection graph.
- **DIMENSION entities** = annotated values.
- **Layer organization** = components grouped by drawing layer.

For DWG files (AutoCAD native binary format), `ezdxf` cannot read them directly — they must be converted to DXF first. The extractor auto-converts via ODA File Converter using `xvfb-run` for headless operation. Temporary conversion directories are cleaned up with `shutil.rmtree` after extraction. If ODA is not installed, the extractor falls back to the corresponding PDF in `P&C_PDF/`.

The cross-reference pattern `E[CPS]-XXX.X` matches EC- (electrical control), EP- (panel), and ES- (schedule) drawing references.

### `extractors/xlsx_extractor.py` — Schedule & BOM Extraction

Reads Excel files in the `SCHEDULES/` directory via `openpyxl` (XLSX) and `xlrd` (legacy XLS):

- **Drawing index** (`NRE P&C DRAWING INDEX.xlsx`) — maps drawing numbers to titles and revisions.
- **Junction box BOMs** (`CT-B1M JUNCTION BOX BOM.xlsx`, etc.) — full bill-of-materials extraction.
- **Panel elevation schedules** — physical panel layouts with exhaustive component detection.
- **Cable/fiber schedules** — cable run details.
- **Abbreviation tables** — project-specific naming conventions.

**Exhaustive Regex Extraction (PDF/DXF Parity):** All 50+ pattern groups from the PDF extractor are applied to every XLSX row — IEEE/ANSI device numbers, relay models, instrument transformers, extended equipment (DPAC, DFR, FPP, RTAC, CLOCK, PDC, CISCO, etc.), fuses (FU, BAF), terminal blocks (TB, TS), cable specs, voltage/current/impedance values, cross-references, and terminal block connections. This ensures components mentioned in any XLSX file are detected at the same level as PDF and DXF sources.

**Full BOM Line-Item Extraction:** When a file's name contains "BOM", every line item in every sheet is extracted as a tracked component — in addition to the regex-based detection. The extractor handles three BOM sheet types:

- **MATERIAL sheets** (columns: ITEM#, QTY, CATALOG#, MATERIAL) — each row becomes a component with type inferred from the description (terminal block, fuse, enclosure, ground bar, cable, consumable, etc.), catalog number as ID, and all cell values stored as attributes.
- **NAMEPLATE sheets** (columns: ITEM#, SIZE, LETTER SIZE, QTY, 1st LINE, 2nd LINE) — sub-component designations (TB1, FU1, etc.) are extracted with nameplate metadata. Multi-sheet BOMs (e.g., YARD LIGHT with LT1-LT9 sheets) get sheet-specific prefixes to distinguish components across junction boxes.
- **DEVICE LIST sheets** (columns: ITEM#, DEVICE#, QTY, CATALOG#, MATERIAL) — IRIG-B style lists where device designations (T-CONN, RESISTOR, COAX) serve as component IDs, with deduplication for repeated device names.

Two new `ComponentType` values support BOM items that don't map to existing electrical types: `BOM_ITEM` (generic parts like enclosures, DIN rails, connectors) and `CONSUMABLE` (bulk/lot items like wire, crimp terminals, hardware).

**Extraction Results:** 133 components extracted across 9 BOM files (121 from BOM line items + 12 from regex patterns), versus only 4 components with the previous regex-only approach.

### `db.py` — Component Registry Database

SQLite database with 9 tables and indexes. The database is opened with `PRAGMA journal_mode=WAL` for safe concurrent access and `PRAGMA foreign_keys = ON` for referential integrity. Key operations:

- **`store_drawing()`** — Upserts a complete drawing extraction inside a single atomic transaction (commit/rollback). Before overwriting, it snapshots the old data and detects changes (new/removed/modified components), logging each to the change log. If any step fails, the entire operation rolls back to prevent partial data loss.
- **`get_component_across_drawings()`** — Returns every instance of a component across all drawings, with values and attributes from each.
- **`get_shared_components()`** — Returns components appearing in 2+ drawings (265 of 494 in the NRE set). Uses `||` as the `GROUP_CONCAT` delimiter instead of comma to avoid splitting drawing IDs that could theoretically contain commas.
- **`has_drawing_changed()`** — Compares file SHA-256 hash to skip unchanged files on incremental scans.
- **`store_mismatch()` / `get_active_mismatches()`** — Mismatch alert persistence and retrieval, sorted by severity.
- **`log_change()`** — Audit trail for every detected modification.
- **`close()`** — Idempotent; safe to call multiple times without raising exceptions.

### `sync_engine.py` — Core Orchestrator

The central engine that ties everything together:

- **`scan_directory()`** — Walks a directory tree (with `followlinks=False` to prevent symlink cycles), extracts each PDF/DXF/XLSX file, and stores results. Skips backup directories, hidden files (`.` prefix), temp files (`~` prefix), and unchanged files. Returns scan statistics.
- **`scan_single_file_with_results()`** — Scans a single file and returns the same results dict format as `scan_directory()`, enabling single-file pipeline runs with full classification, enrichment, and reporting.
- **`check_mismatches()`** — Delegates to `MismatchDetector` to run all 12 checks.
- **`get_sync_report(drawing_id)`** — For a single drawing, returns all its components, which other drawings share them, active mismatches involving this drawing, and actionable recommendations.
- **`get_component_sync_status(component_id)`** — For a single component, compares its values across all drawings and reports whether it's consistent.
- **`propagate_update(source_drawing, component_id)`** — When a component is updated in one drawing, finds all other drawings containing the same component and reports exactly which values differ and need updating.
- **`get_dependency_graph()`** — Builds a graph of all drawing-to-drawing relationships via cross-references and shared components. Cross-references are stored with their full `NRE-` prefixed drawing ID for consistent lookup.

### `drawing_classifier.py` — Drawing Type Detection & Classification

Classifies drawings into types (`ONE_LINE`, `AC_SCHEMATIC`, `DC_SCHEMATIC`, etc.) using three strategies in priority order:

1. **Drawing Index XLSX lookup** (confidence 1.0) — Loads the project drawing index spreadsheet and maps drawing numbers to types using column header detection (`DWG`/`DRAWING` + `TYPE` columns). Supports `NRE-` prefix normalization.
2. **Title block DWGTYPE attribute** (confidence 0.9) — Reads the `drawing_type` field from the title block and normalizes it via a mapping table (e.g., `"DC ELEMENTARY"` -> `DC_SCHEMATIC`, `"WIRING DIAGRAM"` -> `PANEL_WIRING`).
3. **Drawing number series inference** (confidence 0.7) — Parses the numeric portion of the drawing ID (from `NRE-EC-XXX.Y` format) and maps it to a type using known ranges (e.g., 100-104 = `ONE_LINE`, 300-380 = `DC_SCHEMATIC`).

The `classify_all()` method batch-classifies all drawings in the database and updates the `drawing_type` column. All classification decisions are recorded to the audit trail when available.

### `authority.py` — Source-of-Truth Hierarchy & Authority Rules

Defines which drawing type is authoritative for each parameter using configurable rules:

- **`AuthorityRule`** dataclass — Specifies a parameter name, applicable component types (or `["*"]` for all), and a priority-ordered list of drawing types.
- **`AuthorityConfig`** class — Manages 8 default rules covering `voltage_rating`, `current_rating`, `power_rating`, `impedance`, `ratio`, `cable_specification`, `terminal_assignment`, and `relay_settings`. Can be loaded from or exported to JSON.
- **`get_authoritative_drawing()`** — Given a parameter, component type, and a dict of candidate drawings with their types, returns the drawing with the highest authority.
- **`get_authority_basis()`** — Returns a human-readable explanation of why a drawing type is authoritative for a parameter.

### `propagation_engine.py` — Authority-Based Attribute Propagation

Plans and applies attribute propagation across drawings using authority rules:

- **`PropagationAction`** dataclass — Represents a single proposed or applied change with `action_id`, source/target drawings, component, parameter, old/new values, authority basis, and status (`PROPOSED`, `APPLIED`, `FAILED`, `DRY_RUN`).
- **`plan_propagation(component_id)`** — Generates proposed changes for a single component by comparing its values across all drawings, determining the authoritative source, and creating actions for each target that differs.
- **`plan_all_propagations()`** — Plans propagation for all shared components (those appearing in 2+ drawings).
- **`apply_propagation(actions)`** — Applies actions to the database, updating component values and logging each action to the `propagation_log` table. Supports `dry_run` mode.

All applied propagations are recorded to the change log and decision audit trail.

### `mismatch_detector.py` — Automated Mismatch Detection

Runs 12 independent checks against the database and returns `Mismatch` objects with severity, affected drawings, conflicting values, and recommendations. Each check is a separate method that can be called individually. The first 8 checks cover value mismatches, component type consistency, cross-reference integrity, cable spec consistency, terminal block conflicts, voltage level consistency, relay assignment consistency, and orphan components. The 4 protection logic checks (9-12) verify relay-breaker trip paths, lockout relay completeness, CT-relay associations, and DC supply connections.

Before each full run, all previously detected mismatches are marked as resolved. Each active mismatch is then re-inserted with `INSERT OR REPLACE`, so issues that have been fixed since the last run are automatically cleared from the active alerts — no stale mismatches accumulate. Mismatch IDs are generated as 24-character SHA-256 hashes (96-bit entropy) of the component, parameter, and check type to prevent collisions. Value comparisons filter out `None`/null entries to avoid false positives when one drawing has a value and another doesn't.

Value mismatches and voltage level mismatches are enriched with authority-based resolution options. For each conflicting value, the system identifies which drawing holds the authoritative value based on the authority rules and drawing type classification.

The orphan component check (check #8) finds components appearing in only one drawing where other drawings cross-reference that drawing, flagging them as potentially missing from referencing drawings.

### `cable_export.py` — Cable List Extraction & Excel Export

Exports cable/connection data from the database to a formatted XLSX workbook:

- **`CableListExporter`** class — Queries the `connections` table, enriches with component signal attributes, and writes a professional workbook with styled headers, alternating row colors, auto-fitted columns, and frozen header panes.
- **Sheet 1: Cable List** — Every cable with cable number, from/to component and terminal, cable type/spec, wire label, signal type, source drawing, and notes.
- **Sheet 2: Cable Schedule Summary** — Aggregated by cable spec with counts, associated drawings, and from/to components.
- **Sheet 3: By Drawing** — Same columns as Cable List, grouped by source drawing with bold separator rows.
- Supports filtering by a single drawing via `filter_drawing` parameter.

### `audit.py` — Decision Audit Trail

Records the reasoning behind every automated decision for compliance and traceability:

- **`DecisionRecord`** dataclass — Captures `decision_id`, timestamp, `decision_type` (CLASSIFICATION, AUTHORITY, PROPAGATION, MISMATCH_DETECTION), component/drawing context, input data, reasoning, confidence score (0.0-1.0), outcome, and alternatives considered.
- **`AuditTrail`** class — Manages the `decisions` database table with `record_decision()`, `get_decisions()` (with optional filters), `generate_decision_tree()` (nested report for a component), and `export_audit_report()` (formal compliance document with 5 sections).
- **`get_statistics()`** — Returns aggregate stats: total decisions, counts by type, average confidence, date range.
- The audit trail is integrated into the classifier (records CLASSIFICATION decisions) and propagation engine (records PROPAGATION decisions).

### `watcher.py` — Real-Time File Monitoring

Uses the `watchdog` library to monitor directories for file changes. When a PDF, DXF, DWG, or XLSX file is modified or created:

1. Debounces the event (waits 2 seconds after last change — editors often trigger multiple save events).
2. Re-extracts the changed file via the sync engine.
3. Runs mismatch detection on affected components. Relevant mismatches are filtered using proper list membership (`drawing_id in m.drawings_involved`), not string substring matching, to prevent false matches between similarly-named drawings.
4. Computes propagation — which other drawings are now out of sync.
5. Prints alerts to console and calls an optional callback function.

### `reports.py` — Report Generation

Generates human-readable text reports:

- **Scan report** — Drawing-by-drawing table of components, connections, cross-refs, labels, cables extracted. Plus database statistics. Includes three enhanced sections: **Component Inventory** (per-drawing table of every component with ID, type, values, and attributes), **Extraction Completeness Notes** (flags `[MISSING DATA]` for components without values or connections), **Extraction Warnings** (unrecognized blocks and other warnings from the extraction process), and **Voltage Levels** (per-drawing voltage level summary).
- **Mismatch alert report** — Grouped by severity (CRITICAL / WARNING / INFO). Each mismatch shows the component, parameter, conflicting values per drawing, and recommended action. Mismatches with authority-based resolution options include guidance on which value to use.
- **Component report** — Deep-dive on a single component across all drawings.
- **Shared components report** — Table of all 265 components appearing in 2+ drawings, with drawing counts and lists.
- **Dependency graph report** — For each drawing, shows outgoing references, incoming references, and shared components with other drawings.
- **Change log report** — Chronological audit trail of all detected changes.
- **Propagation report** — Lists all proposed or applied propagation actions with source/target drawings, parameters, old/new values, and authority basis.
- **Decision tree report** — Structured view of all decisions made for a component (classification, authority, propagation, mismatches).
- **Audit report** — Formal compliance document with recent decisions, filterable by decision type.

### `cli.py` — Command-Line Interface

14 commands available via `python -m drawing_sync.cli`. All commands support `-i` (input directory) and `-o` (output file/directory):

| Command | Purpose |
|---|---|
| `scan -i <file-or-dir>` | Scan drawings and populate database |
| `check` | Run mismatch detection (12 checks), print alerts |
| `status [-c COMP]` | Show shared components or single component deep-dive |
| `propagate <drawing> <component>` | Show update propagation impact (legacy mode) |
| `propagate --plan [--all]` | Plan propagation actions using authority rules |
| `propagate --apply [--all] [--force]` | Apply propagation actions to database |
| `propagate --log` | Show propagation log |
| `classify [--index FILE]` | Classify all drawings by type |
| `authority show` | Display authority rules hierarchy |
| `authority export -o <file>` | Export authority rules to JSON |
| `cable-list -o <file> [-d DWG]` | Export cable list to formatted Excel |
| `audit [--show -c COMP] [--export] [--log]` | Decision audit trail operations |
| `graph` | Print dependency graph |
| `log [-d DRAWING]` | Show change history |
| `watch -i <dir>` | Live file monitoring with auto-detection |
| `export [-c COMP] [-d DWG]` | Export data as JSON |
| `report-all` | Generate all reports to a directory |
| `pipeline -i <file-or-dir> -o <dir>` | Full pipeline: scan + detect + report with structured output |

---

## Python Package Dependencies

| Package | Version | Purpose |
|---|---|---|
| **pdfplumber** | >= 0.11.0 | Primary PDF text extraction engine. Extracts every word from PDF pages with exact X/Y coordinates, and can extract tables. Built on top of `pdfminer.six`. This is what makes PDF component extraction possible — it provides the spatial data needed to associate values with nearby components. |
| **pdfminer.six** | >= 20221105 | Low-level PDF parsing library that `pdfplumber` builds on. Handles PDF page rendering, font decoding, and text object extraction. |
| **pypdf** | >= 3.0 | PDF reading/writing library. Used as a fallback for basic PDF metadata extraction and for potential future PDF modification (updating values in PDFs). |
| **ezdxf** | >= 1.3.0 | DXF file reader/writer. Reads AutoCAD DXF files to extract block references (component symbols), text entities, polylines (wires), dimensions, and layer data. Cannot read DWG directly — DWG must be converted to DXF first. |
| **openpyxl** | >= 3.1.0 | Excel XLSX reader. Extracts data from equipment schedule spreadsheets, drawing indices, junction box BOMs, panel elevation schedules, and cable/fiber schedules. |
| **watchdog** | >= 3.0.0 | Cross-platform file system event monitoring. Watches directories for file creation/modification events and triggers callbacks. Powers the live monitoring feature that auto-detects drawing changes. |
| **rich** | >= 13.0.0 | Terminal formatting library for colored, styled console output. Used for enhanced CLI display of alerts and reports. |
| **click** | >= 8.1.0 | Command-line interface toolkit. Used internally by some components for argument parsing support. |
| **tabulate** | >= 0.9.0 | Table formatting library. Generates aligned text tables in reports (drawing summaries, component lists, statistics). |

### System Dependencies (Optional)

| Tool | Purpose |
|---|---|
| **ODA File Converter** | Converts DWG (AutoCAD binary) to DXF (open text format) so `ezdxf` can read it. Run `./install_oda.sh` to download and install automatically. Without this, DWG scanning falls back to reading the corresponding PDF. |
| **xvfb** | Virtual framebuffer X server. Allows ODA File Converter (a Qt GUI app) to run headless via `xvfb-run -a` without popping up a window. Install with `sudo apt install xvfb`. |
| **SQLite3** | Included with Python. The component registry database engine. Uses WAL journal mode and enforced foreign keys. Can also be accessed directly via the `sqlite3` CLI for ad-hoc queries. |

---

## Data Flow Diagram

```
 [PDF Drawings]     [DWG/DXF Drawings]     [XLSX Schedules]
       |                    |                      |
       v                    v                      v
  PDFExtractor        DXFExtractor          XLSXExtractor
       |                    |                      |
       +--------+-----------+----------+-----------+
                |                      |
                v                      v
          DrawingData             DrawingData
          (components,            (components,
           connections,            BOM data,
           labels, ...)            drawing index)
                |                      |
                +----------+-----------+
                           |
                           v
                   ComponentDatabase
                      (SQLite)
                           |
                           v
                  DrawingClassifier
              (type detection & classification)
                           |
              +------------+------------+-----------+
              |            |            |           |
              v            v            v           v
        MismatchDetector  SyncEngine  AuthorityConfig  AuditTrail
        (12 checks)        |         (8 rules)     (decisions DB)
              |            |            |           |
              v            v            v           v
         [RED FLAG      [Propagation  [Source-of  [Decision
          Alerts]        Engine]       Truth]      Trees]
              |            |            |           |
              +-----+------+------+----+-----+-----+
                    |             |           |
                    v             v           v
               CLI Output    Report Files  Cable XLSX
              (terminal)    (reports/*.txt) (cable_list.xlsx)
```

---

## From Raw Library Data to Structured Objects

This section shows exactly what each Python library gives us — the raw, unstructured data — and how drawing-sync's object-oriented model transforms it into structured, queryable metadata at every step. Nothing is hidden. Every transformation is shown.

### What the Libraries Actually Return

The three extractor libraries each return fundamentally different raw data. Drawing-sync uses all three because no single library can read PDFs, CAD files, and spreadsheets. But all three produce the same output: a `DrawingData` object.

#### pdfplumber: Raw Word Dictionaries with Pixel Coordinates

`pdfplumber.open(pdf_path)` returns a PDF document object. For each page, `page.extract_words(keep_blank_chars=True, x_tolerance=3, y_tolerance=3)` returns a **flat list of dictionaries** — one per word — with exact pixel positions on the page:

```python
# What pdfplumber actually returns for a page of NRE-EC-301.1.pdf:
[
    {"text": "52-L1",   "x0": 245.3, "top": 412.7, "x1": 282.1, "bottom": 423.5},
    {"text": "138kV",   "x0": 248.9, "top": 425.1, "x1": 279.4, "bottom": 435.2},
    {"text": "TRIP",    "x0": 310.2, "top": 413.0, "x1": 335.8, "bottom": 423.3},
    {"text": "86MP",    "x0": 450.7, "top": 412.5, "x1": 483.2, "bottom": 423.1},
    {"text": "2/C#10",  "x0": 375.4, "top": 413.2, "x1": 408.7, "bottom": 423.6},
    {"text": "TB6-71",  "x0": 520.1, "top": 460.8, "x1": 558.4, "bottom": 471.0},
    {"text": "EC-307.0","x0": 600.5, "top": 412.9, "x1": 648.2, "bottom": 423.4},
    # ... hundreds more words per page, thousands per drawing
]
```

And `page.extract_text()` returns the full page as a single flat string (no coordinates):

```
"52-L1 138kV TRIP 86MP 2/C#10 TB6-71 EC-307.0 ..."
```

This is **completely unstructured**. pdfplumber has no concept of "component," "connection," or "electrical value." It just sees text at pixel positions. A breaker label, a voltage rating, a signal keyword, and a cable spec are all just words with x/y coordinates. Drawing-sync's extractors must infer all structure from spatial relationships and pattern matching.

#### ezdxf: Typed Entity Objects with Named Attributes

`ezdxf.readfile(dxf_path)` returns a DXF document with typed entity objects in modelspace and paper space layouts. Unlike PDF words, DXF entities have **structure built in** — block references carry named attributes, text entities have exact insertion points, and line entities define wire paths:

```python
# What ezdxf actually returns — INSERT entity (a block reference = component symbol):
entity.dxftype()           # "INSERT"
entity.dxf.name            # "FUSE_HORIZONTAL"  (the block definition name)
entity.dxf.insert          # Vec3(145.2, 203.8, 0.0)  (insertion point in drawing units)
# The attribs collection contains named attribute values:
for attrib in entity.attribs:
    attrib.dxf.tag         # "FUSE_NUM"    → attribute name
    attrib.dxf.value       # "FU12"        → attribute value
# Full attribute set for this block:
#   FUSE_NUM = "FU12",  FUSE_SIZE = "10A",  LEFT_TERM = "1",  RIGHT_TERM = "2"

# TEXT entity:
entity.dxftype()           # "TEXT"
entity.dxf.text            # "52-L1"  (the text content)
entity.dxf.insert          # Vec3(245.3, 412.7, 0.0)

# MTEXT entity (multiline text — must be split before pattern matching):
entity.dxftype()           # "MTEXT"
entity.plain_text()        # "DC PANEL\nDC2\n125V DC"  (contains newlines)
entity.dxf.insert          # Vec3(100.5, 300.2, 0.0)

# LINE entity (a single wire segment):
entity.dxftype()           # "LINE"
entity.dxf.start           # Vec3(145.2, 203.8, 0.0)  (wire start point)
entity.dxf.end             # Vec3(245.3, 203.8, 0.0)  (wire end point)

# LWPOLYLINE entity (a multi-segment wire path):
entity.dxftype()           # "LWPOLYLINE"
list(entity.get_points())  # [(145.2, 203.8), (200.0, 203.8), (245.3, 203.8)]
```

DXF data is fundamentally richer than PDF. Block attributes like `FUSE_NUM = "FU12"` directly identify components without spatial inference. Wire polylines provide actual connection paths between components. Title block attributes provide drawing metadata directly. The 11-strategy block extraction pipeline exists to handle the full range of AutoCAD Electrical attribute conventions.

#### openpyxl: Cell Values in Rows and Columns

`openpyxl.load_workbook(xlsx_path, data_only=True)` returns a Workbook with sheets containing rows of Cell objects. Formulas are resolved to their computed values:

```python
# What openpyxl actually returns for a BOM sheet (CT-B1M JUNCTION BOX BOM.xlsx):
# Header row: ["ITEM#", "QTY", "CATALOG#", "MATERIAL"]
# Data rows:
#   [1,  2,  "WAGO-281-611",    "TERMINAL BLOCK, DIN RAIL, 600VAC, 30A"]
#   [2,  4,  "BUSS-BAF-15",     "FUSE, FAST ACTING, 15A, 600VAC"]
#   [3,  1,  "HOFFMAN-A16R126", "ENCLOSURE, NEMA 3R, STEEL"]

# What openpyxl returns for a drawing index sheet (NRE P&C DRAWING INDEX.xlsx):
# Header row: ["DWG NO", "DWG TYPE", "DRAWING TITLE", "REV", "DATE"]
# Data rows:
#   ["EC-001.0",  "DRAWING INDEX",  "NRE P&C DRAWING INDEX",      "C",  "2025-10-15"]
#   ["EC-100.0",  "ONE LINE",       "138KV ONE-LINE DIAGRAM",     "B",  "2025-09-20"]
#   ["EC-301.1",  "DC SCHEMATIC",   "138KV LINE L1 DC SCHEMATIC", "C",  "2025-09-20"]
```

openpyxl returns raw cell values — strings, numbers, dates. It has no knowledge of electrical components. Drawing-sync applies both structured BOM parsing (using column headers to identify catalog numbers, quantities, descriptions) and exhaustive regex pattern matching (the same 50+ patterns used for PDF and DXF) to extract components from every row.

### How Drawing-Sync's OOP Model Transforms Raw Data

Every extractor transforms raw library output into the same object hierarchy. The transformation follows one pattern: **raw data → regex pattern matching + spatial/structural analysis → typed dataclass objects → DrawingData container**.

#### The Object Hierarchy

Every file, regardless of format, produces one `DrawingData` object containing typed sub-objects:

```
DrawingData                              ← One per file (the container)
├── drawing_id: str                      ← "NRE-EC-301.1" (from filename)
├── file_path: str                       ← Absolute path to source file
├── file_type: str                       ← "pdf", "dxf", "dwg", "xlsx"
├── drawing_type: str                    ← "" initially, set by DrawingClassifier
├── title_block: TitleBlock              ← Drawing header metadata
│   ├── drawing_number: str              ← "NRE-EC-301.1"
│   ├── revision: str                    ← "C"
│   ├── project_name: str                ← "NOMADIC RED EGRET 138/34.5kV"
│   ├── drawn_by: str                    ← "JKS"
│   ├── company: str                     ← "MORTENSON"
│   └── drawing_type: str                ← "DC SCHEMATIC" (from title block)
├── components: Dict[str, Component]     ← Keyed by component_id
│   └── Component                        ← One per identified electrical device
│       ├── component_id: str            ← "52-L1", "SEL-451", "FU12"
│       ├── component_type: ComponentType ← Enum: BREAKER, RELAY, FUSE, CT, ...
│       ├── description: str             ← "AC circuit breaker 52-L1"
│       ├── values: List[ComponentValue]  ← Electrical ratings
│       │   └── ComponentValue
│       │       ├── parameter: str       ← "voltage_rating", "current_rating"
│       │       ├── value: str           ← "138kV", "2000A", "14.95%Z"
│       │       ├── unit: str            ← "kV", "A", "ohm"
│       │       └── numeric_value: float ← 138.0, 2000.0, 14.95
│       ├── connections: List[Connection] ← Wiring to other components
│       │   └── Connection
│       │       ├── from_component: str  ← "52-L1"
│       │       ├── from_terminal: str   ← "A01"
│       │       ├── to_component: str    ← "86MP"
│       │       ├── to_terminal: str     ← "52T"
│       │       ├── cable_spec: str      ← "2/C#10"
│       │       ├── wire_label: str      ← "W001"
│       │       └── signal_type: str     ← "TRIP"
│       ├── labels: List[TextLabel]      ← Source text positions
│       ├── drawing_refs: List[str]      ← ["EC-307.0", "EC-308.0"]
│       └── attributes: Dict             ← {"signal": "TRIP", "in_table": True}
├── connections: List[Connection]        ← All connections in this drawing
├── cross_references: List[str]          ← ["EC-301.0", "EC-307.0", "EC-308.0"]
├── all_labels: List[TextLabel]          ← Every extracted text with position
│   └── TextLabel
│       ├── text: str                    ← "52-L1", "138kV", "TRIP"
│       ├── x: float                     ← 245.3 (pixel or drawing units)
│       ├── y: float                     ← 412.7
│       └── category: str                ← "component", "value", "signal", "cable"
├── cable_schedule: List[str]            ← ["2/C#10", "12/C#12SH", "CAT5E"]
├── terminal_blocks: Dict[str, List]     ← {"TB6": [71, 72, 73], "TS3": [8, 9]}
├── voltage_levels: List[str]            ← ["138kV", "34.5kV", "125V DC"]
├── raw_text: str                        ← All text concatenated for pattern matching
├── notes: List[str]                     ← ["NOTE 1: ALL WIRING 600V RATED"]
└── index_metadata: Dict                 ← {} initially, enriched from drawing index
```

This hierarchy is defined in `drawing_sync/models.py` using Python `@dataclass` decorators. Every object has `to_dict()` and `from_dict()` methods for JSON serialization, enabling round-trip storage in SQLite and export to JSON files.

#### Transformation Example: PDF Raw Words → OOP Objects

Here is the exact sequence that turns flat pdfplumber word dictionaries into a structured `Component`:

```
Raw pdfplumber dict:
  {"text": "52-L1", "x0": 245.3, "top": 412.7, "x1": 282.1, "bottom": 423.5}

Step 1 — Label categorization (_categorize_label):
  Regex DEVICE_PATTERNS[BREAKER] = r'\b(52-[A-Z0-9]+)\b' matches "52-L1"
  Result:
    → TextLabel(text="52-L1", x=245.3, y=412.7, category="component")

Step 2 — Component creation (_extract_device_components):
  Same regex applied to full page text → captures "52-L1"
  Result:
    → Component(
          component_id="52-L1",
          component_type=ComponentType.BREAKER,    # IEEE/ANSI 52 = breaker
          description="AC circuit breaker 52-L1"
      )

Step 3 — Value association (_associate_value_with_component):
  Another raw dict: {"text": "138kV", "x0": 248.9, "top": 425.1, ...}
  VOLTAGE_PATTERN matches "138kV" → it's an electrical value
  Find nearest component by Euclidean distance:
    Distance to "52-L1" at (245.3, 412.7):
      sqrt((248.9 − 245.3)² + (425.1 − 412.7)²) = 12.9 pixels  ← nearest
    Distance to "86MP" at (450.7, 412.5):
      sqrt((248.9 − 450.7)² + (425.1 − 412.5)²) = 202.2 pixels
  "52-L1" is closest → value is associated with it
  Result:
    → ComponentValue(parameter="voltage_rating", value="138kV",
                     unit="kV", numeric_value=138.0)
      appended to component.values

Step 4 — Connection inference (_build_connection_graph):
  "86MP" at (450.7, 412.5) shares the same Y-row as "52-L1" at (245.3, 412.7)
  Vertical distance: |412.7 − 412.5| = 0.2 pixels (within 15-unit threshold)
  Horizontal distance: |450.7 − 245.3| = 205.4 pixels (within 300-unit threshold)
  Same row = same circuit path in a schematic
  Result:
    → Connection(from_component="52-L1", to_component="86MP",
                 signal_type="TRIP")

Step 5 — Signal keyword association:
  "TRIP" at (310.2, 413.0) is within 30 vertical / 200 horizontal units of "52-L1"
  Result:
    → component.attributes["signal"] = "TRIP"

Final Component object:
  Component(
      component_id = "52-L1",
      component_type = ComponentType.BREAKER,
      description = "AC circuit breaker 52-L1",
      values = [ComponentValue("voltage_rating", "138kV", "kV", 138.0)],
      connections = [Connection("52-L1", "", "86MP", "", "", "", "TRIP")],
      labels = [TextLabel("52-L1", 245.3, 412.7, "component")],
      drawing_refs = ["EC-307.0", "EC-308.0"],
      attributes = {"signal": "TRIP"}
  )
```

A flat PDF word dictionary has become a typed `Component` with electrical values, connections, and signal attributes — all inferred from spatial proximity on the page.

#### Transformation Example: DXF Block Reference → OOP Objects

Here is how an ezdxf INSERT entity becomes a structured `Component` — no spatial inference needed:

```
Raw ezdxf INSERT entity:
  entity.dxf.name = "FUSE_HORIZONTAL"
  entity.dxf.insert = Vec3(145.2, 203.8, 0.0)
  entity.attribs:
    Attrib(tag="FUSE_NUM",   value="FU12")
    Attrib(tag="FUSE_SIZE",  value="10A")
    Attrib(tag="LEFT_TERM",  value="1")
    Attrib(tag="RIGHT_TERM", value="2")

11-Strategy Pipeline — Strategy 4 (FUSE_NUM attribute) matches:
  attrs["FUSE_NUM"] = "FU12"  → component_id
  attrs["FUSE_SIZE"] = "10A"  → current rating value

Final Component object:
  Component(
      component_id = "FU12",
      component_type = ComponentType.FUSE,
      description = "Fuse FU12 (10A)",
      values = [ComponentValue("current_rating", "10A", "A", 10.0)],
      connections = [],
      labels = [],
      drawing_refs = [],
      attributes = {
          "fuse_size": "10A",
          "left_terminal": "1",
          "right_terminal": "2",
          "block_name": "FUSE_HORIZONTAL"
      }
  )
```

The block's named attributes (`FUSE_NUM`, `FUSE_SIZE`, `LEFT_TERM`, `RIGHT_TERM`) directly provide component identity, rating, and terminal assignments. No distance calculations, no spatial inference — the CAD file's structure does the work. This is why DXF extraction produces richer data than PDF extraction for the same drawing.

#### Transformation Example: XLSX Row → OOP Objects

Here is how an openpyxl BOM row becomes a structured `Component` — through both BOM parsing and regex matching:

```
Raw openpyxl row (MATERIAL sheet):
  [Cell(1), Cell(2), Cell("WAGO-281-611"), Cell("TERMINAL BLOCK, DIN RAIL, 600VAC, 30A")]

Pass 1 — BOM line-item extraction:
  Column mapping: ITEM#=0, QTY=1, CATALOG#=2, MATERIAL=3
  catalog_number = "WAGO-281-611"
  description = "TERMINAL BLOCK, DIN RAIL, 600VAC, 30A"
  Type inferred from description: "TERMINAL BLOCK" → ComponentType.TERMINAL_BLOCK
  Values extracted from description: "600VAC" → voltage, "30A" → current

Pass 2 — Exhaustive regex extraction (additive):
  Same row text matched against all 50+ patterns:
    VOLTAGE_PATTERN matches "600VAC" → already associated (no duplicate)
    CURRENT_PATTERN matches "30A" → already associated
    No new components found (WAGO-281-611 already extracted in Pass 1)

Final Component object:
  Component(
      component_id = "WAGO-281-611",
      component_type = ComponentType.TERMINAL_BLOCK,
      description = "TERMINAL BLOCK, DIN RAIL, 600VAC, 30A",
      values = [
          ComponentValue("voltage_rating", "600VAC", "V", 600.0),
          ComponentValue("current_rating", "30A", "A", 30.0),
      ],
      connections = [],
      labels = [TextLabel("TERMINAL BLOCK, DIN RAIL, 600VAC, 30A", x=3, y=2, category="")],
      drawing_refs = [],
      attributes = {"catalog_number": "WAGO-281-611", "quantity": 2, "item_number": 1}
  )
```

BOM extraction is purely additive — Pass 1 extracts structured line items using column headers, Pass 2 applies the same 50+ regex patterns used by PDF and DXF extractors to catch anything the structured pass missed.

### Metadata at Each Pipeline Step

This shows the exact metadata that exists after each step, using concrete examples from the NRE P&C drawing set.

#### After Step 1 (Extraction) — DrawingData Object

Each file produces a `DrawingData` with these populated fields:

| Field | Example Value | Source |
|---|---|---|
| `drawing_id` | `"NRE-EC-301.1"` | Derived from filename (strip extension) |
| `file_path` | `"/home/.../NRE-EC-301.1.pdf"` | Absolute path to source file |
| `file_type` | `"pdf"` | File extension |
| `drawing_type` | `""` (empty) | Not yet classified |
| `title_block.drawing_number` | `"NRE-EC-301.1"` | Extracted from PDF title block area or DXF TITLEBLOCK attributes |
| `title_block.project_name` | `"NOMADIC RED EGRET 138/34.5kV"` | Regex match on project name keywords |
| `title_block.company` | `"MORTENSON"` | Regex match on company name |
| `title_block.revision` | `"C"` | From title block REV field |
| `components` | Dict with 8-25 entries per drawing | IEEE/ANSI regex patterns (PDF) or block attributes (DXF) |
| `connections` | 5-30 Connection objects per drawing | Spatial proximity (PDF) or wire polyline tracing (DXF) |
| `cross_references` | `["EC-301.0", "EC-307.0", "EC-308.0"]` | `E[CPS]-\d{3}\.\d` pattern |
| `all_labels` | 200-800 TextLabel objects per page | Every pdfplumber word or ezdxf text entity |
| `cable_schedule` | `["2/C#10", "12/C#12SH", "CAT5E"]` | Cable spec regex patterns |
| `terminal_blocks` | `{"TB6": [71, 72, 73], "TS3": [8, 9]}` | Terminal block regex patterns |
| `voltage_levels` | `["138kV", "34.5kV", "125V DC"]` | Voltage regex patterns |
| `raw_text` | Full concatenated text (thousands of chars) | `page.extract_text()` or entity text accumulation |
| `notes` | `["NOTE 1: ALL WIRING 600V RATED"]` | NOTES section regex |
| `index_metadata` | `{}` (empty) | Not yet enriched from drawing index |

**What is NOT yet present:** drawing type classification, index enrichment metadata (title, design phase, revision history), database timestamps, file hash, mismatch data, propagation actions, or audit decisions.

#### After Step 3 (Classification) — DrawingType and Index Metadata Added

The `DrawingClassifier` populates these previously empty fields:

| Field | Example Value | Source |
|---|---|---|
| `drawing_type` | `"DC_SCHEMATIC"` | Strategy 1: Drawing index XLSX lookup (confidence 1.0) |
| `title_block.drawing_name` | `"138KV LINE L1 DC SCHEMATIC"` | From drawing index XLSX title column |
| `index_metadata.drawing_title` | `"138KV LINE L1 DC SCHEMATIC"` | From drawing index entry |
| `index_metadata.current_revision` | `"C"` | From drawing index REV column |
| `index_metadata.current_revision_date` | `"2025-09-20"` | From drawing index DATE column |
| `index_metadata.design_phase` | `"90%"` | From sheet name (30%/60%/90%) |
| `index_metadata.revision_history` | `[("2025-03-15","A"), ("2025-06-10","B"), ("2025-09-20","C")]` | All revision entries for this drawing |

A `DecisionRecord` is also written to the `decisions` database table:

| Field | Example Value |
|---|---|
| `decision_type` | `"CLASSIFICATION"` |
| `drawing_id` | `"NRE-EC-301.1"` |
| `reasoning` | `"Drawing index XLSX matched NRE-EC-301.1 → DC SCHEMATIC"` |
| `confidence` | `1.0` |
| `outcome` | `"DC_SCHEMATIC"` |
| `alternatives` | `[{"strategy": "number_series", "result": "DC_SCHEMATIC", "confidence": 0.7}]` |

#### After Step 2 (Storage) — Database Records Created

`ComponentDatabase.store_drawing()` transforms the `DrawingData` object into relational database rows:

| Table | Key Fields Added | Example |
|---|---|---|
| `drawings` | `file_hash`, `last_scanned`, `last_modified`, all JSON columns | `file_hash="a3f2b8..."`, `last_scanned="2026-04-06T15:09:34"` |
| `components` | One row per component per drawing | `component_id="52-L1"`, `drawing_id="NRE-EC-301.1"`, `values_json='[{"parameter":"voltage_rating","value":"138kV",...}]'` |
| `connections` | One row per wiring connection | `from_component="52-L1"`, `to_component="86MP"`, `cable_spec="2/C#10"`, `signal_type="TRIP"` |
| `labels` | One row per text label (81,000+ total) | `text="52-L1"`, `x=245.3`, `y=412.7`, `category="component"` |
| `snapshots` | Previous component data (if re-scanning) | `components_json='{...previous...}'`, `file_hash="old_hash..."` |
| `change_log` | Detected changes (if values changed) | `change_type="VALUE_CHANGED"`, `old_value="34.5kV"`, `new_value="138kV"` |

The `file_hash` (SHA-256) enables incremental scanning — unchanged files are skipped on subsequent runs.

#### After Step 5 (Mismatch Detection) — Mismatch Objects

Each mismatch is a `Mismatch` dataclass stored in the `mismatches` table:

| Field | Example Value |
|---|---|
| `mismatch_id` | `"a3f2b8c1d5e6..."` (24-char SHA-256 of component + parameter + check_type) |
| `severity` | `AlertSeverity.CRITICAL` |
| `component_id` | `"86MS"` |
| `parameter` | `"voltage_rating"` |
| `drawings_involved` | `["NRE-EC-301.1", "NRE-EC-307.0", "NRE-EC-308.1"]` |
| `values_found` | `{"NRE-EC-301.1": "34.5kV", "NRE-EC-307.0": "138kV", "NRE-EC-308.1": "125V DC"}` |
| `message` | `"Value mismatch for 86MS: voltage_rating has 3 different values across 3 drawings"` |
| `recommendation` | `"Verify voltage rating — 3 different values across 3 drawings"` |
| `resolution_options` | `[{"source_drawing": "NRE-EC-307.0", "source_type": "DC_SCHEMATIC", "value": "138kV", "authority_basis": "DC_SCHEMATIC is authoritative for voltage_rating"}]` |

#### After Step 6 (Propagation Planning) — PropagationAction Objects

Each proposed correction is a `PropagationAction` dataclass:

| Field | Example Value |
|---|---|
| `action_id` | `"prop_86MS_voltage_rating_EC307_EC301"` |
| `source_drawing_id` | `"NRE-EC-307.0"` (the authoritative source) |
| `target_drawing_id` | `"NRE-EC-301.1"` (the drawing to update) |
| `component_id` | `"86MS"` |
| `parameter` | `"voltage_rating"` |
| `old_value` | `"34.5kV"` (current value in target) |
| `new_value` | `"138kV"` (value from authoritative source) |
| `authority_basis` | `"DC_SCHEMATIC is authoritative for voltage_rating"` |
| `status` | `"PROPOSED"` (becomes `"APPLIED"` after `--apply`, `"DRY_RUN"` after `--plan`) |

#### After Step 8 (Audit Trail) — DecisionRecord Objects

Every automated decision is a `DecisionRecord` in the `decisions` table:

| Field | Example Value |
|---|---|
| `decision_id` | `"dec_NRE-EC-301.1_classification_..."` (SHA-256 hash) |
| `timestamp` | `"2026-04-06T15:09:34"` |
| `decision_type` | `"CLASSIFICATION"`, `"AUTHORITY"`, `"PROPAGATION"`, or `"MISMATCH_DETECTION"` |
| `component_id` | `""` (empty for drawing-level) or `"86MS"` (for component-level) |
| `drawing_id` | `"NRE-EC-301.1"` |
| `input_data` | `{"strategy": "drawing_index_xlsx", "index_type": "DC SCHEMATIC"}` |
| `reasoning` | `"Drawing index XLSX matched NRE-EC-301.1 → DC SCHEMATIC"` |
| `confidence` | `1.0` (index lookup), `0.9` (title block), `0.7` (number series) |
| `outcome` | `"DC_SCHEMATIC"` |
| `alternatives` | `[{"strategy": "number_series", "result": "DC_SCHEMATIC", "confidence": 0.7}]` |

### Why the Object-Oriented Approach Matters

The OOP model solves three problems that raw library data cannot:

1. **Format independence.** PDF words, DXF entities, and XLSX cells are completely different data structures. The `DrawingData` container normalizes all three into one queryable format. Downstream code (mismatch detection, propagation, reporting) never needs to know whether a component came from a PDF, a DXF, or a spreadsheet.

2. **Cross-drawing comparison.** Raw library data is per-file. The OOP model, stored in SQLite, enables cross-drawing queries: "find every drawing containing breaker 52-L1" or "compare voltage ratings across all appearances of relay 86MS." These queries are impossible with raw pdfplumber dicts or ezdxf entities.

3. **Typed validation.** `ComponentType.BREAKER` is a known enum value, not a freeform string. `ComponentValue.numeric_value` is a float, not a text label that might or might not contain a number. Type constraints catch data quality issues (empty IDs, pure-numeric IDs, single-character IDs) at extraction time rather than downstream.

The `to_dict()` / `from_dict()` methods on every dataclass enable full round-trip serialization: objects can be stored as JSON in SQLite columns, exported to JSON files, and reconstructed without losing any field. This is how per-drawing JSON exports and the full JSON export maintain complete fidelity with the database state.

---

After a full pipeline run (`python -m drawing_sync.cli pipeline -i <input> -o <output_dir>`), the output directory contains:

```
output/
├── drawing_sync.db                          # SQLite database (all extracted data)
├── reports/
│   ├── scan_YYYYMMDD_HHMMSS.txt             # Extraction summary per drawing
│   ├── mismatch_YYYYMMDD_HHMMSS.txt         # All mismatches grouped by severity
│   ├── shared_components_YYYYMMDD_HHMMSS.txt # Components appearing in 2+ drawings
│   ├── dependency_graph_YYYYMMDD_HHMMSS.txt  # Cross-reference relationships
│   └── change_log_YYYYMMDD_HHMMSS.txt        # Chronological change audit trail
├── drawings/
│   ├── NRE-EC-001.0.json                     # Per-drawing sync report
│   ├── NRE-EC-100.0.json
│   ├── NRE-EC-301.1.json
│   └── ... (one JSON file per scanned drawing)
└── exports/
    └── full_export_YYYYMMDD_HHMMSS.json      # Complete cross-drawing JSON export
```

### SQLite Database (`drawing_sync.db`)

The database is the persistent state of the entire system. It contains 9 tables:

| Table | Rows (NRE test) | What it stores |
|---|---|---|
| `drawings` | 177 | One row per drawing: file path, SHA-256 hash, drawing type, title block metadata (JSON), raw text, cross-references, cable schedule, terminal blocks, voltage levels, index metadata, timestamps |
| `components` | 2,315 | One row per component-per-drawing: component ID, drawing ID, type enum, description, electrical values (JSON array), connections (JSON), labels (JSON), attributes (JSON) |
| `connections` | 2,813 | One row per wiring connection: from/to component and terminal, cable spec, wire label, signal type, source drawing |
| `labels` | 81,058 | One row per text label: text string, X/Y pixel position, category (component, value, cable, reference, terminal, signal, note, title, text) |
| `snapshots` | Varies | Historical component data snapshots saved before each re-extraction, enabling change detection and rollback |
| `mismatches` | 511 | One row per detected inconsistency: mismatch ID (SHA-256), severity (CRITICAL/WARNING/INFO), component, parameter, involved drawings (JSON), conflicting values (JSON), message, recommendation, resolution options (JSON), resolved flag |
| `change_log` | Varies | Audit trail of every detected modification: timestamp, drawing, component, change type (COMPONENT_ADDED/REMOVED, VALUE_CHANGED), old/new values, description |
| `propagation_log` | Varies | Every propagation action: action ID, timestamp, source/target drawing, component, parameter, old/new value, authority basis, status (PROPOSED/APPLIED/FAILED/DRY_RUN) |
| `decisions` | 177+ | Decision audit trail for compliance: decision type (CLASSIFICATION/AUTHORITY/PROPAGATION/MISMATCH_DETECTION), component, drawing, input data (JSON), reasoning text, confidence score (0.0–1.0), outcome, alternatives considered (JSON) |

The database uses WAL mode for concurrent read/write, enforced foreign keys, and 12 indexes for fast queries. It can be opened directly with `sqlite3 drawing_sync.db` for ad-hoc queries.

### Scan Report (`reports/scan_*.txt`)

A comprehensive extraction summary (~350+ KB). Contains:

- **Drawing table** — One row per drawing showing component count, connection count, cross-reference count, label count, cable count, and drawing type.
- **Component inventory** — Per-drawing table of every extracted component with its ID, type, electrical values, and attributes. Example:

  ```
  Drawing: NRE-EC-301.1 (DC_SCHEMATIC)
  | Component  | Type    | Values          | Attributes       |
  |------------|---------|-----------------|------------------|
  | 52-L1      | BREAKER | 138kV           | signal: TRIP     |
  | SEL-451    | RELAY   | 138kV           | signal: BF TRIP  |
  | 86MP       | 86      |                 | signal: LOCKOUT  |
  | TB6        | TB      |                 | terminals: 71,72 |
  ```

- **Extraction completeness notes** — Flags `[MISSING DATA]` for components that have no electrical values or no connections, indicating potential extraction gaps.
- **Extraction warnings** — Unrecognized CAD blocks and other extractor warnings for manual review.
- **Voltage level summary** — Per-drawing list of all detected voltage levels (e.g., `138kV, 34.5kV, 125V DC`).
- **Database statistics** — Total drawings, components, connections, labels, and shared components.

### Mismatch Report (`reports/mismatch_*.txt`)

All detected mismatches (~890+ KB), grouped by severity. Each entry contains:

- **Severity** — `[RED FLAG] CRITICAL`, `[WARNING]`, or `[INFO]`
- **Component and parameter** — Which component has the conflict and on which attribute
- **Conflicting values** — The different values found across drawings, with drawing IDs
- **Recommendation** — What action to take
- **Authority-based resolution** (when available) — Which drawing holds the authoritative value and why

Example critical mismatch entry:

```
[RED FLAG] CRITICAL: Value mismatch for component 86MS
  Parameter: voltage_rating
  Values found:
    NRE-EC-301.1 (DC_SCHEMATIC): 34.5kV
    NRE-EC-307.0 (DC_SCHEMATIC): 138kV
    NRE-EC-308.1 (DC_SCHEMATIC): 125V DC
  Recommendation: Verify voltage rating — 3 different values across 3 drawings
  Resolution: Use value from NRE-EC-307.0 (DC_SCHEMATIC) based on authority rules
```

Example protection logic mismatch:

```
[WARNING]: Relay-breaker trip path not found
  Relay 50-L1 (overcurrent) should connect to breaker 52-L1
  No direct or indirect (via lockout 86) connection path found
  Recommendation: Verify trip path in DC schematics
```

### Shared Components Report (`reports/shared_components_*.txt`)

Table of all components appearing in 2+ drawings (~50 KB). Shows the component ID, how many drawings it appears in, and the list of drawing IDs. Used to understand the cross-drawing dependency scope of each component.

```
| Component | Drawings | Drawing List                                        |
|-----------|----------|-----------------------------------------------------|
| 86MP      | 23       | NRE-EC-001.0, NRE-EC-101.0, NRE-EC-102.0, ...     |
| 52-L1     | 18       | NRE-EC-001.0, NRE-EC-100.0, NRE-EC-200.0, ...     |
| SEL-451   | 12       | NRE-EC-100.0, NRE-EC-200.0, NRE-EC-301.0, ...     |
```

### Dependency Graph Report (`reports/dependency_graph_*.txt`)

For each drawing (~100 KB), shows:

- **Outgoing cross-references** — Drawings this drawing explicitly references (e.g., "SEE EC-307.0")
- **Incoming cross-references** — Drawings that reference this drawing
- **Shared components** — Components this drawing shares with other drawings, and which other drawings have them

```
Drawing: NRE-EC-301.1
  Outgoing references: EC-301.0, EC-307.0, EC-308.0, EC-308.1
  Incoming references: NRE-EC-307.0, NRE-EC-400.0
  Shared components:
    86MS — also in NRE-EC-307.0, NRE-EC-308.1
    52-L1 — also in NRE-EC-001.0, NRE-EC-100.0, NRE-EC-200.0, ...
```

### Change Log Report (`reports/change_log_*.txt`)

Chronological audit trail of every detected change when files are re-scanned. Each entry records the timestamp, drawing, component, change type (added/removed/value changed), and old/new values. Enables "what changed and when" queries across the entire drawing set.

### Per-Drawing JSON Files (`drawings/*.json`)

One JSON file per drawing (177 files). Each contains:

```json
{
  "drawing_id": "NRE-EC-301.1",
  "timestamp": "2026-04-06T15:09:34.145387",
  "components": {
    "52-L1": {
      "type": "BREAKER",
      "values": [
        {"parameter": "voltage_rating", "value": "138kV", "unit": "kV", "numeric": 138.0}
      ],
      "also_in_drawings": ["NRE-EC-001.0", "NRE-EC-100.0", "NRE-EC-200.0"],
      "connections": [
        {"to": "86MP", "to_terminal": "52T", "cable_spec": "2/C#10", "signal": "TRIP"}
      ],
      "attributes": {"signal": "TRIP"}
    },
    "86MP": {
      "type": "86",
      "values": [],
      "also_in_drawings": ["NRE-EC-001.0", "NRE-EC-101.0", "NRE-EC-307.0"],
      "connections": [...],
      "attributes": {"signal": "LOCKOUT"}
    }
  },
  "mismatches": [
    {
      "severity": "CRITICAL",
      "component": "86MS",
      "parameter": "voltage_rating",
      "message": "Value mismatch: 34.5kV vs 138kV vs 125V DC",
      "recommendation": "Use value from NRE-EC-307.0 (DC_SCHEMATIC)"
    }
  ],
  "recommendations": [
    "RED FLAG: 86MS has conflicting voltage ratings across 3 drawings",
    "52-L1 appears in 18 drawings — coordinate any changes with all stakeholders"
  ]
}
```

These files enable integration with external tools — any system that reads JSON can consume per-drawing component, mismatch, and recommendation data.

### Full JSON Export (`exports/full_export_*.json`)

A single comprehensive JSON file (~2.5 MB) containing cross-drawing analysis:

```json
{
  "generated": "2026-04-06T15:09:34.571121",
  "input_directory": "/home/adminho/mortensen/data/NRE P&C/P&C_PDF",
  "statistics": {
    "total_drawings": 177,
    "total_components": 494,
    "total_component_instances": 2315,
    "total_connections": 2813,
    "total_labels": 81058,
    "active_mismatches": 511,
    "shared_components": 265
  },
  "shared_components": {
    "86MP": ["NRE-EC-001.0", "NRE-EC-101.0", "NRE-EC-102.0", "..."],
    "52-L1": ["NRE-EC-001.0", "NRE-EC-100.0", "NRE-EC-200.0", "..."],
    "SEL-451": ["NRE-EC-100.0", "NRE-EC-200.0", "NRE-EC-301.0", "..."]
  },
  "dependency_graph": {
    "NRE-EC-301.1": {
      "references": ["EC-301.0", "EC-307.0", "EC-308.0"],
      "referenced_by": ["NRE-EC-307.0", "NRE-EC-400.0"],
      "shared_components": ["86MS", "52-L1", "SEL-451", "86MP"]
    }
  }
}
```

The `statistics` block gives the overall system health at a glance. The `shared_components` map answers "which drawings contain this component?" The `dependency_graph` answers "how are these drawings connected?" Together, they provide the complete cross-drawing picture.

### Additional Outputs (Generated by Individual Commands)

These outputs are not produced by the `pipeline` command but can be generated separately:

| Output | Command | Format | Contents |
|---|---|---|---|
| Cable list workbook | `cable-list -o file.xlsx` | Excel XLSX (3 sheets) | Sheet 1: Every cable with from/to components, terminals, spec, signal. Sheet 2: Aggregated by cable spec with counts. Sheet 3: Grouped by source drawing. |
| Propagation plan | `propagate --plan --all -o file.txt` | Text | Proposed value updates: source drawing, target drawing, component, parameter, old value, new value, authority basis. |
| Authority rules | `authority export -o file.json` | JSON | All 8 authority rules with parameter, applicable component types, and priority-ordered drawing type list. |
| Audit report | `audit --export -c COMP -o file.txt` | Text | Formal compliance document: component overview, classification decisions, authority determinations, propagation actions, mismatch history. |
| Component deep-dive | `status -c COMP -o file.txt` | Text | Single component across all drawings: values per drawing, consistency check, mismatches, recommendations. |

---

## Tools, Technologies & Concepts by Pipeline Step

A per-step catalog of every Python library, shell utility, database operation, mathematical technique, electrical engineering concept, and external tool used — with the intuition for why each one matters.

### Step 1: Extraction

| Category | Tool / Concept | How It's Used | Why It Expands Functionality |
|---|---|---|---|
| **Python — PDF** | `pdfplumber` (built on `pdfminer.six`) | Extracts every word from each PDF page with exact X/Y pixel coordinates | Turns a flat, unstructured PDF into a spatial word map — the foundation for associating values with nearby components without any CAD metadata |
| **Python — PDF fallback** | `pypdf` | Basic PDF metadata extraction; reserved for future direct PDF value editing | Provides a write-capable PDF library so the system could eventually patch corrected values back into PDFs |
| **Python — CAD** | `ezdxf` | Reads DXF files: block references (INSERT entities), TEXT/MTEXT, LINE/LWPOLYLINE, DIMENSION entities, layer data | Accesses structured CAD data — block attributes give component properties directly (TAG, NAME, VOLTAGE) without spatial inference, and polylines give actual wire paths |
| **Python — Excel** | `openpyxl` | Reads XLSX schedules, drawing indices, junction box BOMs, cable/fiber schedules | Ingests project metadata (drawing number → type mapping) and equipment lists that don't exist in any drawing file |
| **Python — stdlib** | `re` (regex) | IEEE/ANSI device patterns (`52-L1`, `87-BH`), relay models (`SEL-451`), cable specs (`2/C#10`), cross-refs (`EC-301.1`), signal keywords (`TRIP`, `CLOSE`) | Regex is the core classification engine — every component, value, and reference is identified by pattern matching against known IEEE/ANSI naming conventions |
| **Python — stdlib** | `hashlib` (SHA-256) | Computes file hash for every drawing before extraction | Enables incremental scanning — unchanged files are skipped entirely, reducing a 75-second full scan to seconds on re-runs |
| **Shell — DWG conversion** | ODA File Converter | Converts proprietary AutoCAD DWG binary format to open DXF text format | DWG is a closed binary format that `ezdxf` cannot read; ODA bridges the gap so the system can ingest native AutoCAD files |
| **Shell — headless GUI** | `xvfb-run` (X Virtual Framebuffer) | Runs ODA File Converter (a Qt GUI application) without a display server | Allows DWG→DXF conversion on headless Linux servers and CI/CD pipelines where no monitor or X11 session exists |
| **Shell — install** | `wget`, `dpkg` (in `install_oda.sh`) | Downloads and installs ODA File Converter `.deb` package | One-command setup of the DWG conversion dependency |
| **Python — stdlib** | `subprocess` | Invokes ODA File Converter as a child process from Python | Bridges the Python pipeline to the external C++ converter binary |
| **Python — stdlib** | `tempfile`, `shutil` | Creates temporary directories for DWG→DXF conversion output; cleans up with `shutil.rmtree` | Prevents temporary DXF files from accumulating on disk after extraction |
| **Math — Euclidean distance** | `sqrt((x₁−x₂)² + (y₁−y₂)²)` | Associates electrical value labels (e.g., `138kV`) with the nearest component label on the page | Spatial proximity is the only way to infer value-to-component relationships in flat PDFs — the closest label is almost always the correct one in engineering drawings where values are placed next to their symbols |
| **Math — coordinate geometry** | Same-Y-coordinate inference | Text labels sharing the same vertical position are inferred to be on the same circuit path | In schematics, horizontal circuit runs are drawn at a constant Y — grouping by row reconstructs circuit topology from flat text |
| **EE — IEEE/ANSI C37.2** | Device function numbers | `52` = circuit breaker, `50/51` = overcurrent relay, `87` = differential relay, `86` = lockout relay, `89` = disconnect switch, `21` = distance relay, `27/59` = under/overvoltage, `67` = directional OC, `81` = frequency relay, `79` = recloser | These standard numbers are the universal vocabulary of substation protection — every protection engineer worldwide uses them, so regex patterns built on them work across any utility's drawings |
| **EE — relay models** | SEL, GE, ABB, Beckwith patterns | `SEL-451`, `SEL-487`, `SEL-2505`, `GE-L90`, etc. | Manufacturer model numbers identify the exact relay hardware, which determines available protection functions and settings |
| **EE — instrument transformers** | CT, PT, VT, CCVT | Current transformers (measure current), potential/voltage transformers (measure voltage), coupling capacitor VTs (high-voltage coupling) | These are the "sensors" of the protection system — every relay depends on them, so tracking their ratios (e.g., `700/1200:1:1`) is critical for correct relay settings |
| **EE — electrical values** | Voltage (kV, V), current (A, kA), impedance (%Z), MVA, CT/PT ratios | Extracted via regex and associated with components via spatial proximity | These are the parameters that must stay synchronized — a voltage mismatch between drawings can mean a relay is set for the wrong system, which is a protection failure |

### Step 2: Storage

| Category | Tool / Concept | How It's Used | Why It Expands Functionality |
|---|---|---|---|
| **Database** | SQLite3 (Python `sqlite3`) | 9-table relational database (`drawing_sync.db`) storing drawings, components, connections, labels, mismatches, change_log, snapshots, propagation_log, decisions | A relational database enables cross-drawing queries that would be impossible with flat files — "find every drawing containing breaker 52-L1" becomes a single SQL query instead of re-parsing 177 files |
| **Database — WAL mode** | `PRAGMA journal_mode=WAL` | Write-Ahead Logging for concurrent read/write access | Allows the file watcher to write new extractions while reports are being read, without locking — essential for real-time monitoring |
| **Database — referential integrity** | `PRAGMA foreign_keys = ON` | Enforces foreign key constraints across tables | Prevents orphaned records — a component cannot reference a drawing that doesn't exist, maintaining data consistency |
| **Database — indexing** | Indexes on `drawing_id`, `component_id`, `status`, `drawing_type` | Speeds up the most frequent queries (component lookups, mismatch filtering) | With 81,000+ labels and 2,300+ component instances, indexes reduce query time from table-scan to near-instant |
| **Database — atomic transactions** | `BEGIN` / `COMMIT` / `ROLLBACK` | Each drawing storage is wrapped in a single transaction; failures roll back completely | Prevents partial data corruption — if extraction fails midway, the database remains in its previous consistent state |
| **Python — stdlib** | `json` | Serializes complex fields (values, connections, labels, attributes, cross-references) to JSON for storage in TEXT columns | SQLite doesn't natively support arrays or nested objects; JSON columns give schema flexibility while keeping the relational structure for indexable fields |
| **Math — cryptographic hashing** | SHA-256 file hash | Stored per drawing; compared on re-scan to detect changes | A 256-bit hash is a near-unique fingerprint — if two files have the same hash, they are identical byte-for-byte, enabling reliable incremental scanning without comparing file contents |
| **Concept — snapshotting** | Before/after component data snapshots | Old component data is saved to `snapshots` table before overwriting | Creates a time-series of component states, enabling "what changed and when" queries for audit and rollback |

### Step 3: Drawing Classification

| Category | Tool / Concept | How It's Used | Why It Expands Functionality |
|---|---|---|---|
| **Python** | `openpyxl` | Reads the project drawing index XLSX to map drawing numbers to types (Strategy 1, confidence 1.0) | The drawing index is the single source of truth for drawing types — when available, it provides 100% confidence classification |
| **Python — stdlib** | `re` | Parses drawing number format `NRE-EC-XXX.Y` to extract the numeric series (Strategy 3, confidence 0.7) | Drawing number ranges follow project conventions (100s = one-lines, 200s = AC schematics, 300s = DC schematics) — pattern parsing enables classification even without an index file |
| **Concept — multi-strategy fallback** | 3-tier classification with confidence scores | Strategy 1 (XLSX, 1.0) → Strategy 2 (title block attribute, 0.9) → Strategy 3 (number series, 0.7) | Graceful degradation — the system classifies correctly even when some data sources are missing, and the confidence score tells downstream logic how much to trust the result |
| **EE — drawing types** | ONE_LINE, AC_SCHEMATIC, DC_SCHEMATIC, PANEL_WIRING, CABLE_WIRING, PANEL_LAYOUT, RELAY_FUNCTIONAL, etc. | Each drawing is classified into one of 12 types | Drawing type determines authority — a one-line diagram is authoritative for voltage ratings while a DC schematic is authoritative for relay wiring. Without classification, the system cannot resolve conflicts |

### Step 4: Source-of-Truth & Authority Rules

| Category | Tool / Concept | How It's Used | Why It Expands Functionality |
|---|---|---|---|
| **Concept — authority hierarchy** | Configurable priority rules per parameter | 8 rules define which drawing type wins for each parameter (e.g., `voltage_rating`: ONE_LINE > AC_SCHEMATIC > DC_SCHEMATIC > PANEL_WIRING) | In real engineering, one-line diagrams are the master reference for system voltages, while DC schematics are the master for relay wiring — encoding this domain knowledge lets the system resolve conflicts automatically instead of flagging every mismatch for manual review |
| **Python — stdlib** | `json` | Authority rules exportable to / importable from JSON | Makes the authority hierarchy project-configurable — different utilities or project standards can define different authority chains |
| **Python** | `dataclasses` | `AuthorityRule` and `AuthorityConfig` as typed dataclasses | Provides structured, validated configuration objects instead of raw dicts, catching misconfiguration at load time |
| **EE — drawing authority** | Engineering hierarchy of drawing types | One-line diagrams show the system overview (highest authority for ratings); DC schematics show protection logic (highest for relay wiring and cable specs); panel wiring shows physical implementation (lowest authority for electrical values) | This mirrors how protection engineers actually work — they start from the one-line and flow down to implementation drawings. Authority rules formalize this implicit workflow |

### Step 5: Mismatch Detection

| Category | Tool / Concept | How It's Used | Why It Expands Functionality |
|---|---|---|---|
| **Database — SQL** | `GROUP BY`, `HAVING`, `GROUP_CONCAT`, `COUNT(DISTINCT)` | Identifies components with conflicting values across drawings, groups by parameter, counts unique values | SQL aggregation turns 2,300+ component instances into a concise set of conflicts — the database does the heavy lifting of cross-referencing |
| **Math — set comparison** | Unique value counting per parameter | If `COUNT(DISTINCT value) > 1` for a component-parameter pair, it's a mismatch | Simple but powerful — any parameter should have exactly one value across all drawings. More than one means something is wrong |
| **Math — hashing** | SHA-256 of `(component, parameter, check_type)` → 24-char hex ID | Generates deterministic, collision-resistant mismatch IDs | Same mismatch always gets the same ID across runs, enabling tracking, resolution, and `INSERT OR REPLACE` without duplicates |
| **Concept — severity classification** | CRITICAL / WARNING / INFO three-tier severity | Value mismatches on safety-relevant parameters (voltage, current) are CRITICAL; naming/reference issues are WARNING; minor discrepancies are INFO | Focuses engineer attention on the 65 critical red flags first, rather than drowning them in 427 informational items |
| **Concept — authority-enriched resolution** | Mismatch records include `resolution_options` with authoritative drawing and basis | 67 of 511 mismatches include "use the value from [drawing] because [authority rule]" guidance | Transforms raw alerts into actionable instructions — instead of "these values differ," the system says "use 138kV from EC-001.0 because one-line diagrams are authoritative for voltage ratings" |
| **EE — protection logic verification** | Checks 9-12: trip paths, lockout completeness, CT associations, DC supply | Verifies that protective relays connect to breakers, lockout relays have both inputs and outputs, CTs feed relays, and DC power reaches all devices | These checks validate the protection system's logical integrity — a relay that can't trip its breaker is a protection failure, regardless of whether the component values match |
| **EE — trip path tracing** | Relay (50/51/87/21/67/81) → Lockout (86) → Breaker (52) | Traces the connection chain from fault-detecting relays through lockout relays to circuit breakers | The entire purpose of a protection system is to trip breakers when faults occur — verifying this path exists is the most fundamental correctness check |

### Step 6: Attribute Propagation

| Category | Tool / Concept | How It's Used | Why It Expands Functionality |
|---|---|---|---|
| **Concept — plan/apply pattern** | `--plan` mode generates proposed actions; `--apply` executes them | Engineers review proposed changes before they're committed to the database | Prevents automated corrections from introducing errors — the system proposes, the engineer disposes |
| **Database — audit logging** | `propagation_log` table records every action with source, target, old/new values, authority basis, status | Full traceability of what was changed, from where, to where, and why | Supports the signing engineer's legal responsibility — they can demonstrate that every change was authority-based and reviewed |
| **Python** | `dataclasses` | `PropagationAction` with typed fields and status enum (PROPOSED/APPLIED/FAILED/DRY_RUN) | Structured action records prevent ambiguity about what will change and enable reliable apply/rollback |
| **Concept — dry run** | `dry_run=True` mode simulates application without modifying the database | Validates that propagation logic works correctly before committing | Risk-free testing of the propagation engine on real data |

### Step 7: Synchronization & Change Detection

| Category | Tool / Concept | How It's Used | Why It Expands Functionality |
|---|---|---|---|
| **Python** | `watchdog` | Monitors directories for file create/modify events on PDF, DXF, DWG, XLSX files | Real-time detection — as soon as an engineer saves a drawing, the system re-extracts and re-checks within seconds, catching mismatches before they propagate further |
| **Concept — debouncing** | 2-second wait after last file event before triggering extraction | Text editors and CAD tools often trigger multiple save events for a single user save | Prevents redundant re-extractions and ensures the file is fully written before reading |
| **Database — snapshot comparison** | New extraction compared against stored snapshot; changes logged to `change_log` | Identifies exactly what changed: components added, removed, or modified with old/new values | Change-level granularity means the system can report "breaker 52-L1 voltage changed from 34.5kV to 138kV in EC-307.0" rather than just "EC-307.0 was modified" |
| **Concept — cascading re-check** | After re-extraction, mismatch detection re-runs on all affected components | A change in one drawing may create or resolve mismatches in other drawings | Ensures the mismatch state is always current — no stale alerts persist after fixes, and new conflicts are caught immediately |

### Step 8: Decision Audit Trail

| Category | Tool / Concept | How It's Used | Why It Expands Functionality |
|---|---|---|---|
| **Database** | `decisions` table with structured fields (decision_type, input_data, reasoning, confidence, outcome, alternatives) | Every classification, authority determination, propagation, and mismatch detection decision is recorded | Creates a complete chain of reasoning that a signing engineer can review for compliance — the system can explain *why* it made every decision |
| **Concept — decision tree** | `generate_decision_tree(component_id)` produces a nested report | Shows all decisions related to a single component: how it was classified, which drawing is authoritative, what propagations were applied, what mismatches were found | Enables targeted review — an engineer investigating a specific component can see every automated decision the system made about it |
| **Concept — confidence scoring** | Decisions carry a 0.0–1.0 confidence score based on the strategy used | XLSX lookup = 1.0, title block = 0.9, number series = 0.7 | Highlights where the system is less certain, directing human review to the decisions most likely to need override |
| **EE — compliance** | Formal audit report with 5 sections (overview, classification, authority, propagation, mismatch history) | Generates documentation suitable for a signing engineer's review file | In regulated utility work, every design decision must be traceable. The audit trail automates this documentation requirement |

### Step 9: Cable List Export

| Category | Tool / Concept | How It's Used | Why It Expands Functionality |
|---|---|---|---|
| **Python** | `openpyxl` (Workbook, Font, PatternFill, Alignment, Border, `get_column_letter`) | Creates a professionally formatted Excel workbook with styled headers, alternating row colors, auto-fitted columns, and frozen panes | Produces a deliverable-quality cable schedule that can go directly to construction — no manual reformatting needed |
| **Database — SQL** | Queries `connections` table joined with `components` for signal attributes | Extracts all 2,813 wiring connections with full metadata | SQL joins combine wiring data with component attributes in a single query, enriching the cable list with signal types and component context |
| **Concept — multi-sheet organization** | Sheet 1: flat cable list; Sheet 2: aggregated by cable spec; Sheet 3: grouped by drawing | Three views of the same data for different use cases (construction, procurement, drawing-by-drawing review) | Different stakeholders need different views — construction crews need the flat list, procurement needs the aggregated specs, and drafters need the per-drawing breakdown |

### Step 10: Continuous Monitoring

| Category | Tool / Concept | How It's Used | Why It Expands Functionality |
|---|---|---|---|
| **Python** | `watchdog` (Observer + FileSystemEventHandler) | Cross-platform filesystem event monitoring with callback architecture | Eliminates polling — the OS notifies the system of changes via inotify (Linux) / FSEvents (macOS) / ReadDirectoryChanges (Windows), making monitoring near-zero-cost in CPU usage |
| **Concept — event-driven pipeline** | File change → extract → classify → detect → propagate → alert, all triggered automatically | The full Steps 1–7 pipeline runs without human intervention on every file save | Transforms a batch tool into a live monitoring system — engineers get immediate feedback instead of waiting for a scheduled scan |
| **Python — stdlib** | `time` (for debounce timing), `logging` (for structured output), `datetime` (for timestamps) | Debounce logic, structured console alerts, timestamped events | Standard library tools keep the monitoring lightweight with no additional dependencies |

### Cross-Cutting Tools (Used Across Multiple Steps)

| Category | Tool / Concept | Where Used | Why It Expands Functionality |
|---|---|---|---|
| **Python** | `dataclasses` | Models, authority rules, propagation actions, audit records | Type-safe structured data throughout the pipeline; `to_dict()` / `from_dict()` enable serialization without losing field definitions |
| **Python** | `typing` (Optional, List, Dict) | Every module | Static type hints enable IDE autocompletion and catch type errors before runtime |
| **Python** | `argparse` | `cli.py` — 14 commands with flags | Provides a self-documenting CLI with `--help` on every command |
| **Python** | `rich` | CLI output formatting | Colored, styled terminal output makes critical alerts visually distinct from informational messages |
| **Python** | `tabulate` | Report generation | Aligned text tables in reports (drawing summaries, component lists) that are readable in any text editor |
| **Python** | `click` | Internal argument parsing support | Lightweight argument handling for subcomponents |
| **Python — stdlib** | `os`, `os.path` | Directory walking, path manipulation, file existence checks | Platform-independent file operations (Windows backslash vs. Linux forward slash) |
| **Python — stdlib** | `json` | Serialization/deserialization of complex fields, authority config, export | Universal interchange format — database columns, config files, CLI export, and audit records all use JSON |
| **Python — stdlib** | `datetime` | Timestamps on every database record, report headers, audit trail | Temporal ordering of events enables change history and "when did this mismatch first appear?" queries |
| **Python — stdlib** | `logging` | Structured log output across all modules | Filterable by level (DEBUG/INFO/WARNING/ERROR); essential for diagnosing extraction issues on specific drawings |
| **Python — stdlib** | `enum` | ComponentType, DrawingType, AlertSeverity | Constrained value sets prevent typos and enable exhaustive matching — `DrawingType.ONE_LINE` can't be misspelled as `"one line"` |
| **Database — SQLite** | Embedded, zero-config, single-file database | All storage and querying | No server to install or manage — the entire component registry is a single portable file that can be copied, backed up, or inspected with the `sqlite3` CLI |
| **Math — regex patterns** | 50+ compiled patterns across extractors | Component identification, value extraction, reference detection, signal classification | Regular expressions are the bridge between unstructured text and structured data — they encode decades of IEEE naming conventions into machine-readable rules |
| **EE — IEEE/ANSI C37.2** | Standard device function numbers | Component identification and type classification across all extractors | The universal language of substation protection; building the system on IEEE standards means it works for any utility's drawings, not just this project |

---

## Tested Results (NRE P&C Drawing Set)

The system has been tested end-to-end on the complete NRE P&C drawing set:

**PDF-only scan (177 drawings):**

| Metric | Value |
|---|---|
| Drawings scanned | 177 (0 errors) |
| Drawings classified | 177 (62 DC_SCHEMATIC, 51 PANEL_WIRING, 19 AC_SCHEMATIC, 5 ONE_LINE, ...) |
| Unique components extracted | 717 |
| Component instances (total across all drawings) | 3,957 |
| Components appearing in 2+ drawings | 482 |
| Total wiring connections | 2,813 |
| Total text labels extracted | 81,058 |
| Cable specifications found | Varies per drawing (2/C#10, 12/C#12SH, CAT5E, MM FIBER, ...) |
| Cables exported to Excel | 2,813 |
| Terminal block mappings | TB1-TB11, TS1-TS10 across 24+ drawings |
| Cross-reference links | 91 from EC-001.0 alone |
| **Mismatches detected (12 checks)** | **511** |
| Critical (RED FLAG) | 65 (voltage/current rating conflicts) |
| Warning | 19 (cross-reference, relay trip paths, lockout relay issues) |
| Info | 427 (terminal block, orphan, CT association, DC supply) |
| Mismatches with authority-based resolution | 67 |
| Propagation actions planned | 109 |
| Audit decisions recorded | 177 |
| Full scan time | ~75 seconds |
| Mismatch detection time | ~0.4 seconds |

**Single DXF/CAD file scan (AutoCAD Electrical):**

| Metric | Value |
|---|---|
| File | `NRE-EC-320.1_in_autocad_2026.dxf` |
| Components extracted | 20 (up from 4 with old extractor) |
| Connections extracted | 7 |
| Voltage levels detected | 125V DC |
| Mismatches detected | 12 |
| Extraction strategies used | DEVICE attr, FUSE_NUM, OUTPUT-#, INPUT-#, block name keywords, PNL_TERM/RELAY_TERM |

The 11-strategy block extraction pipeline recognizes AutoCAD Electrical domain-specific blocks (FUSE_HORIZONTAL, POWER SUPPLY, OUTPUT_IN-RELAYBOX, SERIAL, ETHERNET, PNL_TERM, RELAY_TERM) and their structured attributes (DEVICE, FUSE_NUM, OUTPUT-#, INPUT-#N, PWR_SUP, TOP-TERM, BOTTOM-TERM) that the old TAG/NAME-only approach missed entirely.

**DWG scan via ODA File Converter (204 drawings — includes PANELS, SCHEDULES, XREF):**

| Metric | Value |
|---|---|
| Drawings scanned | 204 (0 errors) |
| Unique components extracted | 576 |
| Component instances (total) | 2,140 |
| Components appearing in 2+ drawings | 212 |
| Total wiring connections | 2,370 |
| Total text labels extracted | 78,285 |

DWG extraction provides richer structured data: more wiring connections per drawing (wire polylines vs spatial inference), title block metadata from block attributes, and block-level component attributes not available in PDFs.

Example critical finding: Component `86MS` (master lockout relay) has voltage ratings of `34.5kV` in EC-301.1, `138kV` in EC-307.0, and `125V DC` in EC-308.1 — three different values across three drawings for the same device.

---

## Quick Start

```bash
# 1. Activate environment
conda activate drawing-sync

# 2. Install ODA File Converter for DWG support (one-time, requires sudo)
./install_oda.sh

# 3. Full pipeline (scan + detect + report — auto-discovers drawing index)
python -m drawing_sync.cli pipeline -i "data/NRE P&C/P&C_PDF/" -o output/

# Or run on a single file:
python -m drawing_sync.cli pipeline -i "data/test_files/NRE-EC-320.1_in_autocad_2026.dxf" -o output_single/

# Or run individual steps:

# 3a. Scan all drawings (or a single file)
python -m drawing_sync.cli scan -i "data/NRE P&C/P&C_PDF/"
python -m drawing_sync.cli scan -i "data/test_files/NRE-EC-320.1_in_autocad_2026.dxf"

# 4. Classify drawing types
python -m drawing_sync.cli classify

# 5. Check for mismatches (12 checks including protection logic)
python -m drawing_sync.cli check

# 6. View authority rules
python -m drawing_sync.cli authority show

# 7. Plan attribute propagation for all components
python -m drawing_sync.cli propagate --plan --all

# 8. Export cable list to Excel
python -m drawing_sync.cli cable-list -o reports/cable_list.xlsx

# 9. View audit trail
python -m drawing_sync.cli audit

# 10. Investigate a specific component
python -m drawing_sync.cli status -c "86MP"

# 11. Start live monitoring
python -m drawing_sync.cli watch -i "data/NRE P&C/P&C_PDF/"
```

See [SCRIPTS.md](SCRIPTS.md) for the complete command reference.
