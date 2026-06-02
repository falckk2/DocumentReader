import logging
import re
import fitz  # PyMuPDF

log = logging.getLogger(__name__)


class PDFReader:
    def __init__(self):
        self._doc = None
        self._path = None

    def open(self, path: str) -> int:
        """Open a PDF file. Returns total page count."""
        if self._doc:
            self._doc.close()
        self._doc = fitz.open(path)
        self._path = path
        # ISSUE-015 fix: detect password-protected PDFs and raise a clear
        # error rather than silently returning empty text for every page.
        if self._doc.is_encrypted:
            self._doc.close()
            self._doc = None
            raise ValueError(
                "This PDF is password-protected. "
                "Encrypted PDFs are not currently supported."
            )
        log.info("PDFReader opened %s (%d pages)", path, len(self._doc))
        return len(self._doc)

    def close(self):
        if self._doc:
            self._doc.close()
            self._doc = None

    @property
    def page_count(self) -> int:
        return len(self._doc) if self._doc else 0

    @property
    def is_open(self) -> bool:
        return self._doc is not None

    def get_page_text(self, page_index: int) -> str:
        """Extract plain text from a page (0-indexed)."""
        if not self._doc or page_index < 0 or page_index >= len(self._doc):
            return ""
        # ISSUE-015 fix: wrap per-page extraction in try/except so a malformed
        # page does not propagate an exception into the GUI thread.
        try:
            page = self._doc[page_index]
            text = page.get_text("text")
        except Exception:
            log.exception("Failed to extract text from page %d of %s", page_index, self._path)
            return ""
        # Normalize whitespace but preserve paragraph breaks
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def get_sentences(self, page_index: int) -> list[str]:
        """Split page text into readable sentences."""
        text = self.get_page_text(page_index)
        if not text:
            return []
        # Split on sentence-ending punctuation followed by whitespace or end
        raw = re.split(r"(?<=[.!?])\s+", text)
        sentences = [s.strip() for s in raw if s.strip()]
        return sentences

    def get_all_text(self, page_index: int) -> str:
        """Return full page text for display in the UI."""
        return self.get_page_text(page_index)
