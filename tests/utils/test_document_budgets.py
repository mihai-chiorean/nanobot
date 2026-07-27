from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from nanobot.utils import document


def test_text_budget_stops_retaining_content_at_limit() -> None:
    budget = document._TextBudget(max_length=8)

    budget.append("abcd")
    budget.append("efghijkl", separator="")

    assert budget.exhausted
    assert budget.render() == "abcdefgh... (truncated at extraction limit)"


def test_pdf_extraction_stops_after_page_budget(tmp_path, monkeypatch) -> None:
    calls: list[int] = []

    class Page:
        def __init__(self, number: int):
            self.number = number

        def extract_text(self) -> str:
            calls.append(self.number)
            return f"page {self.number}"

    class Reader:
        def __init__(self, _path: Path):
            self.pages = [Page(index) for index in range(document._MAX_PDF_PAGES + 20)]

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=Reader))
    path = tmp_path / "large.pdf"
    path.write_bytes(b"%PDF-")

    extracted = document._extract_pdf(path)

    assert len(calls) == document._MAX_PDF_PAGES
    assert "page 99" in extracted
    assert "page 100" not in extracted
    assert "truncated at extraction limit" in extracted
