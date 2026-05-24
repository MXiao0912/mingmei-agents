from __future__ import annotations

from typing import Any

import yaml

from shared.paths import PREFERENCES_PATH, ensure_project_dirs

DEFAULT_PREFERENCES: dict[str, Any] = {
    "keyword_weights": {},
    "topic_mappings": {},
    "source_boosts": {},
    "scoring": {
        "title_multiplier": 2,
        "summary_multiplier": 1,
        "relevance_min": 0,
        "relevance_max": 10,
        "personalized_weight_topical": 0.7,
        "personalized_weight_learned": 0.3,
    },
    "relevance_labels": {
        "high": 7,
        "medium": 4,
    },
    "learning": {
        "prior_rating": 3,
        "shrinkage_n": 3,
    },
}


def deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = defaults.copy()
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_preferences(path = PREFERENCES_PATH) -> dict[str, Any]:
    ensure_project_dirs()
    if not path.exists():
        return DEFAULT_PREFERENCES

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}

    return deep_merge(DEFAULT_PREFERENCES, loaded)


def score_bounds(preferences: dict[str, Any]) -> tuple[float, float]:
    scoring = preferences["scoring"]
    return float(scoring["relevance_min"]), float(scoring["relevance_max"])


def score_label(score: float, preferences: dict[str, Any]) -> str:
    labels = preferences["relevance_labels"]
    if score >= float(labels["high"]):
        return "high"
    if score >= float(labels["medium"]):
        return "medium"
    return "low"
