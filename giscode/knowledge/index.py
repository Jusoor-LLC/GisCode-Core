import hashlib
import re
from typing import Optional


def compute_content_hash(content: str, namespace: str, source: str) -> str:
    payload = f"{namespace}:{source}:{content}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = re.findall(r"[a-z0-9_]+", text)
    return tokens


def compute_token_overlap_score(query: str, content: str) -> float:
    query_tokens = set(tokenize(query))
    content_tokens = set(tokenize(content))
    if not query_tokens:
        return 0.0
    overlap = query_tokens.intersection(content_tokens)
    return len(overlap) / len(query_tokens)
