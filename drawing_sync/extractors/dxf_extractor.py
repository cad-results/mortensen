"""DXF/DWG Drawing Extractor.

Extracts structured component data from DXF files using ezdxf.
DWG files require conversion to DXF first (via ODA File Converter or LibreDWG).

DXF provides richer structured data than PDF:
- Block references with attributes (component symbols with properties)
- Layer organization (components grouped by layer)
- Text entities with exact positions
- Dimension entities with values
- Polyline/line entities (wiring connections)
"""

import re
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from ..models import (
    Component, ComponentType, ComponentValue, Connection,
    TextLabel, TitleBlock, DrawingData,
)

# Reuse patterns from PDF extractor
from .pdf_extractor import (
    DEVICE_PATTERNS, RELAY_PATTERNS, _RELAY_PREFIX_MAP,
    INSTRUMENT_TX_PATTERNS,
    OTHER_PATTERNS, EXTENDED_PATTERNS, DPAC_PATTERN, SIGNAL_KEYWORDS,
    VOLTAGE_PATTERN, CURRENT_PATTERN, CABLE_PATTERN,
    TERMINAL_PATTERN, DRAWING_REF_PATTERN,
    OUTPUT_PATTERN, INPUT_PATTERN, CIRCUIT_PATTERN,
    DFR_NORM_PATTERN, CM_PATTERN, VM_PATTERN, CMET_PATTERN,
    FPP_NORM_PATTERN,
    HS_PATTERN, TEE_PATTERN, AUX_CONTACT_PATTERN, WSL_PATTERN,
    PS_PATTERN, RP_PATTERN, ETM_PATTERN, LTC_PATTERN, SST_PATTERN,
    AC_PANEL_PATTERN, NAMED_FUSE_PATTERN, BESS_PATTERN, IRIG_PATTERN,
    MULTIPAIR_CABLE_PATTERN,
    TC_PATTERN, LOR_PATTERN, VDC_PATTERN, COMM_MODULE_PATTERN,
    CT_CLASS_PATTERN, STANDALONE_TB_PATTERN, STANDALONE_TS_PATTERN,
    RTAC_PATTERN, CLOCK_PATTERN, PDC_PATTERN,
    CISCO_PATTERN, RTR_PATTERN, BAF_PATTERN, POWERSTRIP_PATTERN,
    ATS_PATTERN, RTU_PATTERN, HVAC_PATTERN, BP_PATTERN,
    EMS_PATTERN, BATTERY_EQUIP_PATTERN,
    UPS_PATTERN, MCC_PATTERN, SWGR_PATTERN, VFD_PATTERN,
    PLC_PATTERN, RECTIFIER_PATTERN, MOV_PATTERN, ARRESTER_PATTERN,
    SPD_PATTERN, REGULATOR_PATTERN, GENERATOR_PATTERN,
)

# ─── Block name patterns that represent electrical components ─────────
# Maps block name substrings (uppercase) to (ComponentType, description)
ELECTRICAL_BLOCK_KEYWORDS = {
    "FUSE": (ComponentType.FUSE, "Fuse"),
    "POWER SUPPLY": (ComponentType.BATTERY, "Power supply"),
    "POWERSTRIP": (ComponentType.POWER_SUPPLY, "Power strip"),
    "POWER STRIP": (ComponentType.POWER_SUPPLY, "Power strip"),
    "GROUND": (ComponentType.GROUND, "Ground connection"),
    "GND": (ComponentType.GROUND, "Ground connection"),
    "SERIAL": (ComponentType.UNKNOWN, "Serial communication port"),
    "ETHERNET": (ComponentType.UNKNOWN, "Ethernet communication port"),
    "FIBER": (ComponentType.UNKNOWN, "Fiber optic port"),
    "HAND SWITCH": (ComponentType.HAND_SWITCH, "Hand switch"),
    "HANDSWITCH": (ComponentType.HAND_SWITCH, "Hand switch"),
    "DFR": (ComponentType.DFR, "Digital fault recorder"),
    "FAULT RECORDER": (ComponentType.DFR, "Digital fault recorder"),
    "LTC": (ComponentType.LTC, "Load tap changer"),
    "TAP CHANGER": (ComponentType.LTC, "Load tap changer"),
    "METER": (ComponentType.METER, "Metering device"),
    "RTAC": (ComponentType.RTAC, "Real-time automation controller"),
    "CLOCK": (ComponentType.GPS_CLOCK, "GPS clock"),
    "GPS": (ComponentType.GPS_CLOCK, "GPS clock"),
    "PDC": (ComponentType.PDC, "Phasor data concentrator"),
    "CISCO": (ComponentType.NETWORK_SWITCH, "Network switch"),
    "ROUTER": (ComponentType.ROUTER, "Network router"),
    "MONITOR": (ComponentType.UNKNOWN, "HMI monitor"),
    "KEYBOARD": (ComponentType.UNKNOWN, "HMI keyboard"),
    "ATS": (ComponentType.ATS, "Automatic transfer switch"),
    "TRANSFER SWITCH": (ComponentType.ATS, "Automatic transfer switch"),
    "RTU": (ComponentType.RTAC, "Remote terminal unit"),
    "HVAC": (ComponentType.UNKNOWN, "HVAC unit"),
    "BATTERY BANK": (ComponentType.BATTERY, "Battery bank"),
    "BATTERY CHARGER": (ComponentType.BATTERY, "Battery charger"),
    "CHARGER": (ComponentType.BATTERY, "Battery charger"),
    "INVERTER": (ComponentType.BATTERY, "Inverter"),
    "BUSHING": (ComponentType.PT, "Bushing potential device"),
    "ARRESTER": (ComponentType.MOV, "Surge arrester"),
    "LIGHTNING": (ComponentType.MOV, "Lightning arrester"),
    "TESLA": (ComponentType.RELAY, "TESLA controller"),
    "UPS": (ComponentType.UPS, "Uninterruptible power supply"),
    "MCC": (ComponentType.MCC, "Motor control center"),
    "SWITCHGEAR": (ComponentType.SWGR, "Switchgear"),
    "SWGR": (ComponentType.SWGR, "Switchgear"),
    "VFD": (ComponentType.VFD, "Variable frequency drive"),
    "PLC": (ComponentType.PLC, "Programmable logic controller"),
    "RECTIFIER": (ComponentType.RECTIFIER, "Rectifier"),
    "MOV": (ComponentType.MOV, "Metal oxide varistor"),
    "SURGE": (ComponentType.SPD, "Surge protective device"),
    "SPD": (ComponentType.SPD, "Surge protective device"),
    "REGULATOR": (ComponentType.REGULATOR, "Voltage regulator"),
    "GENERATOR": (ComponentType.GENERATOR, "Generator"),
    "MOTOR": (ComponentType.UNKNOWN, "Motor"),
}

# Attribute keys that identify component function
DEVICE_ATTR_KEYS = ("DEVICE", "RELAY_MODEL", "RELAY_TYPE")
FUSE_ATTR_KEYS = ("FUSE_NUM", "FUSE_ID")
OUTPUT_ATTR_KEYS = ("OUTPUT-#", "OUTPUT_NUM", "OUTPUT")
INPUT_ATTR_KEY_PREFIX = "INPUT-#"
POWER_ATTR_KEYS = ("PWR_SUP", "POWER_SUPPLY", "PWR")


def _find_oda_converter():
    """Find ODA File Converter on the system."""
    possible_paths = [
        "/usr/bin/ODAFileConverter",
        "/usr/local/bin/ODAFileConverter",
        os.path.expanduser("~/ODAFileConverter/ODAFileConverter"),
        "/opt/ODAFileConverter/ODAFileConverter",
    ]
    for p in possible_paths:
        if os.path.isfile(p):
            return p

    # Try PATH
    try:
        result = subprocess.run(
            ["which", "ODAFileConverter"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return None


def convert_dwg_to_dxf(dwg_path: str, output_dir: Optional[str] = None) -> Optional[str]:
    """Convert a DWG file to DXF using ODA File Converter.

    Returns the path to the converted DXF file, or None if conversion failed.
    """
    oda_path = _find_oda_converter()
    if not oda_path:
        return None

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="dwg2dxf_")

    input_dir = os.path.dirname(dwg_path)
    filename = os.path.basename(dwg_path)

    try:
        # Use xvfb-run to prevent the GUI from popping up
        xvfb = shutil.which("xvfb-run")
        cmd = [oda_path, input_dir, output_dir, "ACAD2018", "DXF", "0", "1", filename]
        if xvfb:
            cmd = [xvfb, "-a"] + cmd
        subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60,
        )
        # Check for output
        dxf_name = os.path.splitext(filename)[0] + ".dxf"
        dxf_path = os.path.join(output_dir, dxf_name)
        if os.path.isfile(dxf_path):
            return dxf_path
    except Exception:
        pass

    return None


class DXFExtractor:
    """Extracts component data from DXF files (and DWG via conversion)."""

    def __init__(self):
        self._ezdxf = None

    def _get_ezdxf(self):
        if self._ezdxf is None:
            import ezdxf
            self._ezdxf = ezdxf
        return self._ezdxf

    def extract(self, file_path: str) -> DrawingData:
        """Extract all data from a DXF or DWG file."""
        drawing_id = os.path.splitext(os.path.basename(file_path))[0]
        ext = os.path.splitext(file_path)[1].lower()

        drawing = DrawingData(
            drawing_id=drawing_id,
            file_path=file_path,
            file_type=ext.lstrip("."),
        )

        dxf_path = file_path
        temp_dxf = None

        if ext == ".dwg":
            # Try to convert DWG to DXF
            converted = convert_dwg_to_dxf(file_path)
            if converted:
                dxf_path = converted
                temp_dxf = converted
            else:
                # Fall back to PDF extraction if available
                pdf_path = file_path.replace(".dwg", ".pdf")
                pdf_path = pdf_path.replace("P&C_CAD", "P&C_PDF")
                if os.path.isfile(pdf_path):
                    from .pdf_extractor import PDFExtractor
                    return PDFExtractor().extract(pdf_path)
                else:
                    drawing.notes.append(
                        "WARNING: DWG file could not be converted to DXF. "
                        "Install ODA File Converter for full DWG support."
                    )
                    return drawing

        try:
            ezdxf = self._get_ezdxf()
            doc = ezdxf.readfile(dxf_path)
            self._extract_from_doc(doc, drawing)
        except Exception as e:
            drawing.notes.append(f"DXF read error: {str(e)}")

        # Clean up temp file and directory
        if temp_dxf:
            temp_dir = os.path.dirname(temp_dxf)
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

        return drawing

    def _extract_from_doc(self, doc, drawing: DrawingData):
        """Extract all data from an ezdxf document.

        Iterates over modelspace and all paper space layouts, since DWG files
        converted via ODA File Converter often place entities in paper space.
        """
        layouts = [doc.modelspace()]
        for layout in doc.layouts:
            if layout.name != "Model":
                layouts.append(layout)

        for layout in layouts:
            # 1. Extract all TEXT and MTEXT entities
            self._extract_text_entities(layout, drawing)

            # 2. Extract block references (component symbols)
            self._extract_block_references(layout, drawing)

            # 4. Extract dimension entities
            self._extract_dimensions(layout, drawing)

            # 6. Build connections from line entities
            self._extract_wire_connections(layout, drawing)

        # 3. Extract block definitions for attribute info (document-level, once)
        self._extract_block_definitions(doc, drawing)

        # 5. Extract layer information (document-level, once)
        self._extract_layers(doc, drawing)

        # 7. Parse all extracted text for components
        self._parse_text_for_components(drawing)

    def _extract_text_entities(self, msp, drawing: DrawingData):
        """Extract all TEXT and MTEXT entities."""
        for entity in msp:
            if entity.dxftype() == "TEXT":
                text = entity.dxf.text
                if text and text.strip():
                    x = entity.dxf.insert.x if hasattr(entity.dxf, 'insert') else 0
                    y = entity.dxf.insert.y if hasattr(entity.dxf, 'insert') else 0
                    label = TextLabel(
                        text=text.strip(),
                        x=x, y=y,
                        category=self._categorize_text(text),
                    )
                    drawing.all_labels.append(label)
                    drawing.raw_text += text + "\n"

            elif entity.dxftype() == "MTEXT":
                text = entity.plain_text()
                if text and text.strip():
                    x = entity.dxf.insert.x if hasattr(entity.dxf, 'insert') else 0
                    y = entity.dxf.insert.y if hasattr(entity.dxf, 'insert') else 0
                    # Split multiline MTEXT into individual lines for better
                    # pattern matching (avoid matching across line boundaries)
                    for line in text.split("\n"):
                        line = line.strip()
                        if line:
                            label = TextLabel(
                                text=line,
                                x=x, y=y,
                                category=self._categorize_text(line),
                            )
                            drawing.all_labels.append(label)
                            drawing.raw_text += line + "\n"

    def _extract_block_references(self, msp, drawing: DrawingData):
        """Extract block references — these represent component symbols.

        Uses a multi-strategy approach:
        1. Title block detection (skip, extract metadata)
        2. Metadata/wire-diagram blocks (skip)
        3. Attribute-based identification (DEVICE, FUSE_NUM, OUTPUT-#, INPUT-#, PWR_SUP)
        4. Block name keyword matching (POWER SUPPLY, SERIAL, ETHERNET, GROUND, etc.)
        5. TAG/NAME attribute or block-name pattern matching (fallback)
        6. Unrecognized blocks logged for reporting
        """
        # Track the relay device context for associating sub-components
        relay_device_id = None

        for entity in msp:
            if entity.dxftype() == "INSERT":
                block_name = entity.dxf.name
                x = entity.dxf.insert.x
                y = entity.dxf.insert.y

                # Extract attributes from block reference
                attribs = {}
                if hasattr(entity, 'attribs'):
                    for attrib in entity.attribs:
                        tag = attrib.dxf.tag
                        value = attrib.dxf.text
                        if value:
                            attribs[tag] = value

                block_upper = block_name.upper()

                # ── 1. Title blocks ──────────────────────────────────
                if "TITLEBLOCK" in block_upper or "TITLE" in block_upper:
                    self._extract_title_from_attribs(attribs, drawing)
                    continue

                # Title block variants with drawing metadata
                if ("ATT BLK" in block_upper or "ATT_BLK" in block_upper) and "DWGNO" in attribs:
                    self._extract_title_from_attribs(attribs, drawing)
                    continue

                # Mortenson title block border (no component data)
                if "MORTENSON TB" in block_upper and not attribs:
                    continue

                # ── 2. Skip non-component metadata blocks ────────────
                # AutoCAD Electrical wire diagram metadata
                if block_upper == "WD_M":
                    continue
                # Revision markers
                if ("REV" in block_upper or "REVISION" in block_upper) and not any(
                    k in attribs for k in ("DEVICE", "FUSE_NUM", "OUTPUT-#")
                ):
                    continue
                # Tag triangles, annotation markers
                if "TAG" in block_upper and "TAGNUMBER" in attribs:
                    continue

                # ── Add attribute text to raw_text for pattern matching ──
                for tag, value in attribs.items():
                    if value.strip():
                        drawing.raw_text += value + "\n"
                        drawing.all_labels.append(TextLabel(
                            text=value, x=x, y=y,
                            category=self._categorize_text(value),
                        ))

                matched = False

                # ── 3. DEVICE attribute → relay/device component ─────
                device_val = None
                for key in DEVICE_ATTR_KEYS:
                    if key in attribs and attribs[key].strip():
                        device_val = attribs[key].strip()
                        break
                if device_val:
                    relay_device_id = device_val
                    comp_type = self._identify_component_type(device_val)
                    if comp_type == ComponentType.UNKNOWN:
                        comp_type = ComponentType.RELAY
                    if device_val not in drawing.components:
                        comp = Component(
                            component_id=device_val,
                            component_type=comp_type,
                            description=f"{comp_type.name} {device_val} from block {block_name}",
                            attributes={},
                        )
                        comp.labels.append(TextLabel(
                            text=device_val, x=x, y=y, category="component",
                        ))
                        drawing.components[device_val] = comp
                    # Enrich with all block attributes
                    comp = drawing.components[device_val]
                    for k, v in attribs.items():
                        if k not in DEVICE_ATTR_KEYS and v.strip():
                            comp.attributes[k] = v
                    # Add specific values
                    if "RELAY" in attribs:
                        comp.values.append(ComponentValue(
                            parameter="relay_assignment", value=attribs["RELAY"],
                        ))
                    if "LOC" in attribs:
                        comp.values.append(ComponentValue(
                            parameter="location", value=attribs["LOC"],
                        ))
                    if "MOUNT" in attribs:
                        comp.values.append(ComponentValue(
                            parameter="mount_type", value=attribs["MOUNT"],
                        ))
                    matched = True

                # ── 4. FUSE_NUM attribute → fuse component ───────────
                fuse_id = None
                for key in FUSE_ATTR_KEYS:
                    if key in attribs and attribs[key].strip():
                        fuse_id = attribs[key].strip()
                        break
                if fuse_id and not matched:
                    if fuse_id not in drawing.components:
                        comp = Component(
                            component_id=fuse_id,
                            component_type=ComponentType.FUSE,
                            description=f"Fuse {fuse_id} from block {block_name}",
                            attributes={},
                        )
                        comp.labels.append(TextLabel(
                            text=fuse_id, x=x, y=y, category="component",
                        ))
                        drawing.components[fuse_id] = comp
                    comp = drawing.components[fuse_id]
                    # Enrich with fuse-specific values (deduplicate)
                    existing_vals = {(v.parameter, v.value) for v in comp.values}
                    if "FUSE_SIZE" in attribs and ("fuse_rating", attribs["FUSE_SIZE"]) not in existing_vals:
                        comp.values.append(ComponentValue(
                            parameter="fuse_rating",
                            value=attribs["FUSE_SIZE"],
                        ))
                    if "LOC" in attribs and ("location", attribs["LOC"]) not in existing_vals:
                        comp.values.append(ComponentValue(
                            parameter="location", value=attribs["LOC"],
                        ))
                    # Extract terminal assignments
                    for tkey in ("LEFT_TERM", "RIGHT_TERM", "TOP_TERM", "BOTTOM_TERM"):
                        if tkey in attribs and attribs[tkey].strip():
                            comp.attributes[tkey] = attribs[tkey]
                    matched = True

                # ── 5. OUTPUT-# attribute → output contact ───────────
                output_id = None
                for key in OUTPUT_ATTR_KEYS:
                    if key in attribs and attribs[key].strip():
                        output_id = attribs[key].strip()
                        break
                if output_id and not matched:
                    if output_id not in drawing.components:
                        comp = Component(
                            component_id=output_id,
                            component_type=ComponentType.RELAY,
                            description=f"Relay output contact {output_id}",
                            attributes={},
                        )
                        comp.labels.append(TextLabel(
                            text=output_id, x=x, y=y, category="component",
                        ))
                        drawing.components[output_id] = comp
                    comp = drawing.components[output_id]
                    for k, v in attribs.items():
                        if "TERM" in k and v.strip():
                            comp.attributes[k] = v
                    if "POLARITY" in attribs and attribs["POLARITY"].strip():
                        comp.attributes["POLARITY"] = attribs["POLARITY"]
                    matched = True

                # ── 6. INPUT-#N attributes → input contact(s) ────────
                if not matched:
                    input_ids = []
                    for k, v in attribs.items():
                        if k.startswith(INPUT_ATTR_KEY_PREFIX) and v.strip() and not v.startswith("IN "):
                            # "IN" alone means unused slot, skip
                            if v.strip() != "IN":
                                input_ids.append((k, v.strip()))
                    if input_ids:
                        for attr_key, inp_id in input_ids:
                            if inp_id not in drawing.components:
                                comp = Component(
                                    component_id=inp_id,
                                    component_type=ComponentType.RELAY,
                                    description=f"Relay input contact {inp_id}",
                                    attributes={},
                                )
                                comp.labels.append(TextLabel(
                                    text=inp_id, x=x, y=y, category="component",
                                ))
                                drawing.components[inp_id] = comp
                            comp = drawing.components[inp_id]
                            for k, v in attribs.items():
                                if "TERM" in k and v.strip() and v != "##":
                                    comp.attributes[k] = v
                        matched = True

                # ── 7. PWR_SUP attribute → power supply component ────
                if not matched:
                    pwr_val = None
                    for key in POWER_ATTR_KEYS:
                        if key in attribs and attribs[key].strip():
                            pwr_val = attribs[key].strip()
                            break
                    if pwr_val:
                        comp_id = f"PWR-SUPPLY"
                        if relay_device_id:
                            comp_id = f"PWR-SUPPLY-{relay_device_id}"
                        if comp_id not in drawing.components:
                            comp = Component(
                                component_id=comp_id,
                                component_type=ComponentType.BATTERY,
                                description=f"Power supply input ({pwr_val})",
                                attributes={},
                            )
                            comp.labels.append(TextLabel(
                                text=comp_id, x=x, y=y, category="component",
                            ))
                            drawing.components[comp_id] = comp
                        comp = drawing.components[comp_id]
                        for k, v in attribs.items():
                            if "TERM" in k and v.strip():
                                comp.attributes[k] = v
                        matched = True

                # ── 8. Block name keyword matching ────────────────────
                if not matched:
                    for keyword, (comp_type, desc) in ELECTRICAL_BLOCK_KEYWORDS.items():
                        if keyword in block_upper:
                            # Use a clean name for ground connections
                            if keyword in ("GROUND", "GND"):
                                comp_id = "GND"
                                if relay_device_id:
                                    comp_id = f"GND-{relay_device_id}"
                            else:
                                comp_id = block_name.replace(" ", "-").upper()
                                if relay_device_id:
                                    comp_id = f"{comp_id}-{relay_device_id}"
                            if comp_id not in drawing.components:
                                comp = Component(
                                    component_id=comp_id,
                                    component_type=comp_type,
                                    description=f"{desc} from block {block_name}",
                                    attributes={k: v for k, v in attribs.items() if v.strip()},
                                )
                                comp.labels.append(TextLabel(
                                    text=comp_id, x=x, y=y, category="component",
                                ))
                                drawing.components[comp_id] = comp
                            matched = True
                            break

                # ── 9. Panel terminal blocks ──────────────────────────
                if not matched and ("PNL_TERM" in block_upper or "RELAY_TERM" in block_upper):
                    loc = attribs.get("LOC", "")
                    strip_name = attribs.get("TAGSTRIP", "")
                    term_num = attribs.get("TERM#", attribs.get("TERM", ""))
                    term_desc = attribs.get("TERM_DESC", "")
                    if strip_name and term_num:
                        comp_id = f"{strip_name}-{term_num}"
                    elif term_desc:
                        comp_id = f"TERM-{term_desc}"
                    else:
                        comp_id = f"TERM-{block_name}"
                    if comp_id not in drawing.components:
                        comp = Component(
                            component_id=comp_id,
                            component_type=ComponentType.TERMINAL_BLOCK,
                            description=f"Terminal {comp_id} at {loc}" if loc else f"Terminal {comp_id}",
                            attributes={k: v for k, v in attribs.items() if v.strip()},
                        )
                        comp.labels.append(TextLabel(
                            text=comp_id, x=x, y=y, category="component",
                        ))
                        drawing.components[comp_id] = comp
                    matched = True

                # ── 10. Fallback: TAG/NAME or pattern match ──────────
                if not matched:
                    comp_id = attribs.get("TAG", attribs.get("NAME", ""))
                    if not comp_id:
                        comp_id = self._extract_component_id(block_name)

                    if comp_id:
                        comp_type = self._identify_component_type(comp_id)
                        if comp_id not in drawing.components:
                            comp = Component(
                                component_id=comp_id,
                                component_type=comp_type,
                                description=f"{comp_type.name} from block {block_name}",
                                attributes={k: v for k, v in attribs.items() if v.strip()},
                            )
                            comp.labels.append(TextLabel(
                                text=comp_id, x=x, y=y, category="component",
                            ))
                            drawing.components[comp_id] = comp
                        matched = True

                # ── 11. Log unrecognized blocks for reporting ─────────
                if not matched and not block_name.startswith("*") and attribs:
                    # Non-anonymous block with attributes that we couldn't classify
                    drawing.notes.append(
                        f"Unrecognized electrical block: {block_name} "
                        f"(attributes: {', '.join(f'{k}={v}' for k, v in attribs.items())})"
                    )
                    drawing.all_labels.append(TextLabel(
                        text=block_name, x=x, y=y, category="block",
                    ))

    @staticmethod
    def _extract_title_from_attribs(attribs: dict, drawing: DrawingData):
        """Extract title block metadata from block reference attributes."""
        tb = drawing.title_block
        for tag, value in attribs.items():
            tag_upper = tag.upper()
            if "DWGNO" in tag_upper or tag_upper == "DRAWING_NUMBER":
                tb.drawing_number = value
            elif "DWGTITLE" in tag_upper or tag_upper == "TITLE":
                if not tb.drawing_name or tag_upper.endswith("1"):
                    tb.drawing_name = value
            elif "DWGTYPE" in tag_upper:
                if value:
                    tb.drawing_type = value
            elif "CURRREV" in tag_upper or tag_upper == "REVISION":
                tb.revision = value
            elif "DRWNBY" in tag_upper:
                tb.drawn_by = value
            elif "DSGNBY" in tag_upper:
                tb.designed_by = value
            elif "APPDBY" in tag_upper or "APPD" == tag_upper:
                tb.reviewed_by = value
            elif "ORGDATE" in tag_upper:
                tb.date = value
            elif tag_upper == "COMPANY" or "MORTENSON" in value.upper():
                tb.company = value

    def _extract_block_definitions(self, doc, drawing: DrawingData):
        """Extract block definitions for component attribute templates."""
        for block in doc.blocks:
            if block.name.startswith("*"):  # Skip anonymous blocks
                continue

            for entity in block:
                if entity.dxftype() == "ATTDEF":
                    # Attribute definition — these define what data a component block carries
                    tag = entity.dxf.tag
                    default = entity.dxf.text if hasattr(entity.dxf, 'text') else ""
                    drawing.notes.append(
                        f"Block '{block.name}' attribute: {tag}={default}"
                    )

    def _extract_dimensions(self, msp, drawing: DrawingData):
        """Extract dimension entities (electrical values, distances)."""
        for entity in msp:
            if entity.dxftype() in ("DIMENSION", "ALIGNED_DIMENSION"):
                try:
                    text = entity.dxf.text if hasattr(entity.dxf, 'text') else ""
                    if text:
                        drawing.all_labels.append(TextLabel(
                            text=text, x=0, y=0, category="dimension",
                        ))
                except Exception:
                    pass

    def _extract_layers(self, doc, drawing: DrawingData):
        """Extract layer names — layers often organize components by type."""
        for layer in doc.layers:
            drawing.attributes = getattr(drawing, 'attributes', {})
            # Layer names can indicate component groupings
            layer_name = layer.dxf.name

    def _extract_wire_connections(self, msp, drawing: DrawingData):
        """Extract LINE and POLYLINE entities that represent wires/connections."""
        wire_endpoints = []

        for entity in msp:
            if entity.dxftype() == "LINE":
                start = (entity.dxf.start.x, entity.dxf.start.y)
                end = (entity.dxf.end.x, entity.dxf.end.y)
                wire_endpoints.append((start, end))

            elif entity.dxftype() in ("LWPOLYLINE", "POLYLINE"):
                try:
                    points = list(entity.get_points())
                    for i in range(len(points) - 1):
                        start = (points[i][0], points[i][1])
                        end = (points[i + 1][0], points[i + 1][1])
                        wire_endpoints.append((start, end))
                except Exception:
                    pass

        # Match wire endpoints to component positions to build connections
        comp_positions = {}
        for label in drawing.all_labels:
            if label.category == "component":
                comp_positions[label.text] = (label.x, label.y)

        tolerance = 5.0  # Position matching tolerance

        for start, end in wire_endpoints:
            from_comp = self._find_nearest_component(start, comp_positions, tolerance)
            to_comp = self._find_nearest_component(end, comp_positions, tolerance)

            if from_comp and to_comp and from_comp != to_comp:
                conn = Connection(
                    from_component=from_comp,
                    from_terminal="",
                    to_component=to_comp,
                    to_terminal="",
                )
                drawing.connections.append(conn)

    def _parse_text_for_components(self, drawing: DrawingData):
        """Parse all extracted raw text for component patterns."""
        # Replace newlines with spaces so patterns don't match across lines
        text = " ".join(drawing.raw_text.splitlines())

        # Device function numbers
        for comp_type, pattern in DEVICE_PATTERNS.items():
            for match in re.finditer(pattern, text):
                comp_id = match.group(1)
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=comp_type,
                    )

        # Relay models (with space-separated GE/BECKWITH support)
        for idx, pattern in enumerate(RELAY_PATTERNS):
            for match in re.finditer(pattern, text):
                comp_id = match.group(1)
                prefix = _RELAY_PREFIX_MAP.get(idx, "")
                if prefix:
                    comp_id = prefix + comp_id
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=ComponentType.RELAY,
                    )

        # Instrument transformers
        for comp_type, pattern in INSTRUMENT_TX_PATTERNS.items():
            for match in re.finditer(pattern, text):
                comp_id = match.group(1)
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=comp_type,
                    )

        # Other components (fuses, switches, panels, NGRs)
        for comp_type, pattern in OTHER_PATTERNS.items():
            for match in re.finditer(pattern, text):
                comp_id = match.group(1).strip()
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=comp_type,
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

        # Input contacts (IN101, IN102, etc.)
        for match in re.finditer(INPUT_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.RELAY,
                    description=f"Relay input contact {comp_id}",
                )

        # Circuit identifiers ("CIRCUIT 16")
        for match in re.finditer(CIRCUIT_PATTERN, text):
            circuit_num = match.group(1)
            comp_id = f"CIRCUIT-{circuit_num}"
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.BREAKER,
                    description=f"DC panel circuit breaker #{circuit_num}",
                )

        # DC panels
        for match in re.finditer(r'\b(DC\s*PANEL\s*[A-Z]*\d*)\b', text):
            comp_id = match.group(1).strip()
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.PANEL,
                )

        # Breaker identifiers in "BREAKER XX" format
        for match in re.finditer(r'BREAKER\s+(\d+)', text):
            comp_id = f"BREAKER-{match.group(1)}"
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.BREAKER,
                )

        # ─── Tier 1 & 2 substation equipment ──────────────────────────

        # Digital fault recorders (normalize DFR1 → DFR-1)
        for match in re.finditer(DFR_NORM_PATTERN, text):
            comp_id = f"DFR-{match.group(2)}"
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.DFR,
                    description=f"Digital fault recorder {comp_id}",
                )

        # DFR current and voltage modules
        for pattern, desc in ((CM_PATTERN, "DFR current module"),
                              (VM_PATTERN, "DFR voltage module")):
            for match in re.finditer(pattern, text):
                comp_id = match.group(1)
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=ComponentType.DFR,
                        description=f"{desc} {comp_id}",
                    )

        # Custody meters
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

        # Standalone terminal blocks (TB1, TB2)
        for match in re.finditer(STANDALONE_TB_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.TERMINAL_BLOCK,
                    description=f"Terminal block {comp_id}",
                )

        # Standalone test switches (TS1, TS3)
        for match in re.finditer(STANDALONE_TS_PATTERN, text):
            comp_id = match.group(1)
            if comp_id not in drawing.components:
                drawing.components[comp_id] = Component(
                    component_id=comp_id,
                    component_type=ComponentType.TERMINAL_BLOCK,
                    description=f"Test switch {comp_id}",
                )

        # ─── Substation automation & communication equipment ──────────

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

        # Power strips
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

        _FUTURE_PATTERNS = [
            (UPS_PATTERN, ComponentType.UPS, "Uninterruptible power supply"),
            (MCC_PATTERN, ComponentType.MCC, "Motor control center"),
            (SWGR_PATTERN, ComponentType.SWGR, "Switchgear"),
            (VFD_PATTERN, ComponentType.VFD, "Variable frequency drive"),
            (PLC_PATTERN, ComponentType.PLC, "Programmable logic controller"),
            (RECTIFIER_PATTERN, ComponentType.RECTIFIER, "Rectifier"),
            (MOV_PATTERN, ComponentType.MOV, "Metal oxide varistor"),
            (ARRESTER_PATTERN, ComponentType.MOV, "Surge arrester"),
            (SPD_PATTERN, ComponentType.SPD, "Surge protective device"),
            (REGULATOR_PATTERN, ComponentType.REGULATOR, "Voltage regulator"),
            (GENERATOR_PATTERN, ComponentType.GENERATOR, "Generator"),
        ]
        for pattern, comp_type, desc in _FUTURE_PATTERNS:
            for match in re.finditer(pattern, text):
                comp_id = match.group(1)
                if comp_id not in drawing.components:
                    drawing.components[comp_id] = Component(
                        component_id=comp_id,
                        component_type=comp_type,
                        description=f"{desc} {comp_id}",
                    )

        # Cross-references
        for match in re.finditer(DRAWING_REF_PATTERN, text):
            ref = match.group(1)
            own_ref = drawing.drawing_id.replace("NRE-", "")
            if ref != own_ref and ref not in drawing.cross_references:
                drawing.cross_references.append(ref)

        # Cables
        for match in re.finditer(CABLE_PATTERN, text):
            spec = match.group(1)
            if spec not in drawing.cable_schedule:
                drawing.cable_schedule.append(spec)

        # Cable types (CAT5E, FIBER, etc.)
        for match in re.finditer(r'\b(CAT[56]E?|MM\s*FIBER|SM\s*FIBER|COAX)\b', text, re.IGNORECASE):
            cable_type = match.group(1).upper()
            if cable_type not in drawing.cable_schedule:
                drawing.cable_schedule.append(cable_type)

        # Terminal blocks
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

        # Voltage levels
        for match in re.finditer(r'(\d+\.?\d*)\s*(kV|KV)', text):
            vl = f"{match.group(1)}kV"
            if vl not in drawing.voltage_levels:
                drawing.voltage_levels.append(vl)

        # DC voltage levels (e.g., "125V DC")
        for match in re.finditer(r'(\d+)\s*V\s*(AC|DC)', text):
            vl = f"{match.group(1)}V {match.group(2)}"
            if vl not in drawing.voltage_levels:
                drawing.voltage_levels.append(vl)

    @staticmethod
    def _find_nearest_component(point, comp_positions, tolerance):
        """Find the nearest component to a point within tolerance."""
        best = None
        best_dist = tolerance

        for comp_id, pos in comp_positions.items():
            dx = point[0] - pos[0]
            dy = point[1] - pos[1]
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best = comp_id

        return best

    @staticmethod
    def _extract_component_id(block_name: str) -> str:
        """Try to extract a component ID from a block name."""
        all_patterns = (
            list(DEVICE_PATTERNS.values())
            + RELAY_PATTERNS
            + list(INSTRUMENT_TX_PATTERNS.values())
            + list(OTHER_PATTERNS.values())
            + list(EXTENDED_PATTERNS.values())
            + [OUTPUT_PATTERN, INPUT_PATTERN,
               CM_PATTERN, VM_PATTERN, CMET_PATTERN,
               HS_PATTERN, TEE_PATTERN, AUX_CONTACT_PATTERN,
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
               GENERATOR_PATTERN]
        )
        for idx, pattern in enumerate(all_patterns):
            match = re.search(pattern, block_name)
            if match:
                comp_id = match.group(1)
                # Handle space-separated relay patterns (GE L90, BECKWITH M-2001D)
                relay_start = len(DEVICE_PATTERNS)
                if relay_start <= idx < relay_start + len(RELAY_PATTERNS):
                    relay_idx = idx - relay_start
                    prefix = _RELAY_PREFIX_MAP.get(relay_idx, "")
                    if prefix:
                        comp_id = prefix + comp_id
                return comp_id
        # Normalizing patterns (two-group: prefix + number → PREFIX-NUMBER)
        match = re.search(DPAC_PATTERN, block_name)
        if match:
            return f"DPAC-{match.group(2)}"
        match = re.search(DFR_NORM_PATTERN, block_name)
        if match:
            return f"DFR-{match.group(2)}"
        match = re.search(FPP_NORM_PATTERN, block_name)
        if match:
            return f"FPP-{match.group(2)}"
        return ""

    @staticmethod
    def _identify_component_type(comp_id: str) -> ComponentType:
        """Identify the component type from its ID."""
        for comp_type, pattern in DEVICE_PATTERNS.items():
            if re.match(pattern, comp_id):
                return comp_type
        for pattern in RELAY_PATTERNS:
            if re.match(pattern, comp_id):
                return ComponentType.RELAY
        for comp_type, pattern in INSTRUMENT_TX_PATTERNS.items():
            if re.match(pattern, comp_id):
                return comp_type
        for comp_type, pattern in OTHER_PATTERNS.items():
            if re.match(pattern, comp_id):
                return comp_type
        for comp_type, pattern in EXTENDED_PATTERNS.items():
            if re.match(pattern, comp_id):
                return comp_type
        if re.match(DPAC_PATTERN, comp_id):
            return ComponentType.RELAY
        if re.match(OUTPUT_PATTERN, comp_id):
            return ComponentType.RELAY
        if re.match(INPUT_PATTERN, comp_id):
            return ComponentType.RELAY
        # New substation equipment type identification
        _TYPE_MAP = [
            (DFR_NORM_PATTERN, ComponentType.DFR),
            (CM_PATTERN, ComponentType.DFR),
            (VM_PATTERN, ComponentType.DFR),
            (CMET_PATTERN, ComponentType.CUSTODY_METER),
            (FPP_NORM_PATTERN, ComponentType.FIBER_PATCH),
            (HS_PATTERN, ComponentType.HAND_SWITCH),
            (TEE_PATTERN, ComponentType.UNKNOWN),
            (AUX_CONTACT_PATTERN, ComponentType.BREAKER),
            (WSL_PATTERN, ComponentType.CT),
            (PS_PATTERN, ComponentType.POWER_SUPPLY),
            (RP_PATTERN, ComponentType.PANEL),
            (ETM_PATTERN, ComponentType.RELAY),
            (LTC_PATTERN, ComponentType.LTC),
            (SST_PATTERN, ComponentType.TRANSFORMER),
            (AC_PANEL_PATTERN, ComponentType.PANEL),
            (NAMED_FUSE_PATTERN, ComponentType.FUSE),
            (BESS_PATTERN, ComponentType.BATTERY),
            (TC_PATTERN, ComponentType.BREAKER),
            (LOR_PATTERN, ComponentType.LOCKOUT),
            (VDC_PATTERN, ComponentType.POWER_SUPPLY),
            (COMM_MODULE_PATTERN, ComponentType.RELAY),
            (CT_CLASS_PATTERN, ComponentType.CT),
            (STANDALONE_TB_PATTERN, ComponentType.TERMINAL_BLOCK),
            (STANDALONE_TS_PATTERN, ComponentType.TERMINAL_BLOCK),
            (RTAC_PATTERN, ComponentType.RTAC),
            (CLOCK_PATTERN, ComponentType.GPS_CLOCK),
            (PDC_PATTERN, ComponentType.PDC),
            (CISCO_PATTERN, ComponentType.NETWORK_SWITCH),
            (RTR_PATTERN, ComponentType.ROUTER),
            (BAF_PATTERN, ComponentType.FUSE),
            (POWERSTRIP_PATTERN, ComponentType.POWER_SUPPLY),
            (ATS_PATTERN, ComponentType.ATS),
            (RTU_PATTERN, ComponentType.RTAC),
            (HVAC_PATTERN, ComponentType.UNKNOWN),
            (BP_PATTERN, ComponentType.PT),
            (EMS_PATTERN, ComponentType.RTAC),
            (BATTERY_EQUIP_PATTERN, ComponentType.BATTERY),
            (UPS_PATTERN, ComponentType.UPS),
            (MCC_PATTERN, ComponentType.MCC),
            (SWGR_PATTERN, ComponentType.SWGR),
            (VFD_PATTERN, ComponentType.VFD),
            (PLC_PATTERN, ComponentType.PLC),
            (RECTIFIER_PATTERN, ComponentType.RECTIFIER),
            (MOV_PATTERN, ComponentType.MOV),
            (ARRESTER_PATTERN, ComponentType.MOV),
            (SPD_PATTERN, ComponentType.SPD),
            (REGULATOR_PATTERN, ComponentType.REGULATOR),
            (GENERATOR_PATTERN, ComponentType.GENERATOR),
        ]
        for pattern, ctype in _TYPE_MAP:
            if re.match(pattern, comp_id):
                return ctype
        return ComponentType.UNKNOWN

    @staticmethod
    def _categorize_text(text: str) -> str:
        """Categorize a text entity."""
        upper = text.upper().strip()

        all_comp_patterns = (
            list(DEVICE_PATTERNS.values())
            + RELAY_PATTERNS
            + list(INSTRUMENT_TX_PATTERNS.values())
            + list(OTHER_PATTERNS.values())
            + list(EXTENDED_PATTERNS.values())
            + [DPAC_PATTERN, OUTPUT_PATTERN, INPUT_PATTERN,
               DFR_NORM_PATTERN, CM_PATTERN, VM_PATTERN, CMET_PATTERN,
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
               GENERATOR_PATTERN]
        )
        for pattern in all_comp_patterns:
            if re.search(pattern, upper):
                return "component"

        if re.search(VOLTAGE_PATTERN, upper) or re.search(CURRENT_PATTERN, upper):
            return "value"
        if re.search(CABLE_PATTERN, text):
            return "cable"
        if re.search(DRAWING_REF_PATTERN, text):
            return "reference"
        if re.search(TERMINAL_PATTERN, text):
            return "terminal"

        return "text"
