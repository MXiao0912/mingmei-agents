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
NEWS_SOURCES = {"Financial Times Markets"}
POLICY_SOURCES = {
    "BIS Working Papers",
    "CEPR Feed",
    "VoxEU Recent Content",
    "IMF Working Papers",
    "NBER Working Papers",
}
JOURNAL_EXCLUDED_SOURCES = NEWS_SOURCES | POLICY_SOURCES
MUST_READ_VISIBLE_ROWS = 20
SIDE_VISIBLE_ROWS = 5
MUST_READ_ROW_HEIGHT = 112
SIDE_ROW_HEIGHT = 92


def inject_dashboard_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --panel: #121922;
            --panel-raised: #17212c;
            --border: #263442;
            --text: #ecf1f5;
            --muted: #8ea1b4;
            --accent: #39c0c3;
            --high: #39c0c3;
            --medium: #e5b85c;
            --low: #708395;
            --saved: #d7ae55;
        }
        .stApp { background: #0a1118; color: var(--text); text-align: left; }
        [data-testid="stSidebar"] { background: #0d151e; border-right: 1px solid var(--border); text-align: left; }
        [data-testid="stMainBlockContainer"] { padding-top: 1.25rem; padding-bottom: 1rem; max-width: 1500px; }
        h1 { font-size: 1.7rem !important; font-weight: 650 !important; letter-spacing: 0 !important; margin-bottom: .3rem !important; }
        h2, h3 { letter-spacing: 0 !important; }
        [data-testid="stVerticalBlockBorderWrapper"] {
            border-color: var(--border) !important;
            border-radius: 6px !important;
            background: var(--panel);
        }
        [data-testid="stMetric"] {
            border: 1px solid var(--border);
            border-radius: 6px;
            background: var(--panel);
            padding: .45rem .7rem;
        }
        [data-testid="stMetricLabel"] { color: var(--muted); font-size: .72rem; }
        [data-testid="stMetricValue"] { color: var(--text); font-size: 1.18rem; }
        [data-testid="stButton"] button {
            text-align: left !important;
            justify-content: flex-start !important;
        }
        [data-testid="stButton"] button[kind="tertiary"],
        button[data-testid="stBaseButton-tertiary"] {
            color: var(--text);
            padding: 0 !important;
            min-height: 0 !important;
            line-height: 1.25 !important;
            font-weight: 600;
        }
        /* Streamlit renders paper titles inside a tertiary button label. */
        .paper-title,
        [data-testid="stButton"] button p,
        [data-testid="stButton"] button div,
        button[data-testid="stBaseButton-tertiary"] p,
        button[data-testid="stBaseButton-tertiary"] div {
            width: 100%;
            text-align: left !important;
            justify-content: flex-start !important;
        }
        .paper-meta { text-align: left !important; }
        .paper-summary { text-align: left !important; }
        [data-testid="stButton"] button[kind="tertiary"]:hover,
        button[data-testid="stBaseButton-tertiary"]:hover { color: var(--accent); }
        .rr-head {
            color: var(--text);
            font-size: .98rem;
            font-weight: 650;
            margin: .1rem 0 .45rem;
            text-align: left;
        }
        .rr-count { display: block; color: var(--muted); font-size: .73rem; font-weight: 400; margin-top: .12rem; text-align: left; }
        .rr-score {
            display: inline-flex;
            min-width: 2.65rem;
            justify-content: flex-start;
            padding: .2rem .35rem;
            border-radius: 4px;
            font-size: .75rem;
            font-weight: 700;
            color: #081117;
            background: var(--accent);
            text-align: left;
        }
        .rr-label { display: inline-block; padding: .1rem .35rem; border-radius: 4px; font-size: .66rem; font-weight: 700; text-transform: uppercase; text-align: left; }
        .rr-label-high { color: var(--high); background: rgba(57,192,195,.14); }
        .rr-label-medium { color: var(--medium); background: rgba(229,184,92,.14); }
        .rr-label-low { color: var(--low); background: rgba(112,131,149,.15); }
        .rr-status { display: inline-block; padding: .1rem .35rem; border-radius: 4px; font-size: .66rem; text-transform: uppercase; text-align: left; }
        .rr-status-saved { color: var(--saved); background: rgba(215,174,85,.14); }
        .rr-status-irrelevant { color: var(--low); background: rgba(112,131,149,.15); }
        .rr-meta { color: var(--muted); font-size: .7rem; line-height: 1.25; margin: .1rem 0; text-align: left; }
        .rr-snippet { color: #b9c7d3; font-size: .75rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: .12rem; text-align: left; }
        .rr-tag { color: var(--accent); }
        .rr-empty { color: var(--muted); font-size: .8rem; padding: .5rem 0; }
        .rr-reading { max-width: 900px; padding-top: .45rem; }
        .rr-reading-title { color: var(--text); font-size: 2rem; line-height: 1.2; font-weight: 650; margin: .65rem 0 .65rem; }
        .rr-reading-meta { color: var(--muted); font-size: .82rem; line-height: 1.45; margin-bottom: 1.25rem; }
        .rr-section-title { color: var(--muted); font-size: .7rem; font-weight: 700; letter-spacing: .08em; margin: 1.2rem 0 .45rem; }
        .rr-summary { color: #d7e0e8; font-size: .98rem; line-height: 1.62; max-width: 780px; }
        .rr-scoreline { color: var(--muted); font-size: .8rem; margin: .65rem 0 1rem; }
        hr { border-color: var(--border) !important; }
        [data-testid="stExpander"] { border-color: var(--border); background: var(--panel); }
        </style>
        """,
        unsafe_allow_html=True,
    )


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
        if "authors" in columns:
            authors_expr = "authors AS authors"
        elif "author" in columns:
            authors_expr = "author AS authors"
        else:
            authors_expr = "NULL AS authors"
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
                {authors_expr},
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
    items["authors"] = items["authors"].fillna("")
    for column in ["title", "authors", "source_name", "read_status", "topic_tags", "latest_notes"]:
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


def short_summary(value: object, limit: int = 150) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rsplit(' ', 1)[0]}..."


def label_html(label: str) -> str:
    return f'<span class="rr-label rr-label-{html.escape(label)}">{html.escape(label)}</span>'


def status_html(status: str) -> str:
    safe_status = html.escape(str(status))
    if status in {"saved", "irrelevant"}:
        return f'<span class="rr-status rr-status-{safe_status}">{safe_status}</span>'
    return safe_status


def render_detail_view(row: pd.Series) -> None:
    preferences = load_preferences()
    label = score_label(float(row["personalized_score"]), preferences)

    if st.button("Back to dashboard", icon=":material/arrow_back:", type="tertiary"):
        st.session_state.pop("selected_item_id", None)
        st.rerun()

    with st.container():
        st.markdown('<div class="rr-reading">', unsafe_allow_html=True)
        st.markdown(
            f'<div class="rr-reading-title paper-title">{html.escape(str(row["title"]))}</div>',
            unsafe_allow_html=True,
        )
        author_line = f"{row['authors']} | " if row.get("authors") else ""
        st.markdown(
            f'<div class="rr-reading-meta paper-meta">{html.escape(author_line)}'
            f'{html.escape(str(row["source_name"]))} | {html.escape(format_date(row["published_date"]))} | '
            f'{status_html(str(row["read_status"]))} &nbsp; {label_html(label)}</div>',
            unsafe_allow_html=True,
        )
        tags = html.escape(str(row["topic_tags"] or "No topic tags"))
        st.markdown(
            f'<div class="rr-scoreline">Topics: {tags}<br>'
            f'Topical {row["relevance_score"]:.1f} &nbsp;|&nbsp; '
            f'Learned {row["learned_preference_score"]:.1f} &nbsp;|&nbsp; '
            f'Personalized {row["personalized_score"]:.1f}</div>',
            unsafe_allow_html=True,
        )
        if row.get("link"):
            st.link_button("Open article", str(row["link"]), type="primary", icon=":material/open_in_new:")
        st.markdown('<div class="rr-section-title">SUMMARY</div>', unsafe_allow_html=True)
        summary = str(row.get("summary") or "No summary available.")
        st.markdown(f'<div class="rr-summary paper-summary">{html.escape(summary)}</div>', unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    st.divider()
    with st.container(border=True):
        st.subheader("Feedback")
        rating_value = row["user_rating"]
        default_rating = 3 if pd.isna(rating_value) else int(rating_value)
        rating = st.slider(
            "Rating",
            min_value=1,
            max_value=5,
            value=default_rating,
            key=f"rating_details_{row['id']}",
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
            key=f"status_details_{row['id']}",
        )
        notes = st.text_area(
            "Notes",
            value=row["latest_notes"],
            key=f"notes_details_{row['id']}",
        )
        if st.button("Save feedback", key=f"save_details_{row['id']}"):
            save_feedback(int(row["id"]), rating, read_status, notes)
            learn_preferences(DB_PATH)
            load_items.clear()
            database_health.clear()
            st.success("Feedback saved and scores updated.")
            st.rerun()

def select_item(item_id: int) -> None:
    st.session_state.selected_item_id = item_id


def render_must_read_row(row: pd.Series, section_key: str, preferences: dict) -> None:
    label = score_label(float(row["personalized_score"]), preferences)
    title = str(row["title"])
    meta = (
        f"{html.escape(str(row['source_name']))} | {html.escape(format_date(row['published_date']))} | "
        f"{html.escape(str(row['topic_tags'] or 'untagged'))} | {status_html(str(row['read_status']))}"
    )

    with st.container(border=True):
        score_column, body_column = st.columns([0.72, 6], vertical_alignment="top")
        with score_column:
            st.markdown(
                f'<span class="rr-score">{row["personalized_score"]:.1f}</span>',
                unsafe_allow_html=True,
            )
            st.markdown(label_html(label), unsafe_allow_html=True)
        with body_column:
            if st.button(
                title,
                key=f"select_{section_key}_{row['id']}",
                type="tertiary",
                width="stretch",
            ):
                select_item(int(row["id"]))
                st.rerun()
            st.markdown(f'<div class="rr-meta paper-meta">{meta}</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="rr-snippet paper-summary">{html.escape(short_summary(row["summary"]))}</div>',
                unsafe_allow_html=True,
            )


def render_stream_row(row: pd.Series, section_key: str, preferences: dict) -> None:
    label = score_label(float(row["personalized_score"]), preferences)
    title = str(row["title"])
    meta = (
        f"{html.escape(str(row['source_name']))} | {html.escape(format_date(row['published_date']))} | "
        f"score {row['personalized_score']:.1f} | {status_html(str(row['read_status']))}"
    )

    with st.container(border=True):
        if st.button(
            title,
            key=f"select_{section_key}_{row['id']}",
            type="tertiary",
            width="stretch",
        ):
            select_item(int(row["id"]))
            st.rerun()
        st.markdown(
            f'<div class="rr-meta paper-meta">{label_html(label)} &nbsp; {meta}</div>',
            unsafe_allow_html=True,
        )


def render_list_panel(
    title: str,
    items: pd.DataFrame,
    empty_message: str,
    section_key: str,
    preferences: dict,
    main: bool = False,
) -> None:
    visible_rows = MUST_READ_VISIBLE_ROWS if main else SIDE_VISIBLE_ROWS
    row_height = MUST_READ_ROW_HEIGHT if main else SIDE_ROW_HEIGHT
    panel_height = 58 + visible_rows * row_height

    with st.container(border=True, height=panel_height):
        st.markdown(
            f'<div class="rr-head">{html.escape(title)}'
            f'<span class="rr-count">{len(items)} items</span></div>',
            unsafe_allow_html=True,
        )
        if items.empty:
            st.markdown(f'<div class="rr-empty">{html.escape(empty_message)}</div>', unsafe_allow_html=True)
            return

        for _, row in items.iterrows():
            if main:
                render_must_read_row(row, section_key, preferences)
            else:
                render_stream_row(row, section_key, preferences)


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
    inject_dashboard_css()
    st.title("Research Intelligence")
    st.caption("PERSONAL RESEARCH COMMAND CENTER  |  RANKED MONITOR")

    health = database_health(str(DB_PATH))
    with st.sidebar:
        with st.expander("App Status"):
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
    selected_item_id = st.session_state.get("selected_item_id")
    if selected_item_id is not None:
        selected_rows = items[items["id"] == selected_item_id]
        if not selected_rows.empty:
            render_detail_view(selected_rows.iloc[0])
            return
        st.session_state.pop("selected_item_id", None)

    with st.sidebar.expander("Filters", expanded=True):
        sources = sorted(items["source_name"].dropna().unique())
        selected_sources = st.multiselect("Source", sources, placeholder="All sources")

        selected_statuses = st.multiselect(
            "Status",
            READ_STATUS_OPTIONS,
            default=READ_STATUS_OPTIONS,
        )

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

    with st.sidebar:
        render_preferences_editor(preferences)

    source_matches = (
        items["source_name"].isin(selected_sources)
        if selected_sources
        else pd.Series(True, index=items.index)
    )
    filtered = items[
        source_matches
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

    high_threshold = float(preferences["relevance_labels"]["high"])
    # Must Read is an actionable queue: recent filtered items, not finished,
    # whose personalized ranking reaches the configured high threshold.
    must_read = filtered[
        filtered["read_status"].isin(["unread", "skimmed"])
        & (filtered["personalized_score"] >= high_threshold)
    ]
    news = filtered[filtered["source_name"].isin(NEWS_SOURCES)]
    policy = filtered[filtered["source_name"].isin(POLICY_SOURCES)]
    research = filtered[~filtered["source_name"].isin(JOURNAL_EXCLUDED_SOURCES)]
    saved = filtered[filtered["read_status"] == "saved"]

    tag_values = [
        tag.strip()
        for tag_text in filtered["topic_tags"]
        for tag in str(tag_text).split(",")
        if tag.strip()
    ]
    top_topic = pd.Series(tag_values).value_counts().index[0] if tag_values else "-"
    summary_columns = st.columns(5)
    summary_columns[0].metric("Shown", len(filtered))
    summary_columns[1].metric("Must Read", len(must_read))
    summary_columns[2].metric("Saved", len(saved))
    summary_columns[3].metric("High Relevance", int((filtered["personalized_score"] >= high_threshold).sum()))
    summary_columns[4].metric("Top Topic", top_topic)

    st.write("")
    main_column, side_column = st.columns([2.1, 1], gap="medium")
    with main_column:
        render_list_panel(
            "Must Read / Ranked Queue",
            must_read,
            "No high-relevance unread or skimmed papers match the current filters.",
            "must_read",
            preferences,
            main=True,
        )
    with side_column:
        render_list_panel("News - Markets", news, "No market news.", "news", preferences)
        render_list_panel("Policy - Institutions", policy, "No policy papers.", "policy", preferences)
        render_list_panel("Research - Journals", research, "No journal papers.", "research", preferences)
        render_list_panel("Saved for Later", saved, "No saved papers yet.", "saved", preferences)


if __name__ == "__main__":
    main()
