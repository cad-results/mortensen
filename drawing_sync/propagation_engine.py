"""Attribute propagation engine with authority-based conflict resolution."""

import json
import hashlib
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from .authority import AuthorityConfig


@dataclass
class PropagationAction:
    """A single proposed or applied attribute propagation."""
    action_id: str
    source_drawing_id: str
    target_drawing_id: str
    component_id: str
    parameter: str
    old_value: str
    new_value: str
    authority_basis: str
    status: str = "PROPOSED"


class PropagationEngine:
    """Engine that plans and applies attribute propagation using authority rules."""

    def __init__(self, db, authority: AuthorityConfig, audit=None):
        self.db = db
        self.authority = authority
        self.audit = audit  # Optional AuditTrail instance

    def plan_propagation(
        self,
        component_id: str,
        source_drawing_id: Optional[str] = None,
    ) -> List[PropagationAction]:
        """Plan propagation actions for a single component.

        1. Get all instances of this component across drawings
        2. For each parameter, collect drawing_id -> value pairs
        3. Determine the authoritative drawing via authority rules
        4. If source_drawing_id is specified, use it instead
        5. Create PropagationAction for each target with a differing value

        Returns list of proposed actions (status=PROPOSED).
        """
        instances = self.db.get_component_across_drawings(component_id)
        if len(instances) < 2:
            return []

        # Build drawing_id -> drawing_type lookup
        drawing_types = self._get_drawing_types(instances)

        # Determine component type from first instance
        component_type = instances[0]["component_type"] if instances else "*"

        # Build parameter -> {drawing_id: value} map
        param_values = {}
        for inst in instances:
            vals = json.loads(inst["values_json"])
            for v in vals:
                param = v["parameter"]
                if param not in param_values:
                    param_values[param] = {}
                # If a drawing already has a value for this parameter, keep the
                # first one (some drawings have duplicate parameter entries).
                if inst["drawing_id"] not in param_values[param]:
                    param_values[param][inst["drawing_id"]] = v["value"]

        actions = []

        for param, dwg_values in param_values.items():
            if len(dwg_values) < 2:
                # Only one drawing has this parameter — nothing to propagate
                # but we can still propose propagation to drawings without the value
                continue

            # Check if values actually differ
            unique_values = set(dwg_values.values())
            if len(unique_values) <= 1:
                continue  # All drawings agree on this parameter

            # Determine source drawing
            if source_drawing_id and source_drawing_id in dwg_values:
                source_id = source_drawing_id
                source_type = drawing_types.get(source_id, "")
                authority_basis = f"Manual override: {source_id} specified as source"
            else:
                # Build candidates dict: drawing_id -> drawing_type
                candidates = {
                    did: drawing_types.get(did, "")
                    for did in dwg_values
                }
                source_id = self.authority.get_authoritative_drawing(
                    param, component_type, candidates,
                )
                if source_id is None:
                    # No authority rule matches; fall back to first drawing with a value
                    source_id = next(iter(dwg_values))
                    authority_basis = (
                        f"No authority rule for {param}; "
                        f"defaulting to first available drawing"
                    )
                else:
                    source_type = drawing_types.get(source_id, "")
                    authority_basis = self.authority.get_authority_basis(
                        param, component_type, source_type,
                    )

            source_value = dwg_values[source_id]

            # Create actions for each target drawing that differs
            for target_id, target_value in dwg_values.items():
                if target_id == source_id:
                    continue
                if target_value == source_value:
                    continue

                action_id = self._make_action_id(
                    source_id, target_id, component_id, param,
                )
                actions.append(PropagationAction(
                    action_id=action_id,
                    source_drawing_id=source_id,
                    target_drawing_id=target_id,
                    component_id=component_id,
                    parameter=param,
                    old_value=target_value,
                    new_value=source_value,
                    authority_basis=authority_basis,
                    status="PROPOSED",
                ))

        return actions

    def plan_all_propagations(self) -> List[PropagationAction]:
        """Plan propagation for all shared components (appear in 2+ drawings).

        Returns combined list of all proposed actions.
        """
        shared = self.db.get_shared_components(min_drawings=2)
        all_actions = []

        for comp_id in shared:
            actions = self.plan_propagation(comp_id)
            all_actions.extend(actions)

        return all_actions

    def apply_propagation(
        self,
        actions: List[PropagationAction],
        dry_run: bool = False,
    ) -> List[PropagationAction]:
        """Apply a list of propagation actions.

        Args:
            actions: List of PropagationAction objects to apply.
            dry_run: If True, mark as DRY_RUN without modifying the database.

        Returns the actions with updated statuses.
        """
        for action in actions:
            if action.status != "PROPOSED":
                continue

            if dry_run:
                action.status = "DRY_RUN"
                continue

            try:
                success = self._apply_single(action)
                if success:
                    action.status = "APPLIED"
                else:
                    action.status = "FAILED"
            except Exception:
                action.status = "FAILED"

            # Log to propagation_log regardless of outcome
            self.db.log_propagation(
                action_id=action.action_id,
                timestamp=datetime.now().isoformat(),
                source=action.source_drawing_id,
                target=action.target_drawing_id,
                component_id=action.component_id,
                parameter=action.parameter,
                old_val=action.old_value,
                new_val=action.new_value,
                authority_basis=action.authority_basis,
                status=action.status,
            )

            # Record audit decision
            self._record_propagation_decision(action)

        return actions

    def _record_propagation_decision(self, action: PropagationAction) -> None:
        """Record a PROPAGATION decision to the audit trail (if available)."""
        if not self.audit:
            return
        try:
            from .audit import DecisionRecord
            self.audit.record_decision(DecisionRecord(
                decision_id=self._make_action_id(
                    action.source_drawing_id, action.target_drawing_id,
                    action.component_id, action.parameter,
                ) + "_decision",
                timestamp=datetime.now().isoformat(),
                decision_type="PROPAGATION",
                component_id=action.component_id,
                drawing_id=action.target_drawing_id,
                input_data={
                    "source_drawing": action.source_drawing_id,
                    "target_drawing": action.target_drawing_id,
                    "parameter": action.parameter,
                    "old_value": action.old_value,
                    "new_value": action.new_value,
                },
                reasoning=action.authority_basis,
                confidence=1.0,
                outcome=f"Updated {action.parameter} from '{action.old_value}' to '{action.new_value}'",
                alternatives=[{"keep_old": action.old_value, "reason": "Target drawing's original value"}],
            ))
        except Exception:
            pass  # Never fail the main operation

    def _apply_single(self, action: PropagationAction) -> bool:
        """Apply a single propagation action by updating the target drawing's DB value.

        1. Read current values_json from components table
        2. Parse the JSON array of value dicts
        3. Find the entry matching action.parameter and update its value
        4. Write back the updated JSON
        5. Log to change_log
        6. Return True on success
        """
        success = self.db.update_component_value(
            drawing_id=action.target_drawing_id,
            component_id=action.component_id,
            parameter=action.parameter,
            new_value=action.new_value,
        )

        if success:
            # Also log to change_log for audit trail
            self.db.log_change(
                drawing_id=action.target_drawing_id,
                component_id=action.component_id,
                change_type="PROPAGATION",
                old_value=action.old_value,
                new_value=action.new_value,
                description=(
                    f"Propagated {action.parameter} from {action.source_drawing_id}: "
                    f"{action.old_value} -> {action.new_value} "
                    f"({action.authority_basis})"
                ),
            )

        return success

    def _make_action_id(
        self, source: str, target: str, component: str, parameter: str,
    ) -> str:
        """Generate a deterministic action ID (24-char SHA-256 prefix)."""
        raw = f"{source}|{target}|{component}|{parameter}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def get_propagation_summary(self, actions: List[PropagationAction]) -> dict:
        """Summarize a list of propagation actions.

        Returns dict with:
            total, by_status counts, affected_components, affected_drawings
        """
        by_status = {}
        affected_components = set()
        affected_drawings = set()

        for a in actions:
            by_status[a.status] = by_status.get(a.status, 0) + 1
            affected_components.add(a.component_id)
            affected_drawings.add(a.target_drawing_id)

        return {
            "total": len(actions),
            "by_status": by_status,
            "affected_components": sorted(affected_components),
            "affected_drawings": sorted(affected_drawings),
        }

    def _get_drawing_types(self, instances: list) -> Dict[str, str]:
        """Build a drawing_id -> drawing_type lookup for the given instances.

        Queries the drawings table for each unique drawing_id.
        """
        drawing_types = {}
        seen = set()
        for inst in instances:
            did = inst["drawing_id"]
            if did in seen:
                continue
            seen.add(did)
            row = self.db.conn.execute(
                "SELECT drawing_type FROM drawings WHERE drawing_id = ?",
                (did,),
            ).fetchone()
            if row and row["drawing_type"]:
                drawing_types[did] = row["drawing_type"]
            else:
                drawing_types[did] = ""
        return drawing_types
