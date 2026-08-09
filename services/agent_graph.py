"""
services/agent_graph.py — LangGraph Agent Workflow for SmartReco.

Refactors the recommendation pipeline into an explicit LangGraph StateGraph with nodes:
  1. analyze_activity: pulls and normalizes user's recent events
  2. decide_retrieval: gates on tracking preference & should_regenerate trigger
  3. retrieve: calls get_recommendation_candidates
  4. evaluate_retrieval_quality: checks candidate count and similarity quality
  5. refine: broadens/widens retrieval if quality is low (retried at most once)
  6. generate: calls generate_narrative via llm_client, hydrates products, saves Recommendation DB row
"""
import json
import logging
from typing import Dict, List, Optional, Any, TypedDict
from sqlalchemy.orm import Session

from database.models import User, Product, Recommendation, Review
from services.scoring_engine import fetch_scoring_events, remove_bot_noise
from services.trigger import should_regenerate
from services.retrieval import get_recommendation_candidates, _cold_start_result
from services.llm_client import generate_narrative
from services.tracking_prefs import is_agent_tracking_enabled
from services.agent import _batch_hydrate_products, _dedupe_products, _build_main_narrative_context, _build_search_intent_narrative_context
from services.reasoning import build_recommendation_reasoning, save_recommendation_explanations

try:
    from langsmith import traceable
except ImportError:
    def traceable(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

logger = logging.getLogger("smartreco.agent_graph")


class AgentState(TypedDict, total=False):
    db: Session
    user: User
    force: bool
    events: List[Dict[str, Any]]
    should_proceed: bool
    candidates: Optional[Dict[str, Any]]
    refine_count: int
    widened: bool
    quality_ok: bool
    main_products: List[Product]
    intent_products: List[Product]
    payload: Dict[str, Any]
    recommendation: Optional[Recommendation]


@traceable(name="analyze_activity", run_type="chain")
def analyze_activity(state: AgentState) -> AgentState:
    """Pull and normalize user's recent events."""
    db = state["db"]
    user = state["user"]
    logger.info("LangGraph Node [analyze_activity] for user_id=%s", user.id)
    raw_events = fetch_scoring_events(db, user)
    cleaned = remove_bot_noise(raw_events)
    state["events"] = cleaned
    return state


@traceable(name="decide_retrieval", run_type="chain")
def decide_retrieval(state: AgentState) -> AgentState:
    """Gates execution based on tracking toggle and should_regenerate trigger."""
    db = state["db"]
    user = state["user"]
    force = state.get("force", False)
    logger.info("LangGraph Node [decide_retrieval] for user_id=%s (force=%s)", user.id, force)

    if not is_agent_tracking_enabled(db, user):
        logger.info("Decide node: tracking disabled for user_id=%s", user.id)
        state["should_proceed"] = False
        return state

    if not force and not should_regenerate(db, user, force=force):
        logger.info("Decide node: trigger threshold not met for user_id=%s", user.id)
        state["should_proceed"] = False
        return state

    state["should_proceed"] = True
    return state


@traceable(name="retrieve", run_type="retriever")
def retrieve(state: AgentState) -> AgentState:
    """Calls get_recommendation_candidates for product retrieval."""
    db = state["db"]
    user = state["user"]
    logger.info("LangGraph Node [retrieve] for user_id=%s (widened=%s)", user.id, state.get("widened", False))
    
    candidates = get_recommendation_candidates(
        db, user, force_widened=state.get("widened", False)
    )
    state["candidates"] = candidates
    return state


@traceable(name="evaluate_retrieval_quality", run_type="chain")
def evaluate_retrieval_quality(state: AgentState) -> AgentState:
    """Checks candidate count and similarity quality scores."""
    candidates = state.get("candidates")
    user = state["user"]
    logger.info("LangGraph Node [evaluate_retrieval_quality] for user_id=%s", user.id)

    if not candidates:
        state["quality_ok"] = False
        return state

    if candidates.get("cold_start") or candidates.get("low_confidence"):
        logger.info("Evaluation node: candidates marked cold_start/low_confidence")
        state["quality_ok"] = False
        return state

    primary = candidates.get("primary_products", [])
    if len(primary) < 2:
        logger.info("Evaluation node: primary candidate count (%d) below threshold", len(primary))
        state["quality_ok"] = False
        return state

    state["quality_ok"] = True
    return state


@traceable(name="refine", run_type="chain")
def refine(state: AgentState) -> AgentState:
    """Widens retrieval query by relaxing filters on low quality (retried once)."""
    user = state["user"]
    refine_count = state.get("refine_count", 0)
    logger.info("LangGraph Node [refine] for user_id=%s (refine_count=%d)", user.id, refine_count)
    state["refine_count"] = refine_count + 1
    state["widened"] = True
    return state


@traceable(name="generate", run_type="chain")
def generate(state: AgentState) -> AgentState:
    """Generates LLM narratives, hydrates products, and persists Recommendation row."""
    db = state["db"]
    user = state["user"]
    candidates = state.get("candidates")
    logger.info("LangGraph Node [generate] for user_id=%s", user.id)

    if not candidates:
        logger.info("Generate node: no candidates available, skipping rec")
        state["recommendation"] = None
        return state

    main_products = _dedupe_products(
        candidates.get("primary_products", [])
        + candidates.get("instructor_own_products", [])
        + candidates.get("instructor_alternative_products", [])
    )

    search_branch = candidates.get("search_intent_branch")
    intent_products: List[Product] = []
    if search_branch and search_branch.get("products"):
        main_ids = {p.id for p in main_products}
        intent_products = [p for p in search_branch["products"] if p.id not in main_ids]

    if not main_products and not intent_products:
        logger.info("Generate node: empty product list for user_id=%s", user.id)
        state["recommendation"] = None
        return state

    payload = {}

    if main_products:
        hydrated_main = _batch_hydrate_products(db, main_products)
        main_context = _build_main_narrative_context(candidates)
        main_narrative = generate_narrative(main_context, hydrated_main)
        payload["main"] = {
            "narrative": main_narrative,
            "product_ids": [p.id for p in main_products],
        }

    if intent_products:
        hydrated_intent = _batch_hydrate_products(db, intent_products)
        intent_context = _build_search_intent_narrative_context(candidates)
        intent_narrative = generate_narrative(intent_context, hydrated_intent)
        payload["search_intent"] = {
            "query": search_branch["search_query"],
            "category": search_branch["inferred_category"],
            "narrative": intent_narrative,
            "product_ids": [p.id for p in intent_products],
        }

    if not payload:
        state["recommendation"] = None
        return state

    # Mark existing recommendations as is_latest=False
    db.query(Recommendation).filter(
        Recommendation.user_id == user.id,
        Recommendation.is_latest == True,  # noqa: E712
    ).update({"is_latest": False})

    if candidates.get("cold_start"):
        trigger_reason = "cold_start"
    elif candidates.get("instructor_mode"):
        trigger_reason = f"instructor_search:{candidates['instructor_name']}"
    elif "search_intent" in payload:
        trigger_reason = f"category:{candidates['dominant_category']}+search_intent:{payload['search_intent']['category']}"
    else:
        trigger_reason = f"category:{candidates['dominant_category']}"

    all_ids = payload.get("main", {}).get("product_ids", []) + payload.get("search_intent", {}).get("product_ids", [])

    recommendation = Recommendation(
        user_id=user.id,
        narrative=json.dumps(payload),
        product_ids=json.dumps(all_ids),
        trigger_reason=trigger_reason,
        is_latest=True,
    )
    db.add(recommendation)
    db.commit()
    db.refresh(recommendation)

    reasoning = build_recommendation_reasoning(db, user, tracking_enabled=True)
    save_recommendation_explanations(db, recommendation, reasoning)

    logger.info("Recommendation persisted via LangGraph pipeline: rec_id=%s user_id=%s", recommendation.id, user.id)
    state["payload"] = payload
    state["recommendation"] = recommendation
    return state


# --- Routing decision functions ---

def route_decide(state: AgentState) -> str:
    if state.get("should_proceed", False):
        return "retrieve"
    return "end"


def route_evaluate(state: AgentState) -> str:
    if state.get("quality_ok", False):
        return "generate"
    if state.get("refine_count", 0) < 1:
        return "refine"
    return "generate"


# --- Build LangGraph StateGraph ---

def build_recommendation_graph():
    try:
        from langgraph.graph import StateGraph, START, END

        graph_builder = StateGraph(AgentState)

        graph_builder.add_node("analyze_activity", analyze_activity)
        graph_builder.add_node("decide_retrieval", decide_retrieval)
        graph_builder.add_node("retrieve", retrieve)
        graph_builder.add_node("evaluate_retrieval_quality", evaluate_retrieval_quality)
        graph_builder.add_node("refine", refine)
        graph_builder.add_node("generate", generate)

        graph_builder.add_edge(START, "analyze_activity")
        graph_builder.add_edge("analyze_activity", "decide_retrieval")
        graph_builder.add_conditional_edges(
            "decide_retrieval",
            route_decide,
            {"retrieve": "retrieve", "end": END},
        )
        graph_builder.add_edge("retrieve", "evaluate_retrieval_quality")
        graph_builder.add_conditional_edges(
            "evaluate_retrieval_quality",
            route_evaluate,
            {"generate": "generate", "refine": "refine"},
        )
        graph_builder.add_edge("refine", "retrieve")
        graph_builder.add_edge("generate", END)

        return graph_builder.compile()
    except Exception as exc:
        logger.warning("LangGraph import/compile failed (%s). Falling back to functional runner.", exc)
        return None


_compiled_graph = None

def get_recommendation_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_recommendation_graph()
    return _compiled_graph


def run_recommendation_pipeline(db: Session, user: User, force: bool = False) -> Optional[Recommendation]:
    """
    Executes the LangGraph StateGraph (or fallback node runner if graph compilation failed).
    """
    initial_state: AgentState = {
        "db": db,
        "user": user,
        "force": force,
        "events": [],
        "should_proceed": True,
        "candidates": None,
        "refine_count": 0,
        "widened": False,
        "quality_ok": True,
        "payload": {},
        "recommendation": None,
    }

    graph = get_recommendation_graph()
    if graph is not None:
        try:
            final_state = graph.invoke(initial_state)
            return final_state.get("recommendation")
        except Exception as exc:
            logger.error("LangGraph invocation failed: %s, executing step runner directly", exc, exc_info=True)

    # Deterministic fallback step execution adhering to the exact same graph node sequence
    state = analyze_activity(initial_state)
    state = decide_retrieval(state)
    if not state.get("should_proceed", False):
        return None

    state = retrieve(state)
    state = evaluate_retrieval_quality(state)
    if not state.get("quality_ok", False) and state.get("refine_count", 0) < 1:
        state = refine(state)
        state = retrieve(state)

    state = generate(state)
    return state.get("recommendation")