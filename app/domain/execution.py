from dataclasses import dataclass

from .errors import BookTtsError


STAGE_ORDER = ("precheck", "synthesize", "package")


@dataclass(frozen=True)
class ResolvedExecutionRange:
    selection_mode: str
    selection: dict
    start_sequence: int
    end_sequence: int
    actual_char_count: int
    chapter_count: int
    precheck_count: int
    segment_count: int
    estimated_duration_ms: int
    warning_message: str | None = None

    def as_record(self, book_id: str, start_stage: str, end_stage: str, output_mode: str) -> dict:
        return {
            "book_id": book_id,
            "selection_mode": self.selection_mode,
            "selection": self.selection,
            "resolved_start_sequence": self.start_sequence,
            "resolved_end_sequence": self.end_sequence,
            "actual_char_count": self.actual_char_count,
            "chapter_count": self.chapter_count,
            "precheck_count": self.precheck_count,
            "segment_count": self.segment_count,
            "estimated_duration_ms": self.estimated_duration_ms,
            "start_stage": start_stage,
            "end_stage": end_stage,
            "output_mode": output_mode,
            "warning_message": self.warning_message,
        }


def resolve_execution_range(
    *,
    selection_mode: str,
    selection: dict,
    start_stage: str,
    chapters: list,
    segments: list,
    batches: list,
) -> ResolvedExecutionRange:
    if not segments:
        raise BookTtsError("书稿还没有可执行的文本分段")
    if start_stage not in STAGE_ORDER:
        raise BookTtsError(f"未知的开始阶段：{start_stage}")
    if selection_mode not in {"chapters", "characters", "advanced"}:
        raise BookTtsError("范围模式必须是 chapters、characters 或 advanced")

    ordered_segments = sorted(segments, key=lambda row: int(row["global_index"]))
    min_sequence = int(ordered_segments[0]["global_index"])
    max_sequence = int(ordered_segments[-1]["global_index"])
    base_start, base_end = _resolve_base_range(
        selection_mode,
        selection,
        start_stage,
        chapters,
        ordered_segments,
        batches,
        min_sequence,
        max_sequence,
    )

    # 只有读音检查必须按完整批次调用 LLM；生成语音和导出可以精确到单个分段
    if start_stage == "precheck":
        resolved_start, resolved_end, warning = _expand_to_precheck_batches(
            base_start,
            base_end,
            batches,
            ordered_segments,
            selection_mode,
        )
    else:
        resolved_start, resolved_end, warning = base_start, base_end, None
    selected = [
        row
        for row in ordered_segments
        if resolved_start <= int(row["global_index"]) <= resolved_end
    ]
    if not selected:
        raise BookTtsError("范围没有覆盖任何文本分段")
    actual_chars = sum(len(row["text"]) for row in selected)
    chapter_count = len({int(row["chapter_id"]) for row in selected})
    precheck_count = sum(
        1
        for batch in batches
        if _batch_intersects(batch, resolved_start, resolved_end, ordered_segments)
    )
    return ResolvedExecutionRange(
        selection_mode=selection_mode,
        selection=selection,
        start_sequence=resolved_start,
        end_sequence=resolved_end,
        actual_char_count=actual_chars,
        chapter_count=chapter_count,
        precheck_count=precheck_count,
        segment_count=len(selected),
        estimated_duration_ms=max(1000, int(actual_chars / 4.5 * 1000)),
        warning_message=warning,
    )


def _resolve_base_range(
    selection_mode: str,
    selection: dict,
    start_stage: str,
    chapters: list,
    segments: list,
    batches: list,
    min_sequence: int,
    max_sequence: int,
) -> tuple[int, int]:
    if selection_mode == "chapters":
        start_chapter = _positive_int(selection.get("chapter_start"), 1)
        end_chapter = _positive_int(selection.get("chapter_end"), start_chapter)
        if end_chapter < start_chapter:
            raise BookTtsError("结束章节不能小于开始章节")
        chapter_map = {int(row["chapter_index"]) + 1: row for row in chapters}
        if start_chapter not in chapter_map or end_chapter not in chapter_map:
            raise BookTtsError(f"章节范围超出当前书稿，共 {len(chapters)} 章")
        chosen = [
            row
            for row in segments
            if start_chapter <= int(row["chapter_index"]) + 1 <= end_chapter
        ]
        if not chosen:
            raise BookTtsError("选择的章节没有可朗读正文")
        return int(chosen[0]["global_index"]), int(chosen[-1]["global_index"])

    if selection_mode == "characters":
        target = _positive_int(selection.get("target_characters"), 5000)
        requested_start = selection.get("start_sequence")
        start = (
            int(requested_start)
            if requested_start is not None
            else _first_unfinished_sequence(start_stage, segments, batches)
        )
        start = max(min_sequence, min(start, max_sequence))
        total = 0
        end = start
        for row in segments:
            sequence = int(row["global_index"])
            if sequence < start:
                continue
            total += len(row["text"])
            end = sequence
            if total >= target:
                break
        return start, end

    start_segment = selection.get("start_segment")
    end_segment = selection.get("end_segment")
    if start_segment is not None or end_segment is not None:
        start = _positive_int(start_segment, 1) - 1
        end = _positive_int(end_segment, start + 1) - 1
        _validate_sequence(start, end, min_sequence, max_sequence)
        return start, end

    start_batch = selection.get("start_batch")
    end_batch = selection.get("end_batch")
    if start_batch is not None or end_batch is not None:
        start_index = _positive_int(start_batch, 1) - 1
        end_index = _positive_int(end_batch, start_index + 1) - 1
        if start_index < 0 or end_index < start_index or end_index >= len(batches):
            raise BookTtsError(f"预检批次范围超出当前书稿，共 {len(batches)} 批")
        batch_segments = [
            segment
            for batch in batches[start_index : end_index + 1]
            for segment in _segments_for_batch(batch, segments)
        ]
        if not batch_segments:
            raise BookTtsError("选择的预检批次没有可执行分段")
        return int(batch_segments[0]["global_index"]), int(batch_segments[-1]["global_index"])

    start_chapter = _positive_int(selection.get("chapter_start"), 1)
    end_chapter = _positive_int(selection.get("chapter_end"), start_chapter)
    return _resolve_base_range(
        "chapters",
        {"chapter_start": start_chapter, "chapter_end": end_chapter},
        start_stage,
        chapters,
        segments,
        batches,
        min_sequence,
        max_sequence,
    )


def _expand_to_precheck_batches(
    start: int,
    end: int,
    batches: list,
    segments: list,
    selection_mode: str,
) -> tuple[int, int, str | None]:
    intersecting = [batch for batch in batches if _batch_intersects(batch, start, end, segments)]
    if not intersecting:
        raise BookTtsError("范围没有覆盖任何发音预检批次")
    expanded_start = min(_batch_bounds(batch, segments)[0] for batch in intersecting)
    expanded_end = max(_batch_bounds(batch, segments)[1] for batch in intersecting)
    if expanded_start == start and expanded_end == end:
        return start, end, None
    if selection_mode == "characters":
        message = "按字数选择已扩展到完整预检批次，实际范围以页面统计为准。"
    else:
        message = "执行范围已对齐完整预检批次。"
    return expanded_start, expanded_end, message


def _batch_bounds(batch, segments: list | None = None) -> tuple[int, int]:
    if segments is None:
        ids = batch.get("global_indices") if isinstance(batch, dict) else None
        if ids:
            return min(ids), max(ids)
        raise BookTtsError("预检批次缺少分段范围")
    selected = _segments_for_batch(batch, segments)
    if not selected:
        raise BookTtsError(f"第 {int(batch['batch_index']) + 1} 批没有分段")
    return int(selected[0]["global_index"]), int(selected[-1]["global_index"])


def _batch_intersects(batch, start: int, end: int, segments: list) -> bool:
    batch_start, batch_end = _batch_bounds(batch, segments)
    return batch_start <= end and batch_end >= start


def _segments_for_batch(batch, segments: list) -> list:
    segment_ids = _batch_segment_ids(batch)
    by_id = {int(row["id"]): row for row in segments}
    selected = [by_id[segment_id] for segment_id in segment_ids if segment_id in by_id]
    return sorted(selected, key=lambda row: int(row["global_index"]))


def _batch_segment_ids(batch) -> list[int]:
    import json

    raw = batch["segment_ids_json"] if not isinstance(batch, dict) else batch.get("segment_ids_json")
    if isinstance(raw, str):
        values = json.loads(raw)
    else:
        values = raw or []
    return [int(value) for value in values]


def _first_unfinished_sequence(start_stage: str, segments: list, batches: list) -> int:
    if start_stage == "precheck":
        for batch in batches:
            if batch["status"] != "completed":
                selected = _segments_for_batch(batch, segments)
                if selected:
                    return int(selected[0]["global_index"])
    else:
        for segment in segments:
            if segment["status"] != "completed":
                return int(segment["global_index"])
    return int(segments[0]["global_index"])


def _positive_int(value, default: int) -> int:
    if value is None:
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise BookTtsError("范围参数必须是整数") from error
    if result < 1:
        raise BookTtsError("范围参数必须大于 0")
    return result


def _validate_sequence(start: int, end: int, min_sequence: int, max_sequence: int) -> None:
    if end < start:
        raise BookTtsError("结束分段不能小于开始分段")
    if start < min_sequence or end > max_sequence:
        raise BookTtsError(f"分段范围超出当前书稿，共 {max_sequence - min_sequence + 1} 段")
