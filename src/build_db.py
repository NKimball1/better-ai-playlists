"""Build the library corpus DB from library.json + artist_genres.json.

Idempotent: drops and rebuilds. Rerun whenever the snapshot or
enrichment data changes.
"""
import json
import sqlite3
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
DB = DATA / "library.db"

SCHEMA = """
DROP TABLE IF EXISTS tracks;
DROP TABLE IF EXISTS artists;
DROP TABLE IF EXISTS track_artists;
DROP TABLE IF EXISTS artist_tags;
DROP TABLE IF EXISTS tracks_fts;

CREATE TABLE tracks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  album TEXT,
  album_id TEXT,
  release_date TEXT,
  year INTEGER,
  duration_ms INTEGER,
  added_at TEXT
);
CREATE TABLE artists (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  mb_id TEXT,
  mb_matched INTEGER DEFAULT 0
);
CREATE TABLE track_artists (
  track_id TEXT REFERENCES tracks(id),
  artist_id TEXT REFERENCES artists(id),
  position INTEGER,
  PRIMARY KEY (track_id, artist_id)
);
CREATE TABLE artist_tags (
  artist_id TEXT REFERENCES artists(id),
  tag TEXT NOT NULL,
  PRIMARY KEY (artist_id, tag)
);
CREATE INDEX idx_tracks_year ON tracks(year);
CREATE INDEX idx_tags_tag ON artist_tags(tag);

CREATE VIRTUAL TABLE tracks_fts USING fts5(
  track_id UNINDEXED, name, artists, album, tags
);
"""


def main():
    tracks = json.loads((DATA / "library.json").read_text(encoding="utf-8"))
    genres_file = DATA / "artist_genres.json"
    enrich = json.loads(genres_file.read_text(encoding="utf-8")) if genres_file.exists() else {}

    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)

    artist_rows, seen = [], set()
    for t in tracks:
        year = int(t["release_date"][:4]) if t["release_date"][:4].isdigit() else None
        con.execute(
            "INSERT OR IGNORE INTO tracks VALUES (?,?,?,?,?,?,?,?)",
            (t["id"], t["name"], t["album"], t["album_id"],
             t["release_date"], year, t["duration_ms"], t["added_at"]),
        )
        for pos, a in enumerate(t["artists"]):
            if not a["id"]:
                continue
            if a["id"] not in seen:
                seen.add(a["id"])
                e = enrich.get(a["id"], {})
                artist_rows.append((a["id"], a["name"], e.get("mb_id"),
                                    1 if e.get("matched") else 0))
            con.execute(
                "INSERT OR IGNORE INTO track_artists VALUES (?,?,?)",
                (t["id"], a["id"], pos),
            )
    con.executemany("INSERT OR IGNORE INTO artists VALUES (?,?,?,?)", artist_rows)

    for aid, e in enrich.items():
        for tag in e.get("tags", []):
            con.execute("INSERT OR IGNORE INTO artist_tags VALUES (?,?)", (aid, tag))

    # FTS: one row per track, artists + tags flattened in
    con.execute("""
        INSERT INTO tracks_fts (track_id, name, artists, album, tags)
        SELECT t.id, t.name,
               (SELECT group_concat(a.name, ' ') FROM track_artists ta
                JOIN artists a ON a.id = ta.artist_id WHERE ta.track_id = t.id),
               t.album,
               (SELECT group_concat(DISTINCT g.tag) FROM track_artists ta
                JOIN artist_tags g ON g.artist_id = ta.artist_id
                WHERE ta.track_id = t.id)
        FROM tracks t
    """)
    con.commit()

    n_tracks = con.execute("SELECT count(*) FROM tracks").fetchone()[0]
    n_artists = con.execute("SELECT count(*) FROM artists").fetchone()[0]
    n_matched = con.execute("SELECT count(*) FROM artists WHERE mb_matched=1").fetchone()[0]
    n_tagged = con.execute(
        "SELECT count(DISTINCT track_id) FROM track_artists ta "
        "JOIN artist_tags g ON g.artist_id = ta.artist_id"
    ).fetchone()[0]
    print(f"tracks: {n_tracks}, artists: {n_artists} "
          f"(mb-matched: {n_matched}), tracks with >=1 tag: {n_tagged}")
    con.close()


if __name__ == "__main__":
    main()
