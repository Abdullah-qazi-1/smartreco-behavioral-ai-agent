"""
services/llm_client.py — Centralized LLM Provider Abstraction & Grounded Generation.

All LLM calls route exclusively through the Mesh API (OpenAI-compatible).
Configure MESH_API_KEY and MESH_BASE_URL in .env — no hardcoded secrets.

DEGRADATION PATH (no silent failure): generate_narrative() wraps the entire
Mesh call (client init, request, retries) in a single try/except. If
MESH_API_KEY is missing, invalid, or Mesh is unreachable/rate-limited, the
app does NOT crash — it logs the failure via record_llm_call(success=False)
and returns a short, honest generic sentence ("Based on your recent
activity, here are a few courses we think you'll find useful.") so the
dashboard always renders something instead of a 500. This is a degradation,
not a silent swap — every fallback path is logged at WARNING/ERROR level.
"""
import os
import json
import time
import logging
from typing import Tuple, List, Dict
from openai import OpenAI

from services.metrics import record_llm_call

logger = logging.getLogger("smartreco.llm_client")

try:
    from langsmith import traceable
except ImportError:
    def traceable(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

MESH_BASE_URL = os.getenv("MESH_BASE_URL", "https://api.meshapi.ai/v1")
MESH_MODEL = os.getenv("MESH_MODEL", "openai/gpt-4o")

LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_RETRY_BASE_SECONDS = float(os.getenv("LLM_RETRY_BASE_SECONDS", "1.0"))
LLM_REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "60.0"))


def _require_mesh_api_key() -> str:
    api_key = os.getenv("MESH_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "MESH_API_KEY is required in .env — all LLM calls use Mesh API only."
        )
    return api_key


def get_client() -> Tuple[OpenAI, str]:
    """Returns (Mesh OpenAI-compatible client, model name)."""
    client = OpenAI(
        api_key=_require_mesh_api_key(),
        base_url=MESH_BASE_URL,
        timeout=LLM_REQUEST_TIMEOUT,
    )
    return client, MESH_MODEL


def validate_narrative_grounding(narrative: str, products: List[Dict]) -> bool:
    """Returns True if narrative appears grounded in candidate product titles."""
    if not products or not narrative:
        return True

    allowed_titles = [p.get("title", "").strip().lower() for p in products if p.get("title")]
    allowed_words = set()
    for title in allowed_titles:
        for word in title.split():
            if len(word) > 3:
                allowed_words.add(word)

    import re
    quoted_phrases = re.findall(r'"([^"]+)"', narrative) + re.findall(r"'([^']+)'", narrative)

    for phrase in quoted_phrases:
        phrase_clean = phrase.strip()
        if len(phrase_clean.split()) >= 2 and not any(t in phrase_clean.lower() for t in allowed_titles):
            matches = sum(1 for w in phrase_clean.lower().split() if w in allowed_words)
            if matches == 0:
                logger.warning("Grounding validation warning: phrase not in candidate products")
                return False

    return True


@traceable(name="generate_narrative", run_type="llm")
def generate_narrative(narrative_context: str, products: List[Dict]) -> str:
    """Generates a catalog-grounded recommendation narrative via Mesh API."""
    provider = "mesh"
    model = MESH_MODEL
    products_json = json.dumps(products, ensure_ascii=False, indent=2)

    system_prompt = (
        "You are SmartReco's recommendation assistant. You write short, "
        "persuasive, friendly course recommendations for a learner based "
        "ONLY on the product data given to you.\n\n"
        "STRICT GROUNDING & ACCURACY RULES:\n"
        "- Only mention the EXACT products listed in the candidate JSON below.\n"
        "- Absolute FORBIDDEN: Do NOT invent, hallucinate, or alter course titles, "
        "course names, prices, instructor names, ratings, or features not explicitly present in the JSON.\n"
        "- If the user's interest context mentions more than one distinct topic, "
        "clearly connect candidate products to those interests.\n"
        "- Keep it concise: 2-4 sentences total, persuasive but strictly honest.\n"
        "- Do not use markdown headers or bullet lists — write in warm, continuous prose.\n"
    )

    user_prompt = (
        f"User's recent interest context:\n{narrative_context}\n\n"
        f"Candidate products (JSON, grounded from catalog):\n{products_json}\n\n"
        "Write the recommendation narrative now."
    )

    start_time = time.time()

    try:
        client, model = get_client()
        logger.info("LLM call starting: provider=%s model=%s products_count=%d", provider, model, len(products))

        def _execute_completion(prompt_modifier: str = "") -> str:
            messages = [
                {"role": "system", "content": system_prompt + prompt_modifier},
                {"role": "user", "content": user_prompt},
            ]
            last_exc = None
            for attempt in range(LLM_MAX_RETRIES):
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        max_tokens=400,
                        temperature=0.7,
                    )
                    return response.choices[0].message.content.strip()
                except Exception as exc:
                    last_exc = exc
                    if attempt < LLM_MAX_RETRIES - 1:
                        delay = LLM_RETRY_BASE_SECONDS * (2 ** attempt)
                        logger.warning(
                            "Mesh LLM call failed (attempt %d/%d), retrying in %.1fs",
                            attempt + 1, LLM_MAX_RETRIES, delay,
                        )
                        time.sleep(delay)
                    else:
                        raise last_exc
            raise RuntimeError("Mesh LLM call failed after retries")

        result = _execute_completion()
        duration_ms = (time.time() - start_time) * 1000.0

        if not validate_narrative_grounding(result, products):
            logger.warning("LLM output failed grounding validation. Attempting strict retry.")
            strict_modifier = (
                "\n\nCRITICAL RETRY WARNING: You previously mentioned an unlisted course "
                "or hallucinated details. Strictly stick to the provided titles ONLY."
            )
            result = _execute_completion(prompt_modifier=strict_modifier)

            if not validate_narrative_grounding(result, products):
                logger.error("LLM retry failed grounding validation. Falling back to generic narrative.")
                return (
                    "Based on your recent activity, here are a few courses from our catalog "
                    "we think you'll find useful."
                )

        logger.info("LLM call completed successfully: provider=%s model=%s duration_ms=%.1f", provider, model, duration_ms)
        record_llm_call(provider, model, success=True, duration_ms=duration_ms)
        return result

    except Exception as exc:
        duration_ms = (time.time() - start_time) * 1000.0
        logger.error(
            "MESH FALLBACK ACTIVE: LLM narrative generation failed for provider=%s model=%s "
            "(likely missing/invalid MESH_API_KEY or Mesh unreachable): %s. "
            "Serving generic fallback narrative instead of crashing.",
            provider, model, exc, exc_info=True,
        )
        record_llm_call(provider, model, success=False, duration_ms=duration_ms)
        return (
            "Based on your recent activity, here are a few courses we think "
            "you'll find useful."
        )