"""Data models for the drawing synchronization system.

Represents components, connections, drawings, and their relationships
with full electrical detail extraction.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import json


class ComponentType(Enum):
    """IEEE/ANSI device function numbers and component categories."""
    BREAKER = "52"           # AC circuit breaker
    OVERCURRENT = "50"       # Instantaneous overcurrent
    OVERCURRENT_TIME = "51"  # AC time overcurrent
    LOCKOUT = "86"           # Lockout relay
    DIFFERENTIAL = "87"      # Differential protective relay
    DISCONNECT = "89"        # Line switch / disconnect
    DISTANCE = "21"          # Distance relay
    SYNC_CHECK = "25"        # Synchronizing / check device
    UNDERVOLTAGE = "27"      # Undervoltage relay
    OVERVOLTAGE = "59"       # Overvoltage relay
    DIRECTIONAL_OC = "67"    # Directional overcurrent
    FREQUENCY = "81"         # Frequency relay
    RECLOSER = "79"          # AC reclosing relay
    TRANSFORMER = "TX"       # Power transformer
    CT = "CT"                # Current transformer
    PT = "PT"                # Potential transformer
    VT = "VT"                # Voltage transformer
    CCVT = "CCVT"            # Coupling capacitor voltage transformer
    RELAY = "RELAY"          # Protective relay (e.g. SEL-451)
    FUSE = "FU"              # Fuse
    PANEL = "PANEL"          # Control panel
    SWITCH = "SW"            # Network switch / misc switch
    CABLE = "CABLE"          # Cable run
    TERMINAL_BLOCK = "TB"    # Terminal block
    JUNCTION_BOX = "JB"      # Junction box
    METER = "METER"          # Metering device
    BATTERY = "BAT"          # Battery / DC supply
    NGR = "NGR"              # Neutral grounding resistor
    DFR = "DFR"              # Digital fault recorder
    HAND_SWITCH = "HS"       # Hand switch / manual control switch
    POWER_SUPPLY = "PS"      # Power supply unit
    LTC = "LTC"              # Load tap changer
    CUSTODY_METER = "CMET"   # Custody / revenue meter
    FIBER_PATCH = "FPP"      # Fiber patch panel
    GROUND = "GND"           # Ground / grounding connection
    RTAC = "RTAC"            # Real-time automation controller (e.g., SEL-3555)
    GPS_CLOCK = "CLOCK"      # GPS clock / time synchronization device (e.g., SEL-2407)
    PDC = "PDC"              # Phasor data concentrator (e.g., SEL-3355)
    NETWORK_SWITCH = "NETSW" # Network switch (e.g., CISCO IE-4010)
    ROUTER = "RTR"           # Network router
    UPS = "UPS"              # Uninterruptible power supply
    ATS = "ATS"              # Automatic transfer switch
    MCC = "MCC"              # Motor control center
    SWGR = "SWGR"            # Switchgear
    VFD = "VFD"              # Variable frequency drive
    PLC = "PLC"              # Programmable logic controller
    RECTIFIER = "RECT"       # Rectifier
    MOV = "MOV"              # Metal oxide varistor / surge arrester
    SPD = "SPD"              # Surge protective device
    REGULATOR = "REG"        # Voltage regulator
    GENERATOR = "GEN"        # Generator
    BOM_ITEM = "BOM"          # Generic BOM line item (enclosure, DIN rail, connector, etc.)
    CONSUMABLE = "CONSUMABLE" # Bulk/lot items (wire, crimp terminals, hardware)
    UNKNOWN = "UNKNOWN"


class AlertSeverity(Enum):
    """Alert severity levels."""
    CRITICAL = "CRITICAL"   # Value mismatch that could cause safety issues
    WARNING = "WARNING"     # Naming inconsistency or missing reference
    INFO = "INFO"           # Minor discrepancy


class DrawingType(Enum):
    """Drawing type classifications."""
    ONE_LINE = "ONE_LINE"
    AC_SCHEMATIC = "AC_SCHEMATIC"
    DC_SCHEMATIC = "DC_SCHEMATIC"
    PANEL_WIRING = "PANEL_WIRING"
    CABLE_WIRING = "CABLE_WIRING"
    PANEL_LAYOUT = "PANEL_LAYOUT"
    SYSTEM_DIAGRAM = "SYSTEM_DIAGRAM"
    DRAWING_INDEX = "DRAWING_INDEX"
    RELAY_FUNCTIONAL = "RELAY_FUNCTIONAL"
    LEGEND = "LEGEND"
    COMMUNICATION = "COMMUNICATION"
    UNKNOWN = "UNKNOWN"


@dataclass
class ComponentValue:
    """An electrical value associated with a component."""
    parameter: str          # e.g., "voltage_rating", "current_rating", "impedance"
    value: str              # e.g., "138kV", "2000A", "14.95%Z"
    unit: str = ""          # e.g., "kV", "A", "ohm"
    numeric_value: Optional[float] = None

    def to_dict(self):
        return {
            "parameter": self.parameter,
            "value": self.value,
            "unit": self.unit,
            "numeric_value": self.numeric_value,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


@dataclass
class Connection:
    """A wiring/circuit connection between two points."""
    from_component: str     # Source component ID
    from_terminal: str      # Source terminal (e.g., "A01", "TB6-71")
    to_component: str       # Destination component ID
    to_terminal: str        # Destination terminal
    cable_spec: str = ""    # Cable specification (e.g., "2/C#10", "12/C #12SH")
    wire_label: str = ""    # Wire label/tag
    signal_type: str = ""   # e.g., "TRIP", "CLOSE", "SCADA", "POWER"

    def to_dict(self):
        return {
            "from_component": self.from_component,
            "from_terminal": self.from_terminal,
            "to_component": self.to_component,
            "to_terminal": self.to_terminal,
            "cable_spec": self.cable_spec,
            "wire_label": self.wire_label,
            "signal_type": self.signal_type,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


@dataclass
class TextLabel:
    """A text label extracted from a drawing with position."""
    text: str
    x: float = 0.0
    y: float = 0.0
    category: str = ""      # "component", "value", "note", "title", "reference", "cable"

    def to_dict(self):
        return {
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


@dataclass
class Component:
    """An electrical component extracted from a drawing."""
    component_id: str             # e.g., "52-L1", "SEL-451", "CT-B1M"
    component_type: ComponentType
    description: str = ""         # Human-readable description
    values: list = field(default_factory=list)       # List of ComponentValue
    connections: list = field(default_factory=list)   # List of Connection
    labels: list = field(default_factory=list)        # Associated TextLabels
    drawing_refs: list = field(default_factory=list)  # Cross-refs (e.g., ["EC-301.1"])
    attributes: dict = field(default_factory=dict)    # Additional key-value attributes

    def to_dict(self):
        return {
            "component_id": self.component_id,
            "component_type": self.component_type.value,
            "description": self.description,
            "values": [v.to_dict() for v in self.values],
            "connections": [c.to_dict() for c in self.connections],
            "labels": [l.to_dict() for l in self.labels],
            "drawing_refs": self.drawing_refs,
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            component_id=d["component_id"],
            component_type=ComponentType(d["component_type"]),
            description=d.get("description", ""),
            values=[ComponentValue.from_dict(v) for v in d.get("values", [])],
            connections=[Connection.from_dict(c) for c in d.get("connections", [])],
            labels=[TextLabel.from_dict(l) for l in d.get("labels", [])],
            drawing_refs=d.get("drawing_refs", []),
            attributes=d.get("attributes", {}),
        )


@dataclass
class TitleBlock:
    """Title block information from a drawing."""
    drawing_number: str = ""      # e.g., "NRE-EC-301.0"
    revision: str = ""
    project_name: str = ""
    project_location: str = ""
    drawn_by: str = ""
    designed_by: str = ""
    reviewed_by: str = ""
    date: str = ""
    drawing_name: str = ""
    company: str = ""
    sheet: str = ""
    drawing_type: str = ""

    def to_dict(self):
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DrawingIndexEntry:
    """Enriched drawing index entry from the master XLSX."""
    drawing_number: str
    drawing_type: str
    drawing_title: str = ""
    current_revision: str = ""
    current_revision_date: str = ""
    design_phase: str = ""
    revision_history: list = field(default_factory=list)

    def to_dict(self):
        return {
            "drawing_number": self.drawing_number,
            "drawing_type": self.drawing_type,
            "drawing_title": self.drawing_title,
            "current_revision": self.current_revision,
            "current_revision_date": self.current_revision_date,
            "design_phase": self.design_phase,
            "revision_history": self.revision_history,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DrawingData:
    """Complete extracted data from a single drawing."""
    drawing_id: str               # e.g., "NRE-EC-301.0"
    file_path: str
    file_type: str                # "pdf", "dxf", "dwg"
    drawing_type: str = ""        # DrawingType value, e.g. "AC_SCHEMATIC"
    title_block: TitleBlock = field(default_factory=TitleBlock)
    components: dict = field(default_factory=dict)    # component_id -> Component
    connections: list = field(default_factory=list)    # All connections
    cross_references: list = field(default_factory=list)  # Drawing IDs referenced
    all_labels: list = field(default_factory=list)    # All text labels
    notes: list = field(default_factory=list)          # Drawing notes
    raw_text: str = ""
    cable_schedule: list = field(default_factory=list)  # Cable specs found
    terminal_blocks: dict = field(default_factory=dict)  # TB_id -> list of terminals
    voltage_levels: list = field(default_factory=list)   # All voltage levels found
    index_metadata: dict = field(default_factory=dict)    # From drawing index XLSX

    def to_dict(self):
        return {
            "drawing_id": self.drawing_id,
            "file_path": self.file_path,
            "file_type": self.file_type,
            "drawing_type": self.drawing_type,
            "title_block": self.title_block.to_dict(),
            "components": {k: v.to_dict() for k, v in self.components.items()},
            "connections": [c.to_dict() for c in self.connections],
            "cross_references": self.cross_references,
            "all_labels": [l.to_dict() for l in self.all_labels],
            "notes": self.notes,
            "raw_text": self.raw_text,
            "cable_schedule": self.cable_schedule,
            "terminal_blocks": self.terminal_blocks,
            "voltage_levels": self.voltage_levels,
            "index_metadata": self.index_metadata,
        }

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d):
        dd = cls(
            drawing_id=d["drawing_id"],
            file_path=d["file_path"],
            file_type=d["file_type"],
            drawing_type=d.get("drawing_type", ""),
        )
        dd.title_block = TitleBlock.from_dict(d.get("title_block", {}))
        dd.components = {
            k: Component.from_dict(v)
            for k, v in d.get("components", {}).items()
        }
        dd.connections = [Connection.from_dict(c) for c in d.get("connections", [])]
        dd.cross_references = d.get("cross_references", [])
        dd.all_labels = [TextLabel.from_dict(l) for l in d.get("all_labels", [])]
        dd.notes = d.get("notes", [])
        dd.raw_text = d.get("raw_text", "")
        dd.cable_schedule = d.get("cable_schedule", [])
        dd.terminal_blocks = d.get("terminal_blocks", {})
        dd.voltage_levels = d.get("voltage_levels", [])
        dd.index_metadata = d.get("index_metadata", {})
        return dd


@dataclass
class Mismatch:
    """A detected mismatch between drawings."""
    mismatch_id: str
    severity: AlertSeverity
    component_id: str
    parameter: str                # What's mismatched
    drawings_involved: list       # List of drawing IDs
    values_found: dict            # drawing_id -> value found
    message: str
    recommendation: str = ""
    resolution_options: list = field(default_factory=list)

    def to_dict(self):
        return {
            "mismatch_id": self.mismatch_id,
            "severity": self.severity.value,
            "component_id": self.component_id,
            "parameter": self.parameter,
            "drawings_involved": self.drawings_involved,
            "values_found": self.values_found,
            "message": self.message,
            "recommendation": self.recommendation,
            "resolution_options": self.resolution_options,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            mismatch_id=d["mismatch_id"],
            severity=AlertSeverity(d["severity"]),
            component_id=d["component_id"],
            parameter=d["parameter"],
            drawings_involved=d.get("drawings_involved", []),
            values_found=d.get("values_found", {}),
            message=d.get("message", ""),
            recommendation=d.get("recommendation", ""),
            resolution_options=d.get("resolution_options", []),
        )
