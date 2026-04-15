# What Happens When the Pipeline Runs

This document explains, step by step, exactly what the `drawing_sync` system does to a given PDF, CAD (DWG/DXF), and supporting Excel file when you run the pipeline command. Nothing is hidden. Every transformation, every decision, every database write is described below.

The core idea: three Python libraries (`pdfplumber`, `ezdxf`, `openpyxl`) each return completely different raw data — flat word dictionaries, typed CAD entities, spreadsheet cell values. Drawing-sync's object-oriented model (`DrawingData`, `Component`, `ComponentValue`, `Connection`, `TextLabel`) normalizes all three into one structured format that downstream analysis (mismatch detection, propagation, auditing) can work with uniformly. This document shows exactly how that transformation happens at every stage.

---

## How to Run It

```bash
python -m drawing_sync.cli pipeline -i "data/NRE P&C/P&C_PDF/" -o output/
```

Or for a single file:

```bash
python -m drawing_sync.cli pipeline -i "data/test_files/NRE-EC-320.1_in_autocad_2026.dxf" -o output_single/
```

When you run this command, the CLI parses your arguments and calls `cmd_pipeline()` in `drawing_sync/cli.py`. That function is the orchestrator. It creates an output directory structure (`reports/`, `exports/`, `drawings/`), initializes the engine, and runs five numbered steps in sequence.

---

## Before Anything Happens: Engine Initialization

The `SyncEngine` constructor (`drawing_sync/sync_engine.py:34`) creates the entire system at startup:

1. **`ComponentDatabase(db_path)`** opens (or creates) a SQLite file at the output path. On first open, it runs `CREATE TABLE IF NOT EXISTS` for all 9 tables: `drawings`, `components`, `connections`, `labels`, `snapshots`, `mismatches`, `change_log`, `propagation_log`, and `decisions`. It sets `PRAGMA journal_mode=WAL` (write-ahead logging for safe concurrent reads/writes) and `PRAGMA foreign_keys = ON` (enforced referential integrity). Two idempotent `ALTER TABLE` migrations add `drawing_type` and `index_metadata_json` columns to the `drawings` table if they don't already exist. Indexes are created on `drawing_id`, `component_id`, `status`, and `drawing_type`.

2. **`PDFExtractor()`** is instantiated with default x/y tolerances of 3 pixels for word grouping.

3. **`DXFExtractor()`** is instantiated. The `ezdxf` library is lazily imported on first use, not at startup.

4. **`XLSXExtractor()`** is instantiated. The `openpyxl` library is also lazily imported.

5. **`MismatchDetector(db)`** is created, which also creates its own `AuthorityConfig` with the 8 default authority rules.

6. **`AuthorityConfig()`** loads the 8 default source-of-truth hierarchy rules (voltage_rating, current_rating, power_rating, impedance, ratio, cable_specification, terminal_assignment, relay_settings).

7. **`AuditTrail(db)`** creates the `decisions` table (if it doesn't exist) with indexes on `component_id`, `decision_type`, and `drawing_id`.

8. **`DrawingClassifier(audit=self.audit)`** is created with a reference to the audit trail so it can record CLASSIFICATION decisions.

9. **`PropagationEngine(db, authority, audit=audit)`** is created with references to the database, authority config, and audit trail.

> **In short:** Before any file is touched, the system has a fully initialized SQLite database with 9 tables, 8 authority rules, 12 mismatch checks ready to run, and a decision audit trail. The entire runtime state lives in `SyncEngine`.

---

## Step 0: Auto-Discover the Drawing Index

Before scanning begins, the engine calls `_auto_discover_index()` (`sync_engine.py:50`). This walks up to 4 directory levels above your input path looking for a `global_reference/` folder containing a file matching `*DRAWING INDEX*.xlsx`. If found (e.g., `data/global_reference/NRE P&C DRAWING INDEX.xlsx`), it is loaded into the `DrawingClassifier`.

The classifier's `_load_drawing_index()` (`drawing_classifier.py:71`) opens the XLSX using `openpyxl` in read-only mode and:
- Sorts worksheets by design phase (30% < 60% < 90%) so higher-phase data overwrites lower-phase data for the same drawing.
- Scans the first 20 rows of each sheet for column headers containing `DWG`/`DRAWING` + `NO`/`NUM`/`#` and `TYPE`.
- Also finds `TITLE` and `REV` columns.
- For each data row after the header, creates a `DrawingIndexEntry` with the drawing number, type, title, current revision, revision date, design phase, and full revision history.
- Stores entries in `_index_map` (drawing_id -> type string) and `_index_entries` (drawing_id -> full entry) with both `NRE-` prefixed and unprefixed keys for flexible lookup.

> **In short:** The system finds the project's master drawing index spreadsheet automatically and loads it into memory so every drawing can be classified by its drawing number alone.

---

## Step 1: Scanning (What Happens to Each File)

The pipeline calls `engine.scan_directory()` or `engine.scan_single_file_with_results()`. For a directory scan, it calls `os.walk()` with `followlinks=False` (prevents symlink cycles), skips `backup` directories, hidden files (`.` prefix), and temp files (`~` prefix). Only `.pdf`, `.dxf`, `.dwg`, `.xlsx`, and `.xls` files are processed.

### Before extracting: Change Detection

For each file, the engine calls `db.has_drawing_changed()` (`db.py:558`). This computes the file's SHA-256 hash (reading in 8KB chunks) and compares it to the hash stored in the `drawings` table from the last scan. If the hashes match, the file is skipped entirely. This is how incremental scans work: a 75-second full scan becomes a 1-second re-scan when nothing changed.

### What happens to a PDF file

When a PDF like `NRE-EC-301.1.pdf` enters `PDFExtractor.extract()` (`extractors/pdf_extractor.py:259`), the extractor must turn a flat, unstructured PDF into a structured electrical dataset. Here is what `pdfplumber` gives us and what drawing-sync does with it.

#### What pdfplumber actually returns

pdfplumber sees a PDF page as a collection of text characters positioned at pixel coordinates. When we call `page.extract_words()`, it groups adjacent characters into words and returns a list of plain Python dictionaries — one per word:

```python
# Actual pdfplumber output for a page of NRE-EC-301.1.pdf (simplified):
[
    {"text": "52-L1",   "x0": 245.3, "top": 412.7, "x1": 282.1, "bottom": 423.5},
    {"text": "138kV",   "x0": 248.9, "top": 425.1, "x1": 279.4, "bottom": 435.2},
    {"text": "TRIP",    "x0": 310.2, "top": 413.0, "x1": 335.8, "bottom": 423.3},
    {"text": "86MP",    "x0": 450.7, "top": 412.5, "x1": 483.2, "bottom": 423.1},
    {"text": "2/C#10",  "x0": 375.4, "top": 413.2, "x1": 408.7, "bottom": 423.6},
    # ... hundreds more words per page
]
```

Each dictionary has the word's text and its bounding box in pixel coordinates (`x0` = left edge, `top` = top edge, `x1` = right edge, `bottom` = bottom edge). This is **all** pdfplumber knows. It has no concept of "component," "electrical value," "connection," or "signal." A breaker label (`52-L1`), a voltage rating (`138kV`), and a signal keyword (`TRIP`) are all just text strings at pixel positions.

The `page.extract_text()` call returns the same words as a single flat string with no coordinates: `"52-L1 138kV TRIP 86MP 2/C#10 ..."`. This is useful for regex pattern matching against the full page content.

#### How drawing-sync transforms this raw data into structured objects

The extractor applies 50+ compiled regex patterns and spatial proximity analysis to turn these flat dictionaries into typed `Component`, `ComponentValue`, `Connection`, and `TextLabel` objects. Here is the step-by-step process:

1. **A `DrawingData` object is created** with `drawing_id = "NRE-EC-301.1"`, `file_path`, and `file_type = "pdf"`. This is the container that will hold all extracted data.

2. **`pdfplumber.open(pdf_path)`** opens the PDF. For each page:

   a. **Raw text extraction:** `page.extract_text()` gets the full text of the page. This is appended to `drawing.raw_text`.

   b. **Positioned word extraction:** `page.extract_words(keep_blank_chars=True, x_tolerance=3, y_tolerance=3)` returns every word on the page with its exact `x0`, `top`, `x1`, `bottom` pixel coordinates. Each word becomes a `TextLabel` with its x/y position.

   c. **Label categorization:** Every word is categorized by `_categorize_label()`. This function runs the word through 50+ compiled regex patterns in priority order:
      - Device patterns (e.g., `\b(52-[A-Z0-9]+)\b` for breakers, `\b(86[A-Z]{1,3}[0-9]*)\b` for lockout relays)
      - Relay model patterns (e.g., `\b(SEL-\d{3,4}[A-Z0-9]{0,5})\b`)
      - Instrument transformer patterns (`CT-`, `PT-`, `VT-`, `CCVT-`)
      - Extended patterns (DPAC, DFR, fuses, hand switches, terminals, substation automation, future-proof equipment)
      - Electrical value patterns (voltage, current)
      - Cable spec patterns
      - Drawing reference patterns (`E[CPS]-\d{3}\.\d`)
      - Terminal patterns
      - Signal keywords (TRIP, CLOSE, SCADA, etc. -- checked with `\b` word boundaries so "CASCADE" doesn't match "SCADA")

      The result is a category string: `"component"`, `"value"`, `"cable"`, `"reference"`, `"terminal"`, `"signal"`, `"note"`, `"title"`, or `"text"`.

   d. **Component extraction:** Four separate methods run regex patterns against the page's full text:
      - `_extract_device_components()`: Matches IEEE/ANSI device function numbers. Each match like `52-L1` creates a `Component` object of type `BREAKER` with description "AC circuit breaker 52-L1".
      - `_extract_relay_components()`: Matches SEL-451, GE-L90, BECKWITH-M-2001D etc. Space-separated patterns like "GE L90" capture "L90" and prepend "GE-" using the `_RELAY_PREFIX_MAP`.
      - `_extract_instrument_transformers()`: Matches CT-B1M, VT-B1, CCVT-HV, etc.
      - `_extract_other_components()`: This is the largest method. It handles fuses (`FU4`), NGRs, switches (`SW-1`), panels (`DC PANEL DC2`), lockout relays (`86MP`, `86MS`), DPAC controllers (normalized from `DPAC1` or `DPAC-1` to canonical `DPAC-1`), SEL communication cables (`SEL-C805`), output contacts (`OUT01`), input contacts (`IN001`), circuit identifiers (`CIRCUIT 16` -> `CIRCUIT-16`), digital fault recorders (normalized `DFR1` -> `DFR-1`), custody meters, fiber patch panels, hand switches, IRIG-B tees, breaker auxiliary contacts, watt sensing links, power supplies, relay panels, trip coils, lockout relay outputs, DC voltage supplies, communication modules, CT class identifiers, standalone terminal blocks, substation automation equipment (RTAC, GPS clock, PDC, CISCO switches, routers), and future-proof patterns (UPS, MCC, switchgear, VFD, PLC, rectifier, MOV, SPD, regulator, generator).

      Each component is stored in the `DrawingData.components` dict keyed by component ID. Duplicate IDs within the same drawing are deduplicated (only the first instance is kept).

   e. **Electrical value extraction:** `_extract_electrical_values()` finds all voltage, current, impedance, MVA, and CT/PT ratio values in the text. For each value found, it calls `_associate_value_with_component()`:
      - The value's text (e.g., "138kV") is located in the word list by matching the numeric prefix (to avoid "kV" inside "kVA" false matches).
      - Its x/y position is retrieved from the word's coordinates.
      - Every component label's position is compared using Euclidean distance: `sqrt((x1-x2)^2 + (y1-y2)^2)`.
      - The value is appended to the nearest component's `.values` list as a `ComponentValue` (e.g., `parameter="voltage_rating", value="138kV", unit="kV", numeric_value=138.0`).
      - Duplicate values on the same component are filtered out.

   f. **Cable extraction:** `_extract_cables()` finds cable specs (`2/C#10`, `12/C #12SH`), cable types (`CAT5E`, `MM FIBER`, `SM FIBER`, `COAX`), cable labels, and multi-pair shielded cables (`3PR #24SH`). Each unique spec is added to `drawing.cable_schedule`.

   g. **Terminal block extraction:** `_extract_terminal_blocks()` finds patterns like `TB6-71` and `TS3-8`, building a dict of terminal block ID -> list of terminal numbers.

   h. **Cross-reference extraction:** `_extract_cross_references()` finds drawing references like `EC-301.1`, `EP-500.0`, `ES-600.0` (the `E[CPS]-XXX.X` pattern), excluding self-references.

   i. **Title block extraction:** `_extract_title_block()` looks for project name, drawn by, designed by, company (MORTENSON), and project identifiers (NOMADIC RED EGRET). If both `138kV` and `34.5kV` appear, the project is labeled as a 138/34.5kV substation.

   j. **Notes extraction:** `_extract_notes()` finds numbered notes and NOTES sections.

   k. **Voltage level extraction:** `_extract_voltage_levels()` finds all kV values and V AC/DC values, stored as a deduplicated sorted list.

   l. **Table extraction:** `_extract_tables()` uses `pdfplumber`'s table extraction on the page. If tables are found, any component references inside them are flagged with `in_table=True` in the component's attributes.

3. **Connection graph building:** After all pages are processed, `_build_connection_graph()` runs. For each component label:
   - Terminal labels within ~50 vertical units are found and linked as connections.
   - Signal keyword labels within ~30 vertical and ~200 horizontal units are found and stored as component signal attributes.
   - Cable spec labels within ~50 vertical units are associated with nearby connections.
   - Cross-reference labels within ~40 vertical and ~200 horizontal units are linked to the component's drawing_refs.
   - Inter-component connections are inferred: two components on the same row (within ~15 vertical units and ~300 horizontal units) are connected. This works because in schematics, items on the same horizontal line are on the same circuit path.

4. **Deduplication:** `_deduplicate_components()` removes noise: empty/whitespace-only IDs, pure numeric IDs, and single-character IDs. Cross-references, cable schedules, and voltage levels are sorted and deduplicated.

#### What the resulting metadata looks like

After extraction completes for a single PDF, the `DrawingData` object contains structured metadata that did not exist in the raw pdfplumber output. Here is a concrete example of what one component looks like after all extraction steps:

```python
# Raw pdfplumber input: {"text": "52-L1", "x0": 245.3, "top": 412.7, ...}
# After drawing-sync extraction:
Component(
    component_id = "52-L1",                          # From regex: r'\b(52-[A-Z0-9]+)\b'
    component_type = ComponentType.BREAKER,           # IEEE/ANSI 52 = circuit breaker
    description = "AC circuit breaker 52-L1",
    values = [
        ComponentValue(                               # From spatial proximity:
            parameter = "voltage_rating",             #   "138kV" at (248.9, 425.1) was
            value = "138kV",                          #   12.9 pixels from "52-L1" at
            unit = "kV",                              #   (245.3, 412.7) — the nearest
            numeric_value = 138.0                     #   component label on the page
        )
    ],
    connections = [
        Connection(                                   # From same-row inference:
            from_component = "52-L1",                 #   "86MP" at y=412.5 is within
            to_component = "86MP",                    #   15 pixels of "52-L1" at y=412.7
            signal_type = "TRIP"                      #   → same circuit path
        )
    ],
    labels = [TextLabel("52-L1", 245.3, 412.7, "component")],
    drawing_refs = ["EC-307.0", "EC-308.0"],          # From cross-ref regex within 40/200 units
    attributes = {"signal": "TRIP"}                   # From signal keyword within 30/200 units
)
```

The key insight: **every relationship** (value-to-component, component-to-component, signal-to-component, cross-reference-to-component) is inferred from **pixel distance** on the page. In engineering drawings, values are always placed near their associated symbols, and devices on the same horizontal line are on the same circuit. Drawing-sync exploits these spatial conventions to reconstruct structure from flat text.

> **In short:** The PDF extractor turns a flat, unstructured PDF into a structured dataset: every word is located spatially, components are identified by IEEE/ANSI patterns, electrical values are assigned to the nearest component by physical distance on the page, and connections are inferred from horizontal proximity.

### What happens to a CAD file (DWG/DXF)

CAD files are fundamentally different from PDFs. Where a PDF contains flat text at pixel positions, a DXF file contains **typed entities** — block references with named attributes, text with insertion points, and line/polyline entities that represent actual wires. The `ezdxf` library reads these entities as Python objects with typed properties, giving drawing-sync structured data to work with instead of raw word positions.

#### What ezdxf actually returns

When we open a DXF file, `ezdxf.readfile()` returns a document object containing entity collections. Here are the key entity types and what they look like in Python:

```python
# INSERT entity — a block reference (component symbol placed on the drawing):
entity.dxftype()           # "INSERT"
entity.dxf.name            # "FUSE_HORIZONTAL" — the block definition name
entity.dxf.insert          # Vec3(145.2, 203.8, 0.0) — where it's placed
for attrib in entity.attribs:  # Named attributes on this instance:
    attrib.dxf.tag         # "FUSE_NUM" → the attribute name
    attrib.dxf.value       # "FU12"     → the attribute value
# This single entity contains: component ID (FU12), component type (fuse),
# rating (10A), and terminal assignments (1, 2) — all as named attributes.

# TEXT entity — a standalone text label:
entity.dxf.text            # "52-L1" — the text content
entity.dxf.insert          # Vec3(245.3, 412.7, 0.0) — insertion point

# MTEXT entity — multiline text (split before matching to prevent cross-line hits):
entity.plain_text()        # "DC PANEL\nDC2\n125V DC"

# LINE entity — a wire segment with start and end points:
entity.dxf.start           # Vec3(145.2, 203.8, 0.0)
entity.dxf.end             # Vec3(245.3, 203.8, 0.0)

# LWPOLYLINE entity — a multi-segment wire path:
list(entity.get_points())  # [(145.2, 203.8), (200.0, 203.8), (245.3, 203.8)]
```

The critical difference from PDF: **block attributes directly identify components**. A block named `FUSE_HORIZONTAL` with attribute `FUSE_NUM = "FU12"` is unambiguously a fuse called FU12. No regex guessing, no spatial inference. Drawing-sync's 11-strategy block extraction pipeline exists to handle the many different attribute conventions used across AutoCAD Electrical block libraries (DEVICE, FUSE_NUM, OUTPUT-#, INPUT-#N, PWR_SUP, TAG, NAME, etc.).

Wire entities (LINE and LWPOLYLINE) provide actual connection paths. Instead of inferring "these two components are on the same row, so they're probably connected" (the PDF approach), drawing-sync traces wire endpoints to nearby component positions using a 5.0-unit tolerance. This produces more accurate connection data.

#### How drawing-sync transforms ezdxf entities into structured objects

When a DWG or DXF file enters `DXFExtractor.extract()` (`extractors/dxf_extractor.py:187`):

1. **DWG conversion (if needed):** If the file is `.dwg`, the extractor calls `convert_dwg_to_dxf()`:
   - Searches for ODA File Converter at `/usr/bin/ODAFileConverter`, `/usr/local/bin/ODAFileConverter`, `~/ODAFileConverter/ODAFileConverter`, `/opt/ODAFileConverter/ODAFileConverter`, or via `which`.
   - Creates a temporary directory via `tempfile.mkdtemp(prefix="dwg2dxf_")`.
   - Runs: `xvfb-run -a ODAFileConverter <input_dir> <output_dir> ACAD2018 DXF 0 1 <filename>` (with a 60-second timeout). `xvfb-run` creates a virtual X11 display so the Qt GUI application runs headless.
   - If the conversion produces a `.dxf` file, that file is used. The temp directory is cleaned up with `shutil.rmtree` after extraction.
   - If ODA is not installed, the extractor falls back to `P&C_PDF/<same_name>.pdf` using the PDFExtractor.

2. **DXF reading:** `ezdxf.readfile(dxf_path)` opens the DXF document. The extractor iterates over **both modelspace and all paper space layouts** (since DWG files converted via ODA typically place entities in paper space, not modelspace).

3. **Text entity extraction:** `_extract_text_entities()` processes every TEXT and MTEXT entity:
   - TEXT: the text string and its insert point (x, y) become a `TextLabel` with category assigned.
   - MTEXT: `entity.plain_text()` gets the content, which is **split into individual lines** before categorization. This prevents regex patterns like `PANEL\s+[A-Z0-9]+` from matching across unrelated lines in multiline text.
   - All text is added to `drawing.raw_text`.

4. **Block reference extraction (the 11-strategy pipeline):** `_extract_block_references()` processes every INSERT entity (block reference). For each block:

   - **Attributes are extracted:** The `attribs` collection on the INSERT entity is read as a dict of tag -> value pairs.

   - **Strategy 1 -- Title block detection:** If the block name contains "TITLEBLOCK" or "TITLE" (or is an "ATT BLK" with DWGNO attribute), metadata is extracted (drawing number, title, type, revision, drawn by, designed by, approved by, date, company) and the block is skipped as a component.

   - **Strategy 2 -- Non-component metadata blocks:** AutoCAD Electrical wire diagram metadata blocks (`WD_M`), revision markers, and tag triangles are skipped.

   - **Attribute text is added to raw_text** for subsequent pattern matching (this is how fuses, switches, panels, and other components embedded in block attributes are detected).

   - **Strategy 3 -- DEVICE attribute:** Blocks with `DEVICE`, `RELAY_MODEL`, or `RELAY_TYPE` attributes are identified as relay/device components. The device value (e.g., `HDV1`) becomes the component ID. Enriched with RELAY, LOC, MOUNT attribute values. The relay device context (`relay_device_id`) is tracked so subsequent sub-components are associated with their parent.

   - **Strategy 4 -- FUSE_NUM attribute:** Blocks with `FUSE_NUM` or `FUSE_ID` are extracted as fuses with their ratings (from `FUSE_SIZE`) and terminal assignments (from `LEFT_TERM`, `RIGHT_TERM`, `TOP_TERM`, `BOTTOM_TERM`). Values are **deduplicated** -- when multiple block instances represent the same physical fuse (e.g., two terminals for `FU12`), duplicate values are merged.

   - **Strategy 5 -- OUTPUT-# attribute:** Blocks with `OUTPUT-#` or `OUTPUT_NUM` are extracted as relay output contacts with terminal and polarity attributes.

   - **Strategy 6 -- INPUT-#N attributes:** Blocks with attributes starting with `INPUT-#` are extracted as relay input contacts. Unused slots (value "IN" alone) are skipped.

   - **Strategy 7 -- PWR_SUP attribute:** Blocks with `PWR_SUP`, `POWER_SUPPLY`, or `PWR` are extracted as power supply components. If a relay device context exists, the component is named `PWR-SUPPLY-<relay_id>`.

   - **Strategy 8 -- Block name keyword matching:** The block name is checked against 50+ known electrical keywords (`FUSE`, `POWER SUPPLY`, `GROUND`, `GND`, `SERIAL`, `ETHERNET`, `FIBER`, `HAND SWITCH`, `DFR`, `LTC`, `METER`, `RTAC`, `CLOCK`, `CISCO`, `ROUTER`, `ATS`, `BATTERY BANK`, `CHARGER`, `UPS`, `MCC`, `SWITCHGEAR`, `VFD`, `PLC`, `RECTIFIER`, `MOV`, `SURGE`, `REGULATOR`, `GENERATOR`, etc.). Each keyword maps to a `ComponentType` and description.

   - **Strategy 9 -- PNL_TERM / RELAY_TERM blocks:** Terminal blocks on panels and relays are extracted with their strip name, terminal number, and description.

   - **Strategy 10 -- TAG/NAME fallback:** If no structured attribute matched, the block's TAG or NAME attribute (or the block name itself) is run through `_extract_component_id()`, which tries all 50+ regex patterns from the PDF extractor.

   - **Strategy 11 -- Unrecognized block logging:** Blocks that didn't match any strategy are logged as extraction warnings (e.g., `"Unrecognized electrical block: CUSTOM_SYMBOL (attributes: VAL1=xxx)"`) for manual review.

5. **Block definitions:** `_extract_block_definitions()` iterates over all block definitions in the document, logging ATTDEF entities (attribute definition templates) as notes.

6. **Dimension entities:** `_extract_dimensions()` reads DIMENSION and ALIGNED_DIMENSION entities for annotated values.

7. **Wire connection extraction:** `_extract_wire_connections()` processes LINE and LWPOLYLINE entities (wires):
   - Wire endpoints (start, end) are collected.
   - Each endpoint is matched to the nearest component label position within a 5.0-unit tolerance using Euclidean distance.
   - If both endpoints match different components, a `Connection` is created between them. This provides richer connection data than PDF spatial inference -- actual wire paths are traced.

8. **Full text pattern matching:** `_parse_text_for_components()` runs all 50+ regex patterns from the PDF extractor against the accumulated raw text. This catches components that were mentioned in text entities but not in block attributes. The text is joined with spaces (not newlines) so patterns can't match across unrelated lines.

9. **Layer extraction:** `_extract_layers()` reads all DXF layer names (layers organize components by type in AutoCAD).

#### What the resulting metadata looks like

After extraction, a DXF-sourced component carries richer metadata than a PDF-sourced one, because block attributes provide structured data directly:

```python
# Raw ezdxf input: INSERT entity with FUSE_NUM="FU12", FUSE_SIZE="10A", terminals
# After drawing-sync extraction:
Component(
    component_id = "FU12",                            # Directly from FUSE_NUM attribute
    component_type = ComponentType.FUSE,              # Inferred from FUSE_NUM attribute presence
    description = "Fuse FU12 (10A)",
    values = [
        ComponentValue(                               # Directly from FUSE_SIZE attribute
            parameter = "current_rating",             #   — no spatial inference needed
            value = "10A",
            unit = "A",
            numeric_value = 10.0
        )
    ],
    connections = [
        Connection(                                   # From wire polyline tracing:
            from_component = "FU12",                  #   LINE entity endpoint at (145.2, 203.8)
            to_component = "HDV1",                    #   matched to FU12 within 5.0 units;
            cable_spec = "",                          #   other endpoint matched to HDV1
            signal_type = ""
        )
    ],
    attributes = {
        "fuse_size": "10A",                           # All block attributes preserved
        "left_terminal": "1",
        "right_terminal": "2",
        "block_name": "FUSE_HORIZONTAL"
    }
)
```

Compare this to the PDF transformation: where PDF extraction required Euclidean distance calculations to associate "10A" with "FU12," DXF extraction reads `FUSE_SIZE = "10A"` directly from the block's attribute collection. Where PDF inferred connections from same-row proximity, DXF traces actual wire polylines. The same `Component` dataclass holds both results — downstream code doesn't know or care whether the data came from a PDF or a DXF.

> **In short:** CAD extraction has two advantages over PDF: structured block attributes give component properties directly (no spatial inference needed), and wire polylines give actual connection paths. The 11-strategy pipeline handles the full range of AutoCAD Electrical block conventions.

### What happens to an Excel file

Excel files serve two roles in the pipeline: drawing indices map drawing numbers to types and titles, while BOMs list every physical component in a junction box or panel. The `openpyxl` library reads XLSX files as workbooks containing sheets of cell values — no electrical knowledge at all.

#### What openpyxl actually returns

openpyxl reads cells as raw Python values — strings, integers, floats, dates, or `None`:

```python
# Raw openpyxl output for a BOM sheet (CT-B1M JUNCTION BOX BOM.xlsx):
# Row 1 (header): ["ITEM#",  "QTY",  "CATALOG#",       "MATERIAL"]
# Row 2 (data):   [1,         2,      "WAGO-281-611",   "TERMINAL BLOCK, DIN RAIL, 600VAC, 30A"]
# Row 3 (data):   [2,         4,      "BUSS-BAF-15",    "FUSE, FAST ACTING, 15A, 600VAC"]
# Row 4 (data):   [3,         1,      "HOFFMAN-A16R126","ENCLOSURE, NEMA 3R, STEEL"]

# Raw openpyxl output for a drawing index sheet (NRE P&C DRAWING INDEX.xlsx):
# Row 1 (header): ["DWG NO",   "DWG TYPE",       "DRAWING TITLE",                "REV"]
# Row 2 (data):   ["EC-001.0", "DRAWING INDEX",  "NRE P&C DRAWING INDEX",        "C"]
# Row 3 (data):   ["EC-100.0", "ONE LINE",       "138KV ONE-LINE DIAGRAM",       "B"]
# Row 4 (data):   ["EC-301.1", "DC SCHEMATIC",   "138KV LINE L1 DC SCHEMATIC",   "C"]
```

These are plain values — openpyxl has no idea that "WAGO-281-611" is a terminal block or that "600VAC" is a voltage rating. Drawing-sync uses two extraction passes to turn these cell values into structured components.

#### How drawing-sync transforms spreadsheet cells into structured objects

When an XLSX like `NRE P&C DRAWING INDEX.xlsx` or `CT-B1M JUNCTION BOX BOM.xlsx` enters `XLSXExtractor.extract()` (`extractors/xlsx_extractor.py`):

1. **Workbook is opened** with `openpyxl.load_workbook(file_path, data_only=True)` (formulas are resolved to values). For legacy `.xls` files (e.g., `NRE FIBER BOM.xls`), `xlrd.open_workbook()` is used instead, with graceful fallback if xlrd is not installed.

2. **BOM detection:** If the filename contains "BOM", the file is flagged for full BOM line-item extraction. This prevents false positives from files like PANEL ELEV SCHEDULES which share similar column structures.

3. **Each worksheet is processed** in two additive passes:

   **Pass 1 — BOM extraction (BOM files only):** The header row is identified by scoring rows for recognizable column keywords (ITEM, QTY, MATERIAL, CATALOG, DEVICE, SIZE — minimum 2 matches). Three sheet types are handled:
   - **MATERIAL sheets** — Each data row becomes a component. Component IDs are derived from the DEVICE# column (if present), the CATALOG# column, or a fallback `BOM-ITEM-N`. Component types are classified from the description text (terminal block, fuse, enclosure, ground bar, cable, consumable, etc.). Every column value is stored in the component's `attributes` dict. Electrical values embedded in descriptions (e.g., "600VAC", "30A") are extracted into the component's `values` list.
   - **NAMEPLATE sheets** — Sub-component designations from the `1st LINE` column (TB1, FU1, etc.) become components with nameplate metadata (size, letter size, text). For multi-sheet BOMs (YARD LIGHT with LT1-LT9), sheet-specific prefixes prevent ID collisions. Components that already exist from device-pattern matching get nameplate attributes merged in.
   - **DEVICE LIST sheets** — IRIG-B style lists where DEVICE# (T-CONN, RESISTOR, COAX) serves as the primary identifier, with item-number suffixes for duplicates.

   **Pass 2 — Exhaustive regex extraction (all XLSX files):** For each row:
   - All cell values are stored as `TextLabel` objects with x=column_index and y=row_index.
   - Row text is searched using **all 50+ pattern groups** from the PDF extractor — IEEE/ANSI device numbers, relay models, instrument transformers, OTHER_PATTERNS (FU, NGR, SW, PANEL), extended equipment (DPAC, DFR, FPP, RTAC, CLOCK, PDC, CISCO, RTR), fuses (BAF, named fuses), terminal blocks (TB, TS), output/input contacts, trip coils, communication modules, and all future-proof patterns (UPS, MCC, SWGR, VFD, PLC, etc.).
   - Electrical values (voltage, current, impedance, MVA, ratios) are extracted and associated with components in the same row.
   - Cable specs and multi-pair cables are added to `cable_schedule`.
   - Cross-references (`NRE-EC-XXX.X` and `E[CPS]-XXX.X` patterns) are added.
   - Terminal block connections (`TB1-5`, `TS3-8`) are parsed into `terminal_blocks`.
   - Cells are categorized using header context: columns named "DRAWING"/"DWG" -> `"reference"`, "DESCRIPTION"/"NAME"/"MATERIAL" -> `"description"`, "QTY"/"QUANTITY" -> `"quantity"`, "CATALOG"/"PART" -> `"reference"`.

#### What the resulting metadata looks like

After both passes complete, a BOM-sourced component carries catalog and quantity metadata that PDF/DXF sources don't have:

```python
# Raw openpyxl input: [1, 2, "WAGO-281-611", "TERMINAL BLOCK, DIN RAIL, 600VAC, 30A"]
# After drawing-sync extraction:
Component(
    component_id = "WAGO-281-611",                    # From CATALOG# column
    component_type = ComponentType.TERMINAL_BLOCK,    # Inferred from "TERMINAL BLOCK" in description
    description = "TERMINAL BLOCK, DIN RAIL, 600VAC, 30A",
    values = [
        ComponentValue("voltage_rating", "600VAC", "V", 600.0),  # Regex-extracted from description
        ComponentValue("current_rating", "30A", "A", 30.0),      # Regex-extracted from description
    ],
    attributes = {
        "catalog_number": "WAGO-281-611",             # From CATALOG# column
        "quantity": 2,                                # From QTY column
        "item_number": 1,                             # From ITEM# column
        "source_sheet": "MATERIAL"                    # Which BOM sheet type
    }
)
```

Drawing index entries produce `DrawingIndexEntry` objects (not `Component` objects) that are used by the classifier to map drawing numbers to types:

```python
# Raw openpyxl input: ["EC-301.1", "DC SCHEMATIC", "138KV LINE L1 DC SCHEMATIC", "C"]
# After drawing-sync extraction:
DrawingIndexEntry(
    drawing_number = "EC-301.1",
    drawing_type = "DC SCHEMATIC",
    drawing_title = "138KV LINE L1 DC SCHEMATIC",
    current_revision = "C",
    design_phase = "90%",                             # From sheet name
    revision_history = [("2025-03-15", "A"), ("2025-06-10", "B"), ("2025-09-20", "C")]
)
```

> **In short:** Excel files get both structured BOM extraction (every line item tracked as a component) and exhaustive regex pattern matching (full parity with PDF/DXF extractors). BOM extraction is purely additive — it runs alongside, not instead of, the regex pass.

### After extraction: Classification

After each file is extracted, the engine classifies the drawing type (`sync_engine.py:123`):

```python
drawing.drawing_type = self.classifier.classify(drawing)
```

The classifier (`drawing_classifier.py:256`) tries three strategies in priority order:

1. **Drawing Index XLSX lookup (confidence 1.0):** The drawing ID is looked up in `_index_map` (populated from the auto-discovered XLSX). Tries exact match, then without file extension, then with/without `NRE-` prefix. If found, the raw type string (e.g., `"DC SCHEMATIC"`) is normalized through `_normalize_type_string()`, which tries exact `DrawingType` enum match, then the `_TITLE_BLOCK_TYPE_MAP` (e.g., `"DC ELEMENTARY"` -> `DC_SCHEMATIC`, `"WIRING DIAGRAM"` -> `PANEL_WIRING`), then partial matching.

2. **Title block DWGTYPE attribute (confidence 0.9):** If the DXF extractor found a DWGTYPE attribute in the title block, it's normalized through the same mapping.

3. **Drawing number series inference (confidence 0.7):** The drawing ID is parsed with `(?:NRE-)?(?:EC-)?(\d{1,3})(?:\.\d+)?` to extract the series number. Ranges: 1-2 = DRAWING_INDEX, 100-104 = ONE_LINE, 110-111 = RELAY_FUNCTIONAL, 120-122 = COMMUNICATION, 200-261 = AC_SCHEMATIC, 300-380 = DC_SCHEMATIC, 400-410 = PANEL_WIRING, 500-557 = CABLE_WIRING, 600-625 = PANEL_LAYOUT, 700-711 = SYSTEM_DIAGRAM.

Every classification decision is recorded to the `decisions` table via the audit trail, including the strategy used, confidence score, outcome, and alternatives considered by other strategies.

After classification, `classifier.enrich_from_index(drawing)` populates the title block with the drawing title and revision from the index, and stores the full index metadata (title, revision history, design phase) in `drawing.index_metadata`.

#### What the classification metadata looks like

Before classification, `drawing.drawing_type` is empty. After classification, it contains a `DrawingType` enum value and the drawing is enriched with index metadata:

```python
# Before classification:
drawing.drawing_type = ""
drawing.index_metadata = {}

# After classification:
drawing.drawing_type = "DC_SCHEMATIC"
drawing.title_block.drawing_name = "138KV LINE L1 DC SCHEMATIC"
drawing.index_metadata = {
    "drawing_title": "138KV LINE L1 DC SCHEMATIC",
    "current_revision": "C",
    "current_revision_date": "2025-09-20",
    "design_phase": "90%",
    "revision_history": [("2025-03-15", "A"), ("2025-06-10", "B"), ("2025-09-20", "C")]
}
```

This classification is critical for everything downstream: authority rules use `drawing_type` to determine which drawing's values win when there are conflicts. A one-line diagram (`ONE_LINE`) is authoritative for voltage ratings. A DC schematic (`DC_SCHEMATIC`) is authoritative for relay wiring and cable specs. Without classification, the system cannot resolve conflicts — it can only detect them.

> **In short:** Each drawing is typed using the most reliable available signal, and the classification decision is recorded with a confidence score for audit traceability.

### After classification: Storage

The engine calls `db.store_drawing(drawing)` (`db.py:182`). Inside a single atomic transaction:

1. **If the drawing already exists in the database:**
   - `_snapshot_drawing()` saves the current component data to the `snapshots` table (component_id -> values_json, attributes_json, plus the file hash and timestamp).
   - `_detect_changes()` compares old vs. new components:
     - New components (in new but not old): logged as `COMPONENT_ADDED`.
     - Removed components (in old but not new): logged as `COMPONENT_REMOVED`.
     - Changed values (same component, different value set): logged as `VALUE_CHANGED`.

2. **The drawing record is upserted** (`INSERT OR REPLACE`) with all metadata: file path, file type, file hash, title block JSON, raw text, notes, cable schedule, terminal blocks, voltage levels, cross-references, drawing type, index metadata, and timestamps.

3. **Old component/connection/label data is deleted** for this drawing (replaced entirely).

4. **Components are inserted** one at a time. Each component row stores: `component_id`, `drawing_id`, `component_type` (enum value string), `description`, `values_json` (array of ComponentValue dicts), `connections_json`, `labels_json`, `drawing_refs_json`, and `attributes_json`.

5. **Connections are inserted** one at a time with from/to components, terminals, cable spec, wire label, and signal type.

6. **Labels are batch-inserted** using `executemany()` for performance (can be 81,000+ labels across all drawings).

7. **`COMMIT`** makes everything permanent. If any step fails, `ROLLBACK` restores the previous state.

#### What the database metadata looks like

After storage, the in-memory `DrawingData` object has been transformed into relational database rows. The `to_dict()` methods on each dataclass serialize complex objects to JSON for storage in TEXT columns:

```sql
-- drawings table: one row per scanned drawing
drawing_id          = 'NRE-EC-301.1'
file_path           = '/home/adminho/mortensen/data/NRE P&C/P&C_PDF/NRE-EC-301.1.pdf'
file_hash           = 'a3f2b8c1...'              -- SHA-256 of file bytes (enables incremental scanning)
drawing_type        = 'DC_SCHEMATIC'
title_block_json    = '{"drawing_number":"NRE-EC-301.1","project_name":"NOMADIC RED EGRET 138/34.5kV",...}'
cross_references_json = '["EC-301.0","EC-307.0","EC-308.0","EC-308.1"]'
voltage_levels_json = '["138kV","34.5kV","125V DC"]'
cable_schedule_json = '["2/C#10","12/C#12SH"]'
terminal_blocks_json = '{"TB6":[71,72,73,74],"TS3":[8,9,10]}'
index_metadata_json = '{"drawing_title":"138KV LINE L1 DC SCHEMATIC","design_phase":"90%",...}'
last_scanned        = '2026-04-06T15:09:34'

-- components table: one row per component per drawing (composite key)
component_id    = '52-L1'
drawing_id      = 'NRE-EC-301.1'
component_type  = '52'                           -- ComponentType.BREAKER enum value
description     = 'AC circuit breaker 52-L1'
values_json     = '[{"parameter":"voltage_rating","value":"138kV","unit":"kV","numeric_value":138.0}]'
connections_json = '[{"from_component":"52-L1","to_component":"86MP","signal_type":"TRIP",...}]'
attributes_json = '{"signal":"TRIP"}'

-- connections table: one row per wiring connection
from_component = '52-L1'
to_component   = '86MP'
to_terminal    = '52T'
cable_spec     = '2/C#10'
signal_type    = 'TRIP'
drawing_id     = 'NRE-EC-301.1'

-- labels table: one row per text label (81,000+ total across all drawings)
text       = '52-L1'
x          = 245.3
y          = 412.7
category   = 'component'
drawing_id = 'NRE-EC-301.1'
```

The OOP model's `to_dict()` methods handle the serialization: `Component.to_dict()` converts its `ComponentValue` list, `Connection` list, and `TextLabel` list into JSON-serializable dicts. `DrawingData.to_dict()` recursively serializes the entire object tree. The inverse `from_dict()` class methods reconstruct the full object hierarchy from stored JSON, enabling round-trip fidelity between Python objects and database rows.

> **In short:** The database gets a complete, atomic snapshot of the drawing's contents. Old data is preserved in snapshots for change tracking. If anything fails mid-write, nothing is corrupted.

---

## Step 2: Mismatch Detection

The pipeline calls `engine.check_mismatches()` which delegates to `MismatchDetector.run_all_checks()` (`mismatch_detector.py:32`).

**First:** All previously active mismatches are marked as resolved (`UPDATE mismatches SET resolved = 1`). This ensures that issues fixed since the last run don't persist as stale alerts.

**Then:** 12 independent checks run in sequence:

### Check 1: Value Mismatches (CRITICAL)

For each component appearing in 2+ drawings, all parameter values are compared. If component `86MS` has `voltage_rating = "34.5kV"` in EC-301.1 but `voltage_rating = "138kV"` in EC-307.0, that's a mismatch. Severity depends on the parameter: `voltage_rating`, `current_rating`, `power_rating` -> CRITICAL; `impedance`, `ratio`, `cable_specification` -> WARNING; everything else -> INFO.

Each mismatch is enriched with authority-based resolution options: the system looks up each drawing's type, queries the authority rules (e.g., ONE_LINE is authoritative for voltage_rating), and marks which drawing's value is the recommended source of truth.

### Check 2: Component Type Consistency (WARNING)

If the same component ID has different `component_type` values in different drawings, it's flagged. This catches naming errors (e.g., the same ID used for a breaker in one drawing and a relay in another).

### Check 3: Cross-Reference Integrity (WARNING)

For each drawing's cross-references (e.g., `EC-307.0` references `EC-301.1`), the system checks whether the referenced drawing exists in the scanned set. Missing references are flagged.

### Check 4: Cable Spec Consistency (WARNING)

Connections between the same pair of components are compared across drawings. If the cable spec differs (e.g., `2/C#10` in one drawing, `4/C#12` in another), it's flagged.

### Check 5: Terminal Block Conflicts (INFO)

Terminal blocks appearing in multiple drawings are compared by terminal count. If the count varies by more than 2x (e.g., 4 terminals in one drawing, 12 in another), it may indicate an incomplete wiring schedule.

### Check 6: Voltage Level Consistency (CRITICAL)

All components with `voltage_rating` values are compared across drawings. Same logic as Check 1 but specifically for voltage. Enriched with authority-based resolution options.

### Check 7: Relay Assignment Consistency (WARNING)

Relay models (like `SEL-451`) are checked for consistent device associations across drawings.

### Check 8: Orphan Component Detection (INFO)

Components appearing in only one drawing, where other drawings cross-reference that drawing, are flagged. They might be missing from the referencing drawings.

### Check 9: Relay-Breaker Trip Path Verification (WARNING)

For each protective relay (50-XX, 51-XX, 87-XX, 21-XX, 67-XX, 81-XX), the system extracts the suffix (e.g., `L1` from `50-L1`), finds the corresponding breaker (`52-L1`), and verifies a connection path exists. It checks:
- Direct connection between relay and breaker.
- Indirect path via a lockout relay (86): relay -> lockout -> breaker.
- Variant component names (e.g., "50-L1 RELAY").

If no path is found, it's flagged. This is the most fundamental protection system check: a relay that can't trip its breaker is a protection failure.

### Check 10: Lockout Relay Completeness (WARNING)

Each lockout relay (86-XX) should have both input connections (from protective relays) and output connections (to breakers). The check flags lockouts with no inputs, no outputs, or neither.

### Check 11: CT-Relay Association (INFO)

Each current transformer (CT) should have at least one connection to a protective relay. CTs without relay connections are flagged.

### Check 12: DC Supply Completeness (INFO)

Protection equipment requires DC power. For each relay, breaker, lockout, RTAC, clock, PDC, network switch, DFR, and LTC, the system checks five potential indicators of DC supply:
1. Connections to/from components containing "DC".
2. Connections using "BREAKER XX" naming (cable drawings).
3. A `.PWR` sub-component exists.
4. Component attributes contain POWER or DC signals.
5. Connections with PWR in the component name.

If none are found, the component is flagged.

**After all checks:** Every mismatch is stored to the `mismatches` table using `INSERT OR REPLACE`. Mismatch IDs are deterministic 24-character SHA-256 hashes of `(component, parameter, check_type)`, so the same issue always gets the same ID across runs.

#### What mismatch metadata looks like

Each detected mismatch becomes a `Mismatch` dataclass stored in the database. Here is a concrete example:

```python
# Component 86MS appears in three drawings with three different voltage ratings:
Mismatch(
    mismatch_id = "a3f2b8c1d5e6...",        # Deterministic SHA-256 of (component, parameter, check)
    severity = AlertSeverity.CRITICAL,        # Voltage mismatch = safety-relevant
    component_id = "86MS",
    parameter = "voltage_rating",
    drawings_involved = ["NRE-EC-301.1", "NRE-EC-307.0", "NRE-EC-308.1"],
    values_found = {
        "NRE-EC-301.1": "34.5kV",            # One drawing says 34.5kV
        "NRE-EC-307.0": "138kV",             # Another says 138kV
        "NRE-EC-308.1": "125V DC"            # A third says 125V DC
    },
    message = "Value mismatch for 86MS: voltage_rating has 3 different values across 3 drawings",
    recommendation = "Verify voltage rating — 3 different values across 3 drawings",
    resolution_options = [{
        "source_drawing": "NRE-EC-307.0",
        "source_type": "DC_SCHEMATIC",
        "value": "138kV",
        "authority_basis": "DC_SCHEMATIC is authoritative for voltage_rating"
    }]
)
```

The `resolution_options` field is what makes mismatches actionable rather than just informational. Instead of saying "these values differ," the system says "use 138kV from EC-307.0 because DC_SCHEMATIC is authoritative for voltage_rating per the authority rules." This guidance comes from the `AuthorityConfig` and `DrawingClassifier` — both of which already ran in earlier steps.

> **In short:** 12 checks compare every component across every drawing. The first 8 catch data inconsistencies. The last 4 verify the protection system's logical integrity. Every mismatch includes a severity, affected drawings, conflicting values, and (where applicable) an authority-based recommendation for which value is correct.

---

## Step 3: Report Generation

The pipeline generates five text reports:

1. **Scan report** (`generate_scan_report`): Drawing-by-drawing table of components, connections, cross-refs, labels, cables extracted. Includes component inventory (per-drawing table of every component with ID, type, values, attributes), extraction completeness notes (`[MISSING DATA]` flags for components without values or connections), extraction warnings (unrecognized blocks), and voltage level summaries.

2. **Mismatch alert report** (`generate_mismatch_report`): Grouped by severity (CRITICAL/WARNING/INFO). Each mismatch shows the component, parameter, conflicting values per drawing, and recommended action. Authority-based resolution options are included where available.

3. **Shared components report** (`generate_shared_components_report`): Table of all components appearing in 2+ drawings with drawing counts and lists.

4. **Dependency graph report** (`generate_dependency_graph_report`): For each drawing, shows outgoing cross-references, incoming cross-references, and shared components with other drawings.

5. **Change log report** (`generate_change_log_report`): Chronological audit trail of all detected changes.

All reports are saved to `output/reports/` with timestamps.

---

## Step 4: Per-Drawing JSON Exports

For every drawing in the database, the engine calls `get_sync_report(drawing_id)` which returns:
- All components in the drawing with their types, values, connections, and attributes.
- Which other drawings share each component (`also_in_drawings`).
- All active mismatches involving this drawing.
- Actionable recommendations (RED FLAG alerts, components in many drawings needing coordinated updates).

Each drawing's sync report is written as a JSON file to `output/drawings/<drawing_id>.json`.

---

## Step 5: Full JSON Export

A comprehensive JSON export is generated containing:
- Generation timestamp and input directory path.
- Database statistics (total drawings, total components, shared components, total connections, total labels, active mismatches).
- Full shared components map (component_id -> list of drawing_ids).
- Full dependency graph (drawing_id -> references, referenced_by, shared_components).

Written to `output/exports/full_export_<timestamp>.json`.

---

## How the Authority Rules Work

The authority system (`drawing_sync/authority.py`) defines 8 rules, each specifying which drawing type is the source of truth for a parameter:

| Parameter | Authority Order (first = highest) |
|---|---|
| `voltage_rating` | ONE_LINE > AC_SCHEMATIC > DC_SCHEMATIC > PANEL_WIRING |
| `current_rating` | ONE_LINE > AC_SCHEMATIC > DC_SCHEMATIC |
| `power_rating` | ONE_LINE (only) |
| `impedance` | ONE_LINE (only) |
| `ratio` (CT/PT/VT/CCVT only) | ONE_LINE > AC_SCHEMATIC |
| `cable_specification` | DC_SCHEMATIC > CABLE_WIRING > PANEL_WIRING |
| `terminal_assignment` | DC_SCHEMATIC > PANEL_WIRING |
| `relay_settings` (relays only) | DC_SCHEMATIC > AC_SCHEMATIC |

When a mismatch is detected, `get_authoritative_drawing()` takes the parameter, the component type, and a dict of `{drawing_id: drawing_type}` for all drawings involved. It walks the authority order for that parameter and returns the first drawing that matches a type in the priority list.

For example, if breaker `52-L1` has voltage `138kV` in `NRE-EC-100.0` (ONE_LINE) and `34.5kV` in `NRE-EC-301.1` (DC_SCHEMATIC), the authority rule says ONE_LINE wins for voltage_rating. The mismatch recommendation will say: "Use value from NRE-EC-100.0 (ONE_LINE) as source of truth."

Rules can be exported to JSON (`authority export -o rules.json`) and loaded from JSON (`AuthorityConfig("rules.json")`) for project-specific customization.

---

## How Propagation Works

The propagation engine (`drawing_sync/propagation_engine.py`) turns authority rules into concrete update actions:

1. **`plan_propagation(component_id)`:** For a single component, gets all instances across drawings, builds a map of `parameter -> {drawing_id: value}`, identifies the authoritative source for each conflicting parameter, and creates `PropagationAction` records for each target that differs from the source.

2. **`plan_all_propagations()`:** Runs `plan_propagation()` for every shared component (those in 2+ drawings).

3. **`apply_propagation(actions, dry_run=False)`:** For each PROPOSED action:
   - If `dry_run`, marks as `DRY_RUN` without modifying the database.
   - Otherwise, calls `db.update_component_value()` which reads the target's `values_json`, finds the matching parameter entry, updates its value, and writes back.
   - Logs to `propagation_log` (action_id, timestamp, source/target drawings, component, parameter, old/new values, authority basis, status).
   - Logs to `change_log` for audit trail.
   - Records a PROPAGATION decision to the `decisions` table.

The `--plan` flag shows what would change. The `--apply` flag (with confirmation prompt unless `--force`) makes the changes.

---

## How the File Watcher Works

The watcher (`drawing_sync/watcher.py`) uses the `watchdog` library:

1. A `DrawingChangeHandler` extends `FileSystemEventHandler`. It watches for `on_modified` and `on_created` events on `.pdf`, `.dxf`, `.dwg`, `.xlsx`, `.xls` files.

2. **Debouncing:** When a file event arrives, the handler checks whether the last event for this file was within 2 seconds. If so, it's ignored. Text editors and CAD tools often trigger multiple save events for a single user save action.

3. **On change:** The handler:
   - Re-extracts the changed file via `engine.scan_single_file()`.
   - Runs full mismatch detection via `engine.check_mismatches()`.
   - Filters to mismatches involving the changed drawing using proper list membership (`drawing_id in m.drawings_involved`), not substring matching.
   - Computes propagation for each component in the drawing: which other drawings are now out of sync.
   - Prints alerts and calls an optional callback function with the event details.

4. The `Observer` from `watchdog` uses OS-level file event APIs: `inotify` on Linux, `FSEvents` on macOS, `ReadDirectoryChanges` on Windows. Near-zero CPU cost.

---

## How `drawing_sync` Is Set Up

### Package Structure

```
drawing_sync/                  # Python package (imported as `drawing_sync`)
    __init__.py                # Sets __version__ = "1.0.0", imports DrawingClassifier
    models.py                  # All dataclasses: ComponentType (52 enum values),
                               #   DrawingType (12 enum values), AlertSeverity,
                               #   ComponentValue, Connection, TextLabel, Component,
                               #   TitleBlock, DrawingIndexEntry, DrawingData, Mismatch
    extractors/
        __init__.py            # Empty (makes it a subpackage)
        pdf_extractor.py       # ~1400 lines. 50+ compiled regex patterns at module
                               #   level. PDFExtractor class with 15 methods.
        dxf_extractor.py       # ~1460 lines. Reuses all patterns from pdf_extractor.
                               #   DXFExtractor class with 11-strategy block pipeline.
                               #   ODA File Converter DWG->DXF conversion.
        xlsx_extractor.py      # ~170 lines. XLSXExtractor class.
    db.py                      # ~710 lines. ComponentDatabase class wrapping SQLite.
                               #   9 tables, 12 indexes, atomic transactions, WAL mode.
    sync_engine.py             # ~505 lines. SyncEngine: central orchestrator.
                               #   Wires together all extractors, DB, classifier,
                               #   authority, propagation, audit, and mismatch detector.
    drawing_classifier.py      # ~500 lines. 3-strategy classification with confidence
                               #   scores. Drawing index XLSX loader. Audit integration.
    authority.py               # ~183 lines. 8 configurable AuthorityRule dataclasses.
                               #   JSON import/export.
    propagation_engine.py      # ~315 lines. PropagationAction dataclass. Plan/apply
                               #   pattern with dry_run support. Audit integration.
    mismatch_detector.py       # ~1060 lines. 12 check methods. Authority-enriched
                               #   resolution options. Protection logic verification.
    cable_export.py            # CableListExporter: formatted XLSX workbook with
                               #   3 sheets (Cable List, Cable Schedule, By Drawing).
    audit.py                   # ~410 lines. DecisionRecord dataclass. AuditTrail class
                               #   with decisions table, decision tree, export report,
                               #   statistics.
    watcher.py                 # ~175 lines. watchdog-based file monitoring with
                               #   debouncing and callback architecture.
    reports.py                 # Text report generators: scan, mismatch, component,
                               #   shared components, dependency graph, change log,
                               #   propagation, decision tree, audit.
    cli.py                     # ~940 lines. argparse CLI with 14 commands.
                               #   Pipeline orchestration in cmd_pipeline().
```

### Database Schema (9 tables)

| Table | Primary Key | Purpose |
|---|---|---|
| `drawings` | `drawing_id` | One row per scanned drawing. File path, hash, type, title block JSON, raw text, notes, cables, terminals, voltages, cross-refs, index metadata, timestamps. |
| `components` | `(component_id, drawing_id)` | One row per component-per-drawing. Type, description, values JSON, connections JSON, labels JSON, drawing refs JSON, attributes JSON. |
| `connections` | `id` (autoincrement) | One row per wiring connection. From/to component and terminal, cable spec, wire label, signal type. |
| `labels` | `id` (autoincrement) | One row per text label. Text, x/y position, category. |
| `snapshots` | `id` (autoincrement) | Historical component snapshots before each update. Drawing ID, timestamp, components JSON, file hash. |
| `mismatches` | `mismatch_id` | Detected inconsistencies. Severity, component, parameter, involved drawings JSON, values found JSON, message, recommendation, timestamps, resolved flag. |
| `change_log` | `id` (autoincrement) | Audit trail of every detected change. Timestamp, drawing, component, change type, old/new values, description. |
| `propagation_log` | `id` (autoincrement) | Every propagation action. Action ID, timestamp, source/target drawings, component, parameter, old/new values, authority basis, status. |
| `decisions` | `decision_id` | Decision audit trail. Timestamp, type (CLASSIFICATION/AUTHORITY/PROPAGATION/MISMATCH_DETECTION), component, drawing, input data JSON, reasoning, confidence (0.0-1.0), outcome, alternatives JSON. |

### Data Flow (Complete)

```
  Input Files                       Auto-Discovery
  ----------                        --------------
  [PDF]  [DWG]  [DXF]  [XLSX]      global_reference/*DRAWING INDEX*.xlsx
    |      |      |       |                    |
    |      |      |       |                    v
    |      |      |       |          DrawingClassifier._load_drawing_index()
    |      |      |       |          -> _index_map, _index_entries (in memory)
    |      |      |       |
    v      v      v       v
   PDFExtractor  DXFExtractor  XLSXExtractor
   (pdfplumber)  (ezdxf)       (openpyxl)
        |            |              |
        |     [DWG -> DXF via      |
        |      ODA + xvfb-run]     |
        |            |              |
        v            v              v
     DrawingData  DrawingData    DrawingData
     {components, {components,   {components,
      connections, connections,   BOM/schedule,
      labels,      labels,       drawing index}
      cables,      cables,
      terminals,   terminals,
      cross-refs,  cross-refs,
      raw_text}    raw_text}
            |           |             |
            +-----------+-------------+
                        |
                        v
               DrawingClassifier.classify()
               (3 strategies, confidence scores)
                        |
                        v
               DrawingClassifier.enrich_from_index()
               (title, revision, index metadata)
                        |
                        v
               ComponentDatabase.store_drawing()
               (atomic transaction: snapshot old, detect
                changes, upsert drawing, clear old data,
                insert components/connections/labels, commit)
                        |
                        v
                   SQLite Database
                   (9 tables, WAL mode, foreign keys)
                        |
          +-------------+-------------+
          |             |             |
          v             v             v
   MismatchDetector  PropagationEngine  AuditTrail
   (12 checks)       (plan/apply)       (decisions DB)
          |             |             |
          v             v             v
   Mismatch objects  PropagationAction  DecisionRecord
   with severity,    objects with       objects with
   resolution        authority basis    confidence,
   options                              reasoning
          |             |             |
          +------+------+------+------+
                 |             |
                 v             v
            CLI Output    File Outputs
           (terminal)    (reports/*.txt,
                          drawings/*.json,
                          exports/*.json,
                          cable_list.xlsx,
                          drawing_sync.db)
```

### Why the Object-Oriented Model Makes Everything Downstream Possible

Every step after extraction — classification, storage, mismatch detection, propagation, auditing, reporting — works because all three extractors produce the same `DrawingData` object. This is the core design principle.

Without the OOP model, you would need separate code paths for "compare PDF-extracted components" vs. "compare DXF-extracted components" vs. "compare XLSX-extracted components." With it, a single `MismatchDetector.run_all_checks()` call works on all 177+ drawings regardless of their source format.

The object hierarchy is designed around the electrical domain, not the file format:

- **`ComponentType` enum** (69 values) maps to IEEE/ANSI C37.2 device function numbers. `ComponentType.BREAKER` is always `"52"`, whether the component came from a PDF regex match, a DXF block attribute, or an XLSX BOM line.
- **`ComponentValue` dataclass** stores both the raw text (`"138kV"`) and the parsed numeric value (`138.0`), enabling both human-readable reports and numeric comparison.
- **`Connection` dataclass** normalizes PDF spatial inference and DXF wire tracing into the same from/to/cable/signal format.
- **`DrawingData.to_dict()`** recursively serializes the entire object tree to JSON, and `from_dict()` reconstructs it. This enables: storage in SQLite JSON columns, export to per-drawing JSON files, full JSON export, and round-trip fidelity between Python objects and database rows.

The `to_dict()` / `from_dict()` cycle is used at every boundary: Python objects → SQLite storage → Python objects → JSON export. No data is lost at any step. A `ComponentValue` stored in the database can be reconstructed with all four fields (`parameter`, `value`, `unit`, `numeric_value`) intact.

### How the Code Adapts the Pipeline

The system is designed to adapt to different input scenarios:

1. **Single file vs. directory:** The CLI detects whether the input is a file or directory. Single-file mode (`scan_single_file_with_results`) runs the full pipeline on one file. Directory mode walks the tree looking for scannable subdirectories.

2. **DWG fallback:** If ODA File Converter isn't installed for DWG files, the extractor automatically looks for the corresponding PDF in `P&C_PDF/` and falls back to PDF extraction. The user gets data either way.

3. **Incremental scanning:** SHA-256 file hashing means only changed files are re-extracted. A 177-drawing set that takes 75 seconds on first scan takes seconds on subsequent runs.

4. **Multi-strategy classification:** If the drawing index XLSX is missing, the classifier falls back to title block attributes, then to drawing number series inference. Each fallback has a lower confidence score.

5. **Extractor polymorphism:** `_extract_file()` dispatches to the correct extractor based on file extension. All extractors return the same `DrawingData` dataclass, so downstream code doesn't care about the source format.

6. **Regex pattern sharing:** The DXF extractor imports all 50+ regex patterns from the PDF extractor module, ensuring both extractors recognize exactly the same component vocabulary.

7. **Authority rule configurability:** The 8 default rules work for standard substation projects. For projects with different conventions, rules can be loaded from a JSON file.

8. **Audit trail integration:** The classifier, propagation engine, and (optionally) mismatch detector record decisions to the audit trail. This is wired through dependency injection: the `SyncEngine` passes the `AuditTrail` instance to the classifier and propagation engine at construction time.

9. **Lazy library loading:** `ezdxf` and `openpyxl` are imported only when first needed (inside the extractor's `_get_ezdxf()` / `_get_openpyxl()` methods). If you only scan PDFs, those libraries are never loaded.

10. **Drawing index auto-discovery:** Instead of requiring an explicit `--index` flag, the engine walks up to 4 directory levels looking for `global_reference/*DRAWING INDEX*.xlsx`. This makes the pipeline work with a single `-i` flag pointing anywhere in the project tree.

---

## What the Pipeline Produces

The pipeline generates seven categories of output. Each serves a different audience and purpose.

```
output/
├── drawing_sync.db                          # The database — persistent system state
├── reports/                                 # Human-readable text reports
│   ├── scan_*.txt                           # What was extracted from each drawing
│   ├── mismatch_*.txt                       # What's inconsistent across drawings
│   ├── shared_components_*.txt              # Which components span multiple drawings
│   ├── dependency_graph_*.txt               # How drawings reference each other
│   └── change_log_*.txt                     # What changed since last scan
├── drawings/                                # Machine-readable per-drawing data
│   └── *.json (one per drawing)             # Components, mismatches, recommendations
└── exports/
    └── full_export_*.json                   # Cross-drawing summary for external tools
```

### 1. SQLite Database (`drawing_sync.db`)

The single source of truth. Nine tables hold everything: drawings (metadata and file hashes), components (every instance across every drawing with values and connections), connections (wiring between components), labels (81,000+ text positions), snapshots (historical data before each update), mismatches (all detected conflicts), change_log (modification history), propagation_log (what was corrected and why), and decisions (audit trail with confidence scores). WAL mode allows concurrent reads during live monitoring. The database can be queried directly with `sqlite3` for ad-hoc analysis.

### 2. Scan Report

What the system extracted from each drawing. Contains a per-drawing table of component/connection/label counts, a component inventory listing every component by ID, type, values, and attributes, extraction completeness flags (`[MISSING DATA]`) for components missing values or connections, extraction warnings for unrecognized CAD blocks, and per-drawing voltage level summaries. This is the first thing to check after a scan — it tells you whether extraction worked correctly.

### 3. Mismatch Report

What's wrong across drawings. All 511+ mismatches grouped by severity: CRITICAL (65 safety-relevant value conflicts like voltage/current mismatches), WARNING (19 naming/reference/protection-logic issues), INFO (427 minor discrepancies). Each entry names the component, the conflicting parameter, the different values found with their drawing IDs, and a recommended action. Where authority rules apply, the report identifies which drawing holds the authoritative value and why. This is the primary deliverable for engineering review.

### 4. Shared Components Report

Which components span multiple drawings. A table of the 265+ components appearing in 2+ drawings, with counts and drawing lists. Answers "if I change this component, which drawings are affected?" at a glance.

### 5. Dependency Graph Report

How drawings connect to each other. For each drawing: outgoing cross-references (drawings it explicitly cites), incoming cross-references (drawings that cite it), and shared components with other drawings. Reveals the web of dependencies that makes manual synchronization so error-prone.

### 6. Per-Drawing JSON Files (`drawings/*.json`)

Machine-readable component data for each drawing. Contains all components with types, values, and connections; which other drawings share each component (`also_in_drawings`); active mismatches involving this drawing; and actionable recommendations. These files are the integration point for external tools — anything that reads JSON can consume per-drawing sync data.

### 7. Full JSON Export (`exports/full_export_*.json`)

The cross-drawing picture in one file. Contains generation timestamp, database statistics (177 drawings, 494 components, 2,813 connections, 511 mismatches), the shared components map (component ID to drawing list), and the full dependency graph (drawing ID to references, referenced-by, and shared components). This is what a dashboard or external monitoring system would consume.

### Outputs from Individual Commands (not part of `pipeline`)

The `cable-list` command produces a formatted Excel workbook with three sheets: a flat cable list (every cable with from/to terminals and specs), a cable schedule summary (aggregated by spec), and a by-drawing view (grouped by source drawing). The `propagate --plan` command produces a list of proposed value updates with authority basis. The `audit --export` command produces a formal compliance document with decision trees. These are generated on demand, not during the standard pipeline run.
