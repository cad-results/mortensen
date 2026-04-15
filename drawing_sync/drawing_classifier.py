"""Drawing Type Detection & Classification.

Classifies drawings into types (ONE_LINE, AC_SCHEMATIC, DC_SCHEMATIC, etc.)
using three strategies in priority order:

1. Drawing Index XLSX lookup (highest confidence)
2. Title block DWGTYPE attribute
3. Drawing number series inference (fallback)
"""

import re
import hashlib
import logging
from datetime import datetime
from typing import Dict, Optional

from .models import DrawingType, DrawingData

logger = logging.getLogger(__name__)

# Mapping from common title block DWGTYPE strings to DrawingType values
_TITLE_BLOCK_TYPE_MAP = {
    "ONE LINE": DrawingType.ONE_LINE.value,
    "ONE-LINE": DrawingType.ONE_LINE.value,
    "ONELINE": DrawingType.ONE_LINE.value,
    "AC SCHEMATIC": DrawingType.AC_SCHEMATIC.value,
    "AC ELEMENTARY": DrawingType.AC_SCHEMATIC.value,
    "DC SCHEMATIC": DrawingType.DC_SCHEMATIC.value,
    "DC ELEMENTARY": DrawingType.DC_SCHEMATIC.value,
    "PANEL WIRING": DrawingType.PANEL_WIRING.value,
    "WIRING DIAGRAM": DrawingType.PANEL_WIRING.value,
    "CABLE WIRING": DrawingType.CABLE_WIRING.value,
    "CABLE": DrawingType.CABLE_WIRING.value,
    "PANEL LAYOUT": DrawingType.PANEL_LAYOUT.value,
    "LAYOUT": DrawingType.PANEL_LAYOUT.value,
    "SYSTEM DIAGRAM": DrawingType.SYSTEM_DIAGRAM.value,
    "DRAWING INDEX": DrawingType.DRAWING_INDEX.value,
    "INDEX": DrawingType.DRAWING_INDEX.value,
    "RELAY FUNCTIONAL": DrawingType.RELAY_FUNCTIONAL.value,
    "FUNCTIONAL": DrawingType.RELAY_FUNCTIONAL.value,
    "LEGEND": DrawingType.LEGEND.value,
    "COMMUNICATION": DrawingType.COMMUNICATION.value,
    "COMM": DrawingType.COMMUNICATION.value,
}

# Drawing number series ranges: (start, end) -> DrawingType value
_NUMBER_SERIES_RANGES = [
    (1, 2, DrawingType.DRAWING_INDEX.value),
    (100, 104, DrawingType.ONE_LINE.value),
    (110, 111, DrawingType.RELAY_FUNCTIONAL.value),
    (120, 122, DrawingType.COMMUNICATION.value),
    (200, 261, DrawingType.AC_SCHEMATIC.value),
    (300, 380, DrawingType.DC_SCHEMATIC.value),
    (400, 410, DrawingType.PANEL_WIRING.value),
    (500, 557, DrawingType.CABLE_WIRING.value),
    (600, 625, DrawingType.PANEL_LAYOUT.value),
    (700, 711, DrawingType.SYSTEM_DIAGRAM.value),
]


class DrawingClassifier:
    """Classifies drawings into types using multiple strategies."""

    def __init__(self, drawing_index_path: Optional[str] = None, audit=None):
        self._index_map: Dict[str, str] = {}
        self._index_entries: Dict[str, 'DrawingIndexEntry'] = {}
        self.audit = audit  # Optional AuditTrail instance
        if drawing_index_path:
            self._load_drawing_index(drawing_index_path)

    def _load_drawing_index(self, xlsx_path: str):
        """Load a drawing index XLSX with rich data: types, titles, revisions.

        Handles multi-phase sheets (30%, 60%, 90%) — prefers the highest
        design phase when the same drawing appears in multiple sheets.
        """
        try:
            import openpyxl
        except ImportError:
            logger.warning("openpyxl not installed — cannot read drawing index XLSX")
            return

        try:
            wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
        except Exception as e:
            logger.warning("Failed to open drawing index %s: %s", xlsx_path, e)
            return

        from .models import DrawingIndexEntry

        # Sort worksheets so higher design phases are processed last (override earlier)
        phase_order = {"30": 0, "60": 1, "90": 2}
        sheets = list(wb.worksheets)
        sheets.sort(key=lambda ws: next(
            (v for k, v in phase_order.items() if k in (ws.title or "")), -1
        ))

        for ws in sheets:
            # Detect design phase from sheet name
            design_phase = ""
            for pct in ("90%", "60%", "30%"):
                if pct.replace("%", "") in (ws.title or ""):
                    design_phase = pct
                    break

            header_row = None
            dwg_col = None
            type_col = None
            title_col = None
            header_cells_cache = None  # Cache header row for rev column detection
            rev_col_pairs = []

            # Single pass: read all rows, find header, then process data rows
            all_rows = []
            for row in ws.iter_rows():
                all_rows.append(list(row))

            # Search first 20 rows for header columns
            for row in all_rows[:20]:
                for cell in row:
                    val = str(cell.value or "").upper().strip()
                    if not val:
                        continue
                    # Only consider short cells as column headers (skip long descriptive text)
                    if len(val) > 30:
                        continue
                    if dwg_col is None and ("DWG" in val or "DRAWING" in val):
                        if "NO" in val or "NUM" in val or "#" in val or val in ("DWG", "DRAWING"):
                            dwg_col = cell.column - 1
                            header_row = cell.row
                            header_cells_cache = row
                    if type_col is None and "TYPE" in val:
                        type_col = cell.column - 1
                        if header_row is None:
                            header_row = cell.row
                            header_cells_cache = row
                    if title_col is None and "TITLE" in val:
                        title_col = cell.column - 1

                if dwg_col is not None and type_col is not None:
                    break

            # Lenient fallback
            if dwg_col is None or type_col is None:
                for row in all_rows[:20]:
                    for i, cell in enumerate(row):
                        val = str(cell.value or "").upper().strip()
                        if len(val) > 30:
                            continue
                        if dwg_col is None and ("DWG" in val or "DRAWING" in val):
                            dwg_col = i
                            header_row = cell.row
                            header_cells_cache = row
                        if type_col is None and "TYPE" in val:
                            type_col = i
                            if header_row is None:
                                header_row = cell.row
                                header_cells_cache = row
                    if dwg_col is not None and type_col is not None:
                        break

            if dwg_col is None or type_col is None or header_row is None:
                continue

            # Find revision columns from cached header row
            if header_cells_cache:
                for ci, cell in enumerate(header_cells_cache):
                    val = str(cell.value or "").upper().strip()
                    if "REV" in val:
                        if ci > 0:
                            prev_val = str(header_cells_cache[ci - 1].value or "").upper().strip()
                            if "DATE" in prev_val or not prev_val:
                                rev_col_pairs.append((ci - 1, ci))
                            else:
                                rev_col_pairs.append((None, ci))
                        else:
                            rev_col_pairs.append((None, ci))

            # Read data rows (after header)
            for row in all_rows[header_row:]:
                cells = list(row)
                if dwg_col >= len(cells) or type_col >= len(cells):
                    continue

                dwg_val = str(cells[dwg_col].value or "").strip()
                type_val = str(cells[type_col].value or "").strip()

                if not dwg_val or not type_val:
                    continue

                # Extract title
                title_val = ""
                if title_col is not None and title_col < len(cells):
                    title_val = str(cells[title_col].value or "").strip()

                # Extract revision history
                revision_history = []
                current_rev = ""
                current_rev_date = ""
                for date_ci, rev_ci in rev_col_pairs:
                    rev_str = ""
                    date_str = ""
                    if rev_ci < len(cells):
                        rev_str = str(cells[rev_ci].value or "").strip()
                    if date_ci is not None and date_ci < len(cells):
                        raw = cells[date_ci].value
                        if raw is not None:
                            if hasattr(raw, 'strftime'):
                                date_str = raw.strftime("%Y-%m-%d")
                            else:
                                date_str = str(raw).strip()
                    if rev_str:
                        revision_history.append({
                            "revision": rev_str,
                            "date": date_str,
                        })
                        # Last non-empty revision is the current one
                        current_rev = rev_str
                        current_rev_date = date_str

                entry = DrawingIndexEntry(
                    drawing_number=dwg_val,
                    drawing_type=type_val,
                    drawing_title=title_val,
                    current_revision=current_rev,
                    current_revision_date=current_rev_date,
                    design_phase=design_phase,
                    revision_history=revision_history,
                )

                # Populate _index_map (backward compat) and _index_entries
                dwg_id = dwg_val
                self._index_map[dwg_id] = type_val
                self._index_entries[dwg_id] = entry

                # Also store with common prefix variations
                if not dwg_id.startswith("NRE-"):
                    alt = f"NRE-{dwg_id}"
                    self._index_map[alt] = type_val
                    self._index_entries[alt] = entry
                if dwg_id.startswith("NRE-"):
                    alt = dwg_id.replace("NRE-", "", 1)
                    self._index_map[alt] = type_val
                    self._index_entries[alt] = entry

        try:
            wb.close()
        except Exception:
            pass

        logger.info(
            "Loaded %d entries (%d unique drawings) from drawing index",
            len(self._index_map), len(set(e.drawing_number for e in self._index_entries.values())),
        )

    def classify(self, drawing: DrawingData) -> str:
        """Classify a drawing using three strategies in priority order.

        Returns a DrawingType value string.
        """
        strategy_used = "unknown"
        confidence = 0.0
        alternatives = []

        # Strategy 1: Drawing index XLSX lookup
        result = self.classify_from_index(drawing.drawing_id)
        if result != DrawingType.UNKNOWN.value:
            strategy_used = "index_lookup"
            confidence = 1.0
            # Collect alternatives from other strategies
            alt_tb = self.classify_from_title_block(drawing.title_block)
            alt_num = self.classify_from_number(drawing.drawing_id)
            if alt_tb != DrawingType.UNKNOWN.value and alt_tb != result:
                alternatives.append({"type": alt_tb, "strategy": "title_block"})
            if alt_num != DrawingType.UNKNOWN.value and alt_num != result:
                alternatives.append({"type": alt_num, "strategy": "number_series"})
            self._record_classification(drawing, result, strategy_used, confidence, alternatives)
            return result

        # Strategy 2: Title block DWGTYPE attribute
        result = self.classify_from_title_block(drawing.title_block)
        if result != DrawingType.UNKNOWN.value:
            strategy_used = "title_block"
            confidence = 0.9
            alt_num = self.classify_from_number(drawing.drawing_id)
            if alt_num != DrawingType.UNKNOWN.value and alt_num != result:
                alternatives.append({"type": alt_num, "strategy": "number_series"})
            self._record_classification(drawing, result, strategy_used, confidence, alternatives)
            return result

        # Strategy 3: Drawing number series inference
        result = self.classify_from_number(drawing.drawing_id)
        if result != DrawingType.UNKNOWN.value:
            strategy_used = "number_series"
            confidence = 0.7
        else:
            strategy_used = "none"
            confidence = 0.0

        self._record_classification(drawing, result, strategy_used, confidence, alternatives)
        return result

    def enrich_from_index(self, drawing) -> None:
        """Populate title_block and index_metadata from the drawing index."""
        if not self._index_entries:
            return

        # Try matching by drawing_id with various prefix forms
        entry = self._index_entries.get(drawing.drawing_id)
        if not entry:
            base = drawing.drawing_id.rsplit(".", 1)[0] if "." in drawing.drawing_id else drawing.drawing_id
            entry = self._index_entries.get(base)
        if not entry and drawing.drawing_id.startswith("NRE-"):
            entry = self._index_entries.get(drawing.drawing_id[4:])
        if not entry and not drawing.drawing_id.startswith("NRE-"):
            entry = self._index_entries.get(f"NRE-{drawing.drawing_id}")

        if not entry:
            return

        # Enrich title_block with index data (don't overwrite extractor data)
        if not drawing.title_block.drawing_name and entry.drawing_title:
            drawing.title_block.drawing_name = entry.drawing_title
        if not drawing.title_block.revision and entry.current_revision:
            drawing.title_block.revision = entry.current_revision

        # Store full index metadata
        drawing.index_metadata = entry.to_dict()

    def _record_classification(
        self, drawing: DrawingData, result: str,
        strategy_used: str, confidence: float, alternatives: list,
    ):
        """Record a CLASSIFICATION decision to the audit trail (if available)."""
        if not self.audit:
            return
        try:
            from .audit import DecisionRecord
            self.audit.record_decision(DecisionRecord(
                decision_id=self._make_id(drawing.drawing_id, "classify"),
                timestamp=datetime.now().isoformat(),
                decision_type="CLASSIFICATION",
                component_id="",
                drawing_id=drawing.drawing_id,
                input_data={
                    "drawing_id": drawing.drawing_id,
                    "file_type": drawing.file_type,
                    "title_block_type": drawing.title_block.drawing_type if drawing.title_block else "",
                    "strategy_used": strategy_used,
                },
                reasoning=f"Classified {drawing.drawing_id} as {result} using {strategy_used}",
                confidence=confidence,
                outcome=result,
                alternatives=alternatives,
            ))
        except Exception:
            pass  # Never fail the main operation

    def classify_from_index(self, drawing_id: str) -> str:
        """Classify using the drawing index XLSX lookup.

        Returns DrawingType value string, or UNKNOWN if not found.
        """
        if not self._index_map:
            return DrawingType.UNKNOWN.value

        # Try exact match first
        type_str = self._index_map.get(drawing_id)
        if type_str:
            return self._normalize_type_string(type_str)

        # Try without file extension
        base_id = drawing_id.rsplit(".", 1)[0] if "." in drawing_id else drawing_id
        type_str = self._index_map.get(base_id)
        if type_str:
            return self._normalize_type_string(type_str)

        # Try with/without NRE- prefix
        if drawing_id.startswith("NRE-"):
            type_str = self._index_map.get(drawing_id[4:])
        else:
            type_str = self._index_map.get(f"NRE-{drawing_id}")

        if type_str:
            return self._normalize_type_string(type_str)

        return DrawingType.UNKNOWN.value

    def classify_from_title_block(self, title_block) -> str:
        """Classify using the title block DWGTYPE attribute.

        Returns DrawingType value string, or UNKNOWN if not available.
        """
        dwg_type = getattr(title_block, 'drawing_type', '') or ''
        if not dwg_type.strip():
            return DrawingType.UNKNOWN.value

        return self._normalize_type_string(dwg_type)

    def classify_from_number(self, drawing_id: str) -> str:
        """Classify using drawing number series inference.

        Parses NRE-EC-XXX.Y format to extract the series number XXX
        and maps it to a drawing type.

        Returns DrawingType value string, or UNKNOWN if pattern doesn't match.
        """
        # Match patterns like NRE-EC-301.0, EC-301, NRE-EC-301, 301.0, etc.
        match = re.search(r'(?:NRE-)?(?:EC-)?(\d{1,3})(?:\.\d+)?', drawing_id)
        if not match:
            return DrawingType.UNKNOWN.value

        try:
            series_num = int(match.group(1))
        except ValueError:
            return DrawingType.UNKNOWN.value

        for start, end, dtype in _NUMBER_SERIES_RANGES:
            if start <= series_num <= end:
                return dtype

        return DrawingType.UNKNOWN.value

    def classify_all(self, db) -> Dict[str, str]:
        """Batch classify all drawings in the database and update drawing_type column.

        Returns dict of drawing_id -> drawing_type.
        """
        results = {}
        drawing_ids = db.get_all_drawing_ids()

        for drawing_id in drawing_ids:
            row = db.get_drawing(drawing_id)
            if not row:
                continue

            # Build a minimal DrawingData for classification
            drawing = DrawingData(
                drawing_id=drawing_id,
                file_path=row.get("file_path", ""),
                file_type=row.get("file_type", ""),
            )

            # Restore title block if available
            import json
            tb_json = row.get("title_block_json", "{}")
            if tb_json:
                from .models import TitleBlock
                try:
                    drawing.title_block = TitleBlock.from_dict(json.loads(tb_json))
                except Exception:
                    pass

            # Classify
            dtype = self.classify(drawing)
            results[drawing_id] = dtype

            # Update in database
            try:
                db.conn.execute(
                    "UPDATE drawings SET drawing_type = ? WHERE drawing_id = ?",
                    (dtype, drawing_id),
                )
            except Exception as e:
                logger.warning("Failed to update drawing_type for %s: %s", drawing_id, e)

        db.conn.commit()
        return results

    @staticmethod
    def _make_id(*parts) -> str:
        """Generate a deterministic decision ID (24-char SHA-256 prefix)."""
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    @staticmethod
    def _normalize_type_string(type_str: str) -> str:
        """Normalize a type string to a DrawingType value.

        Tries exact DrawingType match, then looks up in the title block map,
        then falls back to UNKNOWN.
        """
        upper = type_str.upper().strip()

        # Try exact DrawingType value match
        for dt in DrawingType:
            if dt.value == upper:
                return dt.value

        # Try the title block type map
        mapped = _TITLE_BLOCK_TYPE_MAP.get(upper)
        if mapped:
            return mapped

        # Try partial matching in the map
        for key, value in _TITLE_BLOCK_TYPE_MAP.items():
            if key in upper or upper in key:
                return value

        return DrawingType.UNKNOWN.value
