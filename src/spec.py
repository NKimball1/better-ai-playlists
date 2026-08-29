"""PlaylistSpec: the typed contract between the constraint compiler and
everything downstream.

Design principle: HARD constraints are machine-checkable and enforced in
code (the validator). SOFT intent guides retrieval/ranking and is judged,
not enforced. The LLM translates natural language into this spec; it does
not get to decide what counts as satisfied.
"""
from enum import Enum

from pydantic import BaseModel, Field


class SourceMode(str, Enum):
    """Controls which tools the agent may use. Enforced by tool gating,
    not by prompt: in LIKED_ONLY the spotify search tool is not offered."""
    LIKED_ONLY = "liked_only"          # every track must be in the user's library
    LIBRARY_ADJACENT = "library_adjacent"  # library tracks + artists already in library
    OPEN_DISCOVERY = "open_discovery"  # anything on Spotify


class HardConstraints(BaseModel):
    """Machine-checkable. Every field here must be verifiable by the
    deterministic validator without an LLM."""
    source: SourceMode = SourceMode.LIKED_ONLY
    track_count: int | None = Field(None, ge=1, le=100,
                                    description="exact number of tracks requested")
    target_duration_min: int | None = Field(None, ge=5,
                                            description="target playlist length in minutes")
    duration_tolerance_min: int | None = Field(
        None, description="allowed +/- minutes on target duration; set ONLY if the "
        "user states precision, else null = auto (max of 5 min or 4% of target)")
    year_min: int | None = None
    year_max: int | None = None
    max_per_artist: int | None = Field(None, ge=1)
    include_artists: list[str] = Field(default_factory=list,
                                       description="artists that MUST appear")
    exclude_artists: list[str] = Field(default_factory=list,
                                       description="artists that must NOT appear")
    exclude_tracks: list[str] = Field(default_factory=list,
                                      description="track names that must NOT appear")
    no_duplicates: bool = True


class SoftIntent(BaseModel):
    """Guides retrieval and ordering. Checked by LLM judge, never by the
    validator."""
    vibe: str = Field("", description="mood/aesthetic in the user's own words")
    genres: list[str] = Field(default_factory=list,
                              description="genre hints, lowercase")
    energy_arc: str | None = Field(None,
                                   description="e.g. 'build up then wind down', null if unspecified")
    era_feel: str | None = None
    notes: str = Field("", description="anything else that should influence selection")


class PlaylistSpec(BaseModel):
    title: str = Field(description="short playlist title")
    hard: HardConstraints
    soft: SoftIntent
    ambiguities: list[str] = Field(default_factory=list,
                                   description="requests that could not be captured as hard or soft fields")
