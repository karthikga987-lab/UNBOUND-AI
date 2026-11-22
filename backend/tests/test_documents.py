"""
Comprehensive tests for Member 4 – MCP, Documents & Context Intelligence.

Tests:
- PDF extraction
- DOCX extraction
- TXT extraction
- Unsupported file handling
- Corrupt file handling
- Document persistence (SQLite + file storage)
- Chunk retrieval
- Document search (keyword ranking)
- Evidence provenance (EvidenceItem fields)
- Context retrieval
- Context relationship detection
- MCP tool invocations (via DocumentMCPServer tool methods)
- Tool registration with ToolRegistry
"""
from __future__ import annotations

import io
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app.documents.extractor import extract_document
from backend.app.documents.models import ExtractionStatus, FileType
from backend.app.documents.search import DocumentSearcher
from backend.app.documents.store import DocumentStore
from backend.app.documents.tools import (
    CONTEXT_RELATE_DEFINITION,
    CONTEXT_RETRIEVE_DEFINITION,
    DOCUMENT_SEARCH_DEFINITION,
    DOCUMENT_UPLOAD_DEFINITION,
    ContextRelateInput,
    ContextRetrieveInput,
    DocumentContextTools,
    DocumentSearchInput,
    DocumentUploadInput,
)
from backend.app.intelligence.context import ContextIntelligence
from backend.app.intelligence.entities import extract_entities, find_shared_entities
from backend.app.mcp.server import DocumentMCPServer
from backend.app.orchestrator.tool_registry import ToolRegistry
from backend.app.schemas.contracts import (
    EvidenceItem,
    EvidenceSourceType,
    ToolCall,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path):
    return DocumentStore(storage_dir=tmp_path / "docs")


@pytest.fixture
def searcher(tmp_store):
    return DocumentSearcher(tmp_store)


@pytest.fixture
def intelligence(tmp_store, searcher):
    return ContextIntelligence(tmp_store, searcher)


@pytest.fixture
def mcp_server(tmp_store, searcher, intelligence):
    return DocumentMCPServer(tmp_store, searcher, intelligence)


@pytest.fixture
def task_id():
    return uuid.uuid4()


@pytest.fixture
def sample_txt_bytes():
    return b"""UNBOUND AI Company Report 2024

Revenue reached $5.2 billion in Q3 2024, up from $4.1 billion in Q3 2023.
The AI platform serves enterprise customers across 42 countries.
Key product: UNBOUND Intelligence Suite launched on January 15, 2024.
CEO: Jane Smith announced the expansion into European markets in February 2024.
Metric: 98.7% uptime across all regions.
"""


@pytest.fixture
def sample_txt_path(tmp_path, sample_txt_bytes):
    p = tmp_path / "report.txt"
    p.write_bytes(sample_txt_bytes)
    return p


@pytest.fixture
def ingest_txt(tmp_store, sample_txt_bytes, sample_txt_path):
    """Ingest a TXT file into the store and return document_id."""
    doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
    result = extract_document(sample_txt_bytes, doc_id, "report.txt")
    tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)
    return doc_id, result


# ---------------------------------------------------------------------------
# 1. TXT extraction
# ---------------------------------------------------------------------------


class TestTxtExtraction:
    def test_basic_extraction(self, sample_txt_bytes):
        result = extract_document(sample_txt_bytes, "doc1", "report.txt")
        assert result.status == ExtractionStatus.SUCCESS
        assert result.file_type == FileType.TXT
        assert "UNBOUND AI" in result.full_text
        assert result.chunks, "Should produce at least one chunk"

    def test_chunks_contain_text(self, sample_txt_bytes):
        result = extract_document(sample_txt_bytes, "doc1", "report.txt")
        for chunk in result.chunks:
            assert chunk.text.strip()
            assert chunk.document_id == "doc1"

    def test_empty_file(self):
        result = extract_document(b"", "doc2", "empty.txt")
        assert result.status == ExtractionStatus.EMPTY

    def test_whitespace_only(self):
        result = extract_document(b"   \n\t\n   ", "doc3", "blank.txt")
        assert result.status == ExtractionStatus.EMPTY

    def test_latin1_fallback(self):
        # Bytes that are invalid UTF-8 but valid Latin-1
        content = b"Caf\xe9 report: revenue \xa35.2m"
        result = extract_document(content, "doc4", "latin.txt")
        assert result.status == ExtractionStatus.SUCCESS
        assert "Caf" in result.full_text


# ---------------------------------------------------------------------------
# 2. PDF extraction
# ---------------------------------------------------------------------------


class TestPdfExtraction:
    def _make_minimal_pdf(self) -> bytes:
        """Create a minimal valid PDF in memory using PyMuPDF."""
        try:
            import fitz
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 100), "UNBOUND AI PDF Test Page 1\nRevenue $3.7 billion Q2 2024")
            page.insert_text((72, 200), "Company: Acme Corp founded in 2018")
            pdf_bytes = doc.tobytes()
            doc.close()
            return pdf_bytes
        except ImportError:
            pytest.skip("PyMuPDF not installed")

    def test_pdf_extraction_success(self):
        pdf_bytes = self._make_minimal_pdf()
        result = extract_document(pdf_bytes, "pdf1", "test.pdf")
        assert result.status == ExtractionStatus.SUCCESS
        assert result.file_type == FileType.PDF
        assert "UNBOUND AI" in result.full_text
        assert result.page_count is not None and result.page_count >= 1

    def test_pdf_chunks_have_page_numbers(self):
        pdf_bytes = self._make_minimal_pdf()
        result = extract_document(pdf_bytes, "pdf1", "test.pdf")
        assert result.chunks
        for chunk in result.chunks:
            assert chunk.page is not None, "PDF chunks must carry page numbers"
            assert chunk.page >= 1

    def test_corrupt_pdf(self):
        result = extract_document(b"NOT A PDF %PDF-GARBAGE", "pdf2", "corrupt.pdf")
        assert result.status == ExtractionStatus.FAILED
        assert result.error is not None

    def test_empty_pdf(self):
        result = extract_document(b"", "pdf3", "empty.pdf")
        assert result.status == ExtractionStatus.EMPTY


# ---------------------------------------------------------------------------
# 3. DOCX extraction
# ---------------------------------------------------------------------------


class TestDocxExtraction:
    def _make_minimal_docx(self) -> bytes:
        """Create a minimal DOCX in memory using python-docx."""
        try:
            from docx import Document
            doc = Document()
            doc.add_heading("Executive Summary", level=1)
            doc.add_paragraph("UNBOUND AI achieved revenue of $2.4 billion in 2024.")
            doc.add_heading("Market Analysis", level=2)
            doc.add_paragraph("European market grew by 34% year over year.")
            buf = io.BytesIO()
            doc.save(buf)
            return buf.getvalue()
        except ImportError:
            pytest.skip("python-docx not installed")

    def test_docx_extraction_success(self):
        docx_bytes = self._make_minimal_docx()
        result = extract_document(docx_bytes, "docx1", "report.docx")
        assert result.status == ExtractionStatus.SUCCESS
        assert result.file_type == FileType.DOCX
        assert "UNBOUND AI" in result.full_text

    def test_docx_chunks_have_sections(self):
        docx_bytes = self._make_minimal_docx()
        result = extract_document(docx_bytes, "docx1", "report.docx")
        sections = {c.section for c in result.chunks if c.section}
        assert sections, "DOCX chunks under headings should carry section names"

    def test_corrupt_docx(self):
        result = extract_document(b"NOT DOCX GARBAGE", "docx2", "corrupt.docx")
        assert result.status == ExtractionStatus.FAILED

    def test_empty_docx(self):
        result = extract_document(b"", "docx3", "empty.docx")
        assert result.status == ExtractionStatus.EMPTY


# ---------------------------------------------------------------------------
# 4. Unsupported file types
# ---------------------------------------------------------------------------


class TestUnsupportedFileTypes:
    @pytest.mark.parametrize("filename", [
        "script.py", "data.csv", "image.png", "archive.zip", "page.html",
        "data.json", "binary.exe", "noextension",
    ])
    def test_unsupported_extension(self, filename):
        result = extract_document(b"some content", "docX", filename)
        assert result.status == ExtractionStatus.FAILED
        assert "Unsupported" in (result.error or "")

    def test_txt_masquerading_as_pdf(self):
        """A .pdf file containing non-PDF bytes should fail gracefully."""
        result = extract_document(b"This is text not a pdf", "docX", "fake.pdf")
        assert result.status == ExtractionStatus.FAILED


# ---------------------------------------------------------------------------
# 5. File size limit
# ---------------------------------------------------------------------------


class TestFileSizeLimits:
    def test_oversized_file_rejected(self):
        # 21 MB
        large = b"x" * (21 * 1024 * 1024)
        result = extract_document(large, "big1", "big.txt")
        assert result.status == ExtractionStatus.FAILED
        assert "too large" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# 6. Document persistence
# ---------------------------------------------------------------------------


class TestDocumentPersistence:
    def test_save_and_retrieve_record(self, tmp_store, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        record = tmp_store.get_record(doc_id)
        assert record is not None
        assert record.document_id == doc_id
        assert record.original_filename == "report.txt"
        assert record.extraction_status == ExtractionStatus.SUCCESS

    def test_text_file_stored_separately(self, tmp_store, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        record = tmp_store.get_record(doc_id)
        assert record.text_file_path is not None
        assert Path(record.text_file_path).exists()
        text = tmp_store.get_extracted_text(doc_id)
        assert "UNBOUND AI" in text

    def test_list_records(self, tmp_store, sample_txt_bytes):
        for i in range(3):
            doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, f"doc{i}.txt")
            result = extract_document(sample_txt_bytes, doc_id, f"doc{i}.txt")
            tmp_store.save_extraction(doc_id, f"doc{i}.txt", stored, path, result)

        records = tmp_store.list_records()
        assert len(records) == 3

    def test_safe_stored_filename(self, tmp_store, sample_txt_bytes):
        """Stored filename must not contain path traversal characters."""
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "../../etc/passwd.txt")
        assert ".." not in stored
        assert "/" not in stored

    def test_path_traversal_in_original_filename(self, tmp_store, sample_txt_bytes):
        """Path traversal in original_filename should be neutralized."""
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "../../../etc/passwd.txt")
        dest = Path(path)
        # The stored file must be inside files_dir
        assert str(dest).startswith(str(tmp_store.files_dir))


# ---------------------------------------------------------------------------
# 7. Chunk retrieval
# ---------------------------------------------------------------------------


class TestChunkRetrieval:
    def test_chunks_retrievable(self, tmp_store, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        chunks = tmp_store.get_chunks(doc_id)
        assert chunks
        for chunk in chunks:
            assert chunk.document_id == doc_id
            assert chunk.text.strip()

    def test_all_chunks_query(self, tmp_store, sample_txt_bytes):
        for i in range(2):
            doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, f"doc{i}.txt")
            result = extract_document(sample_txt_bytes, doc_id, f"doc{i}.txt")
            tmp_store.save_extraction(doc_id, f"doc{i}.txt", stored, path, result)

        all_chunks = tmp_store.get_all_chunks()
        assert len(all_chunks) >= 2


# ---------------------------------------------------------------------------
# 8. Document search (keyword ranking)
# ---------------------------------------------------------------------------


class TestDocumentSearch:
    def test_search_returns_results(self, tmp_store, searcher, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        results = searcher.search("revenue billion")
        assert results, "Should find results for 'revenue billion'"
        assert all(r.score > 0 for r in results)

    def test_search_respects_top_k(self, tmp_store, searcher, sample_txt_bytes):
        for i in range(5):
            doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, f"doc{i}.txt")
            result = extract_document(sample_txt_bytes, doc_id, f"doc{i}.txt")
            tmp_store.save_extraction(doc_id, f"doc{i}.txt", stored, path, result)

        results = searcher.search("revenue", top_k=2)
        assert len(results) <= 2

    def test_search_no_results_empty_corpus(self, tmp_store, searcher):
        results = searcher.search("quantum entanglement physics")
        assert results == []

    def test_search_includes_matched_terms(self, tmp_store, searcher, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        results = searcher.search("revenue platform")
        for r in results:
            assert isinstance(r.matched_terms, list)

    def test_search_by_document_id_filter(self, tmp_store, searcher, sample_txt_bytes):
        doc_id1, stored1, path1 = tmp_store.save_file(sample_txt_bytes, "doc1.txt")
        result1 = extract_document(sample_txt_bytes, doc_id1, "doc1.txt")
        tmp_store.save_extraction(doc_id1, "doc1.txt", stored1, path1, result1)

        other_content = b"Completely different document about astronomy and telescopes."
        doc_id2, stored2, path2 = tmp_store.save_file(other_content, "doc2.txt")
        result2 = extract_document(other_content, doc_id2, "doc2.txt")
        tmp_store.save_extraction(doc_id2, "doc2.txt", stored2, path2, result2)

        results = searcher.search("revenue", document_ids=[doc_id1])
        assert all(r.document_record.document_id == doc_id1 for r in results)

    def test_search_result_carries_provenance(self, tmp_store, searcher, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        results = searcher.search("UNBOUND AI platform")
        assert results
        sr = results[0]
        assert sr.document_record.original_filename == "report.txt"
        assert sr.chunk.document_id == doc_id


# ---------------------------------------------------------------------------
# 9. Evidence provenance
# ---------------------------------------------------------------------------


class TestEvidenceProvenance:
    def test_evidence_item_fields(self, tmp_store, intelligence, task_id, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        tool_result = intelligence.retrieve("revenue", task_id=task_id)
        assert tool_result.success
        for ev in tool_result.evidence:
            assert isinstance(ev, EvidenceItem)
            assert ev.task_id == task_id
            assert ev.source_type == EvidenceSourceType.DOCUMENT
            assert ev.source_name  # non-empty filename
            assert ev.excerpt  # non-empty excerpt
            assert ev.supports_claim  # non-empty claim
            assert ev.retrieved_at is not None
            assert ev.file_path is not None

    def test_evidence_excerpt_matches_chunk_text(self, tmp_store, intelligence, task_id, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        tool_result = intelligence.retrieve("revenue", task_id=task_id)
        for ev in tool_result.evidence:
            # Excerpt must be non-empty and taken from actual document text
            assert ev.excerpt and len(ev.excerpt) > 0
            assert len(ev.excerpt) <= 1000  # capped per spec

    def test_no_invented_facts(self, tmp_store, intelligence, task_id):
        """When no documents match, evidence list must be empty – no invented facts."""
        tool_result = intelligence.retrieve("quantum physics entropy", task_id=task_id)
        assert tool_result.success
        assert tool_result.evidence == []


# ---------------------------------------------------------------------------
# 10. Context retrieval
# ---------------------------------------------------------------------------


class TestContextRetrieval:
    def test_retrieve_returns_tool_result(self, tmp_store, intelligence, task_id, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        tool_result = intelligence.retrieve("revenue enterprise customers", task_id=task_id)
        assert isinstance(tool_result, ToolResult)
        assert tool_result.success
        assert "results" in tool_result.data
        assert tool_result.data["total"] >= 1

    def test_retrieve_data_includes_filename(self, tmp_store, intelligence, task_id, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        tool_result = intelligence.retrieve("revenue", task_id=task_id)
        for item in tool_result.data["results"]:
            assert item["filename"] == "report.txt"
            assert "page" in item
            assert "excerpt" in item


# ---------------------------------------------------------------------------
# 11. Context relationship detection
# ---------------------------------------------------------------------------


class TestContextRelate:
    def test_relate_returns_inferred_label(self, tmp_store, intelligence, task_id, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        # Second document sharing some entities
        other = b"UNBOUND AI Company expanded to Asia in 2024. Revenue grew significantly."
        doc_id2, stored2, path2 = tmp_store.save_file(other, "news.txt")
        result2 = extract_document(other, doc_id2, "news.txt")
        tmp_store.save_extraction(doc_id2, "news.txt", stored2, path2, result2)

        tool_result = intelligence.relate("UNBOUND AI revenue 2024", task_id=task_id)
        assert tool_result.success
        for rel in tool_result.data.get("relationships", []):
            assert rel["is_inferred"] is True, "All relationships must be labelled as inferred"

    def test_relate_disclaimer_present(self, tmp_store, intelligence, task_id, sample_txt_bytes):
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        tool_result = intelligence.relate("revenue", task_id=task_id)
        assert "disclaimer" in tool_result.data
        assert "inferred" in tool_result.data["disclaimer"].lower()

    def test_relate_no_causation_claim(self, tmp_store, intelligence, task_id, sample_txt_bytes):
        """Shared dates must not claim causation."""
        doc_id, stored, path = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result = extract_document(sample_txt_bytes, doc_id, "report.txt")
        tmp_store.save_extraction(doc_id, "report.txt", stored, path, result)

        tool_result = intelligence.relate("2024 January revenue", task_id=task_id)
        for rel in tool_result.data.get("relationships", []):
            assert "causation" not in rel.get("note", "").lower() or \
                   "not imply causation" in rel.get("note", "")


# ---------------------------------------------------------------------------
# 12. Entity extraction
# ---------------------------------------------------------------------------


class TestEntityExtraction:
    def test_extract_dates(self):
        text = "Report dated January 15, 2024. Q3 2023 results released."
        ents = extract_entities(text, source_name="test.txt")
        assert ents.dates

    def test_extract_metrics(self):
        text = "Revenue: $5.2 billion. Growth: 34%. Headcount: 12K employees."
        ents = extract_entities(text, source_name="test.txt")
        assert ents.metrics

    def test_extract_organizations(self):
        text = "Acme Corporation and Global Tech Industries both reported gains."
        ents = extract_entities(text, source_name="test.txt")
        assert ents.organizations

    def test_shared_entity_detection(self):
        from backend.app.intelligence.entities import ExtractedEntities
        e1 = ExtractedEntities(
            dates=["2024"],
            organizations=["acme corporation"],
            source_name="doc1.txt",
        )
        e2 = ExtractedEntities(
            dates=["2024"],
            organizations=["acme corporation"],
            source_name="doc2.txt",
        )
        relationships = find_shared_entities([e1, e2])
        types = {r.relationship_type for r in relationships}
        assert "shared_date" in types or "shared_organization" in types
        assert all(r.is_inferred for r in relationships)


# ---------------------------------------------------------------------------
# 13. MCP tool invocations
# ---------------------------------------------------------------------------


class TestMCPToolInvocations:
    def test_mcp_document_upload(self, mcp_server, sample_txt_path):
        result = mcp_server.tool_document_upload(
            file_path=str(sample_txt_path),
            original_filename="report.txt",
        )
        assert result["success"] is True
        assert "document_id" in result
        assert result["extraction_status"] == "success"

    def test_mcp_document_upload_missing_file(self, mcp_server):
        result = mcp_server.tool_document_upload(
            file_path="/nonexistent/path/file.txt",
            original_filename="file.txt",
        )
        assert result["success"] is False
        assert "error" in result

    def test_mcp_document_upload_unsupported_type(self, mcp_server, tmp_path):
        p = tmp_path / "data.csv"
        p.write_bytes(b"a,b,c\n1,2,3")
        result = mcp_server.tool_document_upload(
            file_path=str(p),
            original_filename="data.csv",
        )
        assert result["success"] is False

    def test_mcp_document_search(self, mcp_server, sample_txt_path):
        mcp_server.tool_document_upload(str(sample_txt_path), "report.txt")
        result = mcp_server.tool_document_search(query="revenue billion")
        assert "results" in result
        assert isinstance(result["results"], list)

    def test_mcp_context_retrieve(self, mcp_server, sample_txt_path):
        mcp_server.tool_document_upload(str(sample_txt_path), "report.txt")
        result = mcp_server.tool_context_retrieve(query="revenue")
        assert result["success"] is True
        assert "data" in result
        assert isinstance(result["evidence"], list)

    def test_mcp_context_relate(self, mcp_server, sample_txt_path):
        mcp_server.tool_document_upload(str(sample_txt_path), "report.txt")
        result = mcp_server.tool_context_relate(query="revenue 2024")
        assert result["success"] is True
        assert "disclaimer" in result["data"]

    def test_mcp_evidence_list_all(self, mcp_server, sample_txt_path):
        mcp_server.tool_document_upload(str(sample_txt_path), "report.txt")
        result = mcp_server.tool_evidence_list()
        assert result["success"] is True
        assert result["total"] >= 1

    def test_mcp_evidence_list_specific(self, mcp_server, sample_txt_path):
        upload = mcp_server.tool_document_upload(str(sample_txt_path), "report.txt")
        doc_id = upload["document_id"]
        result = mcp_server.tool_evidence_list(document_id=doc_id)
        assert result["success"] is True
        assert result["document"]["document_id"] == doc_id

    def test_mcp_evidence_list_unknown(self, mcp_server):
        result = mcp_server.tool_evidence_list(document_id="nonexistent-id")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# 14. Tool registry integration
# ---------------------------------------------------------------------------


class TestToolRegistryIntegration:
    def test_all_four_tools_register(self, tmp_store, searcher, intelligence):
        registry = ToolRegistry()
        tools = DocumentContextTools(tmp_store, searcher, intelligence)
        tools.register_all(registry)
        names = {t.tool_name for t in registry.list_tools()}
        assert "document.upload" in names
        assert "document.search" in names
        assert "context.retrieve" in names
        assert "context.relate" in names

    def test_definitions_match_input_schemas(self):
        """ToolDefinition.input_schema must exactly match the Pydantic model schema."""
        from backend.app.documents.tools import (
            DocumentUploadInput, DocumentSearchInput,
            ContextRetrieveInput, ContextRelateInput,
        )
        pairs = [
            (DOCUMENT_UPLOAD_DEFINITION, DocumentUploadInput),
            (DOCUMENT_SEARCH_DEFINITION, DocumentSearchInput),
            (CONTEXT_RETRIEVE_DEFINITION, ContextRetrieveInput),
            (CONTEXT_RELATE_DEFINITION, ContextRelateInput),
        ]
        for defn, model in pairs:
            assert defn.input_schema == model.model_json_schema(), \
                f"Schema mismatch for {defn.tool_name}"

    def test_execute_via_registry(self, tmp_store, searcher, intelligence, task_id, sample_txt_path):
        registry = ToolRegistry()
        tools = DocumentContextTools(tmp_store, searcher, intelligence)
        tools.register_all(registry)

        call = ToolCall(
            task_id=task_id,
            step_id="step_1",
            tool_name="document.search",
            arguments={"query": "revenue"},
        )
        result = registry.execute(call, before_execute=lambda: True)
        assert result.success


# ---------------------------------------------------------------------------
# 15. Full MVP workflow
# ---------------------------------------------------------------------------


class TestMVPWorkflow:
    """End-to-end scenario: upload → search → evidence retrieval."""

    def test_full_workflow(self, tmp_store, searcher, intelligence, task_id, sample_txt_bytes, tmp_path):
        # 1. Upload a TXT
        txt_path = tmp_path / "report.txt"
        txt_path.write_bytes(sample_txt_bytes)
        doc_id1, stored1, path1 = tmp_store.save_file(sample_txt_bytes, "report.txt")
        result1 = extract_document(sample_txt_bytes, doc_id1, "report.txt")
        tmp_store.save_extraction(doc_id1, "report.txt", stored1, path1, result1)

        # 2. Upload a second document (simulate DOCX via TXT for portability)
        second = b"UNBOUND AI quarterly earnings 2024: $1.3 billion net income. Product: AI Suite."
        doc_id2, stored2, path2 = tmp_store.save_file(second, "earnings.txt")
        result2 = extract_document(second, doc_id2, "earnings.txt")
        tmp_store.save_extraction(doc_id2, "earnings.txt", stored2, path2, result2)

        # 3. Search for a topic
        search_results = searcher.search("UNBOUND AI revenue billion", top_k=5)
        assert search_results, "Search should return results"

        # 4. Verify source filename and page/section in results
        for sr in search_results:
            assert sr.document_record.original_filename in {"report.txt", "earnings.txt"}
            # TXT files don't have pages; that's acceptable
            assert isinstance(sr.score, float) and sr.score > 0

        # 5. Retrieve structured EvidenceItems
        tool_result = intelligence.retrieve("UNBOUND AI revenue", task_id=task_id)
        assert tool_result.success
        assert tool_result.evidence

        for ev in tool_result.evidence:
            assert ev.source_type == EvidenceSourceType.DOCUMENT
            assert ev.source_name in {"report.txt", "earnings.txt"}
            assert ev.excerpt
            assert ev.supports_claim
            assert ev.task_id == task_id
