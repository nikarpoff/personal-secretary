"""Use python-frontmatter's YAML handler, preserving body offsets and raw versions."""

import hashlib
import re

from typing import Any
from zoneinfo import ZoneInfo

import yaml
from frontmatter.default_handlers import YAMLHandler
from pydantic import ValidationError
from yaml.events import AliasEvent

from app.core.exceptions import InvalidRequestError
from app.core.notes.models import BODY, MAX_FRONTMATTER_BYTES, MAX_NOTE_BYTES, NoteRecord, validate_metadata


class _BoundedLoader(yaml.SafeLoader):
    """Only YAML safety policy; parsing/dumping belongs to the library."""

    yaml_implicit_resolvers = {
        key: [(tag, pattern) for tag, pattern in rules if tag != "tag:yaml.org,2002:timestamp"]
        for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.depth = self.nodes = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        self.depth += 1
        self.nodes += 1
        try:
            if self.check_event(AliasEvent) or self.depth > 6 or self.nodes > 200:
                raise ValueError("YAML aliases or excessive complexity are unsupported")
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> dict:
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("Frontmatter keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


YAML = YAMLHandler(fm_boundary=re.compile(r"^---[ \t]*$", re.MULTILINE))


def parse_note(raw: bytes, relative_path: str, tz: ZoneInfo) -> NoteRecord:
    if len(raw) > MAX_NOTE_BYTES:
        raise InvalidRequestError("Note is larger than 128 KiB")
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        if not text.startswith("---\n"):
            raise ValueError("Missing YAML frontmatter")
        header, content = YAML.split(text)
        if len(header.removeprefix("\n").removesuffix("\n").encode("utf-8")) > MAX_FRONTMATTER_BYTES:
            raise ValueError("Frontmatter is larger than 8 KiB")
        metadata = validate_metadata(YAML.load(header, Loader=_BoundedLoader), tz)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise InvalidRequestError("Note has invalid UTF-8/YAML frontmatter") from exc
    return NoteRecord(relative_path, metadata, content.removeprefix("\n"), raw, hashlib.sha256(raw).hexdigest())


def render_note(metadata: dict, body: str, tz: ZoneInfo) -> bytes:
    try:
        body = BODY.validate_python(body, strict=True)
        header = YAML.export(validate_metadata(metadata, tz), sort_keys=False)
        if len(header.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
            raise ValueError("Frontmatter is larger than 8 KiB")
        raw = f"---\n{header}\n---\n{body.rstrip()}\n".encode("utf-8")
        if len(raw) > MAX_NOTE_BYTES:
            raise ValueError("Note is larger than 128 KiB")
        return raw
    except (ValidationError, yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise InvalidRequestError("Note body or frontmatter exceeds its limits or contains unsupported values") from exc
