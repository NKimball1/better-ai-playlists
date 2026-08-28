"""Validator unit tests. Run: python -m pytest tests/ -q

The validator is the load-bearing wall - if it passes a bad playlist,
every eval number upstream is fiction. These tests use synthetic track
metadata (no DB, no network) via the track_meta injection path.
"""
import pytest

from src.spec import HardConstraints, PlaylistSpec, SoftIntent, SourceMode
from src.validator import validate


def spec(**hard):
    hard.setdefault("source", SourceMode.OPEN_DISCOVERY)  # skip library check by default
    return PlaylistSpec(title="t", hard=HardConstraints(**hard), soft=SoftIntent())


def track(tid, name="Song", artists=("A",), year=2020, mins=3.0):
    return {"id": tid, "name": name, "artist_list": list(artists),
            "year": year, "duration_ms": int(mins * 60000)}


def meta(*tracks):
    return {t["id"]: t for t in tracks}


def constraints(violations):
    return {v.constraint for v in violations}


class TestSourceMode:
    def test_liked_only_flags_external_tracks(self):
        # ids not in the real library DB -> flagged
        v = validate(spec(source=SourceMode.LIKED_ONLY), ["ext1"],
                     meta(track("ext1")))
        assert "source_liked_only" in constraints(v)

    def test_open_discovery_allows_external(self):
        v = validate(spec(source=SourceMode.OPEN_DISCOVERY), ["ext1"],
                     meta(track("ext1")))
        assert "source_liked_only" not in constraints(v)


class TestCountAndDuration:
    def test_exact_count_enforced(self):
        m = meta(track("a"), track("b"))
        v = validate(spec(track_count=3), ["a", "b"], m)
        assert "track_count" in constraints(v)

    def test_correct_count_passes(self):
        m = meta(track("a"), track("b"))
        v = validate(spec(track_count=2), ["a", "b"], m)
        assert "track_count" not in constraints(v)

    def test_duration_within_tolerance_passes(self):
        m = meta(*[track(f"t{i}", mins=10) for i in range(6)])  # 60 min
        v = validate(spec(target_duration_min=62), list(m), m)
        assert "target_duration" not in constraints(v)

    def test_duration_outside_tolerance_fails(self):
        m = meta(*[track(f"t{i}", mins=10) for i in range(4)])  # 40 min
        v = validate(spec(target_duration_min=60), list(m), m)
        assert "target_duration" in constraints(v)


class TestArtistConstraints:
    def test_excluded_artist_any_position_flags(self):
        # exclusion applies to features too, not just primary artist
        m = meta(track("a", artists=("Main", "Drake")))
        v = validate(spec(exclude_artists=["Drake"]), ["a"], m)
        assert "exclude_artists" in constraints(v)

    def test_exclusion_case_insensitive(self):
        m = meta(track("a", artists=("DRAKE",)))
        v = validate(spec(exclude_artists=["drake"]), ["a"], m)
        assert "exclude_artists" in constraints(v)

    def test_max_per_artist_counts_primary_only(self):
        # 2x primary + 1x feature = OK at max 2 (feature doesn't count)
        m = meta(track("a", artists=("X",)), track("b", artists=("X",)),
                 track("c", artists=("Y", "X")))
        v = validate(spec(max_per_artist=2), ["a", "b", "c"], m)
        assert "max_per_artist" not in constraints(v)

    def test_max_per_artist_over_limit_flags(self):
        m = meta(*[track(f"t{i}", artists=("X",)) for i in range(3)])
        v = validate(spec(max_per_artist=2), list(m), m)
        assert "max_per_artist" in constraints(v)

    def test_include_artist_satisfied_by_feature(self):
        m = meta(track("a", artists=("Main", "Juice WRLD")))
        v = validate(spec(include_artists=["Juice WRLD"]), ["a"], m)
        assert "include_artists" not in constraints(v)

    def test_include_artist_missing_flags(self):
        m = meta(track("a", artists=("Someone",)))
        v = validate(spec(include_artists=["Juice WRLD"]), ["a"], m)
        assert "include_artists" in constraints(v)


class TestYearsAndDupes:
    def test_year_range_violation(self):
        m = meta(track("a", year=2015))
        v = validate(spec(year_min=2016, year_max=2018), ["a"], m)
        assert "year_range" in constraints(v)

    def test_unknown_year_not_flagged(self):
        # missing metadata should not fail closed on year (tracked separately)
        m = meta(track("a", year=None))
        v = validate(spec(year_min=2016), ["a"], m)
        assert "year_range" not in constraints(v)

    def test_duplicates_flagged(self):
        m = meta(track("a"))
        v = validate(spec(), ["a", "a"], m)
        assert "no_duplicates" in constraints(v)

    def test_unknown_ids_flagged(self):
        v = validate(spec(), ["nonexistent"], {})
        assert "unknown_tracks" in constraints(v)


class TestCleanPlaylist:
    def test_fully_compliant_playlist_no_violations(self):
        m = meta(track("a", artists=("X",), year=2017, mins=30),
                 track("b", artists=("Y",), year=2018, mins=30))
        s = spec(track_count=2, target_duration_min=60, year_min=2016,
                 year_max=2018, max_per_artist=1, exclude_artists=["Drake"],
                 include_artists=["X"])
        assert validate(s, ["a", "b"], m) == []
