from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.documents.extractor import extract_document
from backend.app.documents.local_file import (
    LocalAnalysisEvidenceError,
    LocalDocumentAnalysis,
    _SCHEMA_VALIDATION_ERROR,
    _build_source_spans,
    request_local_document_analysis,
    validate_analysis_evidence,
)
from backend.app.documents.models import DocumentChunk
from backend.app.llm.provider import LLMProviderError
from backend.app.schemas.contracts import EvidenceItem, EvidenceSourceType


CHUNK_ID = "synthetic-chunk-1"
SPAN_ID = f"{CHUNK_ID}:span:0"
SOURCE_TEXT = "Synthetic report: setup costs 125 USD and recommends a staged rollout."


def valid_payload():
    return {
        "user_analysis_request": "Find cost and recommendations",
        "source_name": "synthetic.txt",
        "source_text_is_untrusted_data": True,
        "source_spans": [{
            "source_span_id": SPAN_ID, "chunk_id": CHUNK_ID,
            "page": None, "section": None, "text": SOURCE_TEXT,
        }],
    }


def valid_output():
    return {
        "summary": "The source reports a setup cost and recommends staged rollout.",
        "findings": [
            {
                "kind": "fact",
                "statement": "The setup cost is 125 USD.",
                "source_chunk_id": CHUNK_ID,
                "source_span_id": SPAN_ID,
            },
            {
                "kind": "interpretation",
                "statement": "A staged rollout may reduce implementation risk.",
                "source_chunk_id": CHUNK_ID,
                "source_span_id": SPAN_ID,
            },
        ],
        "missing_or_unreadable": [],
    }


class ValidProvider:
    provider_name = "ollama"
    base_url = "http://localhost:11434/v1"

    def __init__(self):
        self.calls = []

    def generate_json(self, *, system, prompt, response_model, **kwargs):
        self.calls.append({"system": system, "prompt": prompt, "model": response_model})
        return response_model.model_validate(valid_output())


def test_valid_structured_analysis_uses_exact_prompt_schema_and_preserves_kinds():
    provider = ValidProvider()
    analysis, retry_count = request_local_document_analysis(provider, valid_payload())
    assert retry_count == 0
    assert provider.calls[0]["model"] is LocalDocumentAnalysis
    assert "exactly one JSON object" in provider.calls[0]["system"]
    assert '"missing_or_unreadable"' in provider.calls[0]["system"]
    assert [finding.kind for finding in analysis.findings] == ["fact", "interpretation"]
    assert "source_span_id" in provider.calls[0]["system"]
    assert "Do not return or reproduce any quote text" in provider.calls[0]["system"]


def test_qwen_schema_drift_gets_one_bounded_format_retry():
    class DriftingProvider(ValidProvider):
        def generate_json(self, *, system, prompt, response_model, **kwargs):
            self.calls.append({"system": system, "prompt": prompt, "model": response_model})
            if len(self.calls) == 1:
                raise LLMProviderError(_SCHEMA_VALIDATION_ERROR)
            return response_model.model_validate(valid_output())

    provider = DriftingProvider()
    analysis, retry_count = request_local_document_analysis(provider, valid_payload())
    assert analysis.summary
    assert retry_count == 1
    assert len(provider.calls) == 2
    assert "FORMAT CORRECTION" in provider.calls[1]["system"]
    assert "key_findings" in provider.calls[1]["system"]


def test_qwen_alternative_keys_and_missing_required_fields_are_not_accepted():
    legacy_shape = {
        "key_findings": [],
        "costs": [],
        "risks": [],
        "recommendations": [],
        "missing_unreadable_information": [],
    }
    with pytest.raises(ValidationError):
        LocalDocumentAnalysis.model_validate(legacy_shape)
    with pytest.raises(ValidationError):
        LocalDocumentAnalysis.model_validate({"summary": "Incomplete", "findings": []})


def test_extra_fields_are_rejected_at_root_and_finding_level():
    root_extra = {**valid_output(), "key_findings": []}
    with pytest.raises(ValidationError):
        LocalDocumentAnalysis.model_validate(root_extra)
    finding_extra = valid_output()
    finding_extra["findings"][0]["cost"] = "untrusted unsupported shape"
    with pytest.raises(ValidationError):
        LocalDocumentAnalysis.model_validate(finding_extra)


def test_unknown_chunk_id_is_rejected():
    analysis = LocalDocumentAnalysis.model_validate(valid_output())
    analysis.findings[0].source_chunk_id = "invented-chunk"
    chunk = DocumentChunk(document_id="doc-1", text=SOURCE_TEXT, chunk_id=CHUNK_ID)
    evidence = EvidenceItem(
        task_id=uuid4(), source_type=EvidenceSourceType.DOCUMENT, source_name="synthetic.txt",
        excerpt=SOURCE_TEXT, supports_claim="Synthetic source excerpt",
    )
    with pytest.raises(LocalAnalysisEvidenceError) as error:
        validate_analysis_evidence(analysis, {CHUNK_ID: chunk}, {CHUNK_ID: evidence}, {SPAN_ID: {"chunk_id": CHUNK_ID, "text": SOURCE_TEXT}})
    assert error.value.category == "source_chunk_or_evidence_missing"


def test_missing_selected_evidence_is_rejected():
    analysis = LocalDocumentAnalysis.model_validate(valid_output())
    chunk = DocumentChunk(document_id="doc-1", text=SOURCE_TEXT, chunk_id=CHUNK_ID)
    with pytest.raises(LocalAnalysisEvidenceError) as error:
        validate_analysis_evidence(analysis, {CHUNK_ID: chunk}, {}, {SPAN_ID: {"chunk_id": CHUNK_ID, "text": SOURCE_TEXT}})
    assert error.value.category == "source_chunk_or_evidence_missing"


def test_unknown_source_span_is_rejected():
    analysis = LocalDocumentAnalysis.model_validate(valid_output())
    analysis.findings[0].source_span_id = "invented-span"
    chunk = DocumentChunk(document_id="doc-1", text=SOURCE_TEXT, chunk_id=CHUNK_ID)
    evidence = EvidenceItem(
        task_id=uuid4(), source_type=EvidenceSourceType.DOCUMENT, source_name="synthetic.txt",
        excerpt=SOURCE_TEXT, supports_claim="Synthetic source excerpt",
    )
    with pytest.raises(LocalAnalysisEvidenceError) as error:
        validate_analysis_evidence(analysis, {CHUNK_ID: chunk}, {CHUNK_ID: evidence}, {SPAN_ID: {"chunk_id": CHUNK_ID, "text": SOURCE_TEXT}})
    assert error.value.category == "source_chunk_or_evidence_missing"


@pytest.mark.parametrize("altered_span", [
    "setup  costs 125 USD and recommends a staged rollout.",
    "Synthetic report: setup costs 125 USD and recommends a staged rollout!",
])
def test_whitespace_or_punctuation_changed_source_excerpt_is_rejected(altered_span):
    analysis = LocalDocumentAnalysis.model_validate(valid_output())
    chunk = DocumentChunk(document_id="doc-1", text=SOURCE_TEXT, chunk_id=CHUNK_ID)
    evidence = EvidenceItem(
        task_id=uuid4(), source_type=EvidenceSourceType.DOCUMENT, source_name="synthetic.txt",
        excerpt=SOURCE_TEXT, supports_claim="Synthetic source excerpt",
    )
    spans = {SPAN_ID: {"chunk_id": CHUNK_ID, "text": altered_span}}
    with pytest.raises(LocalAnalysisEvidenceError) as error:
        validate_analysis_evidence(analysis, {CHUNK_ID: chunk}, {CHUNK_ID: evidence}, spans)
    assert error.value.category == "quote_not_in_source_chunk"


def test_model_supplied_quote_text_is_rejected_including_whitespace_and_punctuation_variants():
    for supplied_quote in (
        "setup costs 125 USD",
        "setup  costs 125 USD",
        "setup costs 125 USD!",
    ):
        response = valid_output()
        response["findings"][0]["quote"] = supplied_quote
        with pytest.raises(ValidationError):
            LocalDocumentAnalysis.model_validate(response)


def test_mismatched_selected_evidence_is_rejected_without_accepting_quote():
    analysis = LocalDocumentAnalysis.model_validate(valid_output())
    chunk = DocumentChunk(document_id="doc-1", text=SOURCE_TEXT, chunk_id=CHUNK_ID)
    mismatched_evidence = EvidenceItem(
        task_id=uuid4(), source_type=EvidenceSourceType.DOCUMENT, source_name="synthetic.txt",
        excerpt="A different source chunk", supports_claim="Synthetic source excerpt",
    )
    with pytest.raises(LocalAnalysisEvidenceError) as error:
        validate_analysis_evidence(analysis, {CHUNK_ID: chunk}, {CHUNK_ID: mismatched_evidence}, {SPAN_ID: {"chunk_id": CHUNK_ID, "text": SOURCE_TEXT}})
    assert error.value.category == "source_chunk_or_evidence_missing"


def test_section_heading_alone_is_not_valid_evidence_quote():
    analysis = LocalDocumentAnalysis.model_validate(valid_output())
    chunk = DocumentChunk(document_id="doc-1", text="Equipment delivery delays.", chunk_id=CHUNK_ID, section="Risks")
    evidence = EvidenceItem(
        task_id=uuid4(), source_type=EvidenceSourceType.DOCUMENT, source_name="synthetic.docx",
        excerpt=chunk.text, supports_claim="Synthetic source excerpt, section Risks",
    )
    analysis.findings[0].source_span_id = "heading-only-span"
    with pytest.raises(LocalAnalysisEvidenceError) as error:
        validate_analysis_evidence(
            analysis, {CHUNK_ID: chunk}, {CHUNK_ID: evidence},
            {"heading-only-span": {"chunk_id": CHUNK_ID, "text": "Risks"}},
        )
    assert error.value.category == "quote_not_in_source_chunk"


def test_fact_and_interpretation_are_preserved_with_evidence_ids():
    analysis = LocalDocumentAnalysis.model_validate(valid_output())
    chunk = DocumentChunk(document_id="doc-1", text=SOURCE_TEXT, chunk_id=CHUNK_ID)
    evidence = EvidenceItem(
        task_id=uuid4(), source_type=EvidenceSourceType.DOCUMENT, source_name="synthetic.txt",
        excerpt=SOURCE_TEXT, supports_claim="Synthetic source excerpt",
    )
    spans = _build_source_spans([chunk])
    refs = validate_analysis_evidence(analysis, {CHUNK_ID: chunk}, {CHUNK_ID: evidence}, spans)
    assert [ref["kind"] for ref in refs] == ["fact", "interpretation"]
    assert {ref["evidence_id"] for ref in refs} == {str(evidence.evidence_id)}
    assert all(ref["quote"] in chunk.text for ref in refs)
    assert all(ref["quote"] == SOURCE_TEXT for ref in refs)


def test_deterministic_source_spans_preserve_whitespace_and_punctuation_exactly():
    source = "Costs are INR 42 lakh,  with a 6-month plan. Next paragraph stays exact?"
    chunk = DocumentChunk(document_id="doc-1", text=source, chunk_id=CHUNK_ID)
    spans = _build_source_spans([chunk])
    assert list(spans.values())[0]["text"] == "Costs are INR 42 lakh,  with a 6-month plan."
    assert list(spans.values())[1]["text"] == "Next paragraph stays exact?"
    assert all(span["text"] in source for span in spans.values())


def test_schema_failure_stops_after_one_correction_retry():
    class AlwaysInvalidProvider(ValidProvider):
        def generate_json(self, *, system, prompt, response_model, **kwargs):
            self.calls.append({"system": system, "prompt": prompt, "model": response_model})
            raise LLMProviderError(_SCHEMA_VALIDATION_ERROR)

    provider = AlwaysInvalidProvider()
    with pytest.raises(LLMProviderError, match="schema"):
        request_local_document_analysis(provider, valid_payload())
    assert len(provider.calls) == 2


def test_txt_extraction_strips_utf8_bom():
    result = extract_document(b"\xef\xbb\xbfSynthetic BOM-safe text.", "synthetic-doc", "bom.txt")
    assert result.full_text == "Synthetic BOM-safe text."
    assert result.chunks[0].text == "Synthetic BOM-safe text."
    assert "\ufeff" not in result.full_text
