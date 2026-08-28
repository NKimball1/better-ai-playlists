"""Library corpus access: the search_library tool and lookup helpers.

Hard filters are pushed down into SQL - the agent can't retrieve tracks
that violate year/artist constraints even if it tries.
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "library.db"


def _connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def _fts_escape(query: str) -> str:
    """Quote each term so FTS5 operators/punctuation in user text can't break the query."""
    terms = [t.replace('"', '""') for t in query.split()]
    return " ".join(f'"{t}"' for t in terms if t)


def search_library(query: str = "", year_min: int | None = None,
                   year_max: int | None = None,
                   exclude_artists: list[str] | None = None,
                   tag: str | None = None, limit: int = 20) -> list[dict]:
    """Search the user's saved tracks. Empty query = filter-only browse."""
    con = _connect()
    where, params = [], []
    if query:
        where.append("f.tracks_fts MATCH ?")
        params.append(_fts_escape(query))
    sql = """
        SELECT t.id, t.name, f.artists, t.album, t.year, t.duration_ms, f.tags
        FROM tracks t JOIN tracks_fts f ON f.track_id = t.id
    """
    if year_min is not None:
        where.append("t.year >= ?")
        params.append(year_min)
    if year_max is not None:
        where.append("t.year <= ?")
        params.append(year_max)
    if tag:
        where.append("t.id IN (SELECT ta.track_id FROM track_artists ta "
                     "JOIN artist_tags g ON g.artist_id = ta.artist_id WHERE g.tag = ?)")
        params.append(tag.lower())
    for a in (exclude_artists or []):
        where.append("t.id NOT IN (SELECT ta.track_id FROM track_artists ta "
                     "JOIN artists ar ON ar.id = ta.artist_id WHERE lower(ar.name) = ?)")
        params.append(a.lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + ("rank" if query else "t.added_at DESC") + " LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in con.execute(sql, params)]
    con.close()
    return rows


def all_tags(min_tracks: int = 5) -> list[tuple[str, int]]:
    """Tags in the library with how many tracks they cover (for the agent's orientation)."""
    con = _connect()
    rows = con.execute("""
        SELECT g.tag, count(DISTINCT ta.track_id) n
        FROM artist_tags g JOIN track_artists ta ON ta.artist_id = g.artist_id
        GROUP BY g.tag HAVING n >= ? ORDER BY n DESC
    """, (min_tracks,)).fetchall()
    con.close()
    return [(r[0], r[1]) for r in rows]


def get_tracks(track_ids: list[str]) -> dict[str, dict]:
    """Lookup full rows (with artists) for validation."""
    con = _connect()
    out = {}
    for tid in track_ids:
        r = con.execute("""
            SELECT t.id, t.name, t.year, t.duration_ms, f.artists
            FROM tracks t JOIN tracks_fts f ON f.track_id = t.id WHERE t.id = ?
        """, (tid,)).fetchone()
        if r:
            row = dict(r)
            row["artist_list"] = [a[0] for a in con.execute(
                "SELECT ar.name FROM track_artists ta JOIN artists ar "
                "ON ar.id = ta.artist_id WHERE ta.track_id = ? ORDER BY ta.position", (tid,))]
            out[tid] = row
    con.close()
    return out


def library_ids() -> set[str]:
    con = _connect()
    ids = {r[0] for r in con.execute("SELECT id FROM tracks")}
    con.close()
    return ids
