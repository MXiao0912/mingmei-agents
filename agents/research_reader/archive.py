from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT))

from shared.paths import ARCHIVE_DIR, DB_PATH, ensure_project_dirs


EXPORT_COLUMNS = [
    "title",
    "link",
    "summary",
    "published_date",
    "source",
    "relevance_score",
    "learned_preference_score",
    "personalized_score",
    "topic_tags",
    "user_rating",
    "read_status",
    "notes",
]


def subtract_months(value: datetime, months: int) -> datetime:
    month = value.month - months
    year = value.year
    while month <= 0:
        month += 12
        year -= 1

    day = min(value.day, days_in_month(year, month))
    return value.replace(year=year, month=month, day=day)


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    this_month = datetime(year, month, 1, tzinfo=timezone.utc)
    return (next_month - this_month).days


def parse_date(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def select_expr(columns: set[str], column: str, default: str) -> str:
    return column if column in columns else f"{default} AS {column}"


def load_archive_rows(db_path: Path, cutoff: datetime) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        table_exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'research_items'"
        ).fetchone()
        if not table_exists:
            return []

        columns = table_columns(connection, "research_items")
        date_expr = (
            "published_date AS published_date"
            if "published_date" in columns
            else "published_at AS published_date"
            if "published_at" in columns
            else "NULL AS published_date"
        )
        source_expr = (
            "source_name AS source"
            if "source_name" in columns
            else "'unknown' AS source"
        )

        rows = connection.execute(
            f"""
            SELECT
                id AS item_id,
                title,
                link,
                summary,
                {date_expr},
                {source_expr},
                {select_expr(columns, "relevance_score", "0")},
                {select_expr(columns, "learned_preference_score", "0")},
                {select_expr(columns, "personalized_score", "0")},
                {select_expr(columns, "topic_tags", "''")},
                {select_expr(columns, "user_rating", "NULL")},
                {select_expr(columns, "read_status", "'unread'")},
                (
                    SELECT notes
                    FROM feedback
                    WHERE feedback.item_id = research_items.id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                ) AS notes
            FROM research_items
            """
        ).fetchall()

    archive_rows = []
    for row in rows:
        published_date = parse_date(row["published_date"])
        if published_date is None or published_date >= cutoff:
            continue

        archive_rows.append(
            {
                "item_id": row["item_id"],
                "title": row["title"],
                "link": row["link"],
                "summary": row["summary"],
                "published_date": row["published_date"],
                "source": row["source"],
                "relevance_score": row["relevance_score"],
                "learned_preference_score": row["learned_preference_score"],
                "personalized_score": row["personalized_score"],
                "topic_tags": row["topic_tags"],
                "user_rating": row["user_rating"],
                "read_status": row["read_status"],
                "notes": row["notes"],
            }
        )

    return archive_rows


def archive_paths(now: datetime) -> tuple[Path, Path, Path]:
    stem = f"archive_{now.year}_{now.month:02d}"
    return (
        ARCHIVE_DIR / f"{stem}.sqlite",
        ARCHIVE_DIR / f"{stem}.csv",
        ARCHIVE_DIR / f"{stem}.json",
    )


def export_sqlite(rows: list[dict[str, Any]], path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS archived_records")
        connection.execute(
            """
            CREATE TABLE archived_records (
                title TEXT,
                link TEXT,
                summary TEXT,
                published_date TEXT,
                source TEXT,
                relevance_score REAL,
                learned_preference_score REAL,
                personalized_score REAL,
                topic_tags TEXT,
                user_rating INTEGER,
                read_status TEXT,
                notes TEXT
            )
            """
        )
        connection.executemany(
            f"""
            INSERT INTO archived_records ({", ".join(EXPORT_COLUMNS)})
            VALUES ({", ".join(["?"] * len(EXPORT_COLUMNS))})
            """,
            [[row[column] for column in EXPORT_COLUMNS] for row in rows],
        )
        connection.commit()


def export_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row[column] for column in EXPORT_COLUMNS})


def export_json(rows: list[dict[str, Any]], path: Path) -> None:
    payload = [{column: row[column] for column in EXPORT_COLUMNS} for row in rows]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def delete_archived_rows(db_path: Path, rows: list[dict[str, Any]]) -> None:
    item_ids = [row["item_id"] for row in rows]
    if not item_ids:
        return

    placeholders = ", ".join(["?"] * len(item_ids))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"DELETE FROM feedback WHERE item_id IN ({placeholders})",
            item_ids,
        )
        connection.execute(
            f"DELETE FROM research_items WHERE id IN ({placeholders})",
            item_ids,
        )
        connection.commit()


def confirm_delete(row_count: int) -> bool:
    response = input(
        f"Delete {row_count} archived record(s) from the active database? Type 'yes' to continue: "
    )
    return response.strip().lower() == "yes"


def archive(db_path: Path, months: int, delete_after_export: bool, yes: bool) -> int:
    ensure_project_dirs()
    now = datetime.now(timezone.utc)
    cutoff = subtract_months(now, months)
    rows = load_archive_rows(db_path, cutoff)
    sqlite_path, csv_path, json_path = archive_paths(now)

    print(f"Cutoff: {cutoff.date()} ({months} month(s))")
    print(f"Records to archive: {len(rows)}")
    print(f"SQLite archive: {sqlite_path}")
    print(f"CSV archive: {csv_path}")
    print(f"JSON archive: {json_path}")

    if not rows:
        return 0

    export_sqlite(rows, sqlite_path)
    export_csv(rows, csv_path)
    export_json(rows, json_path)
    print("Archive export complete.")

    if delete_after_export:
        if yes or confirm_delete(len(rows)):
            delete_archived_rows(db_path, rows)
            print(f"Deleted {len(rows)} archived record(s) from active database.")
        else:
            print("Delete skipped.")

    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive old Research Reader records.")
    parser.add_argument("--database", default=DB_PATH, type=Path)
    parser.add_argument("--months", default=12, type=int)
    parser.add_argument("--delete-after-export", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    archive(args.database, args.months, args.delete_after_export, args.yes)


if __name__ == "__main__":
    main()
