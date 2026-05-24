from __future__ import annotations

import argparse
import html
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT))

from shared.paths import DB_PATH, DIGEST_PATH, ensure_project_dirs
from shared.preferences import load_preferences, score_label


DEFAULT_DB_PATH = DB_PATH
SUMMARY_LIMIT = 500


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    text = html.unescape(str(value))
    parser = TextExtractor()
    parser.feed(text)
    parser.close()
    cleaned = parser.text() if parser.parts else text
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"\S*/var/folders/\S+", " ", cleaned)
    cleaned = re.sub(r"\S*TemporaryItems/\S+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def truncate(text: str, limit: int = SUMMARY_LIMIT) -> str:
    if len(text) <= limit:
        return text

    preview = text[:limit].rsplit(" ", 1)[0].strip()
    return f"{preview}..."


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


def load_recent_ranked_papers(db_path: Path, days: int) -> list[dict[str, Any]]:
    ensure_project_dirs()
    if not db_path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        table_exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'research_items'"
        ).fetchone()
        if not table_exists:
            return []

        columns = table_columns(connection, "research_items")
        score_expr = "relevance_score" if "relevance_score" in columns else "0 AS relevance_score"
        tags_expr = "topic_tags" if "topic_tags" in columns else "'' AS topic_tags"
        learned_expr = (
            "learned_preference_score"
            if "learned_preference_score" in columns
            else (
                "relevance_score AS learned_preference_score"
                if "relevance_score" in columns
                else "0 AS learned_preference_score"
            )
        )
        personalized_expr = (
            "personalized_score"
            if "personalized_score" in columns
            else (
                "relevance_score AS personalized_score"
                if "relevance_score" in columns
                else "0 AS personalized_score"
            )
        )
        if "published_date" in columns:
            date_expr = "published_date AS published_date"
        elif "published_at" in columns:
            date_expr = "published_at AS published_date"
        else:
            date_expr = "NULL AS published_date"

        rows = connection.execute(
            f"""
            SELECT
                title,
                source_name,
                {date_expr},
                summary,
                link,
                {score_expr},
                {tags_expr},
                {learned_expr},
                {personalized_expr}
            FROM research_items
            """
        ).fetchall()

    papers = []
    for row in rows:
        published_date = parse_date(row["published_date"])
        score = float(row["relevance_score"] or 0)
        personalized_score = float(row["personalized_score"] or score)
        if published_date is None or published_date < cutoff or score <= 0:
            continue

        papers.append(
            {
                "title": clean_text(row["title"]),
                "source": clean_text(row["source_name"]),
                "published_date": published_date,
                "summary": truncate(clean_text(row["summary"])),
                "link": row["link"],
                "relevance_score": score,
                "learned_preference_score": float(row["learned_preference_score"] or score),
                "personalized_score": personalized_score,
                "topic_tags": clean_text(row["topic_tags"]),
            }
        )

    return sorted(
        papers,
        key=lambda paper: (paper["personalized_score"], paper["published_date"]),
        reverse=True,
    )


def print_digest(papers: list[dict[str, Any]], days: int) -> None:
    preferences = load_preferences()

    print(f"# Research Digest: Last {days} Days")
    print()

    if not papers:
        print("No relevant papers found.")
        return

    for paper in papers:
        date = paper["published_date"].strftime("%Y-%m-%d")
        print(f"## {paper['title']}")
        print(f"- Source: {paper['source']}")
        print(f"- Published: {date}")
        print(f"- Personalized score: {paper['personalized_score']:.1f}")
        print(f"- Relevance label: {score_label(paper['personalized_score'], preferences)}")
        print(f"- Topical score: {paper['relevance_score']:.1f}")
        print(f"- Learned score: {paper['learned_preference_score']:.1f}")
        if paper["topic_tags"]:
            print(f"- Topics: {paper['topic_tags']}")
        if paper["summary"]:
            print(f"- Summary: {paper['summary']}")
        if paper["link"]:
            print(f"- Link: {paper['link']}")
        print()


def write_digest_html(papers: list[dict[str, Any]], days: int, output_path: Path) -> None:
    ensure_project_dirs()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    preferences = load_preferences()
    parts = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'><title>Research Digest</title></head><body>",
        f"<h1>Research Digest: Last {days} Days</h1>",
    ]

    if not papers:
        parts.append("<p>No relevant papers found.</p>")
    else:
        for paper in papers:
            date = paper["published_date"].strftime("%Y-%m-%d")
            parts.extend(
                [
                    f"<h2>{html.escape(paper['title'])}</h2>",
                    f"<p><strong>Source:</strong> {html.escape(paper['source'])}</p>",
                    f"<p><strong>Published:</strong> {date}</p>",
                    f"<p><strong>Personalized score:</strong> {paper['personalized_score']:.1f}</p>",
                    f"<p><strong>Relevance:</strong> {score_label(paper['personalized_score'], preferences)}</p>",
                ]
            )
            if paper["topic_tags"]:
                parts.append(f"<p><strong>Topics:</strong> {html.escape(paper['topic_tags'])}</p>")
            if paper["summary"]:
                parts.append(f"<p>{html.escape(paper['summary'])}</p>")
            if paper["link"]:
                parts.append(
                    f"<p><a href='{html.escape(paper['link'])}'>Open paper</a></p>"
                )

    parts.append("</body></html>")
    output_path.write_text("\n".join(parts), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a recent ranked research digest.")
    parser.add_argument("--database", default=DEFAULT_DB_PATH, type=Path)
    parser.add_argument("--days", default=7, type=int)
    parser.add_argument("--output", default=DIGEST_PATH, type=Path)
    args = parser.parse_args()

    papers = load_recent_ranked_papers(args.database, args.days)
    print_digest(papers, args.days)
    write_digest_html(papers, args.days, args.output)


if __name__ == "__main__":
    main()
