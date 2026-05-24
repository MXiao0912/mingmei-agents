from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT))

from shared.paths import DB_PATH, ensure_project_dirs
from shared.preferences import load_preferences, score_bounds


DEFAULT_DB_PATH = DB_PATH


def keyword_pattern(keyword: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.IGNORECASE)


def table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def ensure_columns(connection: sqlite3.Connection) -> None:
    columns = table_columns(connection, "research_items")

    if "relevance_score" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN relevance_score REAL NOT NULL DEFAULT 0"
        )
    if "topic_tags" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN topic_tags TEXT NOT NULL DEFAULT ''"
        )
    if "learned_preference_score" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN learned_preference_score REAL NOT NULL DEFAULT 0"
        )
    if "personalized_score" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN personalized_score REAL NOT NULL DEFAULT 0"
        )


def matched_keywords(title: str, summary: str, preferences: dict) -> dict[str, float]:
    scoring = preferences["scoring"]
    title_multiplier = float(scoring["title_multiplier"])
    summary_multiplier = float(scoring["summary_multiplier"])

    matches = {}
    for keyword, weight in preferences["keyword_weights"].items():
        pattern = keyword_pattern(keyword)
        title_matches = len(pattern.findall(title))
        summary_matches = len(pattern.findall(summary))
        if title_matches or summary_matches:
            matches[keyword] = float(weight) * (
                title_matches * title_multiplier
                + summary_matches * summary_multiplier
            )
    return matches


def topic_tags_for_keywords(keywords: set[str], preferences: dict) -> list[str]:
    tags = []
    lowered_keywords = {keyword.lower() for keyword in keywords}

    for topic, topic_keywords in preferences["topic_mappings"].items():
        mapped_keywords = {keyword.lower() for keyword in topic_keywords}
        if lowered_keywords & mapped_keywords:
            tags.append(topic)

    tagged_keywords = {
        keyword.lower()
        for topic_keywords in preferences["topic_mappings"].values()
        for keyword in topic_keywords
    }
    for keyword in keywords:
        if keyword.lower() not in tagged_keywords:
            tags.append(keyword)

    return tags


def score_item(
    title: str | None,
    summary: str | None,
    source_name: str | None,
    preferences: dict,
) -> tuple[float, list[str]]:
    title = title or ""
    summary = summary or ""
    source_name = source_name or ""

    matches = matched_keywords(title, summary, preferences)
    raw_score = sum(matches.values())
    raw_score += float(preferences["source_boosts"].get(source_name, 0))

    relevance_min, relevance_max = score_bounds(preferences)
    score = max(relevance_min, min(raw_score, relevance_max))
    tags = topic_tags_for_keywords(set(matches), preferences)
    return score, tags


def rank_items(db_path: Path) -> int:
    ensure_project_dirs()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    preferences = load_preferences()
    scoring = preferences["scoring"]
    topical_weight = float(scoring["personalized_weight_topical"])
    learned_weight = float(scoring["personalized_weight_learned"])

    with sqlite3.connect(db_path) as connection:
        ensure_columns(connection)
        feedback_exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'feedback'"
        ).fetchone()
        feedback_count = (
            connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            if feedback_exists
            else 0
        )

        rows = connection.execute(
            "SELECT id, title, summary, source_name FROM research_items"
        ).fetchall()

        for item_id, title, summary, source_name in rows:
            score, tags = score_item(title, summary, source_name, preferences)
            connection.execute(
                """
                UPDATE research_items
                SET relevance_score = ?, topic_tags = ?
                WHERE id = ?
                """,
                (score, ", ".join(tags), item_id),
            )

        if feedback_count == 0:
            connection.execute(
                """
                UPDATE research_items
                SET learned_preference_score = relevance_score,
                    personalized_score = relevance_score
                """
            )
        else:
            connection.execute(
                """
                UPDATE research_items
                SET personalized_score = (? * relevance_score) + (? * learned_preference_score)
                """,
                (topical_weight, learned_weight),
            )

        connection.commit()

    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank papers by relevance preferences.")
    parser.add_argument("--database", default=DEFAULT_DB_PATH, type=Path)
    args = parser.parse_args()

    count = rank_items(args.database)
    print(f"Ranked {count} paper(s).")


if __name__ == "__main__":
    main()
