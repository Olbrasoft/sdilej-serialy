"""Read-only CR series catalog export and rating-prioritized episode backlog."""

from __future__ import annotations

import gzip
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import psycopg2
import psycopg2.extras


RATING_PRIORS = {
    "imdb": (6.8, 2_500),
    "csfd": (6.8, 1_000),
    "tmdb": (6.5, 500),
}


def readonly_connection(database_url: str):
    """Open a transaction that PostgreSQL itself refuses to write through."""
    connection = psycopg2.connect(database_url, connect_timeout=20)
    connection.set_session(readonly=True, autocommit=False)
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute("SHOW transaction_read_only")
        if cursor.fetchone()["transaction_read_only"] != "on":
            connection.close()
            raise RuntimeError("Refusing a database session that is not read-only")
    return connection


def fetch_episode_rows(connection) -> list[dict[str, Any]]:
    """Fetch only series metadata and episodes; no source-provider data is read."""
    sql = """
        SELECT
            s.id AS series_id,
            s.title AS series_title,
            s.original_title AS series_original_title,
            s.description AS series_description,
            s.imdb_rating, s.imdb_votes,
            s.csfd_rating, s.csfd_rating_count,
            s.tmdb_rating, s.tmdb_vote_count,
            e.id AS episode_id,
            e.season,
            e.episode,
            e.title AS episode_title,
            e.episode_name,
            e.runtime AS runtime_min,
            e.description
        FROM series s
        JOIN episodes e ON e.series_id = s.id
        WHERE s.title IS NOT NULL AND btrim(s.title) <> ''
          AND e.season IS NOT NULL AND e.episode IS NOT NULL
        ORDER BY s.id, e.season, e.episode, e.id
    """
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute("SHOW transaction_read_only")
        if cursor.fetchone()["transaction_read_only"] != "on":
            raise RuntimeError("Database session ceased to be read-only")
        cursor.execute(sql)
        return [dict(row) for row in cursor.fetchall()]


def rating_details(row: dict[str, Any]) -> tuple[str | None, float | None, int, float]:
    for source, rating_key, votes_key, divisor in (
        ("imdb", "imdb_rating", "imdb_votes", 1.0),
        ("csfd", "csfd_rating", "csfd_rating_count", 10.0),
        ("tmdb", "tmdb_rating", "tmdb_vote_count", 1.0),
    ):
        if row.get(rating_key) is None:
            continue
        rating = float(row[rating_key]) / divisor
        votes = int(row.get(votes_key) or 0)
        prior_rating, prior_votes = RATING_PRIORS[source]
        score = ((votes * rating) + (prior_votes * prior_rating)) / (votes + prior_votes)
        return source, rating, votes, round(score, 6)
    return None, None, 0, 0.0


def prepare_episodes(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    # The production catalog can contain multiple database records for the
    # same canonical episode number.  The queue and checkpoints intentionally
    # identify an episode by series/season/number, so exporting those duplicate
    # rows only consumes the episode limit and repeatedly schedules the same
    # work.  Keep the lowest database id, which is also the stable first row of
    # the read-only query.
    unique_rows: dict[tuple[int, int, int], dict[str, Any]] = {}
    for row in rows:
        identity = (int(row["series_id"]), int(row["season"]), int(row["episode"]))
        current = unique_rows.get(identity)
        if current is None or int(row["episode_id"]) < int(current["episode_id"]):
            unique_rows[identity] = row

    prepared: list[dict[str, Any]] = []
    for row in unique_rows.values():
        source, rating, votes, score = rating_details(row)
        prepared.append(
            {
                **row,
                "rating_source": source,
                "rating_value": rating,
                "rating_votes": votes,
                "priority_score": score,
            }
        )
    prepared.sort(
        key=lambda row: (
            -float(row["priority_score"]),
            -int(row["rating_votes"]),
            -float(row["rating_value"] or 0),
            str(row["series_title"]).casefold(),
            int(row["season"]),
            int(row["episode"]),
            int(row["episode_id"]),
        )
    )
    current_series_id: int | None = None
    series_rank = 0
    for episode_rank, row in enumerate(prepared, start=1):
        if row["series_id"] != current_series_id:
            current_series_id = int(row["series_id"])
            series_rank += 1
        row["priority_rank"] = episode_rank
        row["series_priority_rank"] = series_rank
        row["episode_code"] = f"S{int(row['season']):02d}E{int(row['episode']):02d}"
    return prepared


def write_jsonl_gzip(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
            with io.TextIOWrapper(zipped, encoding="utf-8") as output:
                for row in rows:
                    output.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]
