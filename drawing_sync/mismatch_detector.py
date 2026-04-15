"""Mismatch Detection and Alert System.

Compares component data across all drawings to detect:
1. Value mismatches (voltage, current, impedance differ between drawings)
2. Component naming inconsistencies
3. Missing cross-references (drawing references component but doesn't link back)
4. Cable specification mismatches
5. Terminal block connection conflicts
6. Broken cross-reference chains
7. Electrical calculation errors (overcurrent settings vs equipment ratings)
"""

import json
import hashlib
from typing import Dict, List, Optional
from datetime import datetime

from .models import (
    Mismatch, AlertSeverity, ComponentType, ComponentValue,
)
from .db import ComponentDatabase
from .authority import AuthorityConfig


class MismatchDetector:
    """Detects mismatches and inconsistencies across drawings."""

    def __init__(self, db: ComponentDatabase):
        self.db = db
        self.authority = AuthorityConfig()

    def run_all_checks(self) -> list:
        """Run all mismatch detection checks. Returns list of Mismatch objects.

        Clears previously detected mismatches and re-runs all checks from scratch
        so resolved issues don't remain as stale alerts.
        """
        # Mark all existing mismatches as resolved before re-checking
        self.db.conn.execute("""
            UPDATE mismatches SET resolved = 1, resolved_at = ?
            WHERE resolved = 0
        """, (datetime.now().isoformat(),))
        self.db.conn.commit()

        mismatches = []

        mismatches.extend(self.check_value_mismatches())
        mismatches.extend(self.check_component_type_consistency())
        mismatches.extend(self.check_cross_reference_integrity())
        mismatches.extend(self.check_cable_spec_consistency())
        mismatches.extend(self.check_terminal_block_conflicts())
        mismatches.extend(self.check_voltage_level_consistency())
        mismatches.extend(self.check_relay_assignment_consistency())
        mismatches.extend(self.check_orphan_components())
        mismatches.extend(self.check_relay_breaker_trip_paths())
        mismatches.extend(self.check_lockout_relay_completeness())
        mismatches.extend(self.check_ct_pt_relay_association())
        mismatches.extend(self.check_dc_supply_completeness())

        # Store all current mismatches (INSERT OR REPLACE re-opens resolved ones)
        for m in mismatches:
            self.db.store_mismatch(m)

        return mismatches

    def check_value_mismatches(self) -> list:
        """Check for value mismatches of the same component across drawings.

        If component 52-L1 has voltage_rating "138kV" in EC-100.0 but "34.5kV"
        in EC-200.0, that's a CRITICAL mismatch.
        """
        mismatches = []
        shared = self.db.get_shared_components(min_drawings=2)

        for comp_id, drawings in shared.items():
            # Get values for this component in each drawing
            values_by_drawing = self.db.get_component_values(comp_id)

            # Group by parameter
            param_values = {}  # parameter -> {drawing_id: value}
            for dwg_id, vals_json in values_by_drawing.items():
                vals = vals_json if isinstance(vals_json, list) else json.loads(vals_json)
                for v in vals:
                    param = v.get("parameter")
                    val = v.get("value")
                    if not param or not val:
                        continue
                    if param not in param_values:
                        param_values[param] = {}
                    param_values[param][dwg_id] = val

            # Check each parameter for mismatches
            for param, dwg_vals in param_values.items():
                unique_values = set(dwg_vals.values())
                if len(unique_values) > 1:
                    severity = self._value_mismatch_severity(param, unique_values)

                    mismatch = Mismatch(
                        mismatch_id=self._make_id(comp_id, param, "value_mismatch"),
                        severity=severity,
                        component_id=comp_id,
                        parameter=param,
                        drawings_involved=list(dwg_vals.keys()),
                        values_found=dwg_vals,
                        message=(
                            f"Component {comp_id} has conflicting {param} values: "
                            f"{', '.join(f'{d}={v}' for d, v in dwg_vals.items())}"
                        ),
                        recommendation=(
                            f"Verify the correct {param} for {comp_id} and update "
                            f"all drawings to match."
                        ),
                    )

                    # Enrich with authority-based resolution options
                    drawing_types = {}
                    for d_id in dwg_vals.keys():
                        row = self.db.conn.execute(
                            "SELECT drawing_type FROM drawings WHERE drawing_id = ?", (d_id,)
                        ).fetchone()
                        if row:
                            drawing_types[d_id] = row[0] if row[0] else "UNKNOWN"

                    # Look up component type for this component
                    comp_type_row = self.db.conn.execute(
                        "SELECT component_type FROM components WHERE component_id = ? LIMIT 1",
                        (comp_id,)
                    ).fetchone()
                    comp_type_str = comp_type_row[0] if comp_type_row else "*"

                    auth_drawing = self.authority.get_authoritative_drawing(
                        param, comp_type_str, drawing_types
                    )
                    mismatch.resolution_options = [
                        {
                            "drawing_id": d_id,
                            "value": str(v),
                            "drawing_type": drawing_types.get(d_id, "UNKNOWN"),
                            "is_authoritative": d_id == auth_drawing,
                        }
                        for d_id, v in dwg_vals.items()
                    ]
                    if auth_drawing:
                        mismatch.recommendation = (
                            f"Use value from {auth_drawing} ({drawing_types.get(auth_drawing, 'UNKNOWN')}) "
                            f"as source of truth. {self.authority.get_authority_basis(param, comp_type_str, drawing_types.get(auth_drawing, ''))}"
                        )

                    mismatches.append(mismatch)

        return mismatches

    def check_component_type_consistency(self) -> list:
        """Check that the same component ID has the same type everywhere."""
        mismatches = []

        rows = self.db.conn.execute("""
            SELECT component_id, drawing_id, component_type
            FROM components
            ORDER BY component_id
        """).fetchall()

        # Group by component_id
        comp_types = {}
        for r in rows:
            cid = r["component_id"]
            if cid not in comp_types:
                comp_types[cid] = {}
            comp_types[cid][r["drawing_id"]] = r["component_type"]

        for comp_id, type_map in comp_types.items():
            unique_types = set(type_map.values())
            if len(unique_types) > 1:
                mismatch = Mismatch(
                    mismatch_id=self._make_id(comp_id, "type", "type_mismatch"),
                    severity=AlertSeverity.WARNING,
                    component_id=comp_id,
                    parameter="component_type",
                    drawings_involved=list(type_map.keys()),
                    values_found=type_map,
                    message=(
                        f"Component {comp_id} classified as different types: "
                        f"{', '.join(f'{d}={t}' for d, t in type_map.items())}"
                    ),
                    recommendation=(
                        f"Verify component type for {comp_id} — inconsistent "
                        f"classification may indicate a naming error."
                    ),
                )
                mismatches.append(mismatch)

        return mismatches

    def check_cross_reference_integrity(self) -> list:
        """Check that cross-references between drawings are valid and bidirectional."""
        mismatches = []

        all_drawings = set(self.db.get_all_drawing_ids())

        for drawing_id in all_drawings:
            refs_json = self.db.conn.execute(
                "SELECT cross_references_json FROM drawings WHERE drawing_id = ?",
                (drawing_id,),
            ).fetchone()

            if not refs_json:
                continue

            refs = json.loads(refs_json["cross_references_json"])
            for ref in refs:
                # Check if referenced drawing exists
                full_ref = f"NRE-{ref}" if not ref.startswith("NRE-") else ref
                if full_ref not in all_drawings:
                    mismatch = Mismatch(
                        mismatch_id=self._make_id(drawing_id, ref, "broken_ref"),
                        severity=AlertSeverity.WARNING,
                        component_id=drawing_id,
                        parameter="cross_reference",
                        drawings_involved=[drawing_id],
                        values_found={drawing_id: ref},
                        message=(
                            f"Drawing {drawing_id} references {ref} which "
                            f"is not in the scanned drawing set."
                        ),
                        recommendation=(
                            f"Verify that drawing {ref} exists and has been scanned."
                        ),
                    )
                    mismatches.append(mismatch)

        return mismatches

    def check_cable_spec_consistency(self) -> list:
        """Check that cable specs between connected components are consistent."""
        mismatches = []

        # Get all connections with cable specs
        rows = self.db.conn.execute("""
            SELECT drawing_id, from_component, to_component, cable_spec
            FROM connections
            WHERE cable_spec != ''
        """).fetchall()

        # Group by component pair
        pair_cables = {}
        for r in rows:
            pair = tuple(sorted([r["from_component"], r["to_component"]]))
            if pair not in pair_cables:
                pair_cables[pair] = {}
            pair_cables[pair][r["drawing_id"]] = r["cable_spec"]

        for pair, cable_map in pair_cables.items():
            unique_specs = set(cable_map.values())
            if len(unique_specs) > 1:
                mismatch = Mismatch(
                    mismatch_id=self._make_id(
                        f"{pair[0]}-{pair[1]}", "cable", "cable_mismatch",
                    ),
                    severity=AlertSeverity.WARNING,
                    component_id=f"{pair[0]} <-> {pair[1]}",
                    parameter="cable_specification",
                    drawings_involved=list(cable_map.keys()),
                    values_found=cable_map,
                    message=(
                        f"Cable between {pair[0]} and {pair[1]} has different "
                        f"specs: {', '.join(f'{d}={s}' for d, s in cable_map.items())}"
                    ),
                    recommendation="Verify cable specification is consistent.",
                )
                mismatches.append(mismatch)

        return mismatches

    def check_terminal_block_conflicts(self) -> list:
        """Check for terminal block numbering conflicts across drawings."""
        mismatches = []

        rows = self.db.conn.execute(
            "SELECT drawing_id, terminal_blocks_json FROM drawings",
        ).fetchall()

        # Build global terminal block map
        tb_map = {}  # tb_id -> {drawing_id: terminals}
        for r in rows:
            tbs = json.loads(r["terminal_blocks_json"])
            for tb_id, terminals in tbs.items():
                if tb_id not in tb_map:
                    tb_map[tb_id] = {}
                tb_map[tb_id][r["drawing_id"]] = terminals

        # Check for conflicts — same terminal used differently
        for tb_id, drawing_terminals in tb_map.items():
            if len(drawing_terminals) < 2:
                continue

            # Get the union of all terminals — check for gaps or overlaps
            all_terminals = set()
            for terminals in drawing_terminals.values():
                all_terminals.update(terminals)

            # If number of terminals varies significantly, flag it
            terminal_counts = {
                dwg: len(terms) for dwg, terms in drawing_terminals.items()
            }
            counts = list(terminal_counts.values())
            if max(counts) > 2 * min(counts) and min(counts) > 0:
                mismatch = Mismatch(
                    mismatch_id=self._make_id(tb_id, "terminals", "tb_conflict"),
                    severity=AlertSeverity.INFO,
                    component_id=tb_id,
                    parameter="terminal_count",
                    drawings_involved=list(terminal_counts.keys()),
                    values_found={
                        d: str(c) for d, c in terminal_counts.items()
                    },
                    message=(
                        f"Terminal block {tb_id} has varying terminal counts: "
                        f"{terminal_counts}"
                    ),
                    recommendation=(
                        f"Verify terminal block {tb_id} wiring schedule is complete."
                    ),
                )
                mismatches.append(mismatch)

        return mismatches

    def check_voltage_level_consistency(self) -> list:
        """Check that voltage levels are consistent across related drawings."""
        mismatches = []

        # Get all components with voltage values
        rows = self.db.conn.execute("""
            SELECT component_id, drawing_id, values_json
            FROM components
        """).fetchall()

        comp_voltages = {}
        for r in rows:
            vals = json.loads(r["values_json"])
            for v in vals:
                if v["parameter"] == "voltage_rating":
                    cid = r["component_id"]
                    if cid not in comp_voltages:
                        comp_voltages[cid] = {}
                    comp_voltages[cid][r["drawing_id"]] = v["value"]

        # Check each component for voltage consistency
        for comp_id, volt_map in comp_voltages.items():
            unique_voltages = set(volt_map.values())
            if len(unique_voltages) > 1:
                mismatch = Mismatch(
                    mismatch_id=self._make_id(comp_id, "voltage", "voltage_mismatch"),
                    severity=AlertSeverity.CRITICAL,
                    component_id=comp_id,
                    parameter="voltage_rating",
                    drawings_involved=list(volt_map.keys()),
                    values_found=volt_map,
                    message=(
                        f"CRITICAL: {comp_id} has different voltage ratings: "
                        f"{volt_map}"
                    ),
                    recommendation=(
                        f"Immediately verify the voltage rating of {comp_id}. "
                        f"Wrong voltage can cause equipment damage or safety hazard."
                    ),
                )

                # Enrich with authority-based resolution options
                drawing_types = {}
                for d_id in volt_map.keys():
                    row = self.db.conn.execute(
                        "SELECT drawing_type FROM drawings WHERE drawing_id = ?", (d_id,)
                    ).fetchone()
                    if row:
                        drawing_types[d_id] = row[0] if row[0] else "UNKNOWN"

                comp_type_row = self.db.conn.execute(
                    "SELECT component_type FROM components WHERE component_id = ? LIMIT 1",
                    (comp_id,)
                ).fetchone()
                comp_type_str = comp_type_row[0] if comp_type_row else "*"

                auth_drawing = self.authority.get_authoritative_drawing(
                    "voltage_rating", comp_type_str, drawing_types
                )
                mismatch.resolution_options = [
                    {
                        "drawing_id": d_id,
                        "value": str(v),
                        "drawing_type": drawing_types.get(d_id, "UNKNOWN"),
                        "is_authoritative": d_id == auth_drawing,
                    }
                    for d_id, v in volt_map.items()
                ]
                if auth_drawing:
                    mismatch.recommendation = (
                        f"Use value from {auth_drawing} ({drawing_types.get(auth_drawing, 'UNKNOWN')}) "
                        f"as source of truth. {self.authority.get_authority_basis('voltage_rating', comp_type_str, drawing_types.get(auth_drawing, ''))}"
                    )

                mismatches.append(mismatch)

        return mismatches

    def check_relay_assignment_consistency(self) -> list:
        """Check that relay models are consistently assigned across drawings.

        e.g., if 50-L1 is shown as SEL-451 in one drawing but SEL-487 in another.
        """
        mismatches = []

        # Find components that have relay model associations
        rows = self.db.conn.execute("""
            SELECT component_id, drawing_id, attributes_json
            FROM components
            WHERE component_type = 'RELAY'
        """).fetchall()

        # This check is based on proximity associations — if a relay model
        # appears near a device function number differently across drawings,
        # that's a potential issue.
        relay_devices = {}
        for r in rows:
            attrs = json.loads(r["attributes_json"])
            device = attrs.get("associated_device", "")
            if device:
                relay_id = r["component_id"]
                if relay_id not in relay_devices:
                    relay_devices[relay_id] = {}
                relay_devices[relay_id][r["drawing_id"]] = device

        for relay_id, device_map in relay_devices.items():
            unique_devices = set(device_map.values())
            if len(unique_devices) > 1:
                mismatch = Mismatch(
                    mismatch_id=self._make_id(relay_id, "device", "relay_mismatch"),
                    severity=AlertSeverity.WARNING,
                    component_id=relay_id,
                    parameter="associated_device",
                    drawings_involved=list(device_map.keys()),
                    values_found=device_map,
                    message=(
                        f"Relay {relay_id} associated with different devices: "
                        f"{device_map}"
                    ),
                    recommendation=(
                        f"Verify which device {relay_id} is protecting."
                    ),
                )
                mismatches.append(mismatch)

        return mismatches

    def check_orphan_components(self) -> list:
        """Check for components that appear in only one drawing
        but are referenced by cross-references in others.
        """
        mismatches = []

        # Get components that appear in exactly one drawing
        rows = self.db.conn.execute("""
            SELECT component_id, drawing_id
            FROM components
            GROUP BY component_id
            HAVING COUNT(drawing_id) = 1
        """).fetchall()

        single_comps = {r["component_id"]: r["drawing_id"] for r in rows}

        # Check if any other drawing cross-references the drawing containing
        # this component but doesn't also contain the component
        for comp_id, dwg_id in single_comps.items():
            # Get drawings that reference this drawing
            referencing = self.db.conn.execute("""
                SELECT drawing_id, cross_references_json
                FROM drawings
                WHERE drawing_id != ?
            """, (dwg_id,)).fetchall()

            dwg_ref = dwg_id.replace("NRE-", "")
            referencing_dwgs = []
            for r in referencing:
                refs = json.loads(r["cross_references_json"])
                if dwg_ref in refs:
                    referencing_dwgs.append(r["drawing_id"])

            if referencing_dwgs:
                mismatch = Mismatch(
                    mismatch_id=self._make_id(comp_id, dwg_id, "orphan"),
                    severity=AlertSeverity.INFO,
                    component_id=comp_id,
                    parameter="drawing_coverage",
                    drawings_involved=[dwg_id] + referencing_dwgs,
                    values_found={
                        dwg_id: "present",
                        **{d: "referenced but missing" for d in referencing_dwgs},
                    },
                    message=(
                        f"Component {comp_id} only appears in {dwg_id} but "
                        f"{len(referencing_dwgs)} other drawing(s) reference that "
                        f"drawing: {', '.join(referencing_dwgs[:5])}"
                    ),
                    recommendation=(
                        f"Verify whether {comp_id} should also appear in the "
                        f"referencing drawings."
                    ),
                )
                mismatches.append(mismatch)

        return mismatches

    # ------------------------------------------------------------------
    # Protection logic checking helpers
    # ------------------------------------------------------------------

    def _get_drawings_by_type(self, drawing_type: str) -> List[str]:
        """Get all drawing IDs of a specific type."""
        rows = self.db.conn.execute(
            "SELECT drawing_id FROM drawings WHERE drawing_type = ?",
            (drawing_type,)
        ).fetchall()
        return [r[0] for r in rows]

    def _build_relay_breaker_map(self) -> Dict[str, str]:
        """Build a map of relay device suffix -> breaker component_id.

        E.g., 50-L1 has suffix L1, maps to 52-L1.
        """
        breakers = {}
        rows = self.db.conn.execute(
            "SELECT DISTINCT component_id FROM components WHERE component_type = '52'"
        ).fetchall()
        for row in rows:
            bid = row[0]
            if '-' in bid:
                suffix = bid.split('-', 1)[1]
                # Only map base breaker IDs (no dots — those are sub-components)
                if '.' not in suffix:
                    breakers[suffix] = bid
        return breakers

    def _get_component_connections(self, component_id: str) -> List[dict]:
        """Get all connections involving a component (as from or to)."""
        rows = self.db.conn.execute(
            "SELECT drawing_id, from_component, to_component, signal_type, cable_spec "
            "FROM connections WHERE from_component = ? OR to_component = ?",
            (component_id, component_id)
        ).fetchall()
        return [{"drawing_id": r[0], "from": r[1], "to": r[2],
                 "signal": r[3], "cable": r[4]} for r in rows]

    # ------------------------------------------------------------------
    # Protection logic checks (Package D)
    # ------------------------------------------------------------------

    def check_relay_breaker_trip_paths(self) -> List[Mismatch]:
        """Check that protective relays have a connection path to their breaker.

        For each protective relay (50-XX, 51-XX, 87-XX, 21-XX, 67-XX, 81-XX),
        extract the suffix, find the corresponding 52-XX breaker, and verify a
        connection path exists (direct or via lockout 86).
        """
        mismatches: List[Mismatch] = []

        # Protective relay component types (ANSI device numbers)
        relay_types = ('50', '51', '87', '21', '67', '81')

        # Get all protective relay components (base IDs only — skip sub-components)
        rows = self.db.conn.execute(
            "SELECT DISTINCT component_id, component_type FROM components "
            "WHERE component_type IN ({})".format(
                ','.join('?' for _ in relay_types)
            ),
            relay_types,
        ).fetchall()

        # Build suffix -> breaker map
        breaker_map = self._build_relay_breaker_map()

        # Get all lockout relay IDs for indirect path checking
        lockout_rows = self.db.conn.execute(
            "SELECT DISTINCT component_id FROM components WHERE component_type = '86'"
        ).fetchall()
        lockout_ids = {r[0] for r in lockout_rows}

        for row in rows:
            relay_id = row[0]
            # Skip sub-component IDs (e.g. 50-L1.FPP2.FO, 50-L1.PWR)
            if '.' in relay_id:
                continue

            if '-' not in relay_id:
                continue

            suffix = relay_id.split('-', 1)[1]
            expected_breaker = breaker_map.get(suffix)

            if not expected_breaker:
                # No matching breaker found — skip (not every relay maps to a breaker)
                continue

            # Check for direct connection between relay and breaker
            relay_conns = self.db.conn.execute(
                "SELECT drawing_id, from_component, to_component FROM connections "
                "WHERE (from_component = ? AND to_component = ?) "
                "OR (from_component = ? AND to_component = ?)",
                (relay_id, expected_breaker, expected_breaker, relay_id)
            ).fetchall()

            if relay_conns:
                continue  # Direct path found

            # Check indirect path via lockout relays (86-XX)
            # Step 1: find lockouts connected to this relay
            relay_to_lockout = self.db.conn.execute(
                "SELECT DISTINCT to_component FROM connections "
                "WHERE from_component = ? AND to_component IN ({})".format(
                    ','.join('?' for _ in lockout_ids)
                ),
                (relay_id, *lockout_ids)
            ).fetchall() if lockout_ids else []

            lockout_from_relay = self.db.conn.execute(
                "SELECT DISTINCT from_component FROM connections "
                "WHERE to_component = ? AND from_component IN ({})".format(
                    ','.join('?' for _ in lockout_ids)
                ),
                (relay_id, *lockout_ids)
            ).fetchall() if lockout_ids else []

            connected_lockouts = (
                {r[0] for r in relay_to_lockout} |
                {r[0] for r in lockout_from_relay}
            )

            # Step 2: check if any of those lockouts connect to the breaker
            indirect_path_found = False
            for lo_id in connected_lockouts:
                lo_to_brk = self.db.conn.execute(
                    "SELECT 1 FROM connections "
                    "WHERE (from_component = ? AND to_component = ?) "
                    "OR (from_component = ? AND to_component = ?) LIMIT 1",
                    (lo_id, expected_breaker, expected_breaker, lo_id)
                ).fetchone()
                if lo_to_brk:
                    indirect_path_found = True
                    break

            if indirect_path_found:
                continue

            # Also check for connections using variant names
            # (e.g., "50-L1 RELAY" -> "86MP" in the data)
            relay_variant = relay_id + " RELAY"
            variant_conns = self.db.conn.execute(
                "SELECT 1 FROM connections "
                "WHERE (from_component = ? OR to_component = ?) "
                "AND (from_component = ? OR to_component = ? "
                "     OR from_component LIKE '86%' OR to_component LIKE '86%' "
                "     OR from_component = ? OR to_component = ?) LIMIT 1",
                (relay_variant, relay_variant,
                 expected_breaker, expected_breaker,
                 expected_breaker, expected_breaker)
            ).fetchone()
            if variant_conns:
                continue

            # No path found — generate warning
            # Find which drawings contain this relay for context
            relay_drawings = self.db.conn.execute(
                "SELECT DISTINCT drawing_id FROM components WHERE component_id = ?",
                (relay_id,)
            ).fetchall()
            drawings_list = [r[0] for r in relay_drawings]

            mismatch = Mismatch(
                mismatch_id=self._make_id(relay_id, expected_breaker, "trip_path"),
                severity=AlertSeverity.WARNING,
                component_id=relay_id,
                parameter="trip_path",
                drawings_involved=drawings_list,
                values_found={
                    relay_id: "no trip path found",
                    expected_breaker: "expected breaker",
                },
                message=(
                    f"Relay {relay_id} has no connection path to breaker "
                    f"{expected_breaker} (direct or via lockout relay)."
                ),
                recommendation=(
                    f"Verify DC schematic trip path from {relay_id} to "
                    f"{expected_breaker}. Check for missing connections or "
                    f"lockout relay intermediate paths."
                ),
            )
            mismatches.append(mismatch)

        return mismatches

    def check_lockout_relay_completeness(self) -> List[Mismatch]:
        """Check that each lockout relay (86) has both inputs and outputs.

        A lockout relay should have at least one connection TO it (trip input
        from a protective relay) and at least one connection FROM it (trip output
        to a breaker).
        """
        mismatches: List[Mismatch] = []

        # Get all lockout relay components (base IDs, skip sub-components)
        rows = self.db.conn.execute(
            "SELECT DISTINCT component_id FROM components WHERE component_type = '86'"
        ).fetchall()

        for row in rows:
            lockout_id = row[0]

            # Get all connections involving this lockout
            conns_to = self.db.conn.execute(
                "SELECT drawing_id, from_component FROM connections "
                "WHERE to_component = ?",
                (lockout_id,)
            ).fetchall()

            conns_from = self.db.conn.execute(
                "SELECT drawing_id, to_component FROM connections "
                "WHERE from_component = ?",
                (lockout_id,)
            ).fetchall()

            has_input = len(conns_to) > 0
            has_output = len(conns_from) > 0

            # Get drawings containing this lockout
            lockout_drawings = self.db.conn.execute(
                "SELECT DISTINCT drawing_id FROM components WHERE component_id = ?",
                (lockout_id,)
            ).fetchall()
            drawings_list = [r[0] for r in lockout_drawings]

            if not has_input and not has_output:
                mismatch = Mismatch(
                    mismatch_id=self._make_id(lockout_id, "lockout_completeness"),
                    severity=AlertSeverity.WARNING,
                    component_id=lockout_id,
                    parameter="lockout_connections",
                    drawings_involved=drawings_list,
                    values_found={
                        lockout_id: "no inputs and no outputs",
                    },
                    message=(
                        f"Lockout relay {lockout_id} has no input connections "
                        f"(from protective relays) and no output connections "
                        f"(to breakers)."
                    ),
                    recommendation=(
                        f"Verify DC schematic wiring for {lockout_id}. "
                        f"Lockout relays should have trip inputs and breaker outputs."
                    ),
                )
                mismatches.append(mismatch)
            elif not has_input:
                mismatch = Mismatch(
                    mismatch_id=self._make_id(lockout_id, "lockout_no_input"),
                    severity=AlertSeverity.WARNING,
                    component_id=lockout_id,
                    parameter="lockout_input",
                    drawings_involved=drawings_list,
                    values_found={
                        lockout_id: "no input connections",
                        "outputs": str(len(conns_from)),
                    },
                    message=(
                        f"Lockout relay {lockout_id} has no input connections "
                        f"(no protective relay trips to it) but has "
                        f"{len(conns_from)} output connection(s)."
                    ),
                    recommendation=(
                        f"Verify that protective relays are wired to trip "
                        f"{lockout_id}."
                    ),
                )
                mismatches.append(mismatch)
            elif not has_output:
                mismatch = Mismatch(
                    mismatch_id=self._make_id(lockout_id, "lockout_no_output"),
                    severity=AlertSeverity.WARNING,
                    component_id=lockout_id,
                    parameter="lockout_output",
                    drawings_involved=drawings_list,
                    values_found={
                        lockout_id: "no output connections",
                        "inputs": str(len(conns_to)),
                    },
                    message=(
                        f"Lockout relay {lockout_id} has {len(conns_to)} input "
                        f"connection(s) but no output connections (no breaker trips)."
                    ),
                    recommendation=(
                        f"Verify that {lockout_id} is wired to trip the "
                        f"appropriate breaker(s)."
                    ),
                )
                mismatches.append(mismatch)

        return mismatches

    def check_ct_pt_relay_association(self) -> List[Mismatch]:
        """Check that each CT has at least one connection to a relay.

        Current transformers should feed protective relays. If a CT exists
        in the database but has no connections to any relay, that may indicate
        missing wiring data.
        """
        mismatches: List[Mismatch] = []

        # Get all CT components (base IDs)
        ct_rows = self.db.conn.execute(
            "SELECT DISTINCT component_id FROM components WHERE component_type = 'CT'"
        ).fetchall()

        # Relay types to check for association
        relay_types = ('50', '51', '87', '21', '67', '81', 'RELAY')

        # Get all relay component IDs for cross-referencing
        relay_rows = self.db.conn.execute(
            "SELECT DISTINCT component_id FROM components WHERE component_type IN ({})".format(
                ','.join('?' for _ in relay_types)
            ),
            relay_types,
        ).fetchall()
        relay_ids = {r[0] for r in relay_rows}

        for ct_row in ct_rows:
            ct_id = ct_row[0]

            # Get all connections involving this CT
            ct_conns = self.db.conn.execute(
                "SELECT from_component, to_component FROM connections "
                "WHERE from_component = ? OR to_component = ?",
                (ct_id, ct_id)
            ).fetchall()

            # Check if any connection endpoint is a relay
            has_relay_connection = False
            for conn in ct_conns:
                other = conn[1] if conn[0] == ct_id else conn[0]
                # Direct match to a relay component
                if other in relay_ids:
                    has_relay_connection = True
                    break
                # Check if the other component is a relay type by querying
                # (handles cases where connection uses a variant name)
                relay_check = self.db.conn.execute(
                    "SELECT 1 FROM components WHERE component_id = ? "
                    "AND component_type IN ({}) LIMIT 1".format(
                        ','.join('?' for _ in relay_types)
                    ),
                    (other, *relay_types)
                ).fetchone()
                if relay_check:
                    has_relay_connection = True
                    break

            if not has_relay_connection:
                # Get drawings containing this CT
                ct_drawings = self.db.conn.execute(
                    "SELECT DISTINCT drawing_id FROM components WHERE component_id = ?",
                    (ct_id,)
                ).fetchall()
                drawings_list = [r[0] for r in ct_drawings]

                connected_to = [
                    conn[1] if conn[0] == ct_id else conn[0]
                    for conn in ct_conns
                ]
                conn_summary = ', '.join(connected_to[:5]) if connected_to else 'none'

                mismatch = Mismatch(
                    mismatch_id=self._make_id(ct_id, "ct_relay_assoc"),
                    severity=AlertSeverity.INFO,
                    component_id=ct_id,
                    parameter="relay_association",
                    drawings_involved=drawings_list,
                    values_found={
                        ct_id: f"connections: {conn_summary}",
                        "relay_connection": "none found",
                    },
                    message=(
                        f"CT {ct_id} has no direct connection to any protective "
                        f"relay in the connections table."
                    ),
                    recommendation=(
                        f"Verify AC schematic wiring for {ct_id}. CTs should "
                        f"feed protective relays (50, 51, 87, etc.)."
                    ),
                )
                mismatches.append(mismatch)

        return mismatches

    def check_dc_supply_completeness(self) -> List[Mismatch]:
        """Check that relays and breakers have DC supply connections.

        For each relay and breaker in the database, check whether there are
        any connections involving DC power (component IDs containing 'DC' and
        'PWR', or attributes with POWER/DC signals).
        """
        mismatches: List[Mismatch] = []

        # Component types that require DC supply
        dc_component_types = (
            '50', '51', '87', '21', '67', '81', '52', '86', 'RELAY',
            'RTAC', 'CLOCK', 'PDC', 'NETSW', 'RTR', 'DFR', 'LTC',
        )

        rows = self.db.conn.execute(
            "SELECT DISTINCT component_id, component_type FROM components "
            "WHERE component_type IN ({})".format(
                ','.join('?' for _ in dc_component_types)
            ),
            dc_component_types,
        ).fetchall()

        for row in rows:
            comp_id = row[0]
            comp_type = row[1]

            # Skip sub-component IDs (e.g. 52-L1.TCA.DC.PWR1) — those are
            # the DC supply points themselves
            if '.' in comp_id:
                continue

            # Check 1: Connection to/from a DC power component
            # DC components typically look like "DC1.TCA.52-L1.PWR" or
            # "DC2.50-L1.PWR" — they contain both "DC" and the component ref
            dc_conns = self.db.conn.execute(
                "SELECT 1 FROM connections "
                "WHERE (from_component LIKE '%DC%' AND (to_component = ? OR to_component LIKE ?)) "
                "OR (to_component LIKE '%DC%' AND (from_component = ? OR from_component LIKE ?)) "
                "LIMIT 1",
                (comp_id, comp_id + '%', comp_id, comp_id + '%')
            ).fetchone()

            if dc_conns:
                continue

            # Check 2: Connection using "BREAKER XX" pattern (used in cable drawings)
            breaker_name = f"BREAKER {comp_id}"
            dc_breaker_conns = self.db.conn.execute(
                "SELECT 1 FROM connections "
                "WHERE (from_component LIKE '%DC%' AND to_component = ?) "
                "OR (to_component LIKE '%DC%' AND from_component = ?) "
                "LIMIT 1",
                (breaker_name, breaker_name)
            ).fetchone()

            if dc_breaker_conns:
                continue

            # Check 3: Component has .PWR sub-component (e.g. 52-L1.PWR exists)
            pwr_subcomp = self.db.conn.execute(
                "SELECT 1 FROM components WHERE component_id = ? LIMIT 1",
                (comp_id + '.PWR',)
            ).fetchone()

            if pwr_subcomp:
                continue

            # Check 4: Component attributes contain DC/POWER signals
            attr_rows = self.db.conn.execute(
                "SELECT attributes_json FROM components WHERE component_id = ?",
                (comp_id,)
            ).fetchall()

            has_dc_signal = False
            for attr_row in attr_rows:
                attrs = json.loads(attr_row[0])
                signals = attrs.get("signals", [])
                for sig in signals:
                    if "POWER" in sig.upper() or "DC" in sig.upper():
                        has_dc_signal = True
                        break
                if has_dc_signal:
                    break

            if has_dc_signal:
                continue

            # Check 5: Connection with PWR in component name
            pwr_conns = self.db.conn.execute(
                "SELECT 1 FROM connections "
                "WHERE (from_component LIKE ? OR to_component LIKE ?) "
                "LIMIT 1",
                (comp_id + '%PWR%', comp_id + '%PWR%')
            ).fetchone()

            if pwr_conns:
                continue

            # No DC supply found — generate INFO mismatch
            comp_drawings = self.db.conn.execute(
                "SELECT DISTINCT drawing_id FROM components WHERE component_id = ?",
                (comp_id,)
            ).fetchall()
            drawings_list = [r[0] for r in comp_drawings]

            mismatch = Mismatch(
                mismatch_id=self._make_id(comp_id, "dc_supply"),
                severity=AlertSeverity.INFO,
                component_id=comp_id,
                parameter="dc_supply",
                drawings_involved=drawings_list,
                values_found={
                    comp_id: "no DC supply connection found",
                },
                message=(
                    f"Component {comp_id} (type {comp_type}) has no identifiable "
                    f"DC supply connection or power signal."
                ),
                recommendation=(
                    f"Verify DC supply wiring for {comp_id}. Protection "
                    f"equipment requires DC power for operation."
                ),
            )
            mismatches.append(mismatch)

        return mismatches

    def get_mismatch_summary(self) -> dict:
        """Get a summary of all active mismatches."""
        active = self.db.get_active_mismatches()

        summary = {
            "total": len(active),
            "critical": sum(1 for m in active if m["severity"] == "CRITICAL"),
            "warning": sum(1 for m in active if m["severity"] == "WARNING"),
            "info": sum(1 for m in active if m["severity"] == "INFO"),
            "mismatches": active,
        }

        return summary

    @staticmethod
    def _value_mismatch_severity(parameter: str, unique_values: set) -> AlertSeverity:
        """Determine severity based on what parameter is mismatched."""
        critical_params = {"voltage_rating", "current_rating", "power_rating"}
        warning_params = {"impedance", "ratio", "cable_specification"}

        if parameter in critical_params:
            return AlertSeverity.CRITICAL
        elif parameter in warning_params:
            return AlertSeverity.WARNING
        else:
            return AlertSeverity.INFO

    @staticmethod
    def _make_id(*parts) -> str:
        """Generate a deterministic mismatch ID."""
        key = "|".join(str(p) for p in parts)
        return hashlib.sha256(key.encode()).hexdigest()[:24]
