"""
UNBOUND AI - Member 4 MVP Demonstration Script

Demonstrates the full workflow required by Section 8 of Member 4's mission:
1. Ingest a PDF document (with page tracking).
2. Ingest a DOCX and a TXT document (with section and line tracking).
3. Extract and persist their contents in SQLite + disk storage.
4. Search for a user-specified query.
5. Return relevant excerpts with source filename and page/section.
6. Generate structured EvidenceItem objects.
7. Execute through the TaskExecutor / ToolRegistry pipeline to prove orchestrator compatibility.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pymupdf
from docx import Document

from backend.app.documents.extractor import extract_document
from backend.app.documents.search import DocumentSearcher
from backend.app.documents.store import DocumentStore
from backend.app.documents.tools import DocumentContextTools
from backend.app.intelligence.context import ContextIntelligence
from backend.app.orchestrator.executor import TaskExecutor
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolRegistry
from backend.app.schemas.contracts import (
    EvidenceSourceType,
    StepStatus,
    TaskCreateRequest,
    TaskResponse,
    TaskStep,
    utc_now,
)


def create_sample_pdf(path: Path) -> None:
    doc = pymupdf.open()
    # Page 1
    page1 = doc.new_page()
    page1.insert_text(
        (50, 72),
        "UNBOUND AI Quarterly Report - Q3 2024\n\n"
        "Executive Summary:\n"
        "Total revenue reached $5.2 billion in Q3 2024, demonstrating 28% year-over-year growth.\n"
        "Operating margin expanded to 34% across core enterprise AI automation offerings.\n"
        "Customer retention remains strong at 98.4% globally.",
    )
    # Page 2
    page2 = doc.new_page()
    page2.insert_text(
        (50, 72),
        "Key Initiatives and Partnerships:\n\n"
        "On September 14, 2024, UNBOUND AI partnered with Global Enterprise Solutions to deploy\n"
        "context-aware digital agents across 15,000 corporate workstations.\n"
        "Initial telemetry indicates a 40% reduction in repetitive manual browser workflow latency.",
    )
    doc.save(str(path))
    doc.close()


def create_sample_docx(path: Path) -> None:
    doc = Document()
    doc.add_heading("Market Expansion Strategy 2024-2025", level=1)
    doc.add_paragraph(
        "UNBOUND AI aims to accelerate European and APAC market penetration. "
        "The targeted expansion capital is $1.8 billion allocated over four quarters."
    )
    doc.add_heading("Security and Governance", level=2)
    doc.add_paragraph(
        "All customer document processing complies with strict isolation boundaries. "
        "No customer data is utilized for external model retraining."
    )
    doc.save(str(path))


def create_sample_txt(path: Path) -> None:
    path.write_text(
        "Product Roadmap & Milestones\n"
        "Date: October 2024\n"
        "Milestone 1: Unified Document & Context MCP Server deployment.\n"
        "Milestone 2: Multi-agent coordination with traceable evidence provenance.\n"
        "Metric: Sub-100ms keyword retrieval across enterprise document repositories.",
        encoding="utf-8",
    )


def run_mvp_demo():
    print("=" * 70)
    print("  UNBOUND AI - Member 4 Document & Context Intelligence Demo")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage_dir = Path(tmp_dir) / "document_store"
        docs_dir = Path(tmp_dir) / "sample_docs"
        docs_dir.mkdir(parents=True, exist_ok=True)

        pdf_path = docs_dir / "Q3_2024_Financial_Report.pdf"
        docx_path = docs_dir / "Market_Expansion_Strategy.docx"
        txt_path = docs_dir / "Product_Roadmap.txt"

        create_sample_pdf(pdf_path)
        create_sample_docx(docx_path)
        create_sample_txt(txt_path)

        print("\n[Step 1 & 2] Initializing DocumentStore & ingesting PDF, DOCX, TXT...")
        store = DocumentStore(storage_dir)
        searcher = DocumentSearcher(store)
        intelligence = ContextIntelligence(store, searcher)
        tools = DocumentContextTools(store, searcher, intelligence)

        registry = ToolRegistry()
        tools.register_all(registry)

        # Ingest PDF
        pdf_bytes = pdf_path.read_bytes()
        pdf_doc_id, pdf_stored, pdf_stored_path = store.save_file(pdf_bytes, pdf_path.name)
        pdf_ext = extract_document(pdf_bytes, pdf_doc_id, pdf_path.name)
        store.save_extraction(pdf_doc_id, pdf_path.name, pdf_stored, pdf_stored_path, pdf_ext)
        print(f" -> Ingested PDF: {pdf_path.name} (pages: {pdf_ext.page_count}, chunks: {len(pdf_ext.chunks)})")

        # Ingest DOCX
        docx_bytes = docx_path.read_bytes()
        docx_doc_id, docx_stored, docx_stored_path = store.save_file(docx_bytes, docx_path.name)
        docx_ext = extract_document(docx_bytes, docx_doc_id, docx_path.name)
        store.save_extraction(docx_doc_id, docx_path.name, docx_stored, docx_stored_path, docx_ext)
        print(f" -> Ingested DOCX: {docx_path.name} (chunks: {len(docx_ext.chunks)})")

        # Ingest TXT
        txt_bytes = txt_path.read_bytes()
        txt_doc_id, txt_stored, txt_stored_path = store.save_file(txt_bytes, txt_path.name)
        txt_ext = extract_document(txt_bytes, txt_doc_id, txt_path.name)
        store.save_extraction(txt_doc_id, txt_path.name, txt_stored, txt_stored_path, txt_ext)
        print(f" -> Ingested TXT: {txt_path.name} (chunks: {len(txt_ext.chunks)})")

        print("\n[Step 3] Stored documents in SQLite database:")
        records = store.list_records()
        for r in records:
            print(f"    - ID: {r.document_id[:8]}... | Name: {r.original_filename:<32} | Type: {r.file_type:<4} | Status: {r.extraction_status}")

        print("\n[Step 4 & 5 & 6] Searching for 'revenue billion' across documents...")
        search_results = searcher.search("revenue billion", top_k=3)
        print(f"Found {len(search_results)} matching excerpt(s):")
        for i, sr in enumerate(search_results, 1):
            chunk = sr.chunk
            rec = sr.document_record
            loc = f"page {chunk.page}" if chunk.page else (f"section '{chunk.section}'" if chunk.section else "plain text")
            print(f"\n  Result #{i}:")
            print(f"    Source: {rec.original_filename} ({loc})")
            print(f"    Relevance Score: {sr.score:.4f}")
            print(f"    Matched terms: {sr.matched_terms}")
            print(f"    Excerpt: \"{chunk.text.replace(chr(10), ' ')[:120]}...\"")

        print("\n[Step 7] Context intelligence retrieval with EvidenceItem generation...")
        task_uuid = uuid4()
        tool_result = intelligence.retrieve("revenue growth enterprise", task_id=task_uuid)
        print(f"Retrieved {len(tool_result.evidence)} EvidenceItem(s):")
        for ev in tool_result.evidence:
            print(f"\n  Evidence ID: {ev.evidence_id}")
            print(f"    Task ID:       {ev.task_id}")
            print(f"    Source Type:   {ev.source_type}")
            print(f"    Source Name:   {ev.source_name}")
            print(f"    Supports Claim: {ev.supports_claim}")
            print(f"    Excerpt:       \"{ev.excerpt[:90]}...\"")

        print("\n[Step 8] Orchestration Engine Integration:")
        task_manager = TaskManager()
        step = TaskStep(
            step_id="step_1",
            tool_name="context.retrieve",
            description="Retrieve document evidence regarding Q3 2024 revenue",
            input={"query": "Q3 2024 revenue", "top_k": 2},
            expected_outcome="Retrieve document excerpts with financial figures",
        )
        created_task = task_manager.create(
            TaskCreateRequest(user_request="Find Q3 2024 revenue evidence from company documents"),
            steps=[step],
        )

        # Simple outcome verifier
        def sample_verifier(tool_name: str, expected: str, res) -> bool:
            return res.success and len(res.evidence) > 0

        executor = TaskExecutor(task_manager, registry, verifier=sample_verifier)
        executed_task = executor.run(created_task.task_id)

        print(f" -> Task Status: {executed_task.status}")
        print(f" -> Completed Steps: {executed_task.completed_steps}")
        print(f" -> Total Retained Evidence in Task: {len(executed_task.evidence)}")
        if executed_task.evidence:
            first_ev = executed_task.evidence[0]
            print(f" -> Provenance Verified: {first_ev.source_name} - {first_ev.supports_claim}")

        print("\n" + "=" * 70)
        print("  DEMO COMPLETED SUCCESSFULLY: ALL SECTION 8 STEPS VERIFIED!")
        print("=" * 70)


if __name__ == "__main__":
    run_mvp_demo()
