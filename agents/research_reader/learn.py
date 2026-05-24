from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT))

from shared.paths import DB_PATH, ensure_project_dirs
from shared.preferences import load_preferences, score_bounds


DEFAULT_DB_PATH = DB_PATH
READ_STATUS_OPTIONS = {"unread", "skimmed", "read", "saved", "irrelevant"}


def table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
            read_status TEXT NOT NULL CHECK (
                read_status IN ('unread', 'skimmed', 'read', 'saved', 'irrelevant')
            ),
            notes TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES research_items(id)
        )
        """
    )

    columns = table_columns(connection, "research_items")
    if "user_rating" not in columns:
        connection.execute("ALTER TABLE research_items ADD COLUMN user_rating INTEGER")
    if "read_status" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN read_status TEXT NOT NULL DEFAULT 'unread'"
        )
    if "personalized_score" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN personalized_score REAL NOT NULL DEFAULT 0"
        )
    if "learned_preference_score" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN learned_preference_score REAL NOT NULL DEFAULT 0"
        )
    if "relevance_score" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN relevance_score REAL NOT NULL DEFAULT 0"
        )
    if "topic_tags" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN topic_tags TEXT NOT NULL DEFAULT ''"
        )
    connection.commit()


def split_tags(topic_tags: str | None) -> list[str]:
    if not topic_tags:
        return []
    return [tag.strip() for tag in topic_tags.split(",") if tag.strip()]


def rating_to_score(rating: float, preferences: dict) -> float:
    relevance_min, relevance_max = score_bounds(preferences)
    return relevance_min + ((rating - 1.0) / 4.0) * (relevance_max - relevance_min)


def smoothed_score(ratings: Iterable[int], preferences: dict) -> float:
    values = list(ratings)
    learning = preferences["learning"]
    prior_rating = float(learning["prior_rating"])
    shrinkage_n = float(learning["shrinkage_n"])
    smoothed_avg = (sum(values) + shrinkage_n * prior_rating) / (len(values) + shrinkage_n)
    return rating_to_score(smoothed_avg, preferences)


def latest_feedback_query() -> str:
    return """
        SELECT f.item_id, f.rating, f.read_status, i.source_name, i.topic_tags
        FROM feedback f
        JOIN research_items i ON i.id = f.item_id
        JOIN (
            SELECT item_id, MAX(id) AS latest_id
            FROM feedback
            GROUP BY item_id
        ) latest ON latest.latest_id = f.id
    """


def learn_preferences(db_path: Path) -> int:
    ensure_project_dirs()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    preferences = load_preferences()
    scoring = preferences["scoring"]
    topical_weight = float(scoring["personalized_weight_topical"])
    learned_weight = float(scoring["personalized_weight_learned"])

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row

        table_exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'research_items'"
        ).fetchone()
        if not table_exists:
            return 0

        ensure_schema(connection)
        feedback_rows = connection.execute(latest_feedback_query()).fetchall()

        if not feedback_rows:
            connection.execute(
                """
                UPDATE research_items
                SET learned_preference_score = relevance_score,
                    personalized_score = relevance_score
                """
            )
            connection.commit()
            return 0

        source_ratings: dict[str, list[int]] = defaultdict(list)
        tag_ratings: dict[str, list[int]] = defaultdict(list)

        for row in feedback_rows:
            rating = int(row["rating"])
            if row["source_name"]:
                source_ratings[row["source_name"]].append(rating)
            for tag in split_tags(row["topic_tags"]):
                tag_ratings[tag].append(rating)

            connection.execute(
                """
                UPDATE research_items
                SET user_rating = ?, read_status = ?
                WHERE id = ?
                """,
                (rating, row["read_status"], row["item_id"]),
            )

        source_scores = {
            source: smoothed_score(ratings, preferences)
            for source, ratings in source_ratings.items()
        }
        tag_scores = {
            tag: smoothed_score(ratings, preferences)
            for tag, ratings in tag_ratings.items()
        }

        items = connection.execute(
            "SELECT id, source_name, topic_tags, relevance_score FROM research_items"
        ).fetchall()

        for item in items:
            feature_scores = []
            if item["source_name"] in source_scores:
                feature_scores.append(source_scores[item["source_name"]])
            for tag in split_tags(item["topic_tags"]):
                if tag in tag_scores:
                    feature_scores.append(tag_scores[tag])

            learned_score = (
                sum(feature_scores) / len(feature_scores)
                if feature_scores
                else float(item["relevance_score"] or 0)
            )
            relevance_score = float(item["relevance_score"] or 0)
            personalized_score = (topical_weight * relevance_score) + (learned_weight * learned_score)

            connection.execute(
                """
                UPDATE research_items
                SET learned_preference_score = ?,
                    personalized_score = ?
                WHERE id = ?
                """,
                (learned_score, personalized_score, item["id"]),
            )

        connection.commit()
        return len(items)


def main() -> None:
    parser = argparse.ArgumentParser(description="Learn reader preferences from feedback.")
    parser.add_argument("--database", default=DEFAULT_DB_PATH, type=Path)
    args = parser.parse_args()

    count = learn_preferences(args.database)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"Updated learned scores for {count} paper(s) at {timestamp}.")


if __name__ == "__main__":
    main()
