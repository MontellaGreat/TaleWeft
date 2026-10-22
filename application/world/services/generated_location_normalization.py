"""Normalize AI-generated location trees before strict Bible validation."""
from __future__ import annotations

import logging
from typing import Any, Mapping


logger = logging.getLogger(__name__)


def prepare_generated_locations(novel_id: str, locations: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep generated locations while repairing missing parents and cycles.

    Both the setup invocation and the legacy generator use this boundary. User
    edits still go through the strict domain validator without these repairs.
    """
    prepared: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    raw_to_final: dict[str, str] = {}

    for idx, loc_data in enumerate(locations or []):
        if not isinstance(loc_data, Mapping):
            continue
        name = str(loc_data.get("name") or "").strip()
        if not name:
            continue
        raw_id = loc_data.get("id")
        normalized_raw_id = raw_id.strip() if isinstance(raw_id, str) else ""
        location_id = normalized_raw_id or f"{novel_id}-loc-{idx + 1}"
        if location_id in seen_ids:
            fallback_id = f"{novel_id}-loc-{idx + 1}-{len(seen_ids)}"
            location_id = fallback_id
            suffix = 1
            while location_id in seen_ids:
                location_id = f"{fallback_id}-{suffix}"
                suffix += 1
        seen_ids.add(location_id)
        if normalized_raw_id:
            raw_to_final.setdefault(normalized_raw_id, location_id)

        prepared.append({
            "location_id": location_id,
            "name": name,
            "description": str(loc_data.get("description") or ""),
            "location_type": str(loc_data.get("type") or loc_data.get("location_type") or "场景"),
            "connections": loc_data.get("connections", []),
            "raw_parent_id": loc_data.get("parent_id"),
        })

    valid_ids = {item["location_id"] for item in prepared}
    for item in prepared:
        raw_parent = item.pop("raw_parent_id", None)
        parent_id = raw_parent.strip() if isinstance(raw_parent, str) and raw_parent.strip() else None
        if parent_id:
            parent_id = raw_to_final.get(parent_id, parent_id)
        if parent_id and parent_id not in valid_ids:
            logger.warning(
                "Generated location %s references missing parent_id=%s, degrading to root node",
                item["location_id"], parent_id,
            )
            parent_id = None
        item["parent_id"] = parent_id

    ordered: list[dict[str, Any]] = []
    remaining = prepared[:]
    saved_ids: set[str] = set()
    while remaining:
        next_remaining = []
        for item in remaining:
            if item["parent_id"] is None or item["parent_id"] in saved_ids:
                ordered.append(item)
                saved_ids.add(item["location_id"])
            else:
                next_remaining.append(item)
        if len(next_remaining) == len(remaining):
            # No parent can be saved: the remaining graph contains a cycle.
            # Match the legacy generator's policy and keep these scenes as roots.
            for item in next_remaining:
                logger.warning(
                    "Generated location %s has unresolved parent_id=%s, degrading to root node",
                    item["location_id"], item["parent_id"],
                )
                item["parent_id"] = None
                ordered.append(item)
            break
        remaining = next_remaining
    return ordered
