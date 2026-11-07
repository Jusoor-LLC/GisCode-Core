from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DocumentChunk:
    document_id: str
    chunk_index: int
    content: str
    start_char: int
    end_char: int
    metadata: dict = field(default_factory=dict)


def chunk_text(
    text: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
    document_id: str = "",
) -> list[DocumentChunk]:
    """
    Chunk text into overlapping segments deterministically.

    Args:
        text: The normalized text to chunk
        chunk_size: Maximum characters per chunk
        chunk_overlap: Overlap between consecutive chunks
        document_id: Identifier for the source document

    Returns:
        List of DocumentChunk objects in order
    """
    if not text:
        return []

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap must be non-negative")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be less than chunk_size")

    chunks = []
    start = 0
    chunk_index = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + chunk_size, text_len)

        # Try to find a good boundary (newline) near the end
        if end < text_len:
            # Look for newline within the last 100 chars of the chunk
            search_start = max(start, end - 100)
            newline_pos = text.rfind("\n", search_start, end)
            if newline_pos != -1 and newline_pos > start:
                end = newline_pos + 1

        chunk_content = text[start:end]
        chunk = DocumentChunk(
            document_id=document_id,
            chunk_index=chunk_index,
            content=chunk_content,
            start_char=start,
            end_char=end,
            metadata={
                "chunk_size": len(chunk_content),
                "is_last": end >= text_len,
            },
        )
        chunks.append(chunk)

        chunk_index += 1
        if end >= text_len:
            break

        # Move start forward, accounting for overlap
        start = max(start + 1, end - chunk_overlap)

    return chunks


def chunk_document(
    content: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
    document_id: str = "",
) -> list[DocumentChunk]:
    """Convenience function to chunk a full document."""
    return chunk_text(content, chunk_size, chunk_overlap, document_id)