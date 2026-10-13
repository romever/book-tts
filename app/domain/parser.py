import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urldefrag

from bs4 import BeautifulSoup
import ebooklib
from ebooklib import epub

from .errors import ParseError
from .text import normalize_text
from .types import ParsedBook, ParsedChapter


_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "p", "li", "blockquote")
_NON_NARRATIVE_EPUB_TYPES = {
    "cover",
    "titlepage",
    "toc",
    "landmarks",
    "page-list",
    "copyright-page",
    "colophon",
    "index",
    "bibliography",
    "footnote",
    "endnote",
    "footnotes",
    "endnotes",
    "rearnotes",
}
_SENTENCE_MARKS = re.compile(r"[。！？!?]")
_PROSE_MARKS = re.compile(r"[。！？!?，,；;：:]")
_NUMBERED_HEADING = re.compile(
    r"^(?:第[0-9一二三四五六七八九十百零〇]+[章节卷部篇]|[上中下][编篇部卷]|[0-9一二三四五六七八九十]+[、.．])"
)


def _read_txt(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError as gb_error:
            raise ParseError("TXT 仅支持 UTF-8、UTF-16 或 GB18030 编码") from gb_error


def _semantic_types(node) -> set[str]:
    return set(str(node.get("epub:type") or "").lower().split()) | {
        str(node.get("role") or "").removeprefix("doc-").lower()
    }


def _link_target(item_name: str, href: str) -> tuple[str, str]:
    name, fragment = urldefrag(unquote(href))
    return (posixpath.normpath(posixpath.join(posixpath.dirname(item_name), name)) if name else item_name, fragment)


def _note_structure(documents) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """根据正文与注释的双向链接识别注释，避免把普通编号列表当成注释。"""
    links = {}
    for item in documents:
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for link in soup.find_all("a", href=True, id=True):
            if not re.fullmatch(r"\s*\[?\d+\]?\s*", link.get_text()):
                continue
            block = link.find_parent(_BLOCK_TAGS)
            leading = block is not None and block.get_text(strip=True).startswith(link.get_text(strip=True))
            links[(item.get_name(), link["id"])] = (_link_target(item.get_name(), link["href"]), leading)
    notes, references = set(), set()
    for origin, (target, leading) in links.items():
        reverse = links.get(target)
        if leading and reverse is not None and reverse[0] == origin and not reverse[1]:
            notes.add(origin)
            references.add(target)
    return notes, references


def _document_body(item, notes=frozenset(), references=frozenset()):
    soup = BeautifulSoup(item.get_content(), "html.parser")
    body = soup.body or soup
    for tag in list(body.find_all(True)):
        if tag.parent is None:
            continue
        if tag.name in {"script", "style", "nav", "header", "footer"} or _semantic_types(tag).intersection(_NON_NARRATIVE_EPUB_TYPES | {"noteref", "backlink", "pagebreak"}):
            tag.decompose()
            continue
        key = (item.get_name(), tag.get("id"))
        if key in notes:
            block = tag.find_parent(_BLOCK_TAGS)
            (block or tag).decompose()
        elif key in references:
            tag.decompose()
    # 图片及其独立短图注依赖视觉，不能作为正文朗读；长段解释仍然保留。
    for image in list(body.find_all("img")):
        container = image.find_parent(["figure", "div"])
        if container and len(container.get_text(strip=True)) <= 80 and not _SENTENCE_MARKS.search(container.get_text()):
            container.decompose()
        else:
            image.decompose()
    return soup, body


def _document_blocks(item, notes=frozenset(), references=frozenset()) -> tuple[list[str], object]:
    _, body = _document_body(item, notes, references)
    nodes = body.find_all(_BLOCK_TAGS)
    blocks = [
        normalize_text(node.get_text(" "))
        for node in nodes
        if not node.find(_BLOCK_TAGS) and normalize_text(node.get_text(" "))
    ]
    if not blocks:
        text = normalize_text(body.get_text("\n"))
        blocks = [text] if text else []
    return blocks, body


def _is_publication_page(blocks: list[str]) -> bool:
    # 出版页是密集的短字段，不以标题中的某个词决定整页去留。
    fields = sum(bool(re.fullmatch(r"[^：:\n。！？!?]{1,16}(?:[：:]|　+| {2,})[^\n。！？!?]{1,60}", block)) for block in blocks)
    return fields >= 3 and fields / len(blocks) >= 0.5 and not any(_SENTENCE_MARKS.search(block) for block in blocks)


def _toc_titles(entries) -> set[str]:
    if isinstance(entries, (epub.Link, epub.Section)):
        return {re.sub(r"\s+", "", entries.title)}
    titles = set()
    for entry in entries:
        if isinstance(entry, tuple):
            section, children = entry
            titles.add(re.sub(r"\s+", "", section.title))
            titles.update(_toc_titles(children))
        else:
            titles.add(re.sub(r"\s+", "", entry.title))
    return titles


def _is_title_page(body, blocks: list[str], titles: set[str]) -> bool:
    # 只有书名或目录标题的独立页才排除，不以篇幅短判断正文。
    return (
        len(blocks) == 1
        and re.sub(r"\s+", "", blocks[0]) in titles
        and len(body.find_all(["p", "h1", "h2", "h3", "h4"])) <= 1
    )


def _is_link_dominant(body, blocks: list[str]) -> bool:
    links = body.find_all("a")
    if len(links) < 3:
        return False
    visible_chars = sum(len(block) for block in blocks)
    linked_chars = sum(len(normalize_text(link.get_text(" "))) for link in links)
    return visible_chars > 0 and linked_chars / visible_chars >= 0.55


def _find_narrative_start(blocks: list[str]) -> int | None:
    """通过连续长句定位正文起点，避免依赖特定书名或页面关键词。"""
    for window_start in range(len(blocks)):
        window = blocks[window_start : window_start + 5]
        if len(window) < 3:
            break
        if sum(len(value) for value in window) < 180:
            continue
        long_positions = [
            window_start + offset
            for offset, value in enumerate(window)
            if len(value) >= 55
        ]
        punctuation_count = sum(len(_PROSE_MARKS.findall(value)) for value in window)
        if len(long_positions) < 2 or punctuation_count < 4:
            continue
        start = long_positions[0]
        # 正文前常有一至数行章名；只回收短而无页码特征的标题行。
        if start > 0:
            candidate = blocks[start - 1]
            if len(candidate) <= 30 and not re.search(r"\d", candidate) and not _SENTENCE_MARKS.search(candidate):
                start -= 1
                if start > 0 and _NUMBERED_HEADING.match(blocks[start - 1]):
                    start -= 1
        return start
    return None


def _narration_score(blocks: list[str]) -> float:
    text_length = sum(len(value) for value in blocks)
    if text_length == 0:
        return 0.0
    sentence_density = len(_SENTENCE_MARKS.findall("\n".join(blocks))) * 1000 / text_length
    prose_blocks = sum(
        len(value) >= 60 and bool(_PROSE_MARKS.search(value))
        for value in blocks
    )
    prose_ratio = prose_blocks / len(blocks)
    return min(1.0, sentence_density / 12) * 0.5 + min(1.0, prose_ratio / 0.35) * 0.5


def _guide_exclusions(book) -> set[str]:
    excluded: set[str] = set()
    for entry in book.guide:
        if str(entry.get("type") or "").lower() not in _NON_NARRATIVE_EPUB_TYPES:
            continue
        href, _ = urldefrag(unquote(str(entry.get("href") or "")))
        if href:
            excluded.add(href.lstrip("/"))
    return excluded


def _has_non_narrative_semantics(item) -> bool:
    properties = {str(value).lower() for value in (getattr(item, "properties", []) or [])}
    if properties.intersection(_NON_NARRATIVE_EPUB_TYPES | {"nav"}):
        return True
    soup = BeautifulSoup(item.get_content(), "html.parser")
    return any(_semantic_types(node).intersection(_NON_NARRATIVE_EPUB_TYPES) for node in (soup.html, soup.body) if node)


def _readable_document(item, notes=frozenset(), references=frozenset(), titles=frozenset()) -> tuple[list[str], float] | None:
    blocks, body = _document_blocks(item, notes, references)
    if not blocks or _is_link_dominant(body, blocks) or _is_publication_page(blocks) or _is_title_page(body, blocks, titles):
        return None
    start = _find_narrative_start(blocks)
    readable_blocks = blocks[start:] if start is not None else blocks
    return readable_blocks, _narration_score(readable_blocks)


def _document_title(item, text: str, index: int) -> str:
    soup = BeautifulSoup(item.get_content(), "html.parser")
    heading = soup.find(["h1", "h2", "h3"])
    if heading and heading.get_text(strip=True):
        return normalize_text(heading.get_text(" "))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and len(lines[0]) <= 40:
        if len(lines) > 1 and len(lines[1]) <= 40:
            return normalize_text(f"{lines[0]} {lines[1]}")
        return lines[0]
    name = Path(item.get_name()).stem.replace("_", " ").replace("-", " ").strip()
    return name or f"第 {index + 1} 章"


def _extract_cover(book, destination: Path) -> Path | None:
    image_items = list(book.get_items_of_type(ebooklib.ITEM_IMAGE))
    if not image_items:
        return None
    selected = next(
        (
            item
            for item in image_items
            if "cover" in item.get_name().lower()
            or "cover-image" in " ".join(getattr(item, "properties", []) or [])
        ),
        image_items[0],
    )
    suffix = Path(selected.get_name()).suffix or ".jpg"
    cover_path = destination / f"cover{suffix}"
    cover_path.write_bytes(selected.get_content())
    return cover_path


def parse_epub(path: Path, destination: Path) -> ParsedBook:
    try:
        book = epub.read_epub(str(path), options={"ignore_ncx": False})
    except Exception as error:
        raise ParseError(f"EPUB 解析失败：{error}") from error

    title_metadata = book.get_metadata("DC", "title")
    author_metadata = book.get_metadata("DC", "creator")
    title = title_metadata[0][0].strip() if title_metadata else path.stem
    author = author_metadata[0][0].strip() if author_metadata else None
    cover_path = _extract_cover(book, destination)

    chapters: list[ParsedChapter] = []
    documents = {
        item.get_id(): item
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)
    }
    notes, references = _note_structure(documents.values())
    titles = _toc_titles(book.toc) | {re.sub(r"\s+", "", title)}
    excluded_names = _guide_exclusions(book)
    candidates: list[tuple[object, list[str], float]] = []
    spine_ids = [item_id for item_id, _ in book.spine]
    linear_by_id = {item_id: linear for item_id, linear in book.spine}
    for item_id in spine_ids:
        item = documents.get(item_id)
        if item is None:
            continue
        item_name = unquote(item.get_name()).lstrip("/")
        if linear_by_id[item_id] == "no" or item_name in excluded_names or _has_non_narrative_semantics(item):
            continue
        readable = _readable_document(item, notes, references, titles)
        if readable is None:
            continue
        blocks, score = readable
        candidates.append((item, blocks, score))

    # 长篇参考资料、索引等常位于书末，叙述性会相对正文显著下降。
    while candidates:
        _, blocks, score = candidates[-1]
        if sum(len(value) for value in blocks) < 1000 or score >= 0.55:
            break
        candidates.pop()

    for item, blocks, _ in candidates:
        text = normalize_text("\n".join(blocks))
        if not text:
            continue
        index = len(chapters)
        chapters.append(
            ParsedChapter(index=index, title=_document_title(item, text, index), text=text)
        )
    if not chapters:
        raise ParseError("EPUB 中没有找到可朗读的正文")
    return ParsedBook(title=title, author=author, chapters=chapters, cover_path=cover_path)


def parse_txt(path: Path, chapter_regex: str | None) -> ParsedBook:
    text = normalize_text(_read_txt(path))
    if not text:
        raise ParseError("TXT 文件没有可朗读内容")

    if chapter_regex:
        try:
            pattern = re.compile(chapter_regex)
        except re.error as error:
            raise ParseError(f"TXT 章节正则无效：{error}") from error
        lines = text.split("\n")
        chapters: list[ParsedChapter] = []
        current_title: str | None = None
        current_lines: list[str] = []
        for line in lines:
            if pattern.search(line.strip()):
                if current_lines:
                    chapter_text = normalize_text("\n".join(current_lines))
                    if chapter_text:
                        chapters.append(
                            ParsedChapter(
                                index=len(chapters),
                                title=current_title or f"第 {len(chapters) + 1} 章",
                                text=chapter_text,
                            )
                        )
                current_title = line.strip()
                current_lines = [line]
            else:
                current_lines.append(line)
        if current_lines:
            chapter_text = normalize_text("\n".join(current_lines))
            if chapter_text:
                chapters.append(
                    ParsedChapter(
                        index=len(chapters),
                        title=current_title or f"第 {len(chapters) + 1} 章",
                        text=chapter_text,
                    )
                )
    else:
        chapters = [ParsedChapter(index=0, title=path.stem, text=text)]

    if not chapters:
        raise ParseError("TXT 没有形成有效章节")
    return ParsedBook(title=path.stem, author=None, chapters=chapters)


def parse_book(path: Path, destination: Path, chapter_regex: str | None = None) -> ParsedBook:
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return parse_epub(path, destination)
    if suffix == ".txt":
        return parse_txt(path, chapter_regex)
    raise ParseError("只支持 EPUB 和 TXT 文件")
