"""PDF Drawing Extractor.

Extracts all electrical components, values, connections, labels,
terminal blocks, cable specs, and cross-references from PDF drawings
of electrical one-line diagrams and schematic drawings.
"""

import re
import os
from typing import Optional
import pdfplumber

from ..models import (
    Component, ComponentType, ComponentValue, Connection,
    TextLabel, TitleBlock, DrawingData,
)

# ─── IEEE/ANSI device number patterns ───────────────────────────────
DEVICE_PATTERNS = {
    ComponentType.BREAKER:        r'\b(52-[A-Z0-9]+(?:\.[A-Z0-9.]+)?)\b',
    ComponentType.OVERCURRENT:    r'\b(50-[A-Z0-9]+(?:\.[A-Z0-9.]+)?)\b',
    ComponentType.OVERCURRENT_TIME: r'\b(51-[A-Z0-9]+(?:\.[A-Z0-9.]+)?)\b',
    ComponentType.LOCKOUT:        r'\b(86[A-Z]{1,3}[0-9]*)\b',
    ComponentType.DIFFERENTIAL:   r'\b(87-[A-Z0-9]+(?:\.[A-Z0-9.]+)?)\b',
    ComponentType.DISCONNECT:     r'\b(89-[A-Z0-9]+(?:\.[A-Z0-9.]+)?)\b',
    ComponentType.DISTANCE:       r'\b(21-[A-Z0-9]+)\b',
    ComponentType.SYNC_CHECK:     r'\b(25-[A-Z0-9]+)\b',
    ComponentType.UNDERVOLTAGE:   r'\b(27-[A-Z0-9]+)\b',
    ComponentType.OVERVOLTAGE:    r'\b(59-[A-Z0-9]+)\b',
    ComponentType.DIRECTIONAL_OC: r'\b(67-[A-Z0-9]+)\b',
    ComponentType.FREQUENCY:      r'\b(81-[A-Z0-9]+)\b',
    ComponentType.RECLOSER:       r'\b(79-[A-Z0-9]+)\b',
}

# Relay model patterns (SEL, GE, ABB, etc.)
# Note: GE and BECKWITH also match space-separated format (e.g., "GE L90", "BECKWITH M-2001D")
RELAY_PATTERNS = [
    r'\b(SEL-\d{3,4}[A-Z0-9]{0,5})\b',
    r'\b(GE-[A-Z0-9]+)\b',
    r'\bGE\s+(L\d+[A-Z]?)\b',              # "GE L90" → captures "L90", caller prefixes "GE-"
    r'\b(ABB-[A-Z0-9]+)\b',
    r'\b(BECKWITH-[A-Z0-9]+)\b',
    r'\bBECKWITH\s+(M-?\d+[A-Z]*)\b',       # "BECKWITH M-2001D" → captures "M-2001D", caller prefixes "BECKWITH-"
    r'\b(TESLA-?\d+)\b',                     # TESLA-4000 (BESS controller)
]
# Indices of patterns that need manufacturer prefix added to the capture group
_RELAY_PREFIX_MAP = {2: "GE-", 4: "BECKWITH-"}

# Instrument transformer patterns
INSTRUMENT_TX_PATTERNS = {
    ComponentType.CT:   r'\b(CT-[A-Z0-9]+(?:\.[A-Z0-9.]+)?)\b',
    ComponentType.PT:   r'\b(PT-[A-Z0-9]+(?:\.[A-Z0-9.]+)?)\b',
    ComponentType.VT:   r'\b(VT-[A-Z0-9]+(?:\.[A-Z0-9.]+)?)\b',
    ComponentType.CCVT: r'\b(CCVT-[A-Z0-9]+(?:\.[A-Z0-9.]+)?)\b',
}

# Other component patterns
OTHER_PATTERNS = {
    ComponentType.FUSE:          r'\b(FU\d+[A-Z]?)\b',
    ComponentType.NGR:           r'\b(NGR-[A-Z0-9]+)\b',
    ComponentType.SWITCH:        r'\b(SW-\d+)\b',
    ComponentType.PANEL:         r'\b((?:DC\s*)?PANEL\s+(?:\d+[A-Z]?|[A-Z]{1,3}\d+))\b',
}

# ─── Extended component patterns (controllers, I/O, communication) ─────
# Note: DPAC pattern uses a normalizing group — callers should normalize
# the matched ID to canonical form (e.g., "DPAC-1" for both "DPAC1" and "DPAC-1")
DPAC_PATTERN = r'\b(DPAC)-?(\d+[A-Z]?)\b'  # Two groups: prefix and number
EXTENDED_PATTERNS = {
    ComponentType.CABLE:   r'\b(SEL-C\d{2,})\b',             # SEL communication cables/modules
}

# Output/Input contact patterns
OUTPUT_PATTERN = r'\b(OUT\d{2,})\b'
INPUT_PATTERN = r'\b(IN\d{3,})\b'        # 3+ digits to avoid false positives

# Circuit identifier pattern
CIRCUIT_PATTERN = r'\bCIRCUIT\s+(\d+)\b'

# ─── Substation equipment patterns (Tier 1 & 2) ───────────────────────
# Digital fault recorders and their modules
# Normalizing pattern like DPAC: "DFR1" and "DFR-1" both → "DFR-1"
DFR_NORM_PATTERN = r'\b(DFR)-?(\d+)\b'   # Two groups: prefix and number
CM_PATTERN = r'\b(CM-\d+)\b'              # Current modules (for DFR)
VM_PATTERN = r'\b(VM-\d+)\b'              # Voltage modules (for DFR)

# Custody / revenue meters
CMET_PATTERN = r'\b(CMET-\d+)\b'

# Fiber patch panels — normalizing: "FPP2" and "FPP-2" both → "FPP-2"
FPP_NORM_PATTERN = r'\b(FPP)-?(\d+)\b'   # Two groups: prefix and number

# Hand switches (manual control)
HS_PATTERN = r'\b(HS\d{2,})\b'

# IRIG-B time sync distribution tees
TEE_PATTERN = r'\b(TEE\d+[A-Z]?)\b'

# Breaker auxiliary contacts (52A = normally open, 52B = normally closed)
AUX_CONTACT_PATTERN = r'\b(52[AB])\b'

# Watt sensing links
WSL_PATTERN = r'\b(WSL-\d+[PS]?)\b'

# Power supplies (standalone units, not "POWER SUPPLY" block keyword)
PS_PATTERN = r'\b(PS\d+)\b'

# Relay panels
RP_PATTERN = r'\b(RP\d+)\b'

# Electronic trip modules and load tap changers
ETM_PATTERN = r'\b(ETM-T\d+)\b'
LTC_PATTERN = r'\b(LTC-T\d+)\b'

# Station service transformer
SST_PATTERN = r'\b(SST)\b'

# AC panel pattern (complements existing DC PANEL pattern)
AC_PANEL_PATTERN = r'\b(AC\s*PANEL\s*AC\d+)\b'

# Named fuses (FU-CL, FU-SST — existing FU\d+ misses these)
NAMED_FUSE_PATTERN = r'\b(FU-[A-Z]{2,})\b'

# BESS (battery energy storage system) feeders
BESS_PATTERN = r'\b(BESS-AUX\d+)\b'

# IRIG-B time synchronization signal reference
IRIG_PATTERN = r'\b(IRIG-B)\b'

# Multi-pair shielded cables (e.g., "3PR #24SH", "4PR #24")
MULTIPAIR_CABLE_PATTERN = r'\b(\d+PR\s*#\d+SH?)\b'

# ─── Trip coils, lockout outputs, DC supply, comm modules ─────────────
# Trip coils (TC1, TC2 — breaker trip/close coils)
TC_PATTERN = r'\b(TC[12])\b'

# Lockout relay output signal
LOR_PATTERN = r'\b(LOR)\b'

# DC voltage supply inputs to relays
VDC_PATTERN = r'\b(VDC\d+)\b'

# Communication processor modules (SEL PM68, SM68)
COMM_MODULE_PATTERN = r'\b([PS]M68)\b'

# Multi-ratio CT class identifiers (C100, C200, C400, C800, etc.)
CT_CLASS_PATTERN = r'\b(C[12348]00)\b'

# Standalone terminal block identifiers (TB1, TB2, TS3 — without terminal suffix)
STANDALONE_TB_PATTERN = r'\b(TB\d{1,2})\b(?!-)'
STANDALONE_TS_PATTERN = r'\b(TS\d{1,2})\b(?!-)'

# ─── Substation automation & communication equipment ─────────────
# Real-time automation controller (SEL-3555)
RTAC_PATTERN = r'\b(RTAC)\b'

# GPS clock / time synchronization device (SEL-2407)
CLOCK_PATTERN = r'\b(CLOCK)\b'

# Phasor data concentrator (SEL-3355)
PDC_PATTERN = r'\b(PDC)\b'

# Network switches (CISCO IE-4010, CISCO CGR-2010)
CISCO_PATTERN = r'\b(CISCO\s+(?:IE-?\d+|CGR-?\d+))\b'

# Network routers (RTR-1, RTR-2)
RTR_PATTERN = r'\b(RTR-\d+)\b'

# BAF-type fuses (BAF-3, BAF-5, BAF-10, BAF-30 — bussman/bussmann fuses)
BAF_PATTERN = r'\b(BAF-\d+)\b'

# Power strip (panel peripheral)
POWERSTRIP_PATTERN = r'\b(POWER\s*STRIP)\b'

# Automatic transfer switch
ATS_PATTERN = r'\b(ATS)\b'

# Remote terminal unit
RTU_PATTERN = r'\b(RTU)\b'

# HVAC units in panel buildings
HVAC_PATTERN = r'\b(HVAC-\d+)\b'

# Bushing potential devices (instrument transformer tap)
BP_PATTERN = r'\b(BP-\d+)\b'

# Energy management system equipment (EMS-RTAC, EMS-SW-3, etc.)
EMS_PATTERN = r'\b(EMS-[A-Z]+-?\d*)\b'

# Battery banks and chargers (multi-word: "BATTERY BANK #1", "BATTERY CHARGER #2")
BATTERY_EQUIP_PATTERN = r'\b(BATTERY\s+(?:BANK|CHARGER)\s+#\d+)\b'

# ─── Future-proof equipment patterns ─────────────────────────────
# These may not appear in the current drawing set but are common
# substation/industrial equipment that could appear in future drawings.

# Uninterruptible power supply (UPS-1, UPS-2, or standalone UPS)
UPS_PATTERN = r'\b(UPS-?\d*)\b'

# Motor control center
MCC_PATTERN = r'\b(MCC-?[A-Z0-9]*)\b'

# Switchgear
SWGR_PATTERN = r'\b(SWGR-?[A-Z0-9]*)\b'

# Variable frequency drive
VFD_PATTERN = r'\b(VFD-?\d+)\b'

# Programmable logic controller
PLC_PATTERN = r'\b(PLC-?\d*)\b'

# Rectifier
RECTIFIER_PATTERN = r'\b(RECT-?\d*)\b'

# Metal oxide varistor / surge arrester (MOV, LA-1, SA-1)
MOV_PATTERN = r'\b(MOV-?\d*)\b'
ARRESTER_PATTERN = r'\b([LS]A-\d+)\b'

# Surge protective device
SPD_PATTERN = r'\b(SPD-?\d*)\b'

# Voltage regulator
REGULATOR_PATTERN = r'\b(REG-?\d*)\b'

# Generator
GENERATOR_PATTERN = r'\b(GEN-?\d*)\b'

# ─── Electrical value patterns ──────────────────────────────────────
VOLTAGE_PATTERN = r'(\d+\.?\d*)\s*(kV|V|KV)\s*(AC|DC)?'
CURRENT_PATTERN = r'(\d+\.?\d*)\s*(A|kA|MA)\b'
IMPEDANCE_PATTERN = r'(\d+\.?\d*)\s*(%Z|%|[Oo][Hh][Mm]|Ω)'
MVA_PATTERN = r'(\d+(?:/\d+)*)\s*(MVA|KVA|kVA)'
RATIO_PATTERN = r'(\d+(?:/\d+)*:\d+(?::\d+)?)'  # CT/PT ratios like "700/1200:1:1"

# ─── Connection patterns ────────────────────────────────────────────
CABLE_PATTERN = r'(\d+/C\s*#\d+\w*)'
TERMINAL_PATTERN = r'\b(T[SB]\d+-\d+)\b'
TERMINAL_BLOCK_PATTERN = r'\b(TB\d+-\d+)\b'
WIRE_LABEL_PATTERN = r'\b(\d+[A-Z]\d+[A-Z]?\d*)\b'

# ─── Cross-reference patterns ──────────────────────────────────────
DRAWING_REF_PATTERN = r'\b(E[CPS]-\d{3}\.\d)\b'

# ─── Signal type keywords ──────────────────────────────────────────
SIGNAL_KEYWORDS = [
    "TRIP", "CLOSE", "OPEN", "SCADA", "BF TRIP", "LOCKOUT",
    "ALARM", "BLOCK", "INITIATE", "TRANSFER", "INTERLOCK",
    "METERING", "STATUS", "CONTROL", "POWER", "AUX",
]


class PDFExtractor:
    """Extracts comprehensive component and connection data from PDF drawings."""

    def __init__(self, x_tolerance=3, y_tolerance=3):
        self.x_tolerance = x_tolerance
        self.y_tolerance = y_tolerance

    def extract(self, pdf_path: str) -> DrawingData:
        """Extract all data from a PDF drawing file."""
        drawing_id = os.path.splitext(os.path.basename(pdf_path))[0]

        drawing = DrawingData(
            drawing_id=drawing_id,
            file_path=pdf_path,
            file_type="pdf",
        )

        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                self._extract_page(page, page_num, drawing)

        # Post-process: build connection graph from proximity analysis
        self._build_connection_graph(drawing)
        # Deduplicate and clean
        self._deduplicate_components(drawing)

        return drawing

    def _extract_page(self, page, page_num: int, drawing: DrawingData):
        """Extract all data from a single PDF page."""
        # 1. Extract raw text
        text = page.extract_text() or ""
        drawing.raw_text += text + "\n"

        # 2. Extract positioned words for spatial analysis
        words = page.extract_words(
            keep_blank_chars=True,
            x_tolerance=self.x_tolerance,
            y_tolerance=self.y_tolerance,
        )

        # 3. Extract all text labels with positions
        for w in words:
            w_text = w.get("text", "").strip()
            if not w_text:
                continue
            label = TextLabel(
                text=w_text,
                x=w.get("x0", 0),
                y=w.get("top", 0),
            )
            label.category = self._categorize_label(label.text)
            drawing.all_labels.append(label)

        # 4. Extract components by type
        self._extract_device_components(text, drawing)
        self._extract_relay_components(text, drawing)
        self._extract_instrument_transformers(text, drawing)
        self._extract_other_components(text, drawing)

        # 5. Extract electrical values and associate with components
        self._extract_electrical_values(text, words, drawing)

        # 6. Extract cable schedules
        self._extract_cables(text, drawing)

        # 7. Extract terminal block connections
        self._extract_terminal_blocks(text, drawing)

        # 8. Extract cross-references
        self._extract_cross_references(text, drawing)

        # 9. Extract title block
        self._extract_title_block(text, drawing)

        # 10. Extract notes
        self._extract_notes(text, drawing)

        # 11. Extract voltage levels
        self._extract_voltage_levels(text, drawing)

        # 12. Extract tables (schedules, BOM data)
        self._extract_tables(page, drawing)

    def _categorize_label(self, text: str) -> str:
        """Categorize a text label by its content."""
        text_upper = text.upper().strip()

        # Component ID patterns
        for pattern in DEVICE_PATTERNS.values():
            if re.search(pattern, text_upper):
                return "component"
        for pattern in RELAY_PATTERNS:
            if re.search(pattern, text_upper):
                return "component"
        for pattern in INSTRUMENT_TX_PATTERNS.values():
            if re.search(pattern, text_upper):
                return "component"
        for pattern in OTHER_PATTERNS.values():
            if re.search(pattern, text_upper):
                return "component"
        for pattern in EXTENDED_PATTERNS.values():
            if re.search(pattern, text_upper):
                return "component"
        if re.search(DPAC_PATTERN, text_upper):
            return "component"
        if re.search(OUTPUT_PATTERN, text_upper) or re.search(INPUT_PATTERN, text_upper):
            return "component"
        # Substation equipment patterns
        for pattern in (DFR_NORM_PATTERN, CM_PATTERN, VM_PATTERN, CMET_PATTERN,
                        FPP_NORM_PATTERN, HS_PATTERN, TEE_PATTERN, AUX_CONTACT_PATTERN,
                        WSL_PATTERN, PS_PATTERN, RP_PATTERN, ETM_PATTERN,
                        LTC_PATTERN, SST_PATTERN, AC_PANEL_PATTERN, NAMED_FUSE_PATTERN,
                        BESS_PATTERN, TC_PATTERN, LOR_PATTERN, VDC_PATTERN,
                        COMM_MODULE_PATTERN, CT_CLASS_PATTERN,
                        RTAC_PATTERN, CLOCK_PATTERN, PDC_PATTERN,
                        CISCO_PATTERN, RTR_PATTERN, BAF_PATTERN, POWERSTRIP_PATTERN,
                        ATS_PATTERN, RTU_PATTERN, HVAC_PATTERN, BP_PATTERN,
                        EMS_PATTERN, BATTERY_EQUIP_PATTERN,
                        UPS_PATTERN, MCC_PATTERN, SWGR_PATTERN, VFD_PATTERN,
                        PLC_PATTERN, RECTIFIER_PATTERN, MOV_PATTERN,
                        ARRESTER_PATTERN, SPD_PATTERN, REGULATOR_PATTERN,
                        GENERATOR_PATTERN):
            if re.search(pattern, text_upper):
                return "component"

        # Electrical values
        if re.search(VOLTAGE_PATTERN, text_upper) or re.search(CURRENT_PATTERN, text_upper):
            return "value"

        # Cable specs
        if re.search(CABLE_PATTERN, text) or re.search(MULTIPAIR_CABLE_PATTERN, text_upper):
            return "cable"

        # Drawing references
        if re.search(DRAWING_REF_PATTERN, text):
            return "reference"

        # Terminal blocks
        if re.search(TERMINAL_PATTERN, text):
            return "terminal"

        # Notes
        if text_upper.startswith("NOTE") or text_upper.startswith("REV"):
            return "note"

        # Title block keywords
        title_kw = ["DRAWN BY", "DESIGNED BY", "REVIEWED BY", "DATE", "PROJECT",
                     "DRAWING", "ENGINEER", "STAMP"]
        if any(kw in text_upper for kw in title_kw):
            return "title"

        # Signal keywords (use word boundary check to avoid false positives
        # like "CASCADE" matching "SCADA" or "STRIP" matching "TRIP")
        if text_upper in SIGNAL_KEYWORDS or any(
            re.search(r'\b' + re.escape(s) + r'\b', text_upper)
            for s in SIGNAL_KEYWORDS
        ):
            return "signal"

        return "text"

    def _extract_device_components(self, text: str, drawing: DrawingData):
        """Extract IEEE/ANSI device function number components."""
        for comp_type, pattern in DEVICE_PATTERNS.items():
            for match in re.finditer(pattern, text):
                comp_id = match.group(1)
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=comp_type,
                        description=self._describe_device(comp_type, comp_id),
                    )

    def _extract_relay_components(self, text: str, drawing: DrawingData):
        """Extract protective relay models (SEL, GE, ABB, etc.)."""
        for idx, pattern in enumerate(RELAY_PATTERNS):
            for match in re.finditer(pattern, text):
                comp_id = match.group(1)
                # Space-separated patterns (GE L90, BECKWITH M-2001D) need prefix
                prefix = _RELAY_PREFIX_MAP.get(idx, "")
                if prefix:
                    comp_id = prefix + comp_id
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=ComponentType.RELAY,
                        description=f"Protective relay {comp_id}",
                    )

    def _extract_instrument_transformers(self, text: str, drawing: DrawingData):
        """Extract CTs, PTs, VTs, CCVTs."""
        for comp_type, pattern in INSTRUMENT_TX_PATTERNS.items():
            for match in re.finditer(pattern, text):
                comp_id = match.group(1)
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=comp_type,
                        description=self._describe_device(comp_type, comp_id),
                    )

    def _extract_other_components(self, text: str, drawing: DrawingData):
        """Extract fuses, NGRs, switches, panels, etc."""
        for comp_type, pattern in OTHER_PATTERNS.items():
            for match in re.finditer(pattern, text):
                comp_id = match.group(1).strip()
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=comp_type,
                        description=f"{comp_type.name} {comp_id}",
                    )

        # Lockout relays need special handling (86XX patterns)
        for match in re.finditer(r'\b(86[A-Z]{1,4}\d?)\b', text):
            comp_id = match.group(1)
            if len(comp_id) > 2 and comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.LOCKOUT,
                    description=f"Lockout relay {comp_id}",
                )

        # Battery/DC supply panels
        for match in re.finditer(r'\b(DC\s*PANEL\s*[A-Z]*\d*)\b', text):
            comp_id = match.group(1).strip()
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.PANEL,
                    description=f"DC panel {comp_id}",
                )

        # Breaker identifiers in "BREAKER XX" format
        for match in re.finditer(r'BREAKER\s+(\d+)', text):
            breaker_num = match.group(1)
            comp_id = f"BREAKER-{breaker_num}"
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.BREAKER,
                    description=f"DC breaker #{breaker_num}",
                )

        # DPAC controllers (normalize "DPAC1" and "DPAC-1" to "DPAC-1")
        for match in re.finditer(DPAC_PATTERN, text):
            comp_id = f"DPAC-{match.group(2)}"  # Canonical form: DPAC-N
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.RELAY,
                    description=f"DPAC controller {comp_id}",
                )

        # Extended patterns (SEL communication cables)
        for comp_type, pattern in EXTENDED_PATTERNS.items():
            for match in re.finditer(pattern, text):
                comp_id = match.group(1).strip()
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=comp_type,
                        description=f"{comp_type.name} {comp_id}",
                    )

        # Output contacts (OUT101, OUT102, etc.)
        for match in re.finditer(OUTPUT_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.RELAY,
                    description=f"Relay output contact {comp_id}",
                )

        # Input contacts (IN101, IN102, etc.) — 3+ digits to avoid false positives
        for match in re.finditer(INPUT_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.RELAY,
                    description=f"Relay input contact {comp_id}",
                )

        # Circuit identifiers ("CIRCUIT 16" → circuit breaker in panel)
        for match in re.finditer(CIRCUIT_PATTERN, text):
            circuit_num = match.group(1)
            comp_id = f"CIRCUIT-{circuit_num}"
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.BREAKER,
                    description=f"DC panel circuit breaker #{circuit_num}",
                )

        # ─── Tier 1: Major substation equipment ───────────────────────

        # Digital fault recorders (normalize DFR1 → DFR-1)
        for match in re.finditer(DFR_NORM_PATTERN, text):
            comp_id = f"DFR-{match.group(2)}"
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.DFR,
                    description=f"Digital fault recorder {comp_id}",
                )

        # DFR current modules
        for match in re.finditer(CM_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.DFR,
                    description=f"DFR current module {comp_id}",
                )

        # DFR voltage modules
        for match in re.finditer(VM_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.DFR,
                    description=f"DFR voltage module {comp_id}",
                )

        # Custody / revenue meters
        for match in re.finditer(CMET_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.CUSTODY_METER,
                    description=f"Custody meter {comp_id}",
                )

        # Fiber patch panels (normalize FPP2 → FPP-2)
        for match in re.finditer(FPP_NORM_PATTERN, text):
            comp_id = f"FPP-{match.group(2)}"
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.FIBER_PATCH,
                    description=f"Fiber patch panel {comp_id}",
                )

        # Hand switches
        for match in re.finditer(HS_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.HAND_SWITCH,
                    description=f"Hand switch {comp_id}",
                )

        # IRIG-B distribution tees
        for match in re.finditer(TEE_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.UNKNOWN,
                    description=f"IRIG-B distribution tee {comp_id}",
                )

        # Breaker auxiliary contacts
        for match in re.finditer(AUX_CONTACT_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.BREAKER,
                    description=f"Breaker auxiliary contact {comp_id}",
                )

        # ─── Tier 2: Significant equipment ────────────────────────────

        # Watt sensing links
        for match in re.finditer(WSL_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.CT,
                    description=f"Watt sensing link {comp_id}",
                )

        # Power supplies
        for match in re.finditer(PS_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.POWER_SUPPLY,
                    description=f"Power supply {comp_id}",
                )

        # Relay panels
        for match in re.finditer(RP_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.PANEL,
                    description=f"Relay panel {comp_id}",
                )

        # Electronic trip modules
        for match in re.finditer(ETM_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.RELAY,
                    description=f"Electronic trip module {comp_id}",
                )

        # Load tap changers
        for match in re.finditer(LTC_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.LTC,
                    description=f"Load tap changer {comp_id}",
                )

        # Station service transformer
        for match in re.finditer(SST_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.TRANSFORMER,
                    description="Station service transformer",
                )

        # AC panels
        for match in re.finditer(AC_PANEL_PATTERN, text):
            comp_id = match.group(1).strip()
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.PANEL,
                    description=f"AC panel {comp_id}",
                )

        # Named fuses (FU-CL, FU-SST)
        for match in re.finditer(NAMED_FUSE_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.FUSE,
                    description=f"Fuse {comp_id}",
                )

        # BESS feeders
        for match in re.finditer(BESS_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.BATTERY,
                    description=f"Battery energy storage feeder {comp_id}",
                )

        # IRIG-B time synchronization signal
        for match in re.finditer(IRIG_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.UNKNOWN,
                    description="IRIG-B time synchronization signal",
                )

        # Multi-pair shielded cables
        for match in re.finditer(MULTIPAIR_CABLE_PATTERN, text):
            spec = match.group(1)
            if spec not in drawing.cable_schedule:
                drawing.cable_schedule.append(spec)

        # ─── Trip coils, lockout outputs, DC supply, comm modules ─────

        # Trip coils (TC1, TC2)
        for match in re.finditer(TC_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.BREAKER,
                    description=f"Trip coil {comp_id}",
                )

        # Lockout relay output (LOR)
        for match in re.finditer(LOR_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.LOCKOUT,
                    description="Lockout relay output",
                )

        # DC voltage supply inputs (VDC1, VDC2)
        for match in re.finditer(VDC_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.POWER_SUPPLY,
                    description=f"DC voltage supply {comp_id}",
                )

        # Communication processor modules (PM68, SM68)
        for match in re.finditer(COMM_MODULE_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.RELAY,
                    description=f"Communication module {comp_id}",
                )

        # Multi-ratio CT class identifiers (C800, C400)
        for match in re.finditer(CT_CLASS_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.CT,
                    description=f"CT ratio class {comp_id}",
                )

        # Standalone terminal blocks (TB1, TB2 — without terminal suffix)
        for match in re.finditer(STANDALONE_TB_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.TERMINAL_BLOCK,
                    description=f"Terminal block {comp_id}",
                )

        # Standalone test switches (TS1, TS3 — without terminal suffix)
        for match in re.finditer(STANDALONE_TS_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.TERMINAL_BLOCK,
                    description=f"Test switch {comp_id}",
                )

        # ─── Substation automation & communication equipment ─────────

        # Real-time automation controller
        for match in re.finditer(RTAC_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.RTAC,
                    description="Real-time automation controller",
                )

        # GPS clock
        for match in re.finditer(CLOCK_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.GPS_CLOCK,
                    description="GPS clock / time synchronization device",
                )

        # Phasor data concentrator
        for match in re.finditer(PDC_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.PDC,
                    description="Phasor data concentrator",
                )

        # CISCO network switches
        for match in re.finditer(CISCO_PATTERN, text):
            comp_id = match.group(1).strip()
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.NETWORK_SWITCH,
                    description=f"Network switch {comp_id}",
                )

        # Network routers
        for match in re.finditer(RTR_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.ROUTER,
                    description=f"Network router {comp_id}",
                )

        # BAF fuses
        for match in re.finditer(BAF_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.FUSE,
                    description=f"BAF fuse {comp_id}",
                )

        # Power strips (panel peripheral)
        for match in re.finditer(POWERSTRIP_PATTERN, text):
            comp_id = match.group(1).strip()
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.POWER_SUPPLY,
                    description="Power strip",
                )

        # Automatic transfer switch
        for match in re.finditer(ATS_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.ATS,
                    description="Automatic transfer switch",
                )

        # Remote terminal unit
        for match in re.finditer(RTU_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.RTAC,
                    description="Remote terminal unit",
                )

        # HVAC units
        for match in re.finditer(HVAC_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.UNKNOWN,
                    description=f"HVAC unit {comp_id}",
                )

        # Bushing potential devices
        for match in re.finditer(BP_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.PT,
                    description=f"Bushing potential device {comp_id}",
                )

        # EMS equipment
        for match in re.finditer(EMS_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.RTAC,
                    description=f"Energy management system {comp_id}",
                )

        # Battery banks and chargers
        for match in re.finditer(BATTERY_EQUIP_PATTERN, text):
            comp_id = match.group(1).strip()
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.BATTERY,
                    description=comp_id,
                )

        # ─── Future-proof equipment ──────────────────────────────────

        # UPS
        for match in re.finditer(UPS_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.UPS,
                    description=f"Uninterruptible power supply {comp_id}",
                )

        # MCC
        for match in re.finditer(MCC_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.MCC,
                    description=f"Motor control center {comp_id}",
                )

        # Switchgear
        for match in re.finditer(SWGR_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.SWGR,
                    description=f"Switchgear {comp_id}",
                )

        # VFD
        for match in re.finditer(VFD_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.VFD,
                    description=f"Variable frequency drive {comp_id}",
                )

        # PLC
        for match in re.finditer(PLC_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.PLC,
                    description=f"Programmable logic controller {comp_id}",
                )

        # Rectifier
        for match in re.finditer(RECTIFIER_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.RECTIFIER,
                    description=f"Rectifier {comp_id}",
                )

        # MOV / Surge arrester
        for match in re.finditer(MOV_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.MOV,
                    description=f"Metal oxide varistor {comp_id}",
                )

        # Lightning/surge arrester (LA-1, SA-1)
        for match in re.finditer(ARRESTER_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.MOV,
                    description=f"Surge arrester {comp_id}",
                )

        # SPD
        for match in re.finditer(SPD_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.SPD,
                    description=f"Surge protective device {comp_id}",
                )

        # Voltage regulator
        for match in re.finditer(REGULATOR_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.REGULATOR,
                    description=f"Voltage regulator {comp_id}",
                )

        # Generator
        for match in re.finditer(GENERATOR_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.GENERATOR,
                    description=f"Generator {comp_id}",
                )

    def _extract_electrical_values(self, text: str, words: list, drawing: DrawingData):
        """Extract all electrical values and associate them with nearby components."""
        # Voltage values
        for match in re.finditer(VOLTAGE_PATTERN, text):
            numeric = float(match.group(1))
            unit = match.group(2)
            ac_dc = match.group(3) or ""
            value_str = f"{match.group(1)}{unit} {ac_dc}".strip()
            cv = ComponentValue(
                parameter="voltage_rating",
                value=value_str,
                unit=unit,
                numeric_value=numeric if unit.upper() == "KV" else numeric / 1000,
            )
            # Associate with nearest component using spatial proximity
            self._associate_value_with_component(cv, match.group(0), words, drawing)

        # Current values
        for match in re.finditer(CURRENT_PATTERN, text):
            numeric = float(match.group(1))
            unit = match.group(2)
            cv = ComponentValue(
                parameter="current_rating",
                value=f"{match.group(1)}{unit}",
                unit=unit,
                numeric_value=numeric,
            )
            self._associate_value_with_component(cv, match.group(0), words, drawing)

        # Impedance values
        for match in re.finditer(IMPEDANCE_PATTERN, text):
            cv = ComponentValue(
                parameter="impedance",
                value=match.group(0),
                unit=match.group(2),
                numeric_value=float(match.group(1)),
            )
            self._associate_value_with_component(cv, match.group(0), words, drawing)

        # MVA ratings
        for match in re.finditer(MVA_PATTERN, text):
            cv = ComponentValue(
                parameter="power_rating",
                value=match.group(0),
                unit=match.group(2),
            )
            self._associate_value_with_component(cv, match.group(0), words, drawing)

        # CT/PT ratios
        for match in re.finditer(RATIO_PATTERN, text):
            cv = ComponentValue(
                parameter="ratio",
                value=match.group(1),
                unit="ratio",
            )
            self._associate_value_with_component(cv, match.group(0), words, drawing)

    def _associate_value_with_component(
        self, value: ComponentValue, value_text: str,
        words: list, drawing: DrawingData,
    ):
        """Associate an electrical value with the nearest component using spatial proximity."""
        # Find the position of this value text in the word list
        # Use the numeric portion of the value for matching to avoid
        # substring false positives (e.g., "kV" matching "kVA")
        value_pos = None
        numeric_part = value_text.split()[0] if value_text else ""
        for w in words:
            w_text = w.get("text", "")
            if w_text == value_text or (numeric_part and w_text.startswith(numeric_part)):
                value_pos = (w.get("x0", 0), w.get("top", 0))
                break

        if not value_pos:
            return

        # Find nearest component label
        best_comp = None
        best_dist = float("inf")

        for label in drawing.all_labels:
            if label.category != "component":
                continue
            dx = label.x - value_pos[0]
            dy = label.y - value_pos[1]
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_comp = label.text

        if best_comp and best_comp in drawing.components:
            # Avoid duplicate values
            existing = [v.value for v in drawing.components[best_comp].values]
            if value.value not in existing:
                drawing.components[best_comp].values.append(value)

    def _extract_cables(self, text: str, drawing: DrawingData):
        """Extract all cable specifications."""
        for match in re.finditer(CABLE_PATTERN, text):
            cable_spec = match.group(1)
            if cable_spec not in drawing.cable_schedule:
                drawing.cable_schedule.append(cable_spec)

        # Also look for cable types like CAT5E, FIBER, etc.
        for match in re.finditer(r'\b(CAT[56]E?|MM\s*FIBER|SM\s*FIBER|COAX)\b', text, re.IGNORECASE):
            cable_type = match.group(1).upper()
            if cable_type not in drawing.cable_schedule:
                drawing.cable_schedule.append(cable_type)

        # Cable labels with component prefix (e.g., "DC2.50-L1.PWR")
        for match in re.finditer(r'\b([A-Z]+\d*\.\d{2}-[A-Z]\d+\.[A-Z]+)\b', text):
            cable_label = match.group(1)
            if cable_label not in drawing.cable_schedule:
                drawing.cable_schedule.append(cable_label)

    def _extract_terminal_blocks(self, text: str, drawing: DrawingData):
        """Extract terminal block connections."""
        for match in re.finditer(r'\b(TB\d+)-(\d+)\b', text):
            tb_id = match.group(1)
            terminal = match.group(2)
            if tb_id not in drawing.terminal_blocks:
                drawing.terminal_blocks[tb_id] = []
            if terminal not in drawing.terminal_blocks[tb_id]:
                drawing.terminal_blocks[tb_id].append(terminal)

        # Test switch terminals (TS blocks)
        for match in re.finditer(r'\b(TS\d+)-(\d+)\b', text):
            ts_id = match.group(1)
            terminal = match.group(2)
            tb_key = f"TS-{ts_id}"
            if tb_key not in drawing.terminal_blocks:
                drawing.terminal_blocks[tb_key] = []
            if terminal not in drawing.terminal_blocks[tb_key]:
                drawing.terminal_blocks[tb_key].append(terminal)

    def _extract_cross_references(self, text: str, drawing: DrawingData):
        """Extract cross-references to other drawings."""
        for match in re.finditer(DRAWING_REF_PATTERN, text):
            ref = match.group(1)
            # Don't include self-references
            own_ref = drawing.drawing_id.replace("NRE-", "")
            if ref != own_ref and ref not in drawing.cross_references:
                drawing.cross_references.append(ref)

    def _extract_title_block(self, text: str, drawing: DrawingData):
        """Extract title block information."""
        tb = drawing.title_block
        tb.drawing_number = drawing.drawing_id

        # Project name
        for match in re.finditer(r'(?:PROJECT[:\s]*)(.*?)(?:\n|$)', text, re.IGNORECASE):
            if match.group(1).strip():
                tb.project_name = match.group(1).strip()

        # Voltage class from drawing content
        if "138kV" in text and "34.5kV" in text:
            tb.project_name = tb.project_name or "138/34.5kV SUBSTATION"

        # Drawn/designed by
        for match in re.finditer(r'DRAWN BY[:\s]*([\w\s]+?)(?:\n|$)', text, re.IGNORECASE):
            tb.drawn_by = match.group(1).strip()
        for match in re.finditer(r'DESIGNED BY[:\s]*([\w\s]+?)(?:\n|$)', text, re.IGNORECASE):
            tb.designed_by = match.group(1).strip()

        # Company
        if "MORTENSON" in text.upper():
            tb.company = "Mortenson Engineering Services, Inc."

        # Look for specific project identifiers
        if "NOMADIC RED EGRET" in text.upper():
            tb.project_location = "TEXAS CITY, TEXAS"
            tb.project_name = tb.project_name or "NOMADIC RED EGRET 138/34.5kV SUBSTATION"

    def _extract_notes(self, text: str, drawing: DrawingData):
        """Extract drawing notes."""
        # Find numbered notes
        for match in re.finditer(
            r'(?:NOTE[S]?\s*:?\s*\n?)?(\d+\.\s+[^\n]+(?:\n\s+[^\n]+)*)',
            text, re.IGNORECASE,
        ):
            note = match.group(1).strip()
            if len(note) > 10 and note not in drawing.notes:
                drawing.notes.append(note)

        # Find standalone NOTES sections
        note_section = re.search(
            r'NOTES?\s*:\s*\n((?:.*\n)*?)(?=\n\s*\n|\Z)',
            text, re.IGNORECASE,
        )
        if note_section:
            for line in note_section.group(1).split("\n"):
                line = line.strip()
                if line and len(line) > 5 and line not in drawing.notes:
                    drawing.notes.append(line)

    def _extract_voltage_levels(self, text: str, drawing: DrawingData):
        """Extract all voltage levels found in the drawing."""
        for match in re.finditer(r'(\d+\.?\d*)\s*(kV|KV)', text):
            vl = f"{match.group(1)}kV"
            if vl not in drawing.voltage_levels:
                drawing.voltage_levels.append(vl)

        for match in re.finditer(r'(\d+)\s*V\s*(AC|DC)', text):
            vl = f"{match.group(1)}V {match.group(2)}"
            if vl not in drawing.voltage_levels:
                drawing.voltage_levels.append(vl)

    def _extract_tables(self, page, drawing: DrawingData):
        """Extract any tables from the PDF page (schedules, BOMs)."""
        try:
            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue
                # Look for component-related table data
                for row in table:
                    if not row:
                        continue
                    row_text = " ".join(str(c) for c in row if c)
                    # Check for component references in tables
                    for pattern in DEVICE_PATTERNS.values():
                        for match in re.finditer(pattern, row_text):
                            comp_id = match.group(1)
                            if comp_id in drawing.components:
                                drawing.components[comp_id].attributes["in_table"] = True
        except Exception:
            pass  # Some pages don't have extractable tables

    def _build_connection_graph(self, drawing: DrawingData):
        """Build connection graph from spatial proximity of labels."""
        # Group labels by vertical position (same "row" = likely connected)
        # This works for schematic drawings where connected items are on same line
        component_labels = [l for l in drawing.all_labels if l.category == "component"]
        terminal_labels = [l for l in drawing.all_labels if l.category == "terminal"]
        signal_labels = [l for l in drawing.all_labels if l.category == "signal"]
        cable_labels = [l for l in drawing.all_labels if l.category == "cable"]

        # For each component, find nearby terminals, signals, and other components
        for comp_label in component_labels:
            comp_id = comp_label.text.strip()
            if comp_id not in drawing.components:
                continue

            comp = drawing.components[comp_id]

            # Find nearby terminals (within ~50 units vertically)
            nearby_terminals = [
                t for t in terminal_labels
                if abs(t.y - comp_label.y) < 50
            ]
            for t in nearby_terminals:
                conn = Connection(
                    from_component=comp_id,
                    from_terminal=t.text,
                    to_component="",
                    to_terminal="",
                )
                comp.connections.append(conn)

            # Find nearby signal types
            nearby_signals = [
                s for s in signal_labels
                if abs(s.y - comp_label.y) < 30 and abs(s.x - comp_label.x) < 200
            ]
            for s in nearby_signals:
                comp.attributes.setdefault("signals", [])
                if s.text not in comp.attributes["signals"]:
                    comp.attributes["signals"].append(s.text)

            # Find nearby cable specs
            nearby_cables = [
                c for c in cable_labels
                if abs(c.y - comp_label.y) < 50
            ]
            for c in nearby_cables:
                for conn in comp.connections:
                    if not conn.cable_spec:
                        conn.cable_spec = c.text
                        break

            # Find nearby cross-references
            ref_labels = [
                l for l in drawing.all_labels
                if l.category == "reference"
                and abs(l.y - comp_label.y) < 40
                and abs(l.x - comp_label.x) < 200
            ]
            for r in ref_labels:
                if r.text not in comp.drawing_refs:
                    comp.drawing_refs.append(r.text)

        # Build inter-component connections from proximity
        for i, c1 in enumerate(component_labels):
            for c2 in component_labels[i + 1:]:
                # Components on same row and close = likely connected
                if abs(c1.y - c2.y) < 15 and abs(c1.x - c2.x) < 300:
                    conn = Connection(
                        from_component=c1.text.strip(),
                        from_terminal="",
                        to_component=c2.text.strip(),
                        to_terminal="",
                    )
                    drawing.connections.append(conn)

    def _deduplicate_components(self, drawing: DrawingData):
        """Clean up and deduplicate extracted data."""
        # Remove components that are clearly just noise
        to_remove = []
        for comp_id in drawing.components:
            # Empty or whitespace-only
            if not comp_id or not comp_id.strip():
                to_remove.append(comp_id)
                continue
            # Pure numbers (not a valid component ID)
            if comp_id.isdigit():
                to_remove.append(comp_id)
                continue
            # Single character
            if len(comp_id) < 2:
                to_remove.append(comp_id)
        for r in to_remove:
            del drawing.components[r]

        # Deduplicate cross_references
        drawing.cross_references = sorted(set(drawing.cross_references))
        drawing.cable_schedule = sorted(set(drawing.cable_schedule))
        drawing.voltage_levels = sorted(set(drawing.voltage_levels))

    @staticmethod
    def _describe_device(comp_type: ComponentType, comp_id: str) -> str:
        """Generate human-readable description for a device."""
        descriptions = {
            ComponentType.BREAKER: "AC circuit breaker",
            ComponentType.OVERCURRENT: "Instantaneous overcurrent relay",
            ComponentType.OVERCURRENT_TIME: "Time overcurrent relay",
            ComponentType.LOCKOUT: "Lockout relay",
            ComponentType.DIFFERENTIAL: "Differential protective relay",
            ComponentType.DISCONNECT: "Disconnect switch",
            ComponentType.DISTANCE: "Distance relay",
            ComponentType.SYNC_CHECK: "Synchronizing check device",
            ComponentType.UNDERVOLTAGE: "Undervoltage relay",
            ComponentType.OVERVOLTAGE: "Overvoltage relay",
            ComponentType.DIRECTIONAL_OC: "Directional overcurrent relay",
            ComponentType.FREQUENCY: "Frequency relay",
            ComponentType.RECLOSER: "AC reclosing relay",
            ComponentType.CT: "Current transformer",
            ComponentType.PT: "Potential transformer",
            ComponentType.VT: "Voltage transformer",
            ComponentType.CCVT: "Coupling capacitor voltage transformer",
        }
        base = descriptions.get(comp_type, comp_type.name)
        return f"{base} {comp_id}"
