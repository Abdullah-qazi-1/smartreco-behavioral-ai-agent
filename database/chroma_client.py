"""
Chroma vector DB client — persistent local storage.
Embeddings route exclusively through the Mesh API when MESH_API_KEY is configured.

DEGRADATION PATH (no silent failure): embed_text() raises when MESH_API_KEY is
missing/invalid or the Mesh embedding call fails after retries — it never
returns a fake/zero vector. Callers do not crash, though: every caller of
semantic search in services/product_service.py (semantic_search_products_scored)
wraps this in a try/except and falls back to services/keyword_fallback.py, a
plain SQL keyword search with no AI/embedding call involved. Callers of
upsert_product() in services/product_service.py (create_product,
update_product, delete_product) also wrap the call in try/except: on failure
the SQL row is still saved/updated, and the miss is recorded in
ChromaSyncLog(status="failed") for visibility instead of failing silently.
"""
import os
import logging
import chromadb
from openai import OpenAI

logger = logging.getLogger("smartreco.chroma_client")

MESH_BASE_URL = os.getenv("MESH_BASE_URL", "https://api.meshapi.ai/v1")
MESH_EMBED_MODEL = os.getenv("MESH_EMBED_MODEL", "openai/text-embedding-3-small")
EMBED_REQUEST_TIMEOUT = float(os.getenv("EMBED_REQUEST_TIMEOUT", "30.0"))
EMBED_MAX_RETRIES = int(os.getenv("EMBED_MAX_RETRIES", "3"))

CHROMA_PATH = os.getenv("CHROMA_PATH", "./chroma_db")
COLLECTION_NAME = "products"

_client = chromadb.PersistentClient(path=CHROMA_PATH)
_collection = _client.get_or_create_collection(name=COLLECTION_NAME)

_last_embedding_backend = "mesh"


def _require_mesh_api_key() -> str:
    api_key = os.getenv("MESH_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "MESH_API_KEY is required in .env — all embedding calls use Mesh API only."
        )
    return api_key


def get_embedding_backend() -> str:
    return _last_embedding_backend


def embed_text(text: str):
    global _last_embedding_backend
    api_key = _require_mesh_api_key()
    client = OpenAI(api_key=api_key, base_url=MESH_BASE_URL, timeout=EMBED_REQUEST_TIMEOUT)

    last_exc = None
    for attempt in range(EMBED_MAX_RETRIES):
        try:
            response = client.embeddings.create(
                model=MESH_EMBED_MODEL,
                input=text,
            )
            _last_embedding_backend = "mesh"
            return response.data[0].embedding
        except Exception as exc:
            last_exc = exc
            if attempt < EMBED_MAX_RETRIES - 1:
                import time
                delay = 1.0 * (2 ** attempt)
                logger.warning("Mesh embedding failed (attempt %d), retrying in %.1fs", attempt + 1, delay)
                time.sleep(delay)
    raise RuntimeError(f"Mesh embedding call failed after {EMBED_MAX_RETRIES} attempts") from last_exc


def upsert_product(
    product_id: int,
    title: str,
    description: str,
    category: str,
    level: str,
    price: float,
    skills: str = "",
    instructor_name: str = "",
    rating: float = None,
    num_ratings: int = None,
    enrolled_students: int = None,
    duration_hours: float = None,
):
    text = (
        f"{title}. "
        f"Category: {category}. "
        f"Level: {level}. "
        f"Skills: {skills}. "
        f"Instructor: {instructor_name}. "
        f"{description}"
    )

    embedding = embed_text(text)

    _collection.upsert(
        ids=[str(product_id)],
        embeddings=[embedding],
        documents=[text],
        metadatas=[{
            "product_id": product_id,
            "title": title,
            "category": category,
            "level": level or "",
            "price": price,
            "skills": skills or "",
            "instructor_name": instructor_name or "",
            "rating": rating if rating is not None else 0.0,
            "num_ratings": num_ratings if num_ratings is not None else 0,
            "enrolled_students": enrolled_students if enrolled_students is not None else 0,
            "duration_hours": duration_hours if duration_hours is not None else 0.0,
        }],
    )


def delete_product(product_id: int):
    _collection.delete(ids=[str(product_id)])


def semantic_search(query: str, top_k: int = 8, category: str = None, level: str = None):
    query_embedding = embed_text(query)

    query_kwargs = {
        "query_embeddings": [query_embedding],
        "n_results": top_k,
    }

    conditions = []
    if category:
        conditions.append({"category": category})
    if level:
        conditions.append({"level": level})

    if len(conditions) == 1:
        query_kwargs["where"] = conditions[0]
    elif len(conditions) > 1:
        query_kwargs["where"] = {"$and": conditions}

    results = _collection.query(**query_kwargs, include=["distances"])

    ids = results.get("ids", [[]])[0]
    return [int(i) for i in ids]


def _distance_to_similarity(distance: float) -> float:
    return max(0.0, min(1.0, 1.0 - float(distance)))


def semantic_search_with_scores(
    query: str,
    top_k: int = 8,
    category: str = None,
    level: str = None,
):
    query_embedding = embed_text(query)

    query_kwargs = {
        "query_embeddings": [query_embedding],
        "n_results": top_k,
        "include": ["distances"],
    }

    conditions = []
    if category:
        conditions.append({"category": category})
    if level:
        conditions.append({"level": level})

    if len(conditions) == 1:
        query_kwargs["where"] = conditions[0]
    elif len(conditions) > 1:
        query_kwargs["where"] = {"$and": conditions}

    results = _collection.query(**query_kwargs)

    ids = results.get("ids", [[]])[0]
    distances = results.get("distances", [[]])[0]
    scored = [
        (int(pid), _distance_to_similarity(dist))
        for pid, dist in zip(ids, distances)
    ]
    return scored


def get_collection_count() -> int:
    return _collection.count()