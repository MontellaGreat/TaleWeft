"""Prop event handler that records canonical narrative events."""
from __future__ import annotations

from domain.prop.entities.prop import Prop
from domain.prop.value_objects.prop_event import PropEvent, PropEventType


_ACTION_LABEL = {
    PropEventType.INTRODUCED: "introduced",
    PropEventType.USED: "used",
    PropEventType.TRANSFERRED: "transferred",
    PropEventType.DAMAGED: "damaged",
    PropEventType.REPAIRED: "repaired",
    PropEventType.UPGRADED: "upgraded",
    PropEventType.RESOLVED: "resolved",
}


class NarrativePropEventHandler:
    """Mirror prop lifecycle events into the shared narrative event stream.

    `prop_events` remains the prop aggregate log. `narrative_events` becomes the
    system-wide fact source that other projections can replay.
    """

    def __init__(self, narrative_event_repository) -> None:
        self._events = narrative_event_repository

    async def handle(self, prop: Prop, event: PropEvent) -> None:
        action = _ACTION_LABEL.get(event.event_type, event.event_type.value.lower())
        mutations = [
            {
                "entity_id": prop.id.value,
                "entity_type": "prop",
                "attribute": "last_event",
                "action": "add",
                "value": {
                    "event_type": event.event_type.value,
                    "chapter_number": event.chapter_number,
                    "source": event.source.value,
                    "description": event.description,
                },
            },
        ]
        # Describe this event, including on a retry after later events committed.
        # A transfer does not change lifecycle state.
        target_state = event.target_lifecycle_state()
        if target_state is not None:
            mutations.insert(0, {
                "entity_id": prop.id.value,
                "entity_type": "prop",
                "attribute": "lifecycle_state",
                "action": "add",
                "value": target_state.value,
            })

        if event.is_transfer() and event.to_holder_id:
            mutations.append(
                {
                    "entity_id": prop.id.value,
                    "entity_type": "prop",
                    "attribute": "holder_character_id",
                    "action": "add",
                    "value": event.to_holder_id,
                }
            )
        actor = event.to_holder_id or event.actor_character_id
        if actor:
            mutations.append(
                {
                    "entity_id": actor,
                    "entity_type": "character",
                    "attribute": "related_props",
                    "action": "append_unique",
                    "value": {
                        "prop_id": prop.id.value,
                        "prop_name": prop.name,
                        "relation": action,
                        "chapter_number": event.chapter_number,
                    },
                }
            )

        upsert = getattr(type(self._events), 'upsert_event', None)
        write = self._events.upsert_event if callable(upsert) else self._events.append_event
        args = [
            event.novel_id,
            event.chapter_number,
            event.description or f"{prop.name} {action}",
            mutations,
        ]
        if callable(upsert):
            args.insert(0, 'prop-event-' + event.id)
        write(
            *args,
            tags=["prop", f"prop:{action}", f"prop_id:{prop.id.value}"],
        )
