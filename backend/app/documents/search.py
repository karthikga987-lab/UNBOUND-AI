"""Keyword-ranked document search over chunked document text.

MVP retrieval: TF-IDF-inspired term frequency scoring with no external dependencies.
Preserves page/section provenance for every returned excerpt.
"""
from __future__ import annotations

import math
import re
import string
from collections import Counter

from backend.app.documents.models import DocumentChunk, DocumentRecord, SearchResult
from backend.app.documents.store import DocumentStore


# Common English stop words to filter during scoring
_STOP_WORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "shall", "can", "it", "its",
        "this", "that", "these", "those", "i", "we", "you", "he", "she",
        "they", "not", "no", "as", "if", "so", "than", "then", "also",
        "into", "over", "after", "before", "about", "between",
    }
)


def _tokenize(text: str) -> list[str]:
    """Lower-case, strip punctuation, remove stop words."""
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    tokens = text.split()
    return [t for t in tokens if t and t not in _STOP_WORDS and len(t) > 1]


def _query_terms(query: str) -> list[str]:
    """Extract meaningful query tokens."""
    return list(dict.fromkeys(_tokenize(query)))  # unique, order-preserved


def _score_chunk(chunk_tokens: list[str], query_terms: list[str], idf: dict[str, float]) -> tuple[float, list[str]]:
    """Score a chunk against query terms using TF-IDF."""
    if not chunk_tokens or not query_terms:
        return 0.0, []

    term_freq = Counter(chunk_tokens)
    total_tokens = len(chunk_tokens)
    matched: list[str] = []
    score = 0.0

    for term in query_terms:
        tf = term_freq.get(term, 0)
        if tf > 0:
            matched.append(term)
            # Normalized TF × IDF
            score += (tf / total_tokens) * idf.get(term, 1.0)

    return score, matched


class DocumentSearcher:
    """
    Keyword-based retrieval engine over chunked document text.

    Uses an in-memory inverted index built from all stored chunks.
    For the MVP scale this is sufficient; a vector index can be added later.
    """

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    def search(
        self,
        query: str,
        top_k: int = 5,
        document_ids: list[str] | None = None,
    ) -> list[SearchResult]:
        """
        Search indexed chunks for the query string.

        Args:
            query: Free-text search query.
            top_k: Maximum number of results to return.
            document_ids: If provided, restrict search to these document IDs.

        Returns:
            Ranked list of SearchResult objects, most relevant first.
        """
        query_terms = _query_terms(query)
        if not query_terms:
            return []

        all_chunks = self._store.get_all_chunks()
        if document_ids:
            id_set = set(document_ids)
            all_chunks = [c for c in all_chunks if c.document_id in id_set]

        if not all_chunks:
            return []

        # Build IDF over the corpus of chunks
        num_chunks = len(all_chunks)
        doc_freq: dict[str, int] = {}
        tokenized: list[list[str]] = []
        section_tokenized: list[list[str]] = []
        for chunk in all_chunks:
            tokens = _tokenize(chunk.text)
            tokenized.append(tokens)
            section_tokens = _tokenize(chunk.section or "")
            section_tokenized.append(section_tokens)
            for term in set(tokens):
                doc_freq[term] = doc_freq.get(term, 0) + 1

        idf = {
            term: math.log(num_chunks / (freq + 1)) + 1.0
            for term, freq in doc_freq.items()
        }

        # Score each chunk
        scored: list[tuple[float, list[str], DocumentChunk]] = []
        total_query_weight = sum(idf.get(term, 1.0) for term in query_terms)
        for chunk, tokens, section_tokens in zip(all_chunks, tokenized, section_tokenized):
            score, matched = _score_chunk(tokens, query_terms, idf)
            section_matches = [term for term in query_terms if term in section_tokens]
            if section_matches:
                # Headings are retrieval metadata, not source text. A heading
                # match gets a ranking boost so heading-only queries find the
                # right section even when its body lacks the heading word.
                # Evidence validation must still quote chunk.text exactly.
                section_weight = sum(idf.get(term, 1.0) for term in section_matches)
                score += total_query_weight + section_weight
                matched = list(dict.fromkeys([*matched, *section_matches]))
            if score > 0:
                scored.append((score, matched, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        results: list[SearchResult] = []
        for score, matched, chunk in top:
            record = self._store.get_record(chunk.document_id)
            if record is None:
                continue
            results.append(
                SearchResult(
                    chunk=chunk,
                    document_record=record,
                    score=round(score, 4),
                    matched_terms=matched,
                )
            )
        return results
