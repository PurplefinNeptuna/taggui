from __future__ import annotations

from enum import Enum, auto


class TagType(Enum):
    DANBOORU = auto()
    PREFIX_DANBOORU = auto()
    TRIGGER = auto()


# Maps TagType to its QSettings color key.
TAG_TYPE_COLOR_KEYS = {
    TagType.DANBOORU: 'color_danbooru_tag',
    TagType.PREFIX_DANBOORU: 'color_prefix_danbooru_tag',
    TagType.TRIGGER: 'color_trigger_tag',
}


class TagClassifier:
    """Classifies a tag string as Danbooru, prefix+Danbooru, or trigger.

    - DANBOORU: the tag (normalized) is a canonical Danbooru tag.
    - PREFIX_DANBOORU: the tag has exactly one leading word followed by a
      canonical Danbooru tag, e.g. "large cowboy_hat" or "large cowboy hat".
    - TRIGGER: any other tag (local-only / LoRA activator / unknown).
    """

    def __init__(self, db) -> None:
        self._db = db

    def classify(self, tag: str) -> TagType:
        if self._db is None:
            return TagType.TRIGGER
        normalized = tag.strip().lower().replace(' ', '_')
        if not normalized:
            return TagType.TRIGGER
        if normalized in self._db._canonical_set:
            return TagType.DANBOORU
        # Check for a single leading prefix word before a Danbooru tag.
        tokens = normalized.split('_')
        if len(tokens) >= 2:
            remainder = '_'.join(tokens[1:])
            if remainder in self._db._canonical_set:
                return TagType.PREFIX_DANBOORU
        return TagType.TRIGGER
