from __future__ import annotations

import csv
import pickle
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from rapidfuzz import fuzz, process


class DanbooruTagDatabase:
    """In-memory fuzzy-searchable Danbooru tag database.

    Internally stores two parallel lists:
      _choices  – lowercased strings to match against (canonical tags + aliases)
      _meta     – tuple (canonical, post_count, alias_or_None) per choice
    """

    def __init__(
        self,
        search_choices: list[str],
        choice_meta: list[tuple[str, int, str | None]],
    ) -> None:
        self._choices = search_choices
        self._meta = choice_meta
        # Set of canonical tag names (lowercase) for O(1) tag-type lookups.
        self._canonical_set: set[str] = {
            meta[0].lower() for meta in choice_meta if meta[2] is None
        }

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pkl_path(csv_path: Path) -> Path:
        return csv_path.with_name(csv_path.stem + '_preprocessed.pkl')

    @classmethod
    def build_from_csv(cls, csv_path: Path) -> DanbooruTagDatabase:
        """Parse the Danbooru CSV and build the database, saving a pickle cache."""
        search_choices: list[str] = []
        # Each entry: (canonical_tag, post_count, alias_or_None)
        choice_meta: list[tuple[str, int, str | None]] = []

        with open(csv_path, newline='', encoding='utf-8') as fh:
            for row in csv.reader(fh):
                if len(row) < 3:
                    continue
                canonical = row[0].strip()
                if not canonical:
                    continue
                try:
                    post_count = int(row[2].strip())
                except ValueError:
                    post_count = 0

                # Canonical tag itself
                search_choices.append(canonical.lower())
                choice_meta.append((canonical, post_count, None))

                # Each alias maps back to the same canonical
                aliases_raw = row[3].strip() if len(row) > 3 else ''
                if aliases_raw:
                    for alias in aliases_raw.split(','):
                        alias = alias.strip()
                        if alias:
                            search_choices.append(alias.lower())
                            choice_meta.append((canonical, post_count, alias))

        db = cls(search_choices, choice_meta)
        pkl = cls._pkl_path(csv_path)
        try:
            with open(pkl, 'wb') as fh:
                pickle.dump((search_choices, choice_meta), fh,
                            protocol=pickle.HIGHEST_PROTOCOL)
        except OSError:
            pass  # Non-fatal; we'll just rebuild next time
        return db

    @classmethod
    def load(cls, csv_path: Path) -> DanbooruTagDatabase:
        """Load from pickle cache if up-to-date, otherwise rebuild from CSV."""
        if not csv_path.exists():
            raise FileNotFoundError(f'Danbooru CSV not found: {csv_path}')
        pkl = cls._pkl_path(csv_path)
        if pkl.exists() and pkl.stat().st_mtime >= csv_path.stat().st_mtime:
            try:
                with open(pkl, 'rb') as fh:
                    search_choices, choice_meta = pickle.load(fh)
                return cls(search_choices, choice_meta)
            except Exception:
                pass
        return cls.build_from_csv(csv_path)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_raw(self, query_norm: str, limit: int) -> list[dict]:
        """Fuzzy-search and return raw results (no display formatting).

        Each result dict has: _key, _alias, score, post_count
        """
        raw = process.extract(
            query_norm,
            self._choices,
            scorer=fuzz.WRatio,
            limit=limit * 4,
            score_cutoff=40,
        )
        seen: dict[str, dict] = {}
        for _, score, idx in raw:
            canonical, post_count, alias = self._meta[idx]
            if canonical not in seen or score > seen[canonical]['score']:
                seen[canonical] = {
                    '_key': canonical,
                    '_alias': alias,
                    'score': score,
                    'post_count': post_count,
                }
        results = sorted(seen.values(),
                         key=lambda x: (-x['score'], -x['post_count']))
        return results[:limit]


# ---------------------------------------------------------------------------
# Blended search  (Danbooru + local image tags)
# ---------------------------------------------------------------------------

def blended_search(
    query: str,
    db: DanbooruTagDatabase | None,
    local_tags: list[tuple[str, int]],
    limit: int,
    replace_underscores: bool,
) -> list[dict]:
    """Merge Danbooru and local image-tag suggestions.

    Returns a list of dicts:
        display     – text shown in the popup
        canonical   – tag string to insert into the image
        score       – fuzzy match score (0–100, boosted for local matches)
        post_count  – Danbooru post count, or local usage count for local-only tags
        is_alias    – True when the match was on an alias
        source      – 'danbooru' | 'local'
    """
    if not query:
        return []

    query_norm = query.lower().replace(' ', '_')

    # ------------------------------------------------------------------
    # Step A: Danbooru results
    # ------------------------------------------------------------------
    raw_danbooru = db.search_raw(query_norm, limit * 2) if db is not None else []
    merged: dict[str, dict] = {r['_key']: dict(r) for r in raw_danbooru}

    # ------------------------------------------------------------------
    # Step B: Local tag fuzzy match  (trigger tags, custom tags, etc.)
    # ------------------------------------------------------------------
    if local_tags:
        local_keys = [t.lower().replace(' ', '_') for t, _ in local_tags]
        raw_local = process.extract(
            query_norm, local_keys,
            scorer=fuzz.WRatio, limit=limit * 2, score_cutoff=40)
        for _, score, idx in raw_local:
            orig_tag, count = local_tags[idx]
            key = orig_tag.lower().replace(' ', '_')
            if key in merged:
                # Known Danbooru tag also used locally: boost score
                merged[key]['score'] = max(merged[key]['score'], score) + 20
                merged[key]['_local_count'] = count
            else:
                # Local-only entry (trigger tags, LoRA activators, etc.)
                merged[key] = {
                    '_key': key,
                    '_alias': None,
                    'score': score,
                    'post_count': count,
                    '_local_only': True,
                }

    # ------------------------------------------------------------------
    # Step D: Build display strings, sort, trim
    # ------------------------------------------------------------------
    final: list[dict] = []
    for r in sorted(merged.values(),
                    key=lambda x: (-x['score'], -x['post_count']))[:limit]:
        key = r['_key']
        alias = r.get('_alias')
        is_local_only = bool(r.get('_local_only'))
        # Underscore replacement only applies to Danbooru tags; local-only
        # trigger/LoRA tags keep their original form (underscores preserved).
        apply_replace = replace_underscores and not is_local_only
        canonical_str = key.replace('_', ' ') if apply_replace else key
        if alias:
            alias_str = alias.replace('_', ' ') if apply_replace else alias
            display = f'{alias_str} \u2192 {canonical_str}'
        else:
            display = canonical_str
        danbooru_count = 0 if is_local_only else r['post_count']
        local_count = r['post_count'] if is_local_only else r.get('_local_count', 0)
        final.append({
            'display': display,
            'canonical': canonical_str,
            'score': r['score'],
            'post_count': r['post_count'],
            'danbooru_count': danbooru_count,
            'local_count': local_count,
            'is_alias': bool(alias),
            'source': 'local' if is_local_only else 'danbooru',
        })
    return final


# ---------------------------------------------------------------------------
# CSV path resolution
# ---------------------------------------------------------------------------

def resolve_csv_path() -> Path:
    """Resolve Danbooru CSV path from settings, falling back to the bundled default."""
    # Late import to avoid circular dependency (settings imports nothing from here)
    from utils.settings import DEFAULT_SETTINGS, get_settings
    settings = get_settings()
    custom = settings.value(
        'danbooru_tags_csv_path',
        defaultValue=DEFAULT_SETTINGS['danbooru_tags_csv_path'],
        type=str)
    if custom:
        return Path(str(custom))
    # This file lives at  taggui/utils/danbooru_tags.py
    # parents[2] is the project root
    return Path(__file__).parents[2] / 'tags' / 'danbooru_2025.csv'


# ---------------------------------------------------------------------------
# Background load thread
# ---------------------------------------------------------------------------

class DanbooruLoadThread(QThread):
    """Loads DanbooruTagDatabase in a background thread."""

    loaded = Signal(object)    # emits DanbooruTagDatabase
    load_failed = Signal(str)  # emits error message string

    def __init__(self, csv_path: Path, parent=None):
        super().__init__(parent)
        self._csv_path = csv_path

    def run(self) -> None:
        try:
            db = DanbooruTagDatabase.load(self._csv_path)
            self.loaded.emit(db)
        except Exception as exc:
            self.load_failed.emit(str(exc))
