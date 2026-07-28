import re
from html.parser import HTMLParser
from pathlib import Path


SUPPORTED_EXTENSIONS = {".md", ".txt", ".html", ".htm", ".bib", ".ris"}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    def text(self) -> str:
        return normalize_whitespace(" ".join(self._parts))


def kind_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "markdown"
    if suffix == ".txt":
        return "text"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix == ".bib":
        return "bibtex"
    if suffix == ".ris":
        return "ris"
    raise RuntimeError(f"Unsupported extension: {suffix}")


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_text(path: Path) -> str:
    kind = kind_for_path(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if kind == "html":
        parser = _HTMLTextExtractor()
        parser.feed(text)
        parser.close()
        return parser.text()
    return normalize_whitespace(text)


def chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    text = normalize_whitespace(text)
    if not text:
        return []
    return [text[start : start + max_chars] for start in range(0, len(text), max_chars)]
