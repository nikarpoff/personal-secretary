"""Small Calendar references to Obsidian person cards; no contact/profile data."""

import json

from typing import Any, Sequence, Tuple
from uuid import UUID

from loguru import logger

from app.core.exceptions import InvalidRequestError

PERSON_IDS_KEY = "secretary_people"
MAX_EVENT_PEOPLE = 10


def normalize_person_ids(values: Sequence[str]) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > MAX_EVENT_PEOPLE:
        raise InvalidRequestError("person_ids must contain at most 10 person-card UUIDs")
    result = set()
    for value in values:
        try:
            canonical = str(UUID(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise InvalidRequestError("person_ids must contain canonical lowercase UUIDs") from exc
        if canonical != value:
            raise InvalidRequestError("person_ids must contain canonical lowercase UUIDs")
        result.add(canonical)
    return sorted(result)


def event_person_ids(extended_properties: Any) -> Tuple[str, ...]:
    """Malformed external metadata must not break listing/reminders of an event."""
    if not isinstance(extended_properties, dict):
        return ()
    private = extended_properties.get("private")
    if not isinstance(private, dict) or PERSON_IDS_KEY not in private:
        return ()
    value = private[PERSON_IDS_KEY]
    try:
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError("Invalid private metadata")
        return tuple(normalize_person_ids(json.loads(value)))
    except (ValueError, TypeError, InvalidRequestError):
        logger.warning("Ignoring invalid Calendar person-card references")
        return ()


def set_event_person_ids(item: dict[str, Any], person_ids: Sequence[str]) -> None:
    """Preserve unrelated private/shared properties in the existing GET/PUT flow."""
    ids = normalize_person_ids(person_ids)
    extended = item.get("extendedProperties") or {}
    if not isinstance(extended, dict):
        raise InvalidRequestError("Event extended properties have an unsupported format")
    private = extended.get("private") or {}
    if not isinstance(private, dict):
        raise InvalidRequestError("Event private properties have an unsupported format")
    private = dict(private)
    if ids:
        private[PERSON_IDS_KEY] = json.dumps(ids, separators=(",", ":"))
    else:
        private.pop(PERSON_IDS_KEY, None)
    item["extendedProperties"] = {**extended, "private": private}
