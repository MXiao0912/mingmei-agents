from __future__ import annotations

import html
import re
import sqlite3
import sys
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from shared.paths import DB_PATH, PREFERENCES_PATH, ensure_project_dirs
from shared.preferences import load_preferences, score_bounds, score_label
from agents.research_reader.learn import learn_preferences
from agents.research_reader.rank import rank_items


SUMMARY_LIMIT = 700
READ_STATUS_OPTIONS = ["unread", "skimmed", "read", "saved", "irrelevant"]
UNGROUPED_GROUP = "Ungrouped"
MAX_PREFERENCE_WEIGHT = 10.0
DATE_RANGE_OPTIONS = {
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 90 days": 90,
    "Last 180 days": 180,
    "All": None,
}


def save_preferences(preferences: dict) -> None:
    ensure_project_dirs()
    with PREFERENCES_PATH.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(preferences, handle, sort_keys=False, allow_unicode=True)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.parts.append(html.unescape(f"&#{name};"))

    def text(self) -> str:
        return " ".join(self.parts)


def clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""

    text = html.unescape(str(value))
    parser = TextExtractor()
    parser.feed(text)
    parser.close()
    cleaned = parser.text() if parser.parts else text
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def clean_summary(value: object) -> str:
    text = clean_text(value)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\S*/var/folders/\S+", " ", text)
    text = re.sub(r"\S*TemporaryItems/\S+", " ", text)
    text = re.sub(r"Screenshot\s+\d{4}-\d{2}-\d{2}\s+at\s+\d{1,2}\.\d{2}\.\d{2}(?:\.\w+)?", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) <= SUMMARY_LIMIT:
        return text

    preview = text[:SUMMARY_LIMIT].rsplit(" ", 1)[0].strip()
    return f"{preview}..."


def table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


@st.cache_data(ttl=60)
def database_health(db_path: str) -> dict[str, object]:
    path = Path(db_path)
    if not path.exists():
        return {
            "exists": False,
            "item_count": 0,
            "last_collection_date": None,
        }

    with sqlite3.connect(path) as connection:
        table_exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'research_items'"
        ).fetchone()
        if not table_exists:
            return {
                "exists": True,
                "item_count": 0,
                "last_collection_date": None,
            }

        item_count = connection.execute("SELECT COUNT(*) FROM research_items").fetchone()[0]
        columns = table_columns(connection, "research_items")
        if "created_at" in columns:
            last_collection_date = connection.execute(
                "SELECT MAX(created_at) FROM research_items"
            ).fetchone()[0]
        else:
            last_collection_date = None

    return {
        "exists": True,
        "item_count": item_count,
        "last_collection_date": last_collection_date,
    }


def ensure_feedback_schema(connection: sqlite3.Connection) -> None:
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
    connection.commit()


def save_feedback(item_id: int, rating: int, read_status: str, notes: str) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        ensure_feedback_schema(connection)
        connection.execute(
            """
            INSERT INTO feedback (item_id, rating, read_status, notes)
            VALUES (?, ?, ?, ?)
            """,
            (item_id, rating, read_status, notes.strip()),
        )
        connection.execute(
            """
            UPDATE research_items
            SET user_rating = ?, read_status = ?
            WHERE id = ?
            """,
            (rating, read_status, item_id),
        )
        connection.commit()


def date_filter(items: pd.DataFrame, label: str) -> pd.DataFrame:
    days = DATE_RANGE_OPTIONS[label]
    if days is None:
        return items

    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    return items[items["published_date"].notna() & (items["published_date"] >= cutoff)]


@st.cache_data(ttl=60)
def load_items(db_path: str) -> pd.DataFrame:
    path = Path(db_path)
    if not path.exists():
        return pd.DataFrame()

    with sqlite3.connect(path) as connection:
        table_exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'research_items'"
        ).fetchone()
        if not table_exists:
            return pd.DataFrame()

        ensure_feedback_schema(connection)
        columns = table_columns(connection, "research_items")
        status_expr = "read_status" if "read_status" in columns else "'unread' AS read_status"
        score_expr = "relevance_score" if "relevance_score" in columns else "0 AS relevance_score"
        tags_expr = "topic_tags" if "topic_tags" in columns else "'' AS topic_tags"
        user_rating_expr = "user_rating" if "user_rating" in columns else "NULL AS user_rating"
        learned_expr = (
            "learned_preference_score"
            if "learned_preference_score" in columns
            else "0 AS learned_preference_score"
        )
        personalized_expr = (
            "personalized_score"
            if "personalized_score" in columns
            else "0 AS personalized_score"
        )
        if "published_date" in columns:
            date_expr = "published_date AS published_date"
        elif "published_at" in columns:
            date_expr = "published_at AS published_date"
        else:
            date_expr = "NULL AS published_date"

        query = f"""
            SELECT
                id,
                title,
                source_name,
                {date_expr},
                summary,
                link,
                {status_expr},
                {score_expr},
                {tags_expr},
                {user_rating_expr},
                {learned_expr},
                {personalized_expr},
                (
                    SELECT notes
                    FROM feedback
                    WHERE feedback.item_id = research_items.id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                ) AS latest_notes
            FROM research_items
        """
        items = pd.read_sql_query(query, connection)

    if items.empty:
        return items

    items["published_date"] = pd.to_datetime(items["published_date"], errors="coerce", utc=True)
    items["relevance_score"] = pd.to_numeric(items["relevance_score"], errors="coerce").fillna(0)
    items["learned_preference_score"] = pd.to_numeric(
        items["learned_preference_score"], errors="coerce"
    ).fillna(items["relevance_score"])
    items["personalized_score"] = pd.to_numeric(
        items["personalized_score"], errors="coerce"
    ).fillna(items["relevance_score"])
    items.loc[items["personalized_score"] == 0, "personalized_score"] = items["relevance_score"]
    items["topic_tags"] = items["topic_tags"].fillna("")
    items["read_status"] = items["read_status"].fillna("unread")
    items["latest_notes"] = items["latest_notes"].fillna("")
    for column in ["title", "source_name", "read_status", "topic_tags", "latest_notes"]:
        items[column] = items[column].apply(clean_text)
    items["summary"] = items["summary"].apply(clean_summary)

    return items.sort_values(
        ["personalized_score", "published_date"],
        ascending=[False, False],
        na_position="last",
    )


def format_date(value: pd.Timestamp) -> str:
    if pd.isna(value):
        return "No date"
    return value.strftime("%Y-%m-%d")


def render_item(row: pd.Series) -> None:
    preferences = load_preferences()
    label = score_label(float(row["personalized_score"]), preferences)

    st.subheader(row["title"])
    st.caption(
        f"{row['source_name']} | {format_date(row['published_date'])} | "
        f"{row['read_status']} | {label} | personalized {row['personalized_score']:.1f}"
    )

    st.write(
        "Scores: "
        f"topical {row['relevance_score']:.1f} | "
        f"learned {row['learned_preference_score']:.1f} | "
        f"personalized {row['personalized_score']:.1f}"
    )

    if row.get("topic_tags"):
        st.write(f"Topics: {row['topic_tags']}")

    if row.get("summary"):
        st.write(row["summary"])

    if row.get("link"):
        st.link_button("Open paper", row["link"])

    with st.expander("Feedback"):
        rating_value = row["user_rating"]
        default_rating = 3 if pd.isna(rating_value) else int(rating_value)
        rating = st.slider(
            "Rating",
            min_value=1,
            max_value=5,
            value=default_rating,
            key=f"rating_{row['id']}",
        )
        status_index = (
            READ_STATUS_OPTIONS.index(row["read_status"])
            if row["read_status"] in READ_STATUS_OPTIONS
            else 0
        )
        read_status = st.selectbox(
            "Read status",
            READ_STATUS_OPTIONS,
            index=status_index,
            key=f"status_{row['id']}",
        )
        notes = st.text_area(
            "Notes",
            value=row["latest_notes"],
            key=f"notes_{row['id']}",
        )
        if st.button("Save feedback", key=f"save_{row['id']}"):
            save_feedback(int(row["id"]), rating, read_status, notes)
            learn_preferences(DB_PATH)
            load_items.clear()
            database_health.clear()
            st.success("Feedback saved and scores updated.")
            st.rerun()

    st.divider()


def grouped_keywords(preferences: dict) -> dict[str, list[dict[str, object]]]:
    weights = preferences["keyword_weights"]
    groups = {}
    assigned = set()

    for group, keywords in preferences["topic_mappings"].items():
        groups[group] = []
        for keyword in keywords:
            groups[group].append(
                {
                    "keyword": keyword,
                    "weight": float(weights.get(keyword, 1)),
                }
            )
            assigned.add(keyword)

    ungrouped = [
        {
            "keyword": keyword,
            "weight": float(weight),
        }
        for keyword, weight in weights.items()
        if keyword not in assigned
    ]
    if ungrouped:
        groups[UNGROUPED_GROUP] = ungrouped

    return groups


def add_pending_preference_items(groups: dict[str, list[dict[str, object]]]) -> dict[str, list[dict[str, object]]]:
    groups = {group: list(items) for group, items in groups.items()}
    for group in st.session_state.pop("new_preference_groups", []):
        groups.setdefault(group, [])

    for group, keyword in st.session_state.pop("new_preference_keywords", []):
        groups.setdefault(group, []).append({"keyword": keyword, "weight": 1.0})

    return groups


def render_preferences_editor(preferences: dict) -> None:
    with st.expander("Preference Settings"):
        groups = add_pending_preference_items(grouped_keywords(preferences))

        with st.form("add_preference_group"):
            columns = st.columns([3, 1])
            new_group = columns[0].text_input("New group")
            add_group = columns[1].form_submit_button("Add group")
        if add_group and new_group.strip():
            st.session_state.setdefault("new_preference_groups", []).append(new_group.strip())
            st.rerun()

        with st.form("preference_settings"):
            keyword_weights = {}
            topic_mappings = {}

            for group, items in groups.items():
                st.subheader(group)
                topic_mappings[group] = []

                for index, item in enumerate(items):
                    columns = st.columns([4, 1])
                    keyword = columns[0].text_input(
                        "Keyword",
                        value=str(item["keyword"]),
                        key=f"pref_keyword_{group}_{index}",
                    ).strip()
                    weight = columns[1].number_input(
                        "Weight",
                        min_value=0.0,
                        max_value=MAX_PREFERENCE_WEIGHT,
                        value=float(item["weight"]),
                        step=0.5,
                        key=f"pref_weight_{group}_{index}",
                    )
                    if keyword:
                        if group != UNGROUPED_GROUP:
                            topic_mappings[group].append(keyword)
                        keyword_weights[keyword] = weight

                new_keyword = st.text_input(
                    f"Add keyword to {group}",
                    key=f"new_keyword_{group}",
                )
                if st.form_submit_button(f"Add keyword to {group}"):
                    if new_keyword.strip():
                        st.session_state.setdefault("new_preference_keywords", []).append(
                            (group, new_keyword.strip())
                        )
                    st.rerun()

            st.subheader("Source Boosts")
            source_boosts = {}
            for source, boost in preferences["source_boosts"].items():
                source_boosts[source] = st.number_input(
                    source,
                    min_value=0.0,
                    max_value=MAX_PREFERENCE_WEIGHT,
                    value=float(boost),
                    step=0.5,
                    key=f"source_boost_{source}",
                )

            save_and_apply = st.form_submit_button("Save Preferences and Recalculate Scores")

        if save_and_apply:
            updated = dict(preferences)
            updated["keyword_weights"] = {
                keyword: min(float(weight), MAX_PREFERENCE_WEIGHT)
                for keyword, weight in keyword_weights.items()
            }
            updated["topic_mappings"] = topic_mappings
            updated["source_boosts"] = {
                source: min(float(boost), MAX_PREFERENCE_WEIGHT)
                for source, boost in source_boosts.items()
            }
            save_preferences(updated)
            rank_items(DB_PATH)
            learn_preferences(DB_PATH)
            load_items.clear()
            database_health.clear()
            st.success("Preferences saved and scores recalculated.")
            st.rerun()


def main() -> None:
    st.set_page_config(page_title="Research Reader", layout="wide")
    ensure_project_dirs()
    st.title("Research Reader")

    health = database_health(str(DB_PATH))
    with st.sidebar:
        st.header("Deployment Health")
        st.write(f"Database: {'found' if health['exists'] else 'missing'}")
        st.write(f"Collected items: {health['item_count']}")
        st.write(f"Last collection: {health['last_collection_date'] or 'unknown'}")

    if not health["exists"]:
        st.warning(
            "No database found yet. Run collect.py and rank.py locally first, "
            "or configure a scheduled collector later."
        )
        st.code(
            "python agents/research_reader/collect.py\n"
            "python agents/research_reader/rank.py\n"
            "python agents/research_reader/learn.py",
            language="bash",
        )
        return

    items = load_items(str(DB_PATH))
    if items.empty:
        st.info(
            "No papers found. Run `python agents/research_reader/collect.py` and "
            "`python agents/research_reader/rank.py` first."
        )
        return

    preferences = load_preferences()
    relevance_min, relevance_max = score_bounds(preferences)
    render_preferences_editor(preferences)

    with st.sidebar:
        st.header("Filters")

        sources = sorted(items["source_name"].dropna().unique())
        selected_sources = st.multiselect("Source", sources, default=sources)

        statuses = sorted(items["read_status"].dropna().unique())
        selected_statuses = st.multiselect("Status", statuses, default=statuses)

        date_range = st.selectbox(
            "Date range",
            list(DATE_RANGE_OPTIONS.keys()),
            index=2,
        )

        tags = sorted(
            {
                tag.strip()
                for tag_list in items["topic_tags"].dropna()
                for tag in tag_list.split(",")
                if tag.strip()
            }
        )
        selected_tags = st.multiselect("Topic", tags)

        min_score = st.slider(
            "Minimum personalized score",
            min_value=relevance_min,
            max_value=relevance_max,
            value=relevance_min,
            step=0.5,
        )

    filtered = items[
        items["source_name"].isin(selected_sources)
        & items["read_status"].isin(selected_statuses)
        & (items["personalized_score"] >= min_score)
    ]
    filtered = date_filter(filtered, date_range)

    if selected_tags:
        filtered = filtered[
            filtered["topic_tags"].apply(
                lambda value: any(tag in value.split(", ") for tag in selected_tags)
            )
        ]

    filtered = filtered.sort_values(
        ["personalized_score", "published_date"],
        ascending=[False, False],
        na_position="last",
    )

    st.caption(f"Showing {len(filtered)} of {len(items)} papers")

    for _, row in filtered.iterrows():
        render_item(row)


if __name__ == "__main__":
    main()
