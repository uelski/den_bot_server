"""Parse PDF bytes to per-page text with pymupdf."""

import logging
from dataclasses import dataclass

import pymupdf

logger = logging.getLogger(__name__)


@dataclass
class ParsedPage:
    page_number: int  # 1-indexed for human-friendly citations
    text: str


def parse_pdf(pdf_bytes: bytes) -> list[ParsedPage]:
    """Parse a PDF byte buffer to a list of per-page text blocks.

    Empty pages are preserved (with `text=""`) so that page numbers
    downstream stay aligned to the original document — important for
    citations like "pages 11-14".
    """
    pages: list[ParsedPage] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            text = page.get_text("text") or ""
            pages.append(ParsedPage(page_number=i + 1, text=text))
    logger.info(
        "parsed pdf: %d pages, %d chars total",
        len(pages),
        sum(len(p.text) for p in pages),
    )
    return pages
