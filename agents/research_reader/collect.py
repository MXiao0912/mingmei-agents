from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, quote

import feedparser
import requests
import yaml
from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import Session, declarative_base


ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT))

from shared.paths import DB_PATH, SOURCES_PATH, ensure_project_dirs


Base = declarative_base()
REQUEST_TIMEOUT_SECONDS = 10
USER_AGENT = "ResearchReader/1.0"


class ResearchItem(Base):
    __tablename__ = "research_items"
    __table_args__ = (UniqueConstraint("link", name="uq_research_items_link"),)

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    link = Column(String, nullable=False)
    summary = Column(Text, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    source_name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class ArticlePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta_descriptions: list[str] = []
        self.in_abstract = False
        self.abstract_parts: list[str] = []
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        self._tag_stack.append(tag)

        if tag == "meta":
            name = attrs_dict.get("name", "").lower()
            prop = attrs_dict.get("property", "").lower()
            content = attrs_dict.get("content", "").strip()
            if content and name in {"citation_abstract", "dc.description", "description"}:
                self.meta_descriptions.append(content)
            elif content and prop in {"og:description", "twitter:description"}:
                self.meta_descriptions.append(content)

        class_text = f"{attrs_dict.get('class', '')} {attrs_dict.get('id', '')}".lower()
        if "abstract" in class_text:
            self.in_abstract = True

    def handle_endtag(self, tag: str) -> None:
        if self._tag_stack:
            self._tag_stack.pop()
        if self.in_abstract and tag in {"section", "div", "article"}:
            self.in_abstract = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if self.in_abstract and text:
            self.abstract_parts.append(text)


def clean_text(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_journal_like(source_name: str, link: str) -> bool:
    source = source_name.lower()
    return (
        "journal" in source
        or "econometrica" in source
        or "review of financial studies" in source
        or "onlinelibrary.wiley.com/doi" in link.lower()
    )


def weak_summary(summary: Optional[str], source_name: str) -> bool:
    text = clean_text(summary or "")
    if not text:
        return True

    lowered = text.lower()
    source = source_name.lower()
    return (
        len(text) < 160
        and (
            "earlyview" in lowered
            or "volume " in lowered
            or "issue " in lowered
            or "page " in lowered
            or lowered == source
            or lowered.startswith(source)
        )
    )


def fetch_article_summary(link: str) -> Optional[str]:
    crossref_summary = fetch_crossref_summary(link)
    if crossref_summary:
        return crossref_summary

    try:
        response = requests.get(
            link,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException:
        return None

    parser = ArticlePageParser()
    parser.feed(response.text)
    parser.close()

    candidates = parser.meta_descriptions + [" ".join(parser.abstract_parts)]
    for candidate in candidates:
        cleaned = clean_text(candidate)
        if len(cleaned) > 120:
            return cleaned

    return None


def extract_doi(link: str) -> Optional[str]:
    match = re.search(r"/doi/(?:abs|full|epdf|pdf/)?([^?#]+)", link)
    if not match:
        match = re.search(r"doi\.org/(10\.\d{4,9}/[^?#]+)", link)
    if not match:
        return None

    return unquote(match.group(1)).strip("/")


def fetch_crossref_summary(link: str) -> Optional[str]:
    doi = extract_doi(link)
    if not doi:
        return None

    url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException:
        return None

    abstract = response.json().get("message", {}).get("abstract")
    if not abstract:
        return None

    cleaned = clean_text(abstract)
    return cleaned if len(cleaned) > 120 else None


def improve_summary(summary: Optional[str], source_name: str, link: str) -> Optional[str]:
    if not is_journal_like(source_name, link) or not weak_summary(summary, source_name):
        return summary

    fetched = fetch_article_summary(link)
    return fetched or summary


def load_sources(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    sources = data if isinstance(data, list) else data.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("sources.yaml must contain a list or a 'sources' list")

    normalized = []
    for source in sources:
        if isinstance(source, str):
            normalized.append({"url": source})
        elif isinstance(source, dict) and source.get("url"):
            normalized.append(source)

    return normalized


def parse_published(entry: dict) -> Optional[datetime]:
    published = entry.get("published") or entry.get("updated")
    if not published:
        return None

    try:
        parsed = parsedate_to_datetime(published)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def entry_to_item(entry: dict, source_name: str) -> Optional[ResearchItem]:
    link = entry.get("link")
    if not link:
        return None

    return ResearchItem(
        title=entry.get("title") or "Untitled",
        link=link,
        summary=improve_summary(entry.get("summary") or entry.get("description"), source_name, link),
        published_at=parse_published(entry),
        source_name=source_name,
    )


def collect(sources_path: Path, database_url: str) -> int:
    ensure_project_dirs()
    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.removeprefix("sqlite:///"))
        if db_path.parent != Path("."):
            db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(database_url)
    Base.metadata.create_all(engine)

    added = 0
    updated = 0
    with Session(engine) as session:
        existing_items = {
            item.link: item
            for item in session.scalars(select(ResearchItem))
        }

        for source in load_sources(sources_path):
            feed = feedparser.parse(source["url"])
            source_name = source.get("name") or feed.feed.get("title") or source["url"]

            for entry in feed.entries:
                item = entry_to_item(entry, source_name)
                if item is None:
                    continue

                existing = existing_items.get(item.link)
                if existing:
                    if weak_summary(existing.summary, existing.source_name) and not weak_summary(item.summary, source_name):
                        existing.summary = item.summary
                        updated += 1
                    continue

                session.add(item)
                existing_items[item.link] = item
                added += 1

        for item in list(existing_items.values()):
            if not is_journal_like(item.source_name, item.link):
                continue
            if not weak_summary(item.summary, item.source_name):
                continue

            improved = fetch_article_summary(item.link)
            if improved:
                item.summary = improved
                updated += 1

        session.commit()

    return added, updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect research RSS items into SQLite.")
    parser.add_argument("--sources", default=SOURCES_PATH, type=Path)
    parser.add_argument("--database", default=f"sqlite:///{DB_PATH}")
    args = parser.parse_args()

    added, updated = collect(args.sources, args.database)
    print(f"Added {added} new item(s). Updated {updated} summary/summaries.")


if __name__ == "__main__":
    main()
