import asyncio
import hashlib
import json
import logging
import secrets
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.background import BackgroundTask

from .adapters import create_tts_adapter
from .config import get_settings
from .database import Database
from .domain.errors import BookTtsError, ProviderError
from .domain.execution import resolve_execution_range
from .domain.packager import safe_name
from .domain.types import SPEECH_STYLES
from .voices import (
    TTS_PROVIDERS,
    CloneVoiceError,
    CloneVoiceStore,
    find_voice,
    provider_voices,
    voice_payload,
)
from .schemas import (
    ContinueRequest,
    ExecutionRequest,
    HintCreate,
    HintPatch,
    PauseRequest,
    SpeechStylePatch,
    TtsConfigPatch,
    VoiceDesignRequest,
    VoicePreviewRequest,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("book-tts.web")

settings = get_settings()
database = Database(settings.database_path)
database.initialize()
clone_voices = CloneVoiceStore(settings.clone_voices_dir)
BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="Book TTS", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.get_session_secret(),
    session_cookie="book_tts_session",
    max_age=60 * 60 * 24 * 14,
    same_site="lax",
    https_only=False,
)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def require_auth(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")


def _row_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _read_diagnostic_text(path: str | None) -> str | None:
    if not path:
        return None
    diagnostic_path = Path(path)
    if not diagnostic_path.is_file():
        return None
    return diagnostic_path.read_text(encoding="utf-8")


def _execution_payload(row) -> dict:
    payload = _row_dict(row)
    payload.pop("lock_token", None)
    payload.pop("lock_until", None)
    try:
        payload["selection"] = json.loads(payload.pop("selection_json") or "{}")
    except json.JSONDecodeError:
        payload["selection"] = {}
        payload.pop("selection_json", None)
    total = int(payload.get("current_total") or 0)
    current = int(payload.get("current_item") or 0)
    payload["progress"] = _percent(current, total)
    payload["has_sample"] = bool(payload.get("sample_output_path"))
    return payload


def _book_payload(book) -> dict:
    payload = _row_dict(book)
    cover_path = payload.pop("cover_path", None)
    payload["has_cover"] = bool(cover_path and Path(cover_path).is_file())
    payload.pop("lock_token", None)
    payload.pop("lock_until", None)
    payload.pop("source_path", None)
    payload.pop("source_sha256", None)
    payload.pop("run_until", None)
    payload.pop("stage_limit", None)
    payload.pop("stage_current", None)
    payload.pop("stage_total", None)
    payload["config"] = json.loads(payload.pop("config_json") or "{}")
    segments = database.get_segments(book["id"])
    counts: dict[str, int] = {}
    for segment in segments:
        counts[segment["status"]] = counts.get(segment["status"], 0) + 1
    payload["counts"] = counts
    payload["total_segments"] = len(segments)
    payload["total_characters"] = sum(len(segment["text"]) for segment in segments)
    batches = database.get_precheck_batches(book["id"])
    payload["precheck_count"] = len(batches)
    payload["precheck_completed"] = sum(batch["status"] == "completed" for batch in batches)
    payload["chapter_count"] = len(database.get_chapters(book["id"]))
    payload["chapters"] = _chapter_summaries(book["id"])
    payload["chapter_outputs"] = [_chapter_output_payload(row) for row in database.get_chapter_outputs(book["id"])]
    payload["executions"] = [_execution_payload(row) for row in database.get_executions(book["id"])]
    active = database.get_active_execution(book["id"])
    payload["active_execution"] = _execution_payload(active) if active else None
    payload["progress"] = _book_progress(payload)
    return payload


def _book_progress(book: dict) -> int:
    if book.get("status") == "completed":
        return 100
    if book.get("status") in {"uploaded", "parsing"}:
        return 5 if book.get("status") == "parsing" else 0
    active = book.get("active_execution")
    if active:
        return int(active.get("progress") or 0)
    if book.get("chapter_count"):
        completed = sum(output.get("status") == "completed" for output in book.get("chapter_outputs", []))
        return _percent(completed, book["chapter_count"])
    return 0


def _audio_voice_label(segment) -> str | None:
    """分段实际使用的供应商和音色名称，用于同一本书混用多个音色时区分。"""
    provider, voice_id = segment["audio_provider"], segment["audio_voice"]
    if not provider or not voice_id:
        return None
    provider_name = {"mimo": "MiMo", "mlx_audio": "本地模型"}[provider]
    voice = find_voice(provider, voice_id, clone_voices)
    return f"{provider_name} · {voice.name if voice else '已删除的音色'}"


def _chapter_summaries(book_id: str) -> list[dict]:
    chapters = database.get_chapters(book_id)
    result = []
    for chapter in chapters:
        segments = database.get_chapter_segments(chapter["id"])
        chapter_batches = [
            batch for batch in database.get_precheck_batches(book_id) if batch["chapter_id"] == chapter["id"]
        ]
        prechecked = sum(
            len(_batch_segment_ids(batch)) for batch in chapter_batches if batch["status"] == "completed"
        )
        audio_completed = sum(
            1 for segment in segments if segment["status"] == "completed" and segment["audio_path"]
        )
        failed = sum(segment["status"] == "failed" for segment in segments)
        taken_over = sum(
            bool(segment["audio_path"] and segment["audio_taken_over"]) for segment in segments
        )
        output = database.get_chapter_output(chapter["id"])
        if output and output["status"] == "completed":
            chapter_status = "completed"
        elif failed or (output and output["status"] == "failed"):
            chapter_status = "attention"
        elif audio_completed:
            chapter_status = "partial"
        else:
            chapter_status = "ready"
        result.append(
            {
                **_row_dict(chapter),
                "segment_count": len(segments),
                "prechecked_segments": prechecked,
                "precheck_count": len(chapter_batches),
                "precheck_completed": sum(batch["status"] == "completed" for batch in chapter_batches),
                "audio_completed": audio_completed,
                "failed_segments": failed,
                "taken_over_segments": taken_over,
                "status": chapter_status,
                "output": _row_dict(output) if output else None,
            }
        )
    return result


def _book_detail(book_id: str) -> dict:
    book = database.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    payload = _book_payload(book)
    payload["attempts"] = [_row_dict(attempt) for attempt in database.get_attempts(book_id)]
    return payload


def _execution_detail(execution_id: str) -> dict:
    execution = database.get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="找不到执行窗口")
    payload = _execution_payload(execution)
    segments = database.get_execution_segments(execution_id)
    payload["segments"] = [
        {
            **_row_dict(segment),
            "hints": [_row_dict(hint) for hint in database.get_hints(segment["id"])],
        }
        for segment in segments
    ]
    payload["book"] = _book_payload(database.get_book(execution["book_id"]))
    payload["problems"] = _execution_problems(execution, segments)
    return payload


def _execution_problems(execution, segments: list) -> list[dict]:
    problems = []
    for segment in segments:
        if segment["status"] == "failed" or segment["error_message"]:
            problems.append(
                {
                    "kind": "segment",
                    "chapter_index": segment["chapter_index"],
                    "segment_index": segment["segment_index"],
                    "message": segment["error_message"],
                    "text": segment["text"],
                }
            )
    for batch in database.get_precheck_batches(execution["book_id"]):
        if batch["status"] != "failed":
            continue
        selected_ids = set(_batch_segment_ids(batch))
        if not selected_ids.intersection({segment["id"] for segment in segments}):
            continue
        context = {}
        try:
            context = json.loads(batch["error_context_json"] or "{}")
        except json.JSONDecodeError:
            context = {}
        first = next((segment for segment in segments if segment["id"] in selected_ids), None)
        response_path = context.get("response_path") or batch["response_path"]
        content_path = context.get("content_path")
        problems.append(
            {
                "kind": "precheck",
                "chapter_index": first["chapter_index"] if first else None,
                "segment_index": context.get("segment_index"),
                "message": batch["error_message"],
                "surface": context.get("surface"),
                "source_text": context.get("source_text") or (first["text"] if first else None),
                "response_path": response_path,
                "content_path": content_path,
                "detail_path": response_path,
                "detail": _read_diagnostic_text(response_path),
                "content_detail": _read_diagnostic_text(content_path),
            }
        )
    if execution["error_message"] and not problems:
        problems.append({"kind": "window", "message": execution["error_message"]})
    return problems


def _preview_payload(book_id: str, request: ExecutionRequest) -> dict:
    book = database.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    if book["status"] in {"uploaded", "parsing"}:
        raise HTTPException(status_code=409, detail="书稿仍在本地解析，请稍候")
    if not database.get_chapters(book_id):
        raise HTTPException(status_code=409, detail="书稿尚未建立章节索引")
    try:
        resolved = resolve_execution_range(
            selection_mode=request.selection_mode,
            selection=request.model_dump(exclude_none=True),
            start_stage=request.start_stage,
            chapters=database.get_chapters(book_id),
            segments=database.get_segments(book_id),
            batches=database.get_precheck_batches(book_id),
        )
    except BookTtsError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    selected_segments = [
        row
        for row in database.get_segments(book_id)
        if resolved.start_sequence <= row["global_index"] <= resolved.end_sequence
    ]
    gaps = _prerequisite_gaps(book_id, request, selected_segments)
    complete_chapters = _complete_chapter_candidates(book_id, selected_segments)
    selected_ids = {row["id"] for row in selected_segments}
    selected_batches = [
        batch
        for batch in database.get_precheck_batches(book_id)
        if selected_ids.intersection(_batch_segment_ids(batch))
    ]
    precheck_completed_count = sum(batch["status"] == "completed" for batch in selected_batches)
    audio_completed_count = sum(
        1 for row in selected_segments if row["status"] == "completed" and row["audio_path"]
    )
    if request.start_stage == "package" and request.output_mode == "chapters" and not complete_chapters:
        gaps.append("当前范围没有可生成的完整章节，请先完成该章节的全部语音分段")
    return {
        **resolved.as_record(book_id, request.start_stage, request.end_stage, request.output_mode),
        "resolved_start_sequence": resolved.start_sequence + 1,
        "resolved_end_sequence": resolved.end_sequence + 1,
        "estimated_duration_seconds": max(1, round(resolved.estimated_duration_ms / 1000)),
        "precheck_requests": resolved.precheck_count,
        "tts_segments": resolved.segment_count,
        "prerequisite_gaps": gaps,
        "precheck_completed_count": precheck_completed_count,
        "audio_completed_count": audio_completed_count,
        "audio_total_count": len(selected_segments),
        "complete_chapter_count": len(complete_chapters),
        "can_start": not gaps,
        "book_status": book["status"],
    }


def _prerequisite_gaps(book_id: str, request: ExecutionRequest, selected_segments: list) -> list[str]:
    gaps: list[str] = []
    selected_ids = {row["id"] for row in selected_segments}
    if request.start_stage in {"synthesize", "package"}:
        batches = database.get_precheck_batches(book_id)
        missing = [
            batch["batch_index"] + 1
            for batch in batches
            if selected_ids.intersection(_batch_segment_ids(batch)) and batch["status"] != "completed"
        ]
        if missing:
            gaps.append("读音检查未完成：第 " + "、".join(str(value) for value in missing[:8]) + " 批")
    if request.start_stage == "package":
        missing_audio = [
            f"第 {row['chapter_index'] + 1} 章·分段 {row['segment_index'] + 1}"
            for row in selected_segments
            if row["status"] != "completed" or not row["audio_path"]
        ]
        if missing_audio:
            gaps.append("语音未完成：" + "、".join(missing_audio[:5]))
    return gaps


def _complete_chapter_candidates(book_id: str, selected_segments: list) -> list:
    selected_ids = {row["id"] for row in selected_segments}
    result = []
    for chapter in database.get_chapters(book_id):
        chapter_segments = database.get_chapter_segments(chapter["id"])
        if chapter_segments and all(
            row["id"] in selected_ids and row["status"] == "completed" and bool(row["audio_path"])
            for row in chapter_segments
        ):
            result.append(chapter)
    return result


def _execution_record(book_id: str, request: ExecutionRequest) -> dict:
    preview = _preview_payload(book_id, request)
    if preview["prerequisite_gaps"]:
        raise HTTPException(
            status_code=409,
            detail="；".join(preview["prerequisite_gaps"]),
        )
    if database.get_active_execution(book_id) is not None:
        raise HTTPException(status_code=409, detail="当前已有执行窗口正在运行或排队")
    return {
        "book_id": book_id,
        "selection_mode": request.selection_mode,
        "selection": request.model_dump(exclude_none=True),
        "resolved_start_sequence": preview["resolved_start_sequence"] - 1,
        "resolved_end_sequence": preview["resolved_end_sequence"] - 1,
        "actual_char_count": preview["actual_char_count"],
        "chapter_count": preview["chapter_count"],
        "precheck_count": preview["precheck_count"],
        "segment_count": preview["segment_count"],
        "estimated_duration_ms": preview["estimated_duration_ms"],
        "start_stage": request.start_stage,
        "end_stage": request.end_stage,
        "output_mode": request.output_mode,
        "warning_message": preview.get("warning_message"),
    }


def _percent(current: int, total: int) -> int:
    if total <= 0 or current <= 0:
        return 0
    if current >= total:
        return 100
    return max(1, int(current / total * 100))


def _chapter_output_payload(row) -> dict:
    payload = _row_dict(row)
    payload["has_file"] = bool(payload.get("output_path") and Path(payload["output_path"]).is_file())
    return payload


def _batch_segment_ids(batch) -> list[int]:
    try:
        return [int(value) for value in json.loads(batch["segment_ids_json"])]
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail="预检批次记录损坏") from error


def _validate_occurrence(text: str, surface: str, occurrence_index: int) -> None:
    positions: list[int] = []
    start = 0
    while True:
        position = text.find(surface, start)
        if position < 0:
            break
        positions.append(position)
        start = position + len(surface)
    if occurrence_index >= len(positions):
        raise HTTPException(status_code=422, detail="发音提示对应的朗读稿或出现序号无效")


def _queue_segment(segment_id: int) -> None:
    segment = database.get_segment(segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="找不到音频分段")
    database.reset_segment(segment_id)
    database.upsert_chapter_output(segment["chapter_id"], status="stale", error_message=None)
    database.update_book(segment["book_id"], status="ready", error_message=None)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    password = str(form.get("password") or "")
    if not secrets.compare_digest(password, settings.app_password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "密码不正确"},
            status_code=401,
        )
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("index.html", {"request": request})


def _provider_model(provider: str) -> str:
    if provider == "mimo":
        return settings.mimo_tts_model
    if provider == "mlx_audio":
        return settings.mlx_audio_tts_model
    raise RuntimeError(f"不支持的 TTS 供应商：{provider}")


def _default_voice(provider: str) -> str | None:
    if provider == "mimo":
        return settings.mimo_tts_voice
    if provider == "mlx_audio":
        # 本地模型只有用户创建的克隆音色，默认取最早创建的一个
        voices = clone_voices.list()
        return voices[0].id if voices else None
    raise RuntimeError(f"不支持的 TTS 供应商：{provider}")


def _validate_tts_config(provider: str, voice_id: str | None):
    if provider not in TTS_PROVIDERS:
        raise HTTPException(status_code=422, detail="请选择支持的 TTS 供应商")
    if not voice_id and provider == "mlx_audio":
        raise HTTPException(status_code=422, detail="请先新建一个克隆音色")
    voice = find_voice(provider, voice_id or "", clone_voices)
    if voice is None:
        raise HTTPException(status_code=422, detail="请选择当前供应商支持的音色")
    return voice


def _voice_preview_path(provider: str, voice_id: str, preview_text: str) -> Path:
    model = _provider_model(provider)
    cache_key = hashlib.sha256(
        f"{provider}\0{model}\0{voice_id}\0{preview_text}".encode("utf-8")
    ).hexdigest()[:24]
    return settings.data_dir / "voice-previews" / f"{cache_key}.wav"


@app.get("/books/{book_id}", response_class=HTMLResponse)
def book_page(book_id: str, request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    if database.get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    return templates.TemplateResponse("book.html", {"request": request, "book_id": book_id})


@app.get("/api/books")
def list_books(_: None = Depends(require_auth)):
    return {"books": [_book_payload(book) for book in database.list_books()]}


def _tts_defaults() -> dict:
    saved = database.get_tts_defaults()
    if saved is not None:
        return dict(saved)
    # 首次设置前使用部署配置；保存后的默认配音由数据库统一管理。
    return {"provider": settings.tts_default_provider, "voice": _default_voice(settings.tts_default_provider)}


@app.patch("/api/tts/defaults")
def patch_tts_defaults(payload: TtsConfigPatch, _: None = Depends(require_auth)):
    _validate_tts_config(payload.provider, payload.voice)
    database.save_tts_defaults(payload.provider, payload.voice)
    return {"defaults": _tts_defaults()}


@app.get("/api/tts/providers")
def list_tts_providers(_: None = Depends(require_auth)):
    if settings.tts_default_provider not in TTS_PROVIDERS:
        raise RuntimeError(
            f"TTS_DEFAULT_PROVIDER 不是支持的供应商：{settings.tts_default_provider}"
        )
    providers = []
    for provider, name in (("mimo", "MiMo"), ("mlx_audio", "本地 MLX Audio")):
        voices = provider_voices(provider, clone_voices)
        default_voice_id = _default_voice(provider)
        if default_voice_id is not None and find_voice(provider, default_voice_id, clone_voices) is None:
            raise RuntimeError(f"{provider} 默认音色不受支持：{default_voice_id}")
        providers.append(
            {
                "id": provider,
                "name": name,
                "model": _provider_model(provider),
                "default_voice": default_voice_id,
                "supports_clone": True,
                "voices": [voice_payload(voice) for voice in voices],
            }
        )
    return {"providers": providers, "defaults": _tts_defaults()}


@app.post("/api/tts/voice-preview")
def preview_tts_voice(payload: VoicePreviewRequest, _: None = Depends(require_auth)):
    voice = _validate_tts_config(payload.provider, payload.voice)
    output_path = _voice_preview_path(payload.provider, voice.id, voice.preview_text)
    if not output_path.is_file():
        try:
            adapter = create_tts_adapter(payload.provider, settings)
            adapter.synthesize_preview(voice.preview_text, voice.id, output_path)
        except ProviderError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
    return FileResponse(output_path, media_type="audio/wav", filename=f"voice-preview-{voice.id}.wav")


@app.post("/api/tts/clone-voices")
async def create_clone_voice(
    name: str = Form(...),
    ref_text: str = Form(...),
    file: UploadFile = File(...),
    _: None = Depends(require_auth),
):
    settings.clone_voices_dir.mkdir(parents=True, exist_ok=True)
    upload_path = settings.clone_voices_dir / f".upload-{uuid.uuid4().hex}{Path(file.filename or '').suffix}"
    total_bytes = 0
    try:
        with upload_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > 20 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="参考音频不能超过 20 MB")
                output.write(chunk)
        voice = clone_voices.create(name, ref_text, upload_path)
    except CloneVoiceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        upload_path.unlink(missing_ok=True)
        await file.close()
    return {"voice": voice_payload(voice.option("mlx_audio"))}


@app.post("/api/tts/voice-design")
def design_tts_voice(payload: VoiceDesignRequest, _: None = Depends(require_auth)):
    """用 MiMo 按描述生成一段样音，只返回音频不保存；满意后由前端作为参考音频创建克隆音色。"""
    output_path = settings.data_dir / "voice-designs" / f"{uuid.uuid4().hex}.wav"
    try:
        adapter = create_tts_adapter("mimo", settings)
        adapter.design_voice(payload.description, payload.text, output_path)
        content = output_path.read_bytes()
    except ProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    finally:
        output_path.unlink(missing_ok=True)
    return Response(content, media_type="audio/wav")


@app.delete("/api/tts/clone-voices/{voice_id}")
def delete_clone_voice(voice_id: str, _: None = Depends(require_auth)):
    defaults = database.get_tts_defaults()
    if defaults is not None and defaults["voice"] == voice_id:
        raise HTTPException(status_code=409, detail="这个音色用于新书默认配音，请先更换默认配音")
    using_titles = [
        book["title"]
        for book in database.list_books()
        if json.loads(book["config_json"] or "{}").get("voice") == voice_id
    ]
    if using_titles:
        raise HTTPException(
            status_code=409,
            detail=f"以下书籍正在使用这个音色，请先更换配音：{'、'.join(using_titles)}",
        )
    try:
        clone_voices.delete(voice_id)
    except CloneVoiceError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"deleted": voice_id}


@app.post("/api/books")
async def upload_book(
    file: UploadFile = File(...),
    chapter_regex: str | None = Form(default=None),
    tts_provider: str | None = Form(default=None),
    voice: str | None = Form(default=None),
    speech_style: str = Form(...),
    _: None = Depends(require_auth),
):
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".epub", ".txt"}:
        raise HTTPException(status_code=422, detail="只支持 EPUB 和 TXT 文件")
    if not filename:
        raise HTTPException(status_code=422, detail="文件名不能为空")
    defaults = _tts_defaults()
    selected_provider = (
        tts_provider.strip()
        if tts_provider and tts_provider.strip()
        else defaults["provider"]
    )
    if selected_provider not in TTS_PROVIDERS:
        raise HTTPException(status_code=422, detail="请选择支持的 TTS 供应商")
    selected_voice = voice.strip() if voice and voice.strip() else (defaults["voice"] if selected_provider == defaults["provider"] else _default_voice(selected_provider))
    _validate_tts_config(selected_provider, selected_voice)
    if speech_style not in SPEECH_STYLES:
        raise HTTPException(status_code=422, detail="请选择朗读稿风格")

    book_id = uuid.uuid4().hex
    book_dir = settings.data_dir / "books" / book_id
    book_dir.mkdir(parents=True, exist_ok=False)
    source_path = book_dir / f"source{suffix}"
    digest = hashlib.sha256()
    total_bytes = 0
    try:
        with source_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > 500 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="单个文件不能超过 500 MB")
                digest.update(chunk)
                output.write(chunk)
    except Exception:
        source_path.unlink(missing_ok=True)
        book_dir.rmdir()
        raise
    finally:
        await file.close()

    source_sha256 = digest.hexdigest()
    duplicate = database.one("SELECT id FROM books WHERE source_sha256 = ?", (source_sha256,))
    if duplicate:
        source_path.unlink(missing_ok=True)
        book_dir.rmdir()
        return JSONResponse({"book_id": duplicate["id"], "duplicate": True})

    database.create_book(
        {
            "id": book_id,
            "source_filename": filename,
            "source_path": str(source_path),
            "source_sha256": source_sha256,
            "title": Path(filename).stem,
            "chapter_regex": chapter_regex.strip() if chapter_regex else None,
            "config": {
                "tts_provider": selected_provider,
                "voice": selected_voice,
                "speech_style": speech_style,
            },
        }
    )
    return {"book_id": book_id, "duplicate": False}


@app.get("/api/books/{book_id}")
def get_book(book_id: str, _: None = Depends(require_auth)):
    return _book_detail(book_id)


@app.patch("/api/books/{book_id}/tts-config")
def patch_book_tts_config(
    book_id: str,
    payload: TtsConfigPatch,
    _: None = Depends(require_auth),
):
    book = database.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    _validate_tts_config(payload.provider, payload.voice)
    current = json.loads(book["config_json"] or "{}")
    if (
        current.get("tts_provider") == payload.provider
        and current.get("voice") == payload.voice
    ):
        return {"book": _book_payload(book)}
    try:
        database.update_book_tts_config(book_id, payload.provider, payload.voice)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"book": _book_payload(database.get_book(book_id))}


@app.patch("/api/books/{book_id}/speech-style")
def patch_book_speech_style(
    book_id: str,
    payload: SpeechStylePatch,
    _: None = Depends(require_auth),
):
    """只影响之后的读音检查；已检查的章节需要重新检查才会换成新风格的朗读稿。"""
    book = database.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    config = json.loads(book["config_json"] or "{}")
    config["speech_style"] = payload.speech_style
    database.update_book(book_id, config_json=json.dumps(config, ensure_ascii=False))
    return {"book": _book_payload(database.get_book(book_id))}


@app.post("/api/books/{book_id}/reparse")
def reparse_book(book_id: str, _: None = Depends(require_auth)):
    book = database.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    if book["status"] in {"uploaded", "parsing", "executing"} or database.get_active_execution(book_id):
        raise HTTPException(status_code=409, detail="当前仍有任务在运行，不能重新识别正文")
    database.update_book(
        book_id,
        status="uploaded",
        stage="import",
        stage_current=0,
        stage_total=0,
        output_path=None,
        error_message=None,
    )
    return {"book": _book_payload(database.get_book(book_id))}


@app.get("/api/books/{book_id}/cover")
def get_book_cover(book_id: str, _: None = Depends(require_auth)):
    book = database.get_book(book_id)
    if book is None or not book["cover_path"]:
        raise HTTPException(status_code=404, detail="这本书没有封面")
    cover_path = Path(book["cover_path"])
    if not cover_path.is_file():
        raise HTTPException(status_code=404, detail="封面文件不存在")
    return FileResponse(cover_path)


@app.get("/api/books/{book_id}/events")
async def book_events(book_id: str, _: None = Depends(require_auth)):
    if database.get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="找不到书籍")

    async def stream():
        for _ in range(3600):
            book = database.get_book(book_id)
            if book is None:
                break
            yield f"data: {json.dumps(_book_payload(book), ensure_ascii=False)}\n\n"
            if book["status"] not in {"uploaded", "parsing", "executing"} and database.get_active_execution(book_id) is None:
                break
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/books/{book_id}/execution-preview")
def execution_preview(book_id: str, payload: ExecutionRequest, _: None = Depends(require_auth)):
    return _preview_payload(book_id, payload)


@app.post("/api/books/{book_id}/executions")
def create_execution(book_id: str, payload: ExecutionRequest, _: None = Depends(require_auth)):
    if database.get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    record = _execution_record(book_id, payload)
    execution_id = database.create_execution(record)
    return {"execution": _execution_payload(database.get_execution(execution_id))}


@app.get("/api/books/{book_id}/executions")
def list_executions(book_id: str, _: None = Depends(require_auth)):
    if database.get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    return {"executions": [_execution_payload(row) for row in database.get_executions(book_id)]}


@app.get("/api/executions/{execution_id}")
def get_execution(execution_id: str, _: None = Depends(require_auth)):
    return _execution_detail(execution_id)


@app.post("/api/executions/{execution_id}/pause")
def pause_execution(
    execution_id: str,
    payload: PauseRequest | None = None,
    _: None = Depends(require_auth),
):
    execution = database.get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="找不到执行窗口")
    if execution["status"] == "queued":
        database.update_execution(execution_id, status="paused", pause_requested=1)
    elif execution["status"] == "running":
        database.update_execution(execution_id, pause_requested=1)
    else:
        raise HTTPException(status_code=409, detail="当前窗口不在执行中")
    return {"execution": _execution_payload(database.get_execution(execution_id))}


@app.post("/api/executions/{execution_id}/retry")
def retry_execution(execution_id: str, _: None = Depends(require_auth)):
    execution = database.get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="找不到执行窗口")
    if execution["status"] not in {"failed", "paused"}:
        raise HTTPException(status_code=409, detail="只有失败或暂停的窗口可以重试")
    if database.get_active_execution(execution["book_id"]) is not None:
        raise HTTPException(status_code=409, detail="当前已有执行窗口正在运行或排队")
    for segment in database.get_execution_segments(execution_id):
        if segment["status"] == "failed":
            database.reset_segment(segment["id"])
    selected_ids = {segment["id"] for segment in database.get_execution_segments(execution_id)}
    for batch in database.get_precheck_batches(execution["book_id"]):
        if batch["status"] == "failed" and selected_ids.intersection(_batch_segment_ids(batch)):
            database.update_precheck_batch(batch["id"], status="queued", error_message=None)
    database.update_execution(
        execution_id,
        status="queued",
        pause_requested=0,
        error_message=None,
        current_item=0,
        current_total=0,
    )
    database.update_book(execution["book_id"], status="ready", error_message=None)
    return {"execution": _execution_payload(database.get_execution(execution_id))}


@app.post("/api/executions/{execution_id}/continue")
def continue_execution(
    execution_id: str,
    payload: ContinueRequest | None = None,
    _: None = Depends(require_auth),
):
    previous = database.get_execution(execution_id)
    if previous is None:
        raise HTTPException(status_code=404, detail="找不到执行窗口")
    if previous["status"] not in {"completed", "failed", "paused"}:
        raise HTTPException(status_code=409, detail="当前窗口仍在执行，不能继续下一窗口")
    if database.get_active_execution(previous["book_id"]) is not None:
        raise HTTPException(status_code=409, detail="当前已有执行窗口正在运行或排队")
    selection = json.loads(previous["selection_json"] or "{}")
    target = (payload.target_characters if payload else None) or selection.get("target_characters") or 5000
    if previous["selection_mode"] == "characters":
        request = ExecutionRequest(
            selection_mode="characters",
            target_characters=target,
            start_sequence=previous["resolved_end_sequence"] + 1,
            start_stage=previous["start_stage"],
            end_stage=previous["end_stage"],
            output_mode=previous["output_mode"],
        )
    elif previous["selection_mode"] == "chapters":
        old_start = int(selection.get("chapter_start") or 1)
        old_end = int(selection.get("chapter_end") or old_start)
        request = ExecutionRequest(
            selection_mode="chapters",
            chapter_start=old_end + 1,
            chapter_end=old_end + (old_end - old_start + 1),
            start_stage=previous["start_stage"],
            end_stage=previous["end_stage"],
            output_mode=previous["output_mode"],
        )
    else:
        request = ExecutionRequest(
            selection_mode="advanced",
            start_segment=previous["resolved_end_sequence"] + 2,
            end_segment=previous["resolved_end_sequence"] + 2,
            start_stage=previous["start_stage"],
            end_stage=previous["end_stage"],
            output_mode=previous["output_mode"],
        )
    record = _execution_record(previous["book_id"], request)
    execution_id = database.create_execution(record)
    return {"execution": _execution_payload(database.get_execution(execution_id))}


@app.get("/api/books/{book_id}/chapters")
def list_chapters(book_id: str, _: None = Depends(require_auth)):
    if database.get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    return {"chapters": _chapter_summaries(book_id)}


@app.get("/api/chapters/{chapter_id}/segments")
def list_chapter_segments(chapter_id: int, _: None = Depends(require_auth)):
    chapter = database.one("SELECT * FROM chapters WHERE id = ?", (chapter_id,))
    if chapter is None:
        raise HTTPException(status_code=404, detail="找不到章节")
    prechecked_ids = {
        segment_id
        for batch in database.get_precheck_batches(chapter["book_id"])
        if batch["status"] == "completed"
        for segment_id in _batch_segment_ids(batch)
    }
    segments = []
    for segment in database.get_chapter_segments(chapter_id):
        segment_payload = _row_dict(segment)
        segment_payload["taken_over"] = bool(segment["audio_path"] and segment["audio_taken_over"])
        segment_payload["audio_voice_label"] = (
            _audio_voice_label(segment) if segment["audio_path"] else None
        )
        audio_path = segment_payload.pop("audio_path", None)
        segment_payload["has_audio"] = bool(audio_path and Path(audio_path).is_file())
        segment_payload["prechecked"] = segment["id"] in prechecked_ids
        segments.append(
            {
                **segment_payload,
                "hints": [_row_dict(hint) for hint in database.get_hints(segment["id"])],
            }
        )
    return {"chapter": _row_dict(chapter), "segments": segments}


@app.get("/api/segments/{segment_id}/audio")
def get_segment_audio(segment_id: int, _: None = Depends(require_auth)):
    segment = database.get_segment(segment_id)
    if segment is None or not segment["audio_path"]:
        raise HTTPException(status_code=404, detail="这个分段还没有生成语音")
    audio_path = Path(segment["audio_path"])
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="分段语音文件不存在")
    return FileResponse(audio_path, media_type="audio/wav", filename=audio_path.name)


@app.patch("/api/hints/{hint_id}")
def patch_hint(hint_id: int, payload: HintPatch, _: None = Depends(require_auth)):
    hint = database.get_hint(hint_id)
    if hint is None:
        raise HTTPException(status_code=404, detail="找不到发音提示")
    segment = database.get_segment(hint["segment_id"])
    if segment is None:
        raise HTTPException(status_code=404, detail="找不到发音提示对应的分段")
    values = payload.model_dump(exclude_none=True)
    surface = values.get("surface", hint["surface"])
    occurrence_index = values.get("occurrence_index", hint["occurrence_index"])
    _validate_occurrence(segment["speech_text"], surface, occurrence_index)
    values["source"] = "manual"
    values["confidence"] = "confirmed"
    database.update_hint(hint_id, **values)
    _queue_segment(segment["id"])
    return {"hint": _row_dict(database.get_hint(hint_id))}


@app.post("/api/books/{book_id}/hints")
def create_hint(book_id: str, payload: HintCreate, _: None = Depends(require_auth)):
    book = database.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    segment = database.get_segment(payload.segment_id)
    if segment is None or segment["book_id"] != book_id:
        raise HTTPException(status_code=422, detail="segment_id 不属于当前书籍")
    _validate_occurrence(segment["speech_text"], payload.surface, payload.occurrence_index)
    hint_id = database.insert_hint(
        {
            "book_id": book_id,
            "chapter_id": segment["chapter_id"],
            "segment_id": segment["id"],
            "surface": payload.surface,
            "occurrence_index": payload.occurrence_index,
            "pronunciation": payload.pronunciation,
            "kind": payload.kind,
            "confidence": "confirmed",
            "source": "manual",
        }
    )
    _queue_segment(segment["id"])
    return {"hint_id": hint_id}


@app.post("/api/chapters/{chapter_id}/recheck")
def recheck_chapter(chapter_id: int, _: None = Depends(require_auth)):
    """重新检查整章读音：先校验能否提交窗口，再重置本章检查批次并提交只做读音检查的窗口。"""
    chapter = database.one("SELECT * FROM chapters WHERE id = ?", (chapter_id,))
    if chapter is None:
        raise HTTPException(status_code=404, detail="找不到章节")
    chapter_number = chapter["chapter_index"] + 1
    request = ExecutionRequest(
        selection_mode="chapters",
        chapter_start=chapter_number,
        chapter_end=chapter_number,
        start_stage="precheck",
        end_stage="precheck",
        output_mode="none",
    )
    record = _execution_record(chapter["book_id"], request)
    database.reset_chapter_precheck_batches(chapter_id)
    execution_id = database.create_execution(record)
    return {"execution": _execution_payload(database.get_execution(execution_id))}


@app.post("/api/chapters/{chapter_id}/regenerate")
def regenerate_chapter(chapter_id: int, _: None = Depends(require_auth)):
    """重新生成整章语音：先校验能否提交窗口，再清除本章全部分段音频，生成后自动重新合并章节文件。"""
    chapter = database.one("SELECT * FROM chapters WHERE id = ?", (chapter_id,))
    if chapter is None:
        raise HTTPException(status_code=404, detail="找不到章节")
    chapter_number = chapter["chapter_index"] + 1
    request = ExecutionRequest(
        selection_mode="chapters",
        chapter_start=chapter_number,
        chapter_end=chapter_number,
        start_stage="synthesize",
        end_stage="package",
        output_mode="chapters",
    )
    record = _execution_record(chapter["book_id"], request)
    for segment in database.get_chapter_segments(chapter_id):
        _queue_segment(segment["id"])
    execution_id = database.create_execution(record)
    return {"execution": _execution_payload(database.get_execution(execution_id))}


@app.post("/api/segments/{segment_id}/regenerate")
def regenerate_segment(segment_id: int, _: None = Depends(require_auth)):
    """重新生成单段语音：先校验能否提交窗口，再清除旧音频并提交只含这一段的语音窗口。"""
    segment = database.get_segment(segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="找不到音频分段")
    sequence = segment["global_index"] + 1
    request = ExecutionRequest(
        selection_mode="advanced",
        start_segment=sequence,
        end_segment=sequence,
        start_stage="synthesize",
        end_stage="synthesize",
        output_mode="none",
    )
    record = _execution_record(segment["book_id"], request)
    _queue_segment(segment_id)
    execution_id = database.create_execution(record)
    return {"execution": _execution_payload(database.get_execution(execution_id))}


@app.post("/api/segments/{segment_id}/retry")
def retry_segment(segment_id: int, _: None = Depends(require_auth)):
    _queue_segment(segment_id)
    return {"status": "queued"}


@app.get("/api/books/{book_id}/download")
def download_book(book_id: str, _: None = Depends(require_auth)):
    return _chapter_archive_response(book_id)


@app.post("/api/books/{book_id}/download")
def download_selected_chapters(
    book_id: str,
    chapter_ids: list[int] = Form(..., min_length=1),
    _: None = Depends(require_auth),
):
    return _chapter_archive_response(book_id, chapter_ids)


def _chapter_archive_response(book_id: str, chapter_ids: list[int] | None = None) -> FileResponse:
    book = database.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="找不到书籍")
    outputs = database.get_chapter_outputs(book_id)
    if chapter_ids is not None:
        selected = set(chapter_ids)
        chapters = database.get_chapters(book_id)
        if not selected.issubset({chapter["id"] for chapter in chapters}):
            raise HTTPException(status_code=404, detail="所选章节不属于这本书或已被删除")
        outputs = [output for output in outputs if output["chapter_id"] in selected]
        if len(outputs) != len(selected) or any(output["status"] != "completed" for output in outputs):
            raise HTTPException(status_code=409, detail="所选章节成品尚未全部生成，请刷新后重新选择")
    else:
        outputs = [output for output in outputs if output["status"] == "completed"]
    if not outputs:
        raise HTTPException(status_code=409, detail="还没有可下载的章节成品")

    paths = []
    for output in outputs:
        if not output["output_path"] or not Path(output["output_path"]).is_file():
            raise HTTPException(status_code=404, detail=f"第 {output['chapter_index'] + 1} 章成品文件不存在")
        paths.append(Path(output["output_path"]))

    temporary_dir = tempfile.TemporaryDirectory(prefix="book-tts-download-")
    archive_path = Path(temporary_dir.name) / "chapters.zip"
    try:
        # M4B 已压缩，直接存入临时 ZIP，避免整本书占用内存和重复压缩。
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for output, output_path in zip(outputs, paths):
                chapter_name = safe_name(output["chapter_title"], "chapter")
                archive.write(output_path, arcname=f"{output['chapter_index'] + 1:04d}-{chapter_name}.m4b")
    except Exception:
        temporary_dir.cleanup()
        raise
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"{safe_name(book['title'], 'book')}-章节成品.zip",
        background=BackgroundTask(temporary_dir.cleanup),
    )


@app.get("/api/chapter-outputs/{output_id}/download")
def download_chapter_output(output_id: int, _: None = Depends(require_auth)):
    output = database.one("SELECT * FROM chapter_outputs WHERE id = ?", (output_id,))
    if output is None or output["status"] != "completed" or not output["output_path"]:
        raise HTTPException(status_code=404, detail="章节成品尚未生成")
    output_path = Path(output["output_path"])
    if not output_path.is_file():
        raise HTTPException(status_code=404, detail="章节成品文件不存在")
    return FileResponse(output_path, media_type="audio/mp4", filename=output_path.name)


@app.get("/api/executions/{execution_id}/download")
def download_execution_sample(execution_id: str, _: None = Depends(require_auth)):
    execution = database.get_execution(execution_id)
    if execution is None or execution["status"] != "completed" or not execution["sample_output_path"]:
        raise HTTPException(status_code=404, detail="样例音频尚未生成")
    output_path = Path(execution["sample_output_path"])
    if not output_path.is_file():
        raise HTTPException(status_code=404, detail="样例音频文件不存在")
    return FileResponse(output_path, media_type="audio/mp4", filename=output_path.name)
