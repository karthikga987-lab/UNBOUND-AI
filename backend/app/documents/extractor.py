"""Text extraction from PDF, DOCX, and TXT files.

All extractors treat file contents as untrusted data. No macros, scripts,
or embedded commands are executed. Extraction is purely text-based.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from backend.app.documents.models import (
    DocumentChunk,
    ExtractionResult,
    ExtractionStatus,
    FileType,
)

logger = logging.getLogger(__name__)

# Maximum characters per chunk (tune for retrieval quality vs. result size)
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

# Safety limits
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt"}


def _chunk_text(
    text: str,
    document_id: str,
    page: int | None = None,
    section: str | None = None,
) -> list[DocumentChunk]:
    """Split text into overlapping chunks preserving page/section context."""
    chunks: list[DocumentChunk] = []
    if not text.strip():
        return chunks
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        # Try to break at a whitespace boundary
        if end < len(text):
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        chunk_text = text[start:end].strip()
        if chunk_text:
            chunks.append(
                DocumentChunk(
                    document_id=document_id,
                    text=chunk_text,
                    page=page,
                    section=section,
                    char_start=start,
                    char_end=end,
                )
            )
        start = end - CHUNK_OVERLAP if end < len(text) else len(text)
    return chunks


def _extract_pdf(file_bytes: bytes, document_id: str) -> ExtractionResult:
    """Extract text from a PDF using PyMuPDF. Treats content as untrusted data."""
    try:
        import pymupdf as fitz  # PyMuPDF (modern import)
    except ImportError:
        try:
            import fitz  # older alias
        except ImportError as exc:
            return ExtractionResult(
                document_id=document_id,
                filename="",
                file_type=FileType.PDF,
                full_text="",
                status=ExtractionStatus.FAILED,
                error=f"PyMuPDF not installed: {exc}",
            )

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as exc:
        return ExtractionResult(
            document_id=document_id,
            filename="",
            file_type=FileType.PDF,
            full_text="",
            status=ExtractionStatus.FAILED,
            error=f"Could not open PDF: {exc}",
        )

    page_texts: list[str] = []
    all_chunks: list[DocumentChunk] = []
    metadata: dict = {}

    try:
        raw_meta = doc.metadata or {}
        metadata = {k: v for k, v in raw_meta.items() if v and isinstance(v, str)}
        page_count = doc.page_count

        for page_num in range(page_count):
            page = doc.load_page(page_num)
            # text_type="text" is safe – no HTML/script rendering
            page_text = page.get_text("text")
            page_texts.append(page_text)
            all_chunks.extend(
                _chunk_text(page_text, document_id, page=page_num + 1)
            )

        doc.close()
        full_text = "\n\n".join(page_texts)
        status = ExtractionStatus.EMPTY if not full_text.strip() else ExtractionStatus.SUCCESS
        return ExtractionResult(
            document_id=document_id,
            filename="",
            file_type=FileType.PDF,
            full_text=full_text,
            chunks=all_chunks,
            metadata=metadata,
            status=status,
            page_count=page_count,
        )
    except Exception as exc:
        logger.exception("PDF extraction failed for document %s", document_id)
        return ExtractionResult(
            document_id=document_id,
            filename="",
            file_type=FileType.PDF,
            full_text="",
            status=ExtractionStatus.FAILED,
            error=f"Extraction error: {exc}",
        )


def _extract_docx(file_bytes: bytes, document_id: str) -> ExtractionResult:
    """Extract text from a DOCX using python-docx. Treats content as untrusted data."""
    try:
        from docx import Document
    except ImportError as exc:
        return ExtractionResult(
            document_id=document_id,
            filename="",
            file_type=FileType.DOCX,
            full_text="",
            status=ExtractionStatus.FAILED,
            error=f"python-docx not installed: {exc}",
        )

    try:
        doc = Document(io.BytesIO(file_bytes))
    except Exception as exc:
        return ExtractionResult(
            document_id=document_id,
            filename="",
            file_type=FileType.DOCX,
            full_text="",
            status=ExtractionStatus.FAILED,
            error=f"Could not open DOCX: {exc}",
        )

    try:
        all_chunks: list[DocumentChunk] = []
        full_parts: list[str] = []
        current_section: str | None = None
        section_buffer: list[str] = []

        def flush_section() -> None:
            if section_buffer:
                section_text = "\n".join(section_buffer)
                all_chunks.extend(
                    _chunk_text(section_text, document_id, section=current_section)
                )
                full_parts.append(section_text)

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            # Detect headings as section delimiters
            style_name = para.style.name if para.style else ""
            if style_name.startswith("Heading"):
                flush_section()
                current_section = text
                section_buffer = []
            else:
                section_buffer.append(text)

        flush_section()

        full_text = "\n\n".join(full_parts)
        core = doc.core_properties
        metadata: dict = {}
        for attr in ("author", "title", "subject", "created", "modified"):
            val = getattr(core, attr, None)
            if val is not None:
                metadata[attr] = str(val)

        status = ExtractionStatus.EMPTY if not full_text.strip() else ExtractionStatus.SUCCESS
        return ExtractionResult(
            document_id=document_id,
            filename="",
            file_type=FileType.DOCX,
            full_text=full_text,
            chunks=all_chunks,
            metadata=metadata,
            status=status,
        )
    except Exception as exc:
        logger.exception("DOCX extraction failed for document %s", document_id)
        return ExtractionResult(
            document_id=document_id,
            filename="",
            file_type=FileType.DOCX,
            full_text="",
            status=ExtractionStatus.FAILED,
            error=f"Extraction error: {exc}",
        )


def _extract_txt(file_bytes: bytes, document_id: str) -> ExtractionResult:
    """Extract text from a plain text file."""
    try:
        # Try UTF-8, then Latin-1 as fallback
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = file_bytes.decode("latin-1", errors="replace")

        full_text = text.strip()
        status = ExtractionStatus.EMPTY if not full_text else ExtractionStatus.SUCCESS
        chunks = _chunk_text(full_text, document_id)
        return ExtractionResult(
            document_id=document_id,
            filename="",
            file_type=FileType.TXT,
            full_text=full_text,
            chunks=chunks,
            status=status,
        )
    except Exception as exc:
        logger.exception("TXT extraction failed for document %s", document_id)
        return ExtractionResult(
            document_id=document_id,
            filename="",
            file_type=FileType.TXT,
            full_text="",
            status=ExtractionStatus.FAILED,
            error=f"Extraction error: {exc}",
        )


def infer_file_type(filename: str) -> FileType | None:
    """Infer file type from extension. Returns None for unsupported types."""
    ext = Path(filename).suffix.lower()
    mapping = {".pdf": FileType.PDF, ".docx": FileType.DOCX, ".txt": FileType.TXT}
    return mapping.get(ext)


def extract_document(
    file_bytes: bytes,
    document_id: str,
    filename: str,
) -> ExtractionResult:
    """
    Entry point: extract text from uploaded file bytes.

    Validates file size and type before extraction. All content is treated
    as untrusted data – no macros or scripts are executed.

    Args:
        file_bytes: Raw file content.
        document_id: Stable ID assigned to this document.
        filename: Original filename (used only to infer file type; not for path operations).

    Returns:
        ExtractionResult with text, chunks, metadata, and status.
    """
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        return ExtractionResult(
            document_id=document_id,
            filename=filename,
            file_type=FileType.TXT,  # placeholder; not used on failure
            full_text="",
            status=ExtractionStatus.FAILED,
            error=f"File too large: {len(file_bytes)} bytes exceeds the {MAX_FILE_SIZE_BYTES // (1024*1024)} MB limit",
        )

    if not file_bytes:
        return ExtractionResult(
            document_id=document_id,
            filename=filename,
            file_type=FileType.TXT,
            full_text="",
            status=ExtractionStatus.EMPTY,
            error="File is empty",
        )

    file_type = infer_file_type(filename)
    if file_type is None:
        ext = Path(filename).suffix.lower() or "(no extension)"
        return ExtractionResult(
            document_id=document_id,
            filename=filename,
            file_type=FileType.TXT,
            full_text="",
            status=ExtractionStatus.FAILED,
            error=f"Unsupported file type: {ext}. Supported: .pdf, .docx, .txt",
        )

    extractors = {
        FileType.PDF: _extract_pdf,
        FileType.DOCX: _extract_docx,
        FileType.TXT: _extract_txt,
    }

    result = extractors[file_type](file_bytes, document_id)
    result.filename = filename
    return result
