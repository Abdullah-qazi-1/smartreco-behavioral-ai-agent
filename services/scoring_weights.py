"""
Level 4.4 — Centralized scoring weights & thresholds.

All previously-hardcoded constants from interest_profile.py,
retrieval.py, and trigger.py live here — tuning the agent means
editing ONE file.
"""

# ---- From services/interest_profile.py ----
CONFIDENCE_CUTOFF_SECONDS = 10
NO_DWELL_DATA_WEIGHT = 0.4
SEARCH_ALIGNED_BOOST_WEIGHT = 0.65
SEARCH_ALIGNMENT_WINDOW_MINUTES = 30
ALIGNMENT_RELATEDNESS_THRESHOLD = 0.5
DECAY_HALF_LIFE_DAYS = 7
EXPLICIT_INTEREST_WEIGHT = 2.0
SPREAD_FACTOR = 0.35
EVENT_LOOKBACK_DAYS = 60  # Configurable lookback window for behavioral event queries

# ---- From services/retrieval.py ----
PRIMARY_RESULTS_LIMIT = 5
ALTERNATIVE_RESULTS_LIMIT = 2
RECENT_TITLES_FOR_QUERY = 5
SEARCH_INTENT_RELATEDNESS_CEILING = 0.5
BRIDGE_RESULTS_LIMIT = 2

# ---- From services/trigger.py ----
NEW_EVENTS_TRIGGER_THRESHOLD = 5

# ---- Negative signals ----
REVIEW_POSITIVE_RATING_CUTOFF = 4.0   # >= this rating -> category boost
REVIEW_NEGATIVE_RATING_CUTOFF = 2.0   # <= this rating -> category penalty
REVIEW_POSITIVE_WEIGHT = 0.8
REVIEW_NEGATIVE_WEIGHT = -0.8

QUICK_CLOSE_SECONDS = 5
QUICK_CLOSE_WEIGHT = -0.3

DISMISS_WEIGHT = -1.5

# ---- Cold-start fallback ----
COLD_START_RESULTS_LIMIT = 5

# ---- Scoring engine (prompt-aligned tunables) ----
MIN_EVENTS_FOR_PERSONALIZATION = 5
SIMILARITY_THRESHOLD = 0.65
EXPLICIT_INTEREST_BOOST = 1.5
LOOKBACK_DAYS = 21
CATEGORY_DOMINANCE_RATIO = 3.0
MIN_CATEGORY_SCORE_FOR_TAG = 0.1

EVENT_BASE_WEIGHTS = {
    "search": 3.0,
    "view": 1.0,
    "time_spent": 1.0,
    "click": 0.5,
    "dismiss": -1.0,
    "enroll": 5.0,
    "scroll_depth": 0.3,
}

# ---- Level 5 Config & Logging ----
LOGGER_NAMESPACE = "smartreco"
HEALTH_CHECK_TIMEOUT_SECONDS = 3.0