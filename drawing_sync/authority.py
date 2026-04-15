"""Source-of-truth hierarchy and authority rules for attribute propagation."""

import json
from dataclasses import dataclass, field
from typing import List, Dict, Optional, ClassVar


@dataclass
class AuthorityRule:
    """A rule defining which drawing type is authoritative for a parameter."""
    parameter: str              # e.g., "voltage_rating", "current_rating"
    component_types: List[str]  # which component types this applies to, or ["*"] for all
    authority_order: List[str]  # DrawingType values in priority order (first = most authoritative)
    description: str = ""


class AuthorityConfig:
    """Manages authority rules for determining source-of-truth across drawing types."""

    DEFAULT_RULES: ClassVar[List[AuthorityRule]] = [
        AuthorityRule(
            parameter="voltage_rating",
            component_types=["*"],
            authority_order=["ONE_LINE", "AC_SCHEMATIC", "DC_SCHEMATIC", "PANEL_WIRING"],
            description="Voltage ratings defined on One-Line",
        ),
        AuthorityRule(
            parameter="current_rating",
            component_types=["*"],
            authority_order=["ONE_LINE", "AC_SCHEMATIC", "DC_SCHEMATIC"],
            description="Current ratings from One-Line",
        ),
        AuthorityRule(
            parameter="power_rating",
            component_types=["*"],
            authority_order=["ONE_LINE"],
            description="MVA/power ratings on One-Line only",
        ),
        AuthorityRule(
            parameter="impedance",
            component_types=["*"],
            authority_order=["ONE_LINE"],
            description="Impedance on One-Line",
        ),
        AuthorityRule(
            parameter="ratio",
            component_types=["CT", "PT", "VT", "CCVT"],
            authority_order=["ONE_LINE", "AC_SCHEMATIC"],
            description="CT/PT ratios from One-Line",
        ),
        AuthorityRule(
            parameter="cable_specification",
            component_types=["*"],
            authority_order=["DC_SCHEMATIC", "CABLE_WIRING", "PANEL_WIRING"],
            description="Cable specs from schematics",
        ),
        AuthorityRule(
            parameter="terminal_assignment",
            component_types=["*"],
            authority_order=["DC_SCHEMATIC", "PANEL_WIRING"],
            description="Terminal assignments from DC schematics",
        ),
        AuthorityRule(
            parameter="relay_settings",
            component_types=[
                "RELAY", "50", "51", "21",
                "67", "81", "87",
            ],
            authority_order=["DC_SCHEMATIC", "AC_SCHEMATIC"],
            description="Relay wiring from schematics",
        ),
    ]

    def __init__(self, config_path: Optional[str] = None):
        self.rules: List[AuthorityRule] = []
        if config_path:
            self._load_from_json(config_path)
        else:
            self._load_defaults()

    def get_authority(self, parameter: str, component_type: str = "*") -> List[str]:
        """Returns drawing types in authority order for a parameter.

        Checks component_type-specific rules first, then "*" wildcard rules.
        """
        # First look for a component-type-specific rule
        for rule in self.rules:
            if rule.parameter == parameter and component_type in rule.component_types and "*" not in rule.component_types:
                return list(rule.authority_order)

        # Fall back to wildcard rules
        for rule in self.rules:
            if rule.parameter == parameter and "*" in rule.component_types:
                return list(rule.authority_order)

        return []

    def get_authoritative_drawing(
        self,
        parameter: str,
        component_type: str,
        candidates: Dict[str, str],
    ) -> Optional[str]:
        """Given a dict of {drawing_id: drawing_type}, returns the drawing_id
        with highest authority for this parameter. Returns None if no rule matches.
        """
        authority_order = self.get_authority(parameter, component_type)
        if not authority_order:
            return None

        # For each drawing type in priority order, find a candidate with that type
        for dtype in authority_order:
            for drawing_id, drawing_type in candidates.items():
                if drawing_type == dtype:
                    return drawing_id

        return None

    def get_authority_basis(
        self,
        parameter: str,
        component_type: str,
        drawing_type: str,
    ) -> str:
        """Returns a human-readable explanation of why a drawing type is authoritative."""
        authority_order = self.get_authority(parameter, component_type)
        if not authority_order:
            return f"No authority rule found for {parameter}"

        if drawing_type in authority_order:
            rank = authority_order.index(drawing_type) + 1
            total = len(authority_order)
            return f"{drawing_type} is authoritative for {parameter} (priority {rank} of {total})"

        return f"{drawing_type} is not in the authority chain for {parameter}"

    def _load_defaults(self) -> None:
        """Load the default authority rules."""
        self.rules = [
            AuthorityRule(
                parameter=r.parameter,
                component_types=list(r.component_types),
                authority_order=list(r.authority_order),
                description=r.description,
            )
            for r in self.DEFAULT_RULES
        ]

    def _load_from_json(self, path: str) -> None:
        """Load rules from a JSON file."""
        with open(path, "r") as f:
            data = json.load(f)

        self.rules = []
        for entry in data.get("rules", []):
            rule = AuthorityRule(
                parameter=entry["parameter"],
                component_types=entry.get("component_types", ["*"]),
                authority_order=entry["authority_order"],
                description=entry.get("description", ""),
            )
            self.rules.append(rule)

    def save_to_json(self, path: str) -> None:
        """Export rules as formatted JSON."""
        data = {
            "rules": [
                {
                    "parameter": r.parameter,
                    "component_types": r.component_types,
                    "authority_order": r.authority_order,
                    "description": r.description,
                }
                for r in self.rules
            ]
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def get_all_rules(self) -> List[AuthorityRule]:
        """Returns all authority rules."""
        return list(self.rules)
