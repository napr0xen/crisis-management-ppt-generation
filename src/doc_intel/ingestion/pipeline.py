"""Pick the parser for an extension, then chunk by CHUNK_TOKENIZER counts (system-design § 2)."""

import uuid
from functools import cache

from tokenizers import Tokenizer

from doc_intel.ingestion.chunker import chunk
from doc_intel.ingestion.docling_parser import parse_docx, parse_pdf, parse_pptx
from doc_intel.ingestion.native_parsers import parse_csv, parse_md, parse_txt, parse_xlsx
from doc_intel.ingestion.schemas import ChunkPayload, ParsedDocument, Parser

PARSERS: dict[str, Parser] = {
    "pdf": parse_pdf,
    "docx": parse_docx,
    "pptx": parse_pptx,
    "xlsx": parse_xlsx,
    "csv": parse_csv,
    "txt": parse_txt,
    "md": parse_md,
}


class ParseError(Exception):
    code = "parse_error"


@cache
def _tokenizer(model: str) -> Tokenizer:
    return Tokenizer.from_pretrained(model)


def parse_and_chunk(
    data: bytes,
    ext: str,
    *,
    document_id: uuid.UUID,
    filename: str,
    max_tokens: int,
    tokenizer_model: str,
) -> tuple[ParsedDocument, list[ChunkPayload]]:
    """CPU-bound: call via `asyncio.to_thread`."""
    try:
        parsed = PARSERS[ext](data)
    except Exception as exc:
        # The parser's message may quote file content; only the code travels on.
        raise ParseError() from exc
    tokenizer = _tokenizer(tokenizer_model)
    chunks = chunk(
        parsed,
        document_id=document_id,
        filename=filename,
        max_tokens=max_tokens,
        count_tokens=lambda text: len(tokenizer.encode(text).ids),
    )
    return parsed, chunks
