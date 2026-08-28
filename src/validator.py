"""Deterministic validator: checks a candidate playlist against the hard
constraints in a PlaylistSpec. No LLM involved.

Returns structured violations designed to be fed back to the agent as
actionable repair instructions - each names the constraint, the offending
tracks, and what would fix it.
"""
from dataclasses import dataclass, field

from .library import get_tracks, library_ids
from .spec import PlaylistSpec, SourceMode


@dataclass
class Violation:
    constraint: str          # machine key, e.g. "source_liked_only"
    message: str             # actionable repair instruction for the agent
    track_ids: list[str] = field(default_factory=list)

    def to_dict(self):
        return {"constraint": self.constraint, "message": self.message,
                "track_ids": self.track_ids}


def validate(spec: PlaylistSpec, track_ids: list[str],
             track_meta: dict[str, dict] | None = None) -> list[Violation]:
    """track_meta supplies rows for tracks outside the library (from
    Spotify search); library tracks are looked up locally."""
    v: list[Violation] = []
    lib = library_ids()
    meta = get_tracks(track_ids)
    if track_meta:
        for tid, m in track_meta.items():
            meta.setdefault(tid, m)

    unknown = [t for t in track_ids if t not in meta]
    if unknown:
        v.append(Violation("unknown_tracks",
                           f"{len(unknown)} track ids could not be resolved; replace them",
                           unknown))

    h = spec.hard

    # source mode
    if h.source == SourceMode.LIKED_ONLY:
        outside = [t for t in track_ids if t not in lib]
        if outside:
            names = ", ".join(meta[t]["name"] for t in outside[:5] if t in meta)
            v.append(Violation(
                "source_liked_only",
                f"{len(outside)} tracks are not in the user's library ({names}...). "
                f"Every track must come from search_library results.", outside))

    # duplicates
    if h.no_duplicates:
        seen, dupes = set(), []
        for t in track_ids:
            if t in seen:
                dupes.append(t)
            seen.add(t)
        if dupes:
            v.append(Violation("no_duplicates",
                               f"{len(dupes)} duplicate tracks; remove them", dupes))

    # count / duration
    if h.track_count is not None and len(track_ids) != h.track_count:
        diff = abs(len(track_ids) - h.track_count)
        verb = "add" if len(track_ids) < h.track_count else "remove"
        v.append(Violation("track_count",
                           f"playlist has {len(track_ids)} tracks, spec requires exactly "
                           f"{h.track_count}; {verb} {diff}"))
    if h.target_duration_min is not None:
        total = sum(meta[t]["duration_ms"] for t in track_ids if t in meta) / 60000
        lo = h.target_duration_min - h.duration_tolerance_min
        hi = h.target_duration_min + h.duration_tolerance_min
        if not (lo <= total <= hi):
            verb = "add" if total < lo else "remove"
            v.append(Violation(
                "target_duration",
                f"total duration {total:.1f} min is outside [{lo}, {hi}]; "
                f"{verb} ~{abs(total - h.target_duration_min):.0f} min of tracks"))

    # years
    for t in track_ids:
        m = meta.get(t)
        if not m or m.get("year") is None:
            continue
        if (h.year_min and m["year"] < h.year_min) or (h.year_max and m["year"] > h.year_max):
            v.append(Violation("year_range",
                               f"'{m['name']}' ({m['year']}) is outside year range "
                               f"[{h.year_min or 'any'}, {h.year_max or 'any'}]; replace it", [t]))

    # artist constraints
    def artists_of(t):
        m = meta.get(t, {})
        return [a.lower() for a in m.get("artist_list", [])]

    excl = {a.lower() for a in h.exclude_artists}
    for t in track_ids:
        bad = excl.intersection(artists_of(t))
        if bad:
            v.append(Violation("exclude_artists",
                               f"'{meta[t]['name']}' features excluded artist(s): "
                               f"{', '.join(sorted(bad))}; remove it", [t]))

    if h.max_per_artist is not None:
        counts: dict[str, list[str]] = {}
        for t in track_ids:
            primary = artists_of(t)
            if primary:
                counts.setdefault(primary[0], []).append(t)
        for artist, ts in counts.items():
            if len(ts) > h.max_per_artist:
                v.append(Violation(
                    "max_per_artist",
                    f"{artist} appears {len(ts)} times as primary artist, max is "
                    f"{h.max_per_artist}; remove {len(ts) - h.max_per_artist}", ts))

    present = {a for t in track_ids for a in artists_of(t)}
    missing = [a for a in h.include_artists if a.lower() not in present]
    if missing:
        v.append(Violation("include_artists",
                           f"required artists missing: {', '.join(missing)}; add tracks by them"))

    return v
