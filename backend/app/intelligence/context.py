"""Context intelligence service: retrieves excerpts, connects evidence, and builds EvidenceItems.

This module orchestrates document search, entity extraction, and relationship
detection. It is the bridge between raw document retrieval and the contracts
layer consumed by the orchestration engine.

Evidence provenance rules:
- Every EvidenceItem excerpt is a direct quote from the source document.
- Inferred relationships are labelled as such and never presented as facts.
- Missing dates, reasons, outcomes, or metrics are never invented.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from backend.app.documents.search import DocumentSearcher
from backend.app.documents.store import DocumentStore
from backend.app.intelligence.entities import (
    ExtractedEntities,
    InferredRelationship,
    extract_entities,
    find_shared_entities,
)
from backend.app.schemas.contracts import (
    EvidenceItem,
    EvidenceSourceType,
    ToolResult,
    utc_now,
)

logger = logging.getLogger(__name__)


class ContextIntelligence:
    """
    High-level context service used by tool implementations.

    Coordinates search, entity extraction, and evidence assembly.
    """

    def __init__(self, store: DocumentStore, searcher: DocumentSearcher) -> None:
        self._store = store
        self._searcher = searcher

    # ------------------------------------------------------------------
    # Core retrieval
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        task_id: UUID,
        top_k: int = 5,
        document_ids: list[str] | None = None,
    ) -> ToolResult:
        """
        Retrieve the most relevant document excerpts for a query.

        Returns a ToolResult with:
        - data: list of excerpt dicts with source metadata
        - evidence: EvidenceItem per excerpt, traceable to source document
        """
        results = self._searcher.search(query, top_k=top_k, document_ids=document_ids)

        excerpts: list[dict[str, Any]] = []
        evidence: list[EvidenceItem] = []

        for sr in results:
            chunk = sr.chunk
            rec = sr.document_record

            source_ref = f"page {chunk.page}" if chunk.page else (
                f"section '{chunk.section}'" if chunk.section else "full text"
            )
            claim = f"Document '{rec.original_filename}' ({source_ref}) contains relevant content for query: {query!r}"

            ev = EvidenceItem(
                task_id=task_id,
                source_type=EvidenceSourceType.DOCUMENT,
                source_name=rec.original_filename,
                file_path=rec.file_path,
                excerpt=chunk.text[:1000],  # cap excerpt length
                retrieved_at=utc_now(),
                supports_claim=claim,
            )
            evidence.append(ev)

            excerpts.append({
                "document_id": rec.document_id,
                "filename": rec.original_filename,
                "file_type": rec.file_type.value,
                "page": chunk.page,
                "section": chunk.section,
                "excerpt": chunk.text,
                "score": sr.score,
                "matched_terms": sr.matched_terms,
                "evidence_id": str(ev.evidence_id),
            })

        return ToolResult(
            success=True,
            data={"query": query, "results": excerpts, "total": len(excerpts)},
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # Relationship detection
    # ------------------------------------------------------------------

    def relate(
        self,
        query: str,
        task_id: UUID,
        top_k: int = 10,
        document_ids: list[str] | None = None,
    ) -> ToolResult:
        """
        Find shared entities and structural patterns across top search results.

        Returned relationships are explicitly labelled as inferred.
        Chronological order alone does not imply causation.
        """
        results = self._searcher.search(query, top_k=top_k, document_ids=document_ids)

        entity_sets: list[ExtractedEntities] = []
        base_evidence: list[EvidenceItem] = []

        for sr in results:
            chunk = sr.chunk
            rec = sr.document_record
            entities = extract_entities(
                chunk.text,
                source_name=rec.original_filename,
                page=chunk.page,
                section=chunk.section,
            )
            entity_sets.append(entities)

            source_ref = f"page {chunk.page}" if chunk.page else (
                f"section '{chunk.section}'" if chunk.section else "full text"
            )
            ev = EvidenceItem(
                task_id=task_id,
                source_type=EvidenceSourceType.DOCUMENT,
                source_name=rec.original_filename,
                file_path=rec.file_path,
                excerpt=chunk.text[:600],
                retrieved_at=utc_now(),
                supports_claim=(
                    f"Source '{rec.original_filename}' ({source_ref}) "
                    f"contributes entities to relationship analysis for query: {query!r}"
                ),
            )
            base_evidence.append(ev)

        relationships = find_shared_entities(entity_sets)

        rel_dicts: list[dict[str, Any]] = []
        for rel in relationships:
            rel_dicts.append({
                "relationship_type": rel.relationship_type,
                "entity": rel.entity,
                "sources": rel.sources,
                "note": rel.note,
                "is_inferred": rel.is_inferred,
            })

        return ToolResult(
            success=True,
            data={
                "query": query,
                "relationships": rel_dicts,
                "total_relationships": len(rel_dicts),
                "sources_analyzed": len(results),
                "disclaimer": (
                    "Relationships are structural patterns inferred from text. "
                    "Co-occurrence does not imply causation. "
                    "Verify claims against primary sources."
                ),
            },
            evidence=base_evidence,
        )

    # ------------------------------------------------------------------
    # Entity summary for a document
    # ------------------------------------------------------------------

    def document_entities(
        self,
        document_id: str,
        task_id: UUID,
    ) -> ToolResult:
        """Extract entities from all chunks of a single document."""
        chunks = self._store.get_chunks(document_id)
        record = self._store.get_record(document_id)
        if record is None:
            return ToolResult(
                success=False,
                error=f"Document '{document_id}' not found",
            )

        all_entities: list[dict[str, Any]] = []
        all_evidence: list[EvidenceItem] = []

        for chunk in chunks:
            ents = extract_entities(
                chunk.text,
                source_name=record.original_filename,
                page=chunk.page,
                section=chunk.section,
            )
            if ents.dates or ents.metrics or ents.organizations:
                entry: dict[str, Any] = {
                    "page": chunk.page,
                    "section": chunk.section,
                    "dates": ents.dates,
                    "metrics": ents.metrics,
                    "organizations": ents.organizations,
                    "excerpt": chunk.text[:300],
                }
                all_entities.append(entry)
                ev = EvidenceItem(
                    task_id=task_id,
                    source_type=EvidenceSourceType.DOCUMENT,
                    source_name=record.original_filename,
                    file_path=record.file_path,
                    excerpt=chunk.text[:600],
                    retrieved_at=utc_now(),
                    supports_claim=(
                        f"Entities extracted from '{record.original_filename}' "
                        f"({'page ' + str(chunk.page) if chunk.page else chunk.section or 'body'})"
                    ),
                )
                all_evidence.append(ev)

        return ToolResult(
            success=True,
            data={
                "document_id": document_id,
                "filename": record.original_filename,
                "entity_regions": all_entities,
                "total_regions": len(all_entities),
            },
            evidence=all_evidence,
        )
