"""Text extraction, clean HTML, and 10-K/10-Q MD&A narrative parsing."""

import re
from typing import List
from bs4 import BeautifulSoup


# ==============================================================================
# HTML & iXBRL Text Sanitizer
# Strips HTML tags, embedded tables, boilerplate scripts, and excess whitespace
# from RSS summaries, news articles, and SEC EDGAR HTML filings.
# ==============================================================================
def clean_html_text(raw_text: str) -> str:
    """Remove HTML/XML markup and normalize whitespace for NLP consumption."""
    if not raw_text:
        return ""
    soup = BeautifulSoup(raw_text, "html.parser")
    for tag in soup(["script", "style", "table", "ix:header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


# ==============================================================================
# SEC 10-K / 10-Q MD&A & Risk Factor Section Extractor
# Locates "Item 7. Management's Discussion and Analysis" (10-K) or "Item 2." (10-Q)
# inside cleaned filing text so FinBERT scores forward-looking narrative rather
# than cover-page boilerplate.
# ==============================================================================
MDA_PATTERNS = [
    re.compile(
        r"item\s+7[\.\s\-:]+management['’]?s\s+discussion\s+and\s+analysis(.*?)(?:item\s+7a|item\s+8)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"item\s+2[\.\s\-:]+management['’]?s\s+discussion\s+and\s+analysis(.*?)(?:item\s+3|item\s+4)",
        re.IGNORECASE | re.DOTALL,
    ),
]


def extract_mda_section(filing_html_or_text: str, max_chars: int = 5000) -> str:
    """Extract the MD&A section from a 10-K or 10-Q filing, falling back to cleaned body text."""
    cleaned = clean_html_text(filing_html_or_text)
    if not cleaned:
        return ""

    for pattern in MDA_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            candidate = match.group(1).strip()
            # Ensure we matched the actual section body and not just a Table of Contents line
            if len(candidate) > 300:
                return candidate[:max_chars]

    return cleaned[:max_chars]


# ==============================================================================
# Sentence & Token-Window Chunker
# Splits long filing excerpts or transcripts into ~400-character passages so
# each passage fits comfortably inside FinBERT's 512-token limit.
# ==============================================================================
def chunk_text_for_finbert(text: str, max_chunk_chars: int = 450, max_chunks: int = 8) -> List[str]:
    """Split cleaned text into sentence-aligned chunks suitable for FinBERT inference."""
    cleaned = clean_html_text(text)
    if not cleaned:
        return []

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    chunks: List[str] = []
    current = ""

    for sent in sentences:
        if not sent:
            continue
        if len(current) + len(sent) + 1 <= max_chunk_chars:
            current = f"{current} {sent}".strip()
        else:
            if current:
                chunks.append(current)
            current = sent[:max_chunk_chars]
        if len(chunks) >= max_chunks:
            break

    if current and len(chunks) < max_chunks:
        chunks.append(current)

    return chunks

