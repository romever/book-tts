import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from .adapters import (
    OpenRouterLLMAdapter,
    create_tts_adapter,
    synthesize_with_pauses,
    validate_speech_texts,
    write_silence,
)
from .config import Settings
from .database import Database, utc_now
from .domain.errors import BookTtsError, PrecheckError, ProviderError
from .domain.packager import FfmpegM4bPackager
from .domain.parser import parse_book
from .domain.prompts import build_tts_instruction
from .domain.segmenter import split_text
from .domain.text import sha256_text
from .domain.types import SPEECH_STYLES, PrecheckResult, SegmentInput
from .voices import CloneVoiceStore

logger = logging.getLogger("book-tts.pipeline")


class ExecutionPaused(Exception):
    pass


class BookPipeline:
    """只负责本地解析和执行窗口；范围决策在创建窗口时已经完成。"""

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def run(self, book_id: str) -> None:
        book = self.database.get_book(book_id)
        if book is None or book["status"] not in {"uploaded", "parsing"}:
            return
        self.database.update_book(
            book_id,
            status="parsing",
            stage="import",
            error_message=None,
            stage_current=0,
            stage_total=0,
        )
        attempt_id = self.database.start_attempt(book_id, "import")
        try:
            self._parse(book_id)
        except Exception as error:
            self.database.finish_attempt(attempt_id, "failed", str(error))
            self.database.update_book(
                book_id,
                status="failed",
                stage="import",
                error_message=str(error),
            )
            raise
        self.database.finish_attempt(attempt_id, "completed")

    def run_execution(self, execution_id: str) -> None:
        execution = self.database.get_execution(execution_id)
        if execution is None:
            raise BookTtsError(f"找不到执行窗口：{execution_id}")
        if execution["status"] == "completed":
            return
        book = self.database.get_book(execution["book_id"])
        if book is None:
            raise BookTtsError(f"找不到书籍：{execution['book_id']}")

        self.database.update_execution(
            execution_id,
            status="running",
            error_message=None,
            current_stage=execution["start_stage"],
            current_item=0,
            current_total=0,
        )
        self.database.update_book(book["id"], status="executing", error_message=None)
        attempt_id = self.database.start_attempt(book["id"], f"execution:{execution_id}")
        try:
            self._validate_prerequisites(execution)
            if self._stage_reached(execution, "precheck"):
                self._run_precheck(execution_id)
            if self._stage_reached(execution, "synthesize"):
                self._run_synthesis(execution_id)
            if self._stage_reached(execution, "package"):
                self._run_package(execution_id)
        except ExecutionPaused:
            self.database.update_execution(
                execution_id,
                status="paused",
                current_stage=self.database.get_execution(execution_id)["current_stage"],
                error_message=None,
            )
            self.database.update_book(book["id"], status="ready", error_message=None)
            self.database.finish_attempt(attempt_id, "paused")
            return
        except Exception as error:
            message = str(error)
            self.database.update_execution(
                execution_id,
                status="failed",
                error_message=message,
            )
            self.database.update_book(book["id"], status="attention", error_message=message)
            self.database.finish_attempt(attempt_id, "failed", message)
            raise
        else:
            self.database.update_execution(
                execution_id,
                status="completed",
                current_stage=execution["end_stage"],
                current_item=int(execution["current_total"] or 0),
                error_message=None,
            )
            self.database.finish_attempt(attempt_id, "completed")
            self._refresh_book_status(book["id"])

    def _parse(self, book_id: str) -> None:
        book = self._required_book(book_id)
        source_path = Path(book["source_path"])
        book_dir = source_path.parent
        parsed = parse_book(source_path, book_dir, book["chapter_regex"])

        self.database.delete_book_content(book_id)
        chapters_dir = book_dir / "chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        global_index = 0
        for chapter in parsed.chapters:
            text_path = chapters_dir / f"{chapter.index:04d}.txt"
            text_path.write_text(chapter.text, encoding="utf-8")
            chapter_id = self.database.insert_chapter(
                {
                    "book_id": book_id,
                    "chapter_index": chapter.index,
                    "title": chapter.title,
                    "text_path": str(text_path),
                    "text_sha256": sha256_text(chapter.text),
                    "char_count": len(chapter.text),
                }
            )
            chapter_segments = split_text(chapter.text)
            if "".join(item.text for item in chapter_segments) != chapter.text:
                raise BookTtsError(f"第 {chapter.index + 1} 章分段后正文不一致")
            for segment in chapter_segments:
                self.database.insert_segment(
                    {
                        "chapter_id": chapter_id,
                        "segment_index": segment.index,
                        "global_index": global_index,
                        "text": segment.text,
                        "text_sha256": sha256_text(segment.text),
                    }
                )
                global_index += 1

        all_segments = self.database.get_segments(book_id)
        batches = _build_precheck_batches(all_segments)
        if not batches:
            raise BookTtsError("解析后没有可处理的文本分段")
        self.database.ensure_precheck_batches(book_id, batches)
        self.database.update_book(
            book_id,
            title=parsed.title,
            author=parsed.author,
            cover_path=str(parsed.cover_path) if parsed.cover_path else None,
            status="ready",
            stage="ready",
            stage_current=0,
            stage_total=0,
            error_message=None,
        )

    def _run_precheck(self, execution_id: str) -> None:
        execution = self._required_execution(execution_id)
        book = self._required_book(execution["book_id"])
        all_segments = self.database.get_segments(book["id"])
        batches = self.database.get_precheck_batches(book["id"])
        selected_segments = self.database.get_execution_segments(execution_id)
        selected_ids = {row["id"] for row in selected_segments}
        selected_batches = [
            batch
            for batch in batches
            if selected_ids.intersection(_batch_segment_ids(batch))
        ]
        if not selected_batches:
            raise BookTtsError("当前窗口没有可执行的发音预检批次")
        adapter = OpenRouterLLMAdapter(self.settings)
        narrated = _book_speech_style(book) == "narrated"
        completed = sum(batch["status"] == "completed" for batch in selected_batches)
        self._set_progress(execution_id, "precheck", completed, len(selected_batches))
        execution_dir = Path(book["source_path"]).parent / "executions" / execution_id / "llm"
        for batch in selected_batches:
            self._check_pause(execution_id)
            if batch["status"] == "completed":
                continue
            batch_segments = _segments_for_batch(all_segments, batch)
            inputs = [
                SegmentInput(
                    id=row["id"],
                    chapter_id=row["chapter_id"],
                    segment_index=row["segment_index"],
                    text=row["text"],
                )
                for row in batch_segments
            ]
            self._set_progress(execution_id, "precheck", completed, len(selected_batches))
            attempts = int(batch["attempts"] or 0)
            try:
                # LLM 偶发输出退化（思考死循环、截断、分段错位等），同一批重新生成通常即可通过；
                # 每次尝试单独保存诊断文件，超过次数仍失败则按原样报错
                for attempt in range(1, max(1, self.settings.llm_max_attempts) + 1):
                    suffix = "" if attempt == 1 else f".attempt-{attempt}"
                    response_path = (
                        execution_dir / f"batch-{batch['batch_index'] + 1:04d}{suffix}.response.txt"
                    )
                    attempts += 1
                    self.database.update_precheck_batch(
                        batch["id"],
                        status="running",
                        attempts=attempts,
                        error_message=None,
                        error_context_json=None,
                        response_path=str(response_path),
                        started_at=utc_now(),
                        finished_at=None,
                    )
                    try:
                        result = self._precheck_batch(adapter, inputs, response_path, narrated)
                        break
                    except (PrecheckError, ProviderError) as error:
                        final = attempt >= self.settings.llm_max_attempts or (
                            isinstance(error, ProviderError) and not error.retryable
                        )
                        if final:
                            raise
                        logger.warning(
                            "读音检查第 %s 批第 %s 次失败，重新请求：%s",
                            batch["batch_index"] + 1,
                            attempt,
                            error,
                        )
                speech_text_by_index = result.speech_text_by_segment
                for row in batch_segments:
                    speech_text = speech_text_by_index.get(row["segment_index"])
                    if speech_text is None:
                        raise BookTtsError(
                            f"LLM 未返回第 {row['segment_index'] + 1} 个分段的朗读稿"
                        )
                    self.database.update_segment(row["id"], speech_text=speech_text)
                self.database.delete_llm_hints_for_segments([row["id"] for row in batch_segments])
                segment_by_id = {row["id"]: row for row in batch_segments}
                for hint in result.hints:
                    segment = segment_by_id[hint.segment_id]
                    self.database.insert_hint(
                        {
                            "book_id": book["id"],
                            "chapter_id": segment["chapter_id"],
                            "segment_id": hint.segment_id,
                            "surface": hint.surface,
                            "occurrence_index": hint.occurrence_index,
                            "pronunciation": hint.pronunciation,
                            "kind": hint.kind,
                            "confidence": hint.confidence,
                            "source": hint.source,
                        }
                    )
            except PrecheckError as error:
                content_path = Path(error.content_path) if error.content_path else None
                context = {
                    "segment_index": error.segment_index,
                    "surface": error.surface,
                    "source_text": error.source_text,
                    "response_path": str(response_path),
                    "content_path": str(content_path) if content_path else None,
                }
                self.database.update_precheck_batch(
                    batch["id"],
                    status="failed",
                    error_message=str(error),
                    response_excerpt=(
                        _read_excerpt(content_path)
                        if content_path and content_path.is_file()
                        else _read_excerpt(response_path)
                    ),
                    error_context_json=json.dumps(context, ensure_ascii=False),
                    finished_at=utc_now(),
                )
                location_row = next(
                    (
                        row
                        for row in batch_segments
                        if error.segment_index is not None
                        and row["segment_index"] == error.segment_index
                    ),
                    batch_segments[0],
                )
                raise BookTtsError(
                    f"执行窗口 {execution_id[:8]} · 第 {location_row['chapter_index'] + 1} 章 · "
                    f"分段 {location_row['segment_index'] + 1}：{error}"
                ) from error
            except ProviderError as error:
                message = str(error)
                if response_path.exists() and "原始响应已保存：" not in message:
                    message += f"；原始响应已保存：{response_path}"
                self.database.update_precheck_batch(
                    batch["id"],
                    status="failed",
                    error_message=message,
                    response_excerpt=_read_excerpt(response_path),
                    error_context_json=json.dumps(
                        {"response_path": str(response_path)}, ensure_ascii=False
                    ),
                    finished_at=utc_now(),
                )
                location_row = batch_segments[0]
                raise BookTtsError(
                    f"执行窗口 {execution_id[:8]} · 第 {location_row['chapter_index'] + 1} 章 · "
                    f"分段 {location_row['segment_index'] + 1}：{message}"
                ) from error
            except Exception as error:
                self.database.update_precheck_batch(
                    batch["id"],
                    status="failed",
                    error_message=str(error),
                    response_excerpt=_read_excerpt(response_path),
                    error_context_json=json.dumps(
                        {"response_path": str(response_path)}, ensure_ascii=False
                    ),
                    finished_at=utc_now(),
                )
                raise
            self.database.update_precheck_batch(
                batch["id"],
                status="completed",
                error_message=None,
                error_context_json=None,
                finished_at=utc_now(),
            )
            completed += 1
            self._set_progress(execution_id, "precheck", completed, len(selected_batches))

    @staticmethod
    def _precheck_batch(
        adapter: OpenRouterLLMAdapter,
        inputs: list[SegmentInput],
        response_path: Path,
        narrated: bool,
    ) -> PrecheckResult:
        """清理成朗读稿；讲述改写模式下再逐段改写，并在最终讲述稿上重新标注发音。"""
        result = adapter.check(inputs, diagnostic_path=response_path)
        if not narrated:
            return result
        stem = response_path.name.removesuffix(".response.txt")

        def narrate(segment: SegmentInput) -> tuple[int, str]:
            speech_text = result.speech_text_by_segment[segment.segment_index]
            if not speech_text.strip():
                return segment.segment_index, ""
            path = response_path.with_name(f"{stem}.narrate-{segment.segment_index + 1:03d}.response.txt")
            return segment.segment_index, adapter.narrate(speech_text, diagnostic_path=path)

        with ThreadPoolExecutor(max_workers=len(inputs)) as executor:
            narrated_texts = dict(executor.map(narrate, inputs))
        validate_speech_texts(dict(enumerate(inputs)), narrated_texts)
        hints = adapter.annotate(
            [
                replace(segment, text=narrated_texts[segment.segment_index])
                for segment in inputs
                if narrated_texts[segment.segment_index].strip()
            ],
            diagnostic_path=response_path.with_name(f"{stem}.hints.response.txt"),
        )
        return PrecheckResult(speech_text_by_segment=narrated_texts, hints=hints)

    def _run_synthesis(self, execution_id: str) -> None:
        execution = self._required_execution(execution_id)
        selected = self.database.get_execution_segments(execution_id)
        pending = [row for row in selected if row["status"] != "completed" or not row["audio_path"]]
        total = len(selected)
        completed = total - len(pending)
        self._set_progress(execution_id, "synthesize", completed, total)
        if not pending:
            return
        book = self._required_book(execution["book_id"])
        provider, voice = _book_tts_config(book)
        adapter = create_tts_adapter(provider, self.settings)
        # MiMo 使用克隆音色时，被内容审核拒绝的分段改由本地模型用同一参考音频生成
        takeover_adapter = (
            create_tts_adapter("mlx_audio", self.settings)
            if provider == "mimo"
            and CloneVoiceStore(self.settings.clone_voices_dir).get(voice) is not None
            else None
        )
        audio_dir = Path(book["source_path"]).parent / "segments"
        concurrency = max(1, self.settings.tts_concurrency)
        failures: list[str] = []
        for offset in range(0, len(pending), concurrency):
            self._check_pause(execution_id)
            wave = pending[offset : offset + concurrency]
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {
                    executor.submit(
                        self._synthesize_one,
                        provider,
                        adapter,
                        takeover_adapter,
                        row,
                        audio_dir,
                        voice,
                    ): row
                    for row in wave
                }
                for future in as_completed(futures):
                    row = futures[future]
                    try:
                        artifact, audio_provider, taken_over = future.result()
                    except Exception as error:
                        self.database.update_segment(
                            row["id"], status="failed", error_message=str(error)
                        )
                        failures.append(
                            f"第 {row['chapter_index'] + 1} 章分段 {row['segment_index'] + 1}：{error}"
                        )
                    else:
                        self.database.update_segment(
                            row["id"],
                            status="completed",
                            audio_path=str(artifact.path),
                            audio_provider=audio_provider,
                            audio_voice=voice if audio_provider else None,
                            audio_taken_over=int(taken_over),
                            duration_ms=artifact.duration_ms,
                            error_message=None,
                        )
                        completed += 1
                        self._set_progress(execution_id, "synthesize", completed, total)
            if failures:
                raise BookTtsError("；".join(failures[:5]))
        self._check_pause(execution_id)

    def _run_package(self, execution_id: str) -> None:
        execution = self._required_execution(execution_id)
        book = self._required_book(execution["book_id"])
        selected = self.database.get_execution_segments(execution_id)
        if any(row["status"] != "completed" or not row["audio_path"] for row in selected):
            missing = next(
                row
                for row in selected
                if row["status"] != "completed" or not row["audio_path"]
            )
            raise BookTtsError(
                f"第 {missing['chapter_index'] + 1} 章分段 {missing['segment_index'] + 1} 尚未生成语音，不能打包"
            )
        self._set_progress(execution_id, "package", 0, 1)
        packager = FfmpegM4bPackager()
        book_dir = Path(book["source_path"]).parent
        if execution["output_mode"] == "sample":
            self._check_pause(execution_id)
            chapters = [
                dict(chapter)
                for chapter in self.database.get_chapters(book["id"])
                if chapter["id"] in {row["chapter_id"] for row in selected}
            ]
            output_path = book_dir / "validation" / execution_id / "sample.m4b"
            output_path, _ = packager.package_sample(
                title=f"{book['title']} · 执行窗口样例",
                author=book["author"],
                chapters=chapters,
                segments=[dict(row) for row in selected],
                cover_path=Path(book["cover_path"]) if book["cover_path"] else None,
                working_dir=book_dir / "validation" / execution_id / "package",
                output_path=output_path,
            )
            self.database.update_execution(execution_id, sample_output_path=str(output_path))
            self._set_progress(execution_id, "package", 1, 1)
            return

        selected_ids = {row["id"] for row in selected}
        chapters = self.database.get_chapters(book["id"])
        output_count = 0
        skipped = []
        for chapter in chapters:
            self._check_pause(execution_id)
            chapter_segments = self.database.get_chapter_segments(chapter["id"])
            if not chapter_segments:
                continue
            if not all(row["id"] in selected_ids for row in chapter_segments):
                skipped.append(chapter["title"])
                continue
            if any(row["status"] != "completed" or not row["audio_path"] for row in chapter_segments):
                skipped.append(chapter["title"])
                continue
            previous = self.database.get_chapter_output(chapter["id"])
            self.database.upsert_chapter_output(
                chapter["id"],
                status="running",
                attempts=int(previous["attempts"] or 0) + 1 if previous else 1,
                error_message=None,
            )
            try:
                output_path, duration_ms = packager.package_chapter(
                    book_id=book["id"],
                    book_title=book["title"],
                    author=book["author"],
                    chapter=dict(chapter),
                    segments=[dict(row) for row in chapter_segments],
                    cover_path=Path(book["cover_path"]) if book["cover_path"] else None,
                    working_dir=book_dir / "package",
                    output_dir=self.settings.audiobooks_dir,
                )
            except Exception as error:
                self.database.upsert_chapter_output(
                    chapter["id"], status="failed", error_message=str(error)
                )
                raise BookTtsError(f"第 {chapter['chapter_index'] + 1} 章打包失败：{error}") from error
            self.database.upsert_chapter_output(
                chapter["id"],
                output_path=str(output_path),
                status="completed",
                duration_ms=duration_ms,
                error_message=None,
                completed_at=utc_now(),
            )
            output_count += 1
            self._set_progress(execution_id, "package", output_count, max(1, len(chapters)))
        if skipped:
            self.database.update_execution(
                execution_id,
                warning_message="当前范围未覆盖完整章节，暂未生成：" + "、".join(skipped[:5]),
            )
        self._set_progress(execution_id, "package", output_count, max(1, output_count))

    def _synthesize_one(
        self, provider: str, adapter, takeover_adapter, row, audio_dir: Path, voice: str
    ):
        """返回 (音频, 实际生成该音频的供应商, 是否本地接管)。"""
        hints = [dict(hint) for hint in self.database.get_hints(row["id"], active_only=True)]
        instruction = build_tts_instruction(hints)
        segment = SegmentInput(
            id=row["id"],
            chapter_id=row["chapter_id"],
            segment_index=row["segment_index"],
            text=row["speech_text"],
        )
        output_path = audio_dir / f"{row['global_index']:08d}.wav"
        # 读音检查后朗读稿为准；为空表示整段都是注释等不朗读的内容
        if not segment.text.strip():
            return write_silence(output_path), None, False
        first_attempt = int(row["attempts"] or 0) + 1
        if first_attempt > self.settings.tts_max_attempts:
            first_attempt = 1
        attempts_recorded = [int(row["attempts"] or 0)]
        try:
            artifact = self._synthesize_with_paragraph_retries(
                adapter, segment, instruction, voice, output_path, row, first_attempt,
                attempts_recorded,
            )
            return artifact, provider, False
        except ProviderError as error:
            if error.content_rejected and takeover_adapter is not None:
                artifact = self._synthesize_takeover(
                    takeover_adapter, segment, instruction, voice, output_path,
                    row, error, first_attempt, attempts_recorded,
                )
                return artifact, "mlx_audio", True
            raise

    def _synthesize_with_paragraph_retries(
        self, adapter, segment, instruction, voice, output_path: Path, row, first_attempt: int,
        attempts_recorded: list[int],
    ):
        """只重试失败的自然段，并保留分段级尝试次数记录。"""
        def synthesize_paragraph(paragraph: SegmentInput, paragraph_path: Path):
            for attempt in range(first_attempt, self.settings.tts_max_attempts + 1):
                attempts_recorded[0] = max(attempts_recorded[0], attempt)
                self.database.update_segment(
                    row["id"], status="running", attempts=attempts_recorded[0], error_message=None
                )
                try:
                    return adapter.synthesize(paragraph, instruction, voice, paragraph_path)
                except ProviderError as error:
                    if (
                        (not error.retryable and not error.content_rejected)
                        or attempt >= self.settings.tts_max_attempts
                    ):
                        raise
                    time.sleep(min(2 ** (attempt - first_attempt), 8))
            raise BookTtsError("TTS 未产生结果")

        return synthesize_with_pauses(segment, output_path, synthesize_paragraph)

    def _synthesize_takeover(
        self, takeover_adapter, segment, instruction, voice, output_path, row,
        rejection: ProviderError, first_attempt: int, attempts_recorded: list[int],
    ):
        logger.warning(
            "分段 %s 被 MiMo 内容审核拒绝，改用本地模型生成：%s", row["global_index"] + 1, rejection
        )
        try:
            return self._synthesize_with_paragraph_retries(
                takeover_adapter, segment, instruction, voice, output_path, row, first_attempt,
                attempts_recorded,
            )
        except ProviderError as error:
            raise ProviderError(
                f"MiMo 内容审核拒绝了该分段，改用本地模型也失败：{error}",
                retryable=error.retryable,
                response_path=error.response_path,
            ) from error

    def _validate_prerequisites(self, execution) -> None:
        selected = self.database.get_execution_segments(execution["id"])
        if execution["start_stage"] in {"synthesize", "package"}:
            batches = self.database.get_precheck_batches(execution["book_id"])
            selected_ids = {row["id"] for row in selected}
            missing = [
                batch["batch_index"] + 1
                for batch in batches
                if selected_ids.intersection(_batch_segment_ids(batch))
                and batch["status"] != "completed"
            ]
            if missing:
                raise BookTtsError(
                    "当前范围缺少发音检查：第 "
                    + "、".join(str(value) for value in missing[:8])
                    + " 批尚未完成"
                )
        if execution["start_stage"] == "package" and any(
            row["status"] != "completed" or not row["audio_path"] for row in selected
        ):
            raise BookTtsError("当前范围缺少已完成的语音分段，不能生成章节音频")

    def _stage_reached(self, execution, stage: str) -> bool:
        order = {"precheck": 0, "synthesize": 1, "package": 2}
        return order[execution["start_stage"]] <= order[stage] <= order[execution["end_stage"]]

    def _set_progress(self, execution_id: str, stage: str, current: int, total: int) -> None:
        self.database.update_execution(
            execution_id,
            current_stage=stage,
            current_item=current,
            current_total=total,
        )

    def _check_pause(self, execution_id: str) -> None:
        execution = self.database.get_execution(execution_id)
        if execution and execution["pause_requested"]:
            raise ExecutionPaused()

    def _refresh_book_status(self, book_id: str) -> None:
        chapters = self.database.get_chapters(book_id)
        outputs = self.database.get_chapter_outputs(book_id)
        output_by_chapter = {row["chapter_id"]: row for row in outputs}
        complete = bool(chapters) and all(
            output_by_chapter.get(chapter["id"]) is not None
            and output_by_chapter[chapter["id"]]["status"] == "completed"
            for chapter in chapters
        )
        output_path = None
        completed_outputs = [row for row in outputs if row["status"] == "completed"]
        if completed_outputs:
            output_path = str(Path(completed_outputs[0]["output_path"]).parent)
        self.database.update_book(
            book_id,
            status="completed" if complete else "ready",
            stage="package" if complete else "ready",
            output_path=output_path,
            error_message=None,
        )

    def _required_book(self, book_id: str):
        book = self.database.get_book(book_id)
        if book is None:
            raise BookTtsError(f"找不到书籍：{book_id}")
        return book

    def _required_execution(self, execution_id: str):
        execution = self.database.get_execution(execution_id)
        if execution is None:
            raise BookTtsError(f"找不到执行窗口：{execution_id}")
        return execution


def _book_speech_style(book) -> str:
    style = json.loads(book["config_json"] or "{}").get("speech_style")
    if style not in SPEECH_STYLES:
        raise BookTtsError(f"书籍的朗读稿风格配置无效：{style}")
    return style


def _book_tts_config(book) -> tuple[str, str]:
    config = json.loads(book["config_json"] or "{}")
    provider = config.get("tts_provider")
    voice = config.get("voice")
    if not isinstance(provider, str) or not provider:
        raise BookTtsError("书籍缺少 TTS 供应商配置")
    if not isinstance(voice, str) or not voice:
        raise BookTtsError("书籍缺少 TTS 音色配置")
    return provider, voice


def _batch_segment_ids(batch) -> list[int]:
    return [int(value) for value in json.loads(batch["segment_ids_json"])]


def _segments_for_batch(rows: list, batch) -> list:
    segment_ids = _batch_segment_ids(batch)
    by_id = {row["id"]: row for row in rows}
    missing = [segment_id for segment_id in segment_ids if segment_id not in by_id]
    if missing:
        raise BookTtsError(f"第 {batch['batch_index'] + 1} 批缺少分段：{missing[:5]}")
    return [by_id[segment_id] for segment_id in segment_ids]


def _build_precheck_batches(rows: list) -> list[list]:
    batches: list[list] = []
    segments_by_chapter: dict[int, list] = {}
    for row in rows:
        segments_by_chapter.setdefault(row["chapter_id"], []).append(row)
    for chapter_segments in segments_by_chapter.values():
        current: list = []
        current_chars = 0
        for row in chapter_segments:
            if current and current_chars + len(row["text"]) > 6000:
                batches.append(current)
                current = []
                current_chars = 0
            current.append(row)
            current_chars += len(row["text"])
        if current:
            batches.append(current)
    return batches


def _read_excerpt(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")[:4000]
