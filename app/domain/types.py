from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ParsedChapter:
    index: int
    title: str
    text: str
    cover_path: Path | None = None


@dataclass(frozen=True)
class ParsedBook:
    title: str
    author: str | None
    chapters: list[ParsedChapter]
    cover_path: Path | None = None


# 朗读稿风格：faithful 贴近原文，只清理不适合朗读的内容；narrated 在此基础上逐段改写成讲述口吻
SPEECH_STYLES = ("faithful", "narrated")


@dataclass(frozen=True)
class SegmentInput:
    id: int
    chapter_id: int
    segment_index: int
    text: str


@dataclass(frozen=True)
class PronunciationHint:
    segment_id: int
    surface: str
    occurrence_index: int
    pronunciation: str
    kind: str
    confidence: str
    source: str = "llm"
    active: bool = True


@dataclass(frozen=True)
class PrecheckResult:
    speech_text_by_segment: dict[int, str]
    hints: list[PronunciationHint]


@dataclass(frozen=True)
class AudioArtifact:
    segment_id: int
    path: Path
    duration_ms: int


@dataclass(frozen=True)
class ChapterAudio:
    chapter_id: int
    title: str
    segment_paths: list[Path] = field(default_factory=list)
