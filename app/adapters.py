import base64
import binascii
import json
import logging
import re
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import httpx

from .config import Settings
from .database import Database
from .domain.errors import PrecheckError, ProviderError
from .domain.segmenter import split_text
from .domain.text import content_overlap, sha256_text, to_chinese_quotes
from .domain.types import AudioArtifact, PrecheckResult, PronunciationHint, SegmentInput
from .mimo_limits import MimoRequestGate
from .voices import ClonedVoice, CloneVoiceStore


logger = logging.getLogger("book-tts.mimo")


class AudiobookPrechecker(Protocol):
    def check(self, segments: list[SegmentInput]) -> PrecheckResult: ...


class SpeechSynthesizer(Protocol):
    def synthesize(
        self,
        segment: SegmentInput,
        instruction: str,
        voice: str,
        output_path: Path,
    ) -> AudioArtifact: ...

    def synthesize_preview(self, text: str, voice: str, output_path: Path) -> Path: ...


def _chat_endpoint(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def _speech_endpoint(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/v1/audio/speech"


def _save_diagnostic(path: Path | None, content: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _content_diagnostic_path(response_path: Path | None) -> Path | None:
    # 外层响应和模型实际内容分开保存，避免调试时把两层 JSON 混为一谈。
    if response_path is None:
        return None
    return response_path.with_name(f"{response_path.stem}.content.txt")


def _diagnostic_suffix(
    response_path: Path | None,
    content_path: Path | None = None,
) -> str:
    paths = []
    if response_path is not None:
        paths.append(f"外层响应已保存：{response_path}")
    if content_path is not None:
        paths.append(f"待解析内容已保存：{content_path}")
    return f"；{'；'.join(paths)}" if paths else ""


def _compact_excerpt(content: str, limit: int = 240) -> str:
    excerpt = " ".join(content.split())
    return excerpt if len(excerpt) <= limit else excerpt[:limit] + "…"


def _tts_diagnostic_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.tts-response.txt")


# 口语改写后朗读稿约六成内容仍来自本段原文，错位到其他分段时通常只有一至两成
SPEECH_TEXT_MIN_OVERLAP = 0.3


_ARABIC_DIGIT = re.compile(r"[0-9０-９]+")


def validate_speech_texts(
    segments_by_position: dict[int, SegmentInput], speech_text_by_segment: dict[int, str]
) -> None:
    """朗读稿的共用校验：内置 LLM 流程和外部工具导入都经过这里。"""
    for segment in segments_by_position.values():
        speech_text = speech_text_by_segment[segment.segment_index]
        # 规则要求数字写成中文读法，TTS 直接读阿拉伯数字时年份等容易读错
        if match := _ARABIC_DIGIT.search(speech_text):
            context = speech_text[max(0, match.start() - 10) : match.end() + 6]
            raise PrecheckError(
                f"分段 {segment.segment_index} 的朗读稿还有阿拉伯数字（…{context}…），需要写成中文读法",
                segment_index=segment.segment_index,
            )
    _validate_speech_alignment(segments_by_position, speech_text_by_segment)


def _validate_speech_alignment(
    segments_by_position: dict[int, SegmentInput], speech_text_by_segment: dict[int, str]
) -> None:
    """模型漏掉或合并分段时会整体错位，朗读稿混入邻近分段的内容。口语改写会整体降低与原文的重合度，
    因此用相对比较：每段朗读稿与本段原文的重合度须高于与同批其他原文的重合度，且不低于绝对下限。
    空朗读稿表示整段都不适合朗读，不参与比较。"""
    for position, segment in segments_by_position.items():
        speech_text = speech_text_by_segment[segment.segment_index]
        if not speech_text.strip():
            continue
        scores = {
            other_position: content_overlap(other.text, speech_text)[1]
            for other_position, other in segments_by_position.items()
        }
        own = scores[position]
        closest = max(scores, key=scores.get)
        if own < SPEECH_TEXT_MIN_OVERLAP or scores[closest] > own:
            detail = f"来自本段原文 {own:.0%}"
            if closest != position:
                other = segments_by_position[closest]
                detail += f"，更接近分段 {other.segment_index}（{scores[closest]:.0%}）"
            raise PrecheckError(
                f"分段 {segment.segment_index} 的朗读稿与原文内容对不上（{detail}），"
                "模型可能把分段错位或合并了",
                segment_index=segment.segment_index,
            )

# 发音提示只保留通用 TTS 容易读错的四类
HINT_KINDS = ("多音字", "易读错字", "文言读音", "专有名词")

# 朗读稿规则正文同时用于 LLM 请求和外部工具（Codex skill）导出的规则文件，只在这里维护
CLEANUP_RULES = """speech_text 是交给 TTS 朗读给听众听的朗读稿，以听起来自然顺耳为准：删去不适合朗读的内容，把书面符号改成读法，正文内容和表达基本保持原文。

朗读稿规则：
1. 删除所有不适合朗读的内容：注释和脚注（例如“[2]《列宁全集》，第28卷，第96页。”）、注释标号（[1]、⑷、①、* 等）、文献出处和引用来源、页码、图表编号和“见图”“见表”、网址、页眉页脚，以及排版和识别残留（多余空格、乱码、明显的错别字）。
2. 原文中被注释块、分页或分段边界截断的句子，要把前后两部分接成完整通顺的句子。输入的分段在书中是连续的：跨分段的句子放在句子开头所在的分段里补全，后一个分段不再重复。第一个分段开头看不到前文的残句、最后一个分段结尾看不到后文的残句，保留原样，不要删除，也不要猜测补全。
3. 改写成适合听的表达：数字、年份、百分比、单位和符号（%、～、—、/ 等）写成中文读法；括号里的外文原名和影响听感的补充说明可以删去或改成自然的说法；过长的句子适当拆分。
4. 正文的叙述内容要完整保留：不得删减人物、事件、数字、观点和论证，不得编造原文没有的事实，不得改变原意和立场。
5. 按意思分成若干自然段，每段不超过三百字，自然段之间用一个空行分隔（朗读时会在自然段之间停顿换气）。
6. 如果整个分段都是注释等不适合朗读的内容，speech_text 返回空字符串。"""

CLEANUP_PROMPT = "请把下面的书稿分段整理成有声书朗读稿，并标注需要特别注意的发音。\n\n" + CLEANUP_RULES

HINT_RULES = """只标注“通用中文 TTS 模型很可能读错”的词，宁缺毋滥；大多数分段不需要任何提示，hints 返回空数组即可。
1. 只标注以下四类，kind 取对应值：
   - 多音字：多音字在此语境下不读最常见的读音，例如“鲜有”的鲜读 xiǎn、“伺机”的伺读 sì、“不胜枚举”的胜读 shèng。
   - 易读错字：生僻字或常被读错的字，例如“桎梏”“戕害”“毗邻”“内讧”。
   - 文言读音：文言、古语、成语中的特殊读音，例如“兄弟阋于墙”的阋读 xì、“纵横捭阖”。
   - 专有名词：人名、地名、机构名等中含有多音字或生僻字、且按常见读音会读错的，例如姓氏“单”读 shàn、“尉迟”读 yù chí。
2. 不要标注：所有字都按最常见读音朗读的词（包括马克思、列宁、莫斯科、张学良这类常见人名地名和译名）、普通常用词、数字和标点。
3. 同一分段内相同的词组只标注一次，occurrence_index 取它第一次出现的位置。
4. 只标注你确定读音的词，confidence 填 confirmed；拿不准的读音宁可不标，错误的提示比没有提示更糟。
5. 优先标注完整词组，不要把单字读音泛化到所有上下文。
6. pronunciation 使用带声调的拼音，按字逐个给出，例如 shàn xióng xìn。
7. occurrence_index 是 surface 在 speech_text 中从 0 开始的出现序号；如果同一词语出现多次，按从左到右计算。
8. surface 应尽量从 speech_text 中复制，不要输出解释或 Markdown。"""

NARRATION_RULES = """1. 用讲述者的口吻，像一位有经验的主讲人在给听众讲，而不是念书面文章。
2. 多用短句，把长句拆开；把书面、名词化的说法（例如“基于……的教训”“持……的态度”）改成口语的动词表达。
3. 每段适当用一两处设问或口语衔接来引出转折、因果和下文，比如“那么”“说白了”“问题在于”“为什么这么说？”，让听众跟得上思路；但不要油滑，不要加感叹和煽情，同一个意思不要重复说。
4. 保留关键人物的原话：书中引用的人物原话、电报、讲话里最能体现态度和语气的部分，用“他回电说：‘……’”这类方式保留为直接引语，保留原来的措辞；引文太长时只保留关键的一两句，其余部分转述。
5. 人物、事件、时间、数字、观点和论证都必须保留，不能编造，不能改变原意和立场；数字保持中文读法。
6. 开头或结尾如果是不完整的半句话，保持原样，不要补全或删除。
7. 按意思分成自然段，每段不超过三百字，自然段之间用一个空行分隔。只输出讲述稿本身，不要标题和说明。"""

NARRATION_PROMPT = (
    "把下面这段有声书朗读稿改写成适合“讲给人听”的讲述稿。\n\n要求：\n"
    + NARRATION_RULES
    + "\n\n朗读稿：\n"
)


def _comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _precheck_response_format(segment_count: int, *, with_speech: bool) -> dict[str, Any]:
    hint_schema = {
        "type": "object",
        "properties": {
            "surface": {"type": "string"},
            "occurrence_index": {"type": "integer", "minimum": 0},
            "pronunciation": {"type": "string"},
            "kind": {"type": "string", "enum": list(HINT_KINDS)},
            "confidence": {"type": "string", "enum": ["confirmed", "uncertain"]},
        },
        "required": ["surface", "occurrence_index", "pronunciation", "kind", "confidence"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "audiobook_precheck",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "segments": {
                        "type": "array",
                        "minItems": segment_count,
                        "maxItems": segment_count,
                        "items": {
                            "type": "object",
                            "properties": {
                                "segment_index": {"type": "integer", "minimum": 0},
                                **({"speech_text": {"type": "string"}} if with_speech else {}),
                                "hints": {"type": "array", "items": hint_schema},
                            },
                            "required": ["segment_index", *(["speech_text"] if with_speech else []), "hints"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["segments"],
                "additionalProperties": False,
            },
        },
    }


def _save_tts_response(path: Path, response: httpx.Response) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(response.text, encoding="utf-8")


SILENCE_MS = 300
# 自然段之间的换气停顿；分段结尾同样留出，章节拼接时段与段之间也有间隔
PARAGRAPH_PAUSE_MS = 700


def synthesize_with_pauses(
    segment: SegmentInput,
    output_path: Path,
    synthesize_paragraph: Callable[[SegmentInput, Path], AudioArtifact],
) -> AudioArtifact:
    """朗读稿按空行拆成自然段合成，每段后插入停顿。"""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", segment.text) if part.strip()]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pause_path = output_path.with_name(f"{output_path.stem}.pause.wav")
    paragraph_paths = [
        output_path.with_name(f"{output_path.stem}.p{index:02d}.wav")
        for index in range(1, len(paragraphs) + 1)
    ]
    try:
        write_silence(pause_path, PARAGRAPH_PAUSE_MS)
        for paragraph, paragraph_path in zip(paragraphs, paragraph_paths):
            synthesize_paragraph(replace(segment, text=paragraph), paragraph_path)
        sequence = [path for paragraph_path in paragraph_paths for path in (paragraph_path, pause_path)]
        # 拼接清单放在独立临时目录，避免并发分段互相覆盖
        with tempfile.TemporaryDirectory(dir=output_path.parent, prefix=f".{output_path.stem}-") as working_dir:
            _concatenate_audio(sequence, output_path, Path(working_dir))
    finally:
        for path in (pause_path, *paragraph_paths):
            path.unlink(missing_ok=True)
    return AudioArtifact(
        segment_id=segment.id, path=output_path, duration_ms=_probe_duration_ms(output_path)
    )


def write_silence(output_path: Path, duration_ms: int = SILENCE_MS) -> AudioArtifact:
    """写入与正常分段音频格式一致的静音；朗读稿为空的分段整段即为静音。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", str(duration_ms / 1000), "-sample_fmt", "s16", "-f", "wav", str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return AudioArtifact(segment_id=0, path=output_path, duration_ms=_probe_duration_ms(output_path))


def _normalize_audio(source: Path, destination: Path) -> None:
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-ar",
                "24000",
                "-ac",
                "1",
                "-sample_fmt",
                "s16",
                "-f",
                "wav",
                str(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise ProviderError("系统中没有找到 ffmpeg") from error
    except subprocess.CalledProcessError as error:
        raise ProviderError(f"TTS 音频转换失败：{error.stderr.strip()}") from error


def _ffconcat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def _concatenate_audio(parts: list[Path], destination: Path, working_dir: Path) -> None:
    if not parts:
        raise ProviderError("本地 TTS 没有产生可合并的音频")
    if len(parts) == 1:
        _normalize_audio(parts[0], destination)
        return
    manifest = working_dir / "parts.ffconcat"
    manifest.write_text(
        "\n".join(f"file '{_ffconcat_path(path)}'" for path in parts) + "\n",
        encoding="utf-8",
    )
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-ar",
                "24000",
                "-ac",
                "1",
                "-sample_fmt",
                "s16",
                "-f",
                "wav",
                str(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise ProviderError("系统中没有找到 ffmpeg") from error
    except subprocess.CalledProcessError as error:
        raise ProviderError(f"本地 TTS 分块音频合并失败：{error.stderr.strip()}") from error


def _is_wav(content: bytes) -> bool:
    return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WAVE"


def _save_mlx_diagnostic(
    path: Path,
    *,
    chunk_index: int,
    chunk_count: int,
    status_code: int | None,
    detail: str,
) -> None:
    payload = {
        "provider": "mlx_audio",
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "status_code": status_code,
        "detail": detail,
    }
    _save_diagnostic(path, json.dumps(payload, ensure_ascii=False, indent=2))


class OpenRouterLLMAdapter:
    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise ProviderError("未配置 LLM_API_KEY")
        self.settings = settings

    def check(
        self,
        segments: list[SegmentInput],
        diagnostic_path: Path | None = None,
    ) -> PrecheckResult:
        """第 1 步：批量清理成朗读稿并标注发音。需要看到前后分段才能接好被截断的句子。"""
        if not segments:
            return PrecheckResult(speech_text_by_segment={}, hints=[])
        segment_payload = [
            # 编号用批内序号 0..n-1，模型天然按从 0 开始计数，返回后再映射回章节内的真实分段。
            # 英文直引号在 JSON 输出里必须转义，模型漏转义时严格格式约束的服务商会卡死在空白输出，
            # 因此先换成中文引号，朗读稿本就允许调整标点
            {"segment_index": position, "text": to_chinese_quotes(segment.text)}
            for position, segment in enumerate(segments)
        ]
        prompt = (
            CLEANUP_PROMPT
            + "\n\n发音提示规则：\n"
            + HINT_RULES
            + """

必须严格返回以下 JSON 结构，不要使用 Markdown 代码块：
{"segments":[{"segment_index":0,"speech_text":"完整朗读稿","hints":[{"surface":"词组","occurrence_index":0,"pronunciation":"pinyin","kind":"多音字","confidence":"confirmed"}]}]}

输入分段：
"""
            + json.dumps(segment_payload, ensure_ascii=False)
        )
        result = self._request_json(
            prompt,
            system="你是有声书的朗读稿编辑，负责把书稿整理成适合朗读的文稿，并标注容易读错的发音。",
            response_format=_precheck_response_format(len(segments), with_speech=True),
            diagnostic_path=diagnostic_path,
        )
        return self._with_diagnostic(
            lambda: self._validate_result(result, segments), diagnostic_path
        )

    def narrate(self, speech_text: str, diagnostic_path: Path | None = None) -> str:
        """第 2 步（可选）：把单段朗读稿改写成讲述口吻。逐段请求时模型才会充分改写。"""
        content, _ = self._request_content(
            NARRATION_PROMPT + speech_text,
            system="你是有声书的讲述稿编辑，负责把书面文字改写成讲给听众听的讲述稿。",
            response_format=None,
            diagnostic_path=diagnostic_path,
        )
        return content.strip()

    def annotate(
        self,
        segments: list[SegmentInput],
        diagnostic_path: Path | None = None,
    ) -> list[PronunciationHint]:
        """第 3 步（讲述改写后）：在最终讲述稿上标注发音，segments 的 text 为讲述稿。"""
        if not segments:
            return []
        segment_payload = [
            {"segment_index": position, "text": segment.text}
            for position, segment in enumerate(segments)
        ]
        prompt = (
            "下面是有声书讲述稿，请标注需要特别注意的发音。\n\n发音提示规则：\n"
            + HINT_RULES.replace("speech_text", "讲述稿")
            + """

必须严格返回以下 JSON 结构，不要使用 Markdown 代码块：
{"segments":[{"segment_index":0,"hints":[{"surface":"词组","occurrence_index":0,"pronunciation":"pinyin","kind":"多音字","confidence":"confirmed"}]}]}

输入分段：
"""
            + json.dumps(segment_payload, ensure_ascii=False)
        )
        result = self._request_json(
            prompt,
            system="你是有声书的发音校对，负责标注讲述稿中容易读错的发音。",
            response_format=_precheck_response_format(len(segments), with_speech=False),
            diagnostic_path=diagnostic_path,
        )
        return self._with_diagnostic(
            lambda: self._validate_hints_result(result, segments), diagnostic_path
        )

    @staticmethod
    def _with_diagnostic(validate, diagnostic_path: Path | None):
        try:
            return validate()
        except PrecheckError as error:
            if diagnostic_path and not error.response_path:
                raise PrecheckError(
                    str(error) + f"；原始响应已保存：{diagnostic_path}",
                    segment_index=error.segment_index,
                    surface=error.surface,
                    source_text=error.source_text,
                    response_path=str(diagnostic_path),
                    content_path=error.content_path,
                ) from error
            raise

    def _request_json(
        self,
        prompt: str,
        *,
        system: str,
        response_format: dict[str, Any],
        diagnostic_path: Path | None,
    ) -> Any:
        content, payload = self._request_content(
            prompt, system=system, response_format=response_format, diagnostic_path=diagnostic_path
        )
        content_path = _content_diagnostic_path(diagnostic_path)
        finish_reason = payload["choices"][0].get("finish_reason")
        try:
            return json.loads(content)
        except json.JSONDecodeError as error:
            if finish_reason is None:
                provider = payload.get("provider") or "未知服务商"
                model = payload.get("model") or self.settings.llm_model
                detail = (
                    f"LLM 响应不完整：服务商 {provider}（模型 {model}）没有返回结束原因，"
                    f"JSON 在生成过程中中断（第 {error.lineno} 行第 {error.colno} 列）"
                )
            else:
                detail = (
                    f"LLM 返回内容不是有效 JSON（第 {error.lineno} 行第 {error.colno} 列，"
                    f"finish_reason={finish_reason}）"
                )
            detail += f"；服务商返回内容：{_compact_excerpt(content)}"
            detail += _diagnostic_suffix(diagnostic_path, content_path)
            raise PrecheckError(
                detail,
                response_path=str(diagnostic_path) if diagnostic_path else None,
                content_path=str(content_path) if content_path else None,
            ) from error

    def _request_content(
        self,
        prompt: str,
        *,
        system: str,
        response_format: dict[str, Any] | None,
        diagnostic_path: Path | None,
    ) -> tuple[str, dict[str, Any]]:
        """发送请求并取出 message.content，统一处理截断、空内容等输出问题。"""
        response = self._request(
            prompt, system=system, response_format=response_format, diagnostic_path=diagnostic_path
        )
        try:
            payload = self._response_payload(response)
        except PrecheckError as error:
            detail = str(error) + _diagnostic_suffix(diagnostic_path)
            raise PrecheckError(
                detail,
                response_path=str(diagnostic_path) if diagnostic_path else None,
            ) from error
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            detail = "LLM 响应缺少 choices[0].message.content"
            detail += _diagnostic_suffix(diagnostic_path)
            raise PrecheckError(
                detail,
                response_path=str(diagnostic_path) if diagnostic_path else None,
            ) from error

        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        content_path = _content_diagnostic_path(diagnostic_path)
        if isinstance(content, str):
            _save_diagnostic(content_path, content)
        if finish_reason == "length":
            detail = (
                "LLM 输出被截断（finish_reason=length），请减少预检批次或提高 "
                "LLM_MAX_COMPLETION_TOKENS"
            )
            detail += _diagnostic_suffix(diagnostic_path, content_path)
            raise PrecheckError(
                detail,
                response_path=str(diagnostic_path) if diagnostic_path else None,
                content_path=str(content_path) if content_path else None,
            )
        if not isinstance(content, str):
            detail = "LLM 的 message.content 不是字符串"
            detail += _diagnostic_suffix(diagnostic_path)
            raise PrecheckError(
                detail,
                response_path=str(diagnostic_path) if diagnostic_path else None,
            )
        if not content.strip():
            detail = "LLM 返回空内容"
            detail += _diagnostic_suffix(diagnostic_path, content_path)
            raise PrecheckError(
                detail,
                response_path=str(diagnostic_path) if diagnostic_path else None,
                content_path=str(content_path) if content_path else None,
            )
        return content, payload

    @staticmethod
    def _response_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise PrecheckError("LLM HTTP 响应不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise PrecheckError("LLM HTTP 响应不是 JSON 对象")
        return payload

    def _request(
        self,
        prompt: str,
        *,
        system: str,
        response_format: dict[str, Any] | None,
        diagnostic_path: Path | None = None,
    ) -> httpx.Response:
        request_payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_completion_tokens": self.settings.llm_max_completion_tokens,
            "stream": False,
            "provider": {
                "allow_fallbacks": self.settings.openrouter_allow_fallbacks,
                "require_parameters": True,
            },
        }
        if response_format is not None:
            request_payload["response_format"] = response_format
        # 同一模型不同服务商的量化精度和稳定性差异很大，可固定优先服务商并排除低精度版本
        provider_order = _comma_list(self.settings.openrouter_provider_order)
        if provider_order:
            request_payload["provider"]["order"] = provider_order
        quantizations = _comma_list(self.settings.openrouter_quantizations)
        if quantizations:
            request_payload["provider"]["quantizations"] = quantizations
        # 推理能力由模型决定；未配置时不发送参数，避免把非推理模型或强制推理模型绑定到同一模式。
        if self.settings.llm_reasoning_effort:
            request_payload["reasoning_effort"] = self.settings.llm_reasoning_effort
        try:
            response = httpx.post(
                _chat_endpoint(self.settings.llm_base_url),
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": self.settings.openrouter_http_referer,
                    "X-OpenRouter-Title": self.settings.openrouter_title,
                },
                json=request_payload,
                timeout=180,
            )
            _save_diagnostic(diagnostic_path, response.text)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            detail = f"LLM 请求失败（HTTP {status}）"
            response_excerpt = _compact_excerpt(error.response.text)
            if response_excerpt:
                detail += f"；服务商响应：{response_excerpt}"
            if diagnostic_path:
                detail += f"；原始响应已保存：{diagnostic_path}"
            raise ProviderError(
                detail,
                retryable=status == 429 or status >= 500,
                response_path=str(diagnostic_path) if diagnostic_path else None,
            ) from error
        except httpx.RequestError as error:
            raise ProviderError("LLM 网络请求失败", retryable=True) from error

    @staticmethod
    def _validate_result(payload: Any, segments: list[SegmentInput]) -> PrecheckResult:
        if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
            raise PrecheckError("LLM 发音预检结果缺少 segments 数组")

        expected = dict(enumerate(segments))
        received: set[int] = set()
        speech_text_by_segment: dict[int, str] = {}
        hints: list[PronunciationHint] = []
        for item in payload["segments"]:
            if not isinstance(item, dict) or not isinstance(item.get("segment_index"), int):
                raise PrecheckError("LLM 发音预检结果缺少有效 segment_index")
            position = item["segment_index"]
            if position not in expected or position in received:
                raise PrecheckError(
                    f"LLM 返回了未知或重复的 segment_index：{position}（本批有效范围 0～{len(segments) - 1}）"
                )
            received.add(position)
            segment = expected[position]
            segment_index = segment.segment_index
            speech_text = item.get("speech_text")
            if not isinstance(speech_text, str):
                raise PrecheckError(f"分段 {segment_index} 的 speech_text 不是字符串")
            speech_text_by_segment[segment_index] = speech_text.strip()
            hints.extend(_parse_hints(item.get("hints"), segment, speech_text))

        if received != set(expected):
            missing = [expected[position].segment_index for position in sorted(set(expected) - received)]
            raise PrecheckError(f"LLM 未返回全部分段的预检结果：{missing}")
        validate_speech_texts(expected, speech_text_by_segment)
        return PrecheckResult(
            speech_text_by_segment=speech_text_by_segment,
            hints=hints,
        )

    @staticmethod
    def _validate_hints_result(payload: Any, segments: list[SegmentInput]) -> list[PronunciationHint]:
        if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
            raise PrecheckError("LLM 发音标注结果缺少 segments 数组")
        expected = dict(enumerate(segments))
        received: set[int] = set()
        hints: list[PronunciationHint] = []
        for item in payload["segments"]:
            if not isinstance(item, dict) or not isinstance(item.get("segment_index"), int):
                raise PrecheckError("LLM 发音标注结果缺少有效 segment_index")
            position = item["segment_index"]
            if position not in expected or position in received:
                raise PrecheckError(
                    f"LLM 返回了未知或重复的 segment_index：{position}（本批有效范围 0～{len(segments) - 1}）"
                )
            received.add(position)
            hints.extend(_parse_hints(item.get("hints"), expected[position], expected[position].text))
        if received != set(expected):
            missing = [expected[position].segment_index for position in sorted(set(expected) - received)]
            raise PrecheckError(f"LLM 未返回全部分段的发音标注：{missing}")
        return hints


def _count_occurrences(text: str, surface: str) -> int:
    """按不重叠匹配计数，与手动编辑发音提示时的出现序号规则一致。"""
    count, start = 0, 0
    while (position := text.find(surface, start)) >= 0:
        count, start = count + 1, position + len(surface)
    return count


def _parse_hints(raw_hints: Any, segment: SegmentInput, speech_text: str) -> list[PronunciationHint]:
    segment_index = segment.segment_index
    if not isinstance(raw_hints, list):
        raise PrecheckError(f"分段 {segment_index} 的 hints 不是数组")
    hints: list[PronunciationHint] = []
    for raw_hint in raw_hints:
        if not isinstance(raw_hint, dict):
            raise PrecheckError(f"分段 {segment_index} 存在无效发音提示")
        surface = raw_hint.get("surface")
        pronunciation = raw_hint.get("pronunciation")
        kind = raw_hint.get("kind")
        confidence = raw_hint.get("confidence")
        occurrence_index = raw_hint.get("occurrence_index")
        if not all(isinstance(value, str) and value.strip() for value in (surface, pronunciation, kind)):
            raise PrecheckError(f"分段 {segment_index} 存在缺少文本字段的发音提示")
        if kind.strip() not in HINT_KINDS:
            raise PrecheckError(f"分段 {segment_index} 的发音提示类型无效：{kind}")
        if confidence not in {"confirmed", "uncertain"}:
            raise PrecheckError(f"分段 {segment_index} 的 confidence 无效")
        if not isinstance(occurrence_index, int) or occurrence_index < 0:
            raise PrecheckError(f"分段 {segment_index} 的 occurrence_index 无效")
        # 提示必须能在朗读稿中定位，否则既无法指导 TTS，也无法在页面上编辑
        if occurrence_index >= _count_occurrences(speech_text, surface.strip()):
            raise PrecheckError(
                f"分段 {segment_index} 的发音提示“{surface.strip()}”在朗读稿中找不到第 {occurrence_index + 1} 处",
                segment_index=segment_index,
                surface=surface.strip(),
            )
        hints.append(
            PronunciationHint(
                segment_id=segment.id,
                surface=surface.strip(),
                occurrence_index=occurrence_index,
                pronunciation=pronunciation.strip(),
                kind=kind.strip(),
                confidence=confidence,
            )
        )
    return hints


class MimoTtsAdapter:
    def __init__(self, settings: Settings):
        if not settings.mimo_api_key:
            raise ProviderError("未配置 MIMO_API_KEY")
        self.settings = settings
        self.clone_voices = CloneVoiceStore(settings.clone_voices_dir)

    def _post_audio(self, model: str, payload: dict[str, Any], output_path: Path) -> httpx.Response:
        # 项目配置一个 MiMo 账号；同模型跨进程共享限流，作用域不使用 API Key。
        scope = sha256_text(f"{self.settings.mimo_base_url.rstrip('/')}\0{model}")
        gate = MimoRequestGate(Database(self.settings.database_path), scope, self.settings.mimo_tts_rpm)
        deadline = time.monotonic() + self.settings.mimo_tts_rate_limit_wait_seconds
        diagnostic_path = _tts_diagnostic_path(output_path)
        last_response: httpx.Response | None = None
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = "MiMo TTS 限流等待超过上限，请稍后重试"
                if last_response is not None:
                    detail += f"；服务商响应：{_compact_excerpt(last_response.text)}"
                    detail += f"；原始响应已保存：{diagnostic_path}"
                raise ProviderError(
                    detail,
                    response_path=str(diagnostic_path) if last_response is not None else None,
                )
            wait = gate.acquire()
            if wait > 0:
                # 等待中重新读取共享状态，确保其他进程延长冷却后不会提前发送。
                time.sleep(min(wait, remaining, 1))
                continue
            attempt += 1
            started_at = datetime.now(timezone.utc).isoformat()
            started = time.monotonic()
            try:
                response = httpx.post(
                    _chat_endpoint(self.settings.mimo_base_url),
                    headers={
                        "Authorization": f"Bearer {self.settings.mimo_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=300,
                )
            except httpx.RequestError as error:
                self._record_request(
                    output_path, model, attempt, started_at, started,
                    network_error=type(error).__name__,
                )
                raise
            self._record_request(output_path, model, attempt, started_at, started, response=response)
            if response.status_code != 429:
                if response.is_success:
                    gate.succeeded()
                return response
            last_response = response
            _save_tts_response(diagnostic_path, response)
            try:
                retry_after = self._retry_after_seconds(response)
            except ProviderError as error:
                gate.cool_down(None)
                raise ProviderError(
                    f"{error}；原始响应已保存：{diagnostic_path}",
                    response_path=str(diagnostic_path),
                ) from error
            delay = gate.cool_down(retry_after)
            logger.warning(
                "MiMo 限流 model=%s attempt=%s cooldown=%.1fs request_id=%s diagnostic=%s",
                model, attempt, delay, response.headers.get("x-request-id"), diagnostic_path,
            )

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if value is None:
            return None
        value = value.strip()
        if value.isascii() and value.isdigit():
            return int(value)
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                raise ValueError("HTTP 日期缺少时区")
        except (TypeError, ValueError, OverflowError) as error:
            raise ProviderError(f"MiMo TTS 返回无效 Retry-After：{value}") from error
        return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())

    @staticmethod
    def _record_request(
        output_path: Path, model: str, attempt: int, started_at: str, started: float,
        *, response: httpx.Response | None = None, network_error: str | None = None,
    ) -> None:
        record = {
            "started_at": started_at,
            "model": model,
            "attempt": attempt,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "status_code": response.status_code if response is not None else None,
            "network_error": network_error,
            # 仅保存诊断需要的响应头，不保存 Key、Cookie、正文或参考音频。
            "headers": {
                key: value for key, value in response.headers.items()
                if key in {"date", "retry-after", "x-request-id", "request-id"}
                or key.startswith("x-ratelimit-")
            } if response is not None else {},
        }
        path = output_path.with_name(f"{output_path.stem}.tts-requests.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info(
            "MiMo 请求 model=%s attempt=%s status=%s elapsed=%.3fs request_id=%s network_error=%s",
            model, attempt, record["status_code"], record["elapsed_seconds"],
            record["headers"].get("x-request-id"), network_error,
        )

    def synthesize(
        self,
        segment: SegmentInput,
        instruction: str,
        voice: str,
        output_path: Path,
    ) -> AudioArtifact:
        return self._synthesize_text(
            segment_id=segment.id,
            text=segment.text,
            instruction=instruction,
            voice=voice,
            output_path=output_path,
        )

    def synthesize_preview(self, text: str, voice: str, output_path: Path) -> Path:
        """用和正式分段相同的请求链路生成试听音频。"""
        artifact = self._synthesize_text(
            segment_id=0,
            text=text,
            instruction="请以自然、清晰、连贯的有声书旁白方式朗读输入文本。",
            voice=voice,
            output_path=output_path,
        )
        return artifact.path

    def design_voice(self, description: str, text: str, output_path: Path) -> Path:
        """按文字描述生成一段朗读 text 的音频；关闭文本润色，保证音频内容与 text 逐字一致，可直接作为克隆参考。"""
        artifact = self._request_audio(
            segment_id=0,
            model=self.settings.mimo_tts_design_model,
            user_content=description,
            text=text,
            audio={"format": "wav", "optimize_text_preview": False},
            output_path=output_path,
        )
        return artifact.path

    def _synthesize_text(
        self,
        segment_id: int,
        text: str,
        instruction: str,
        voice: str,
        output_path: Path,
    ) -> AudioArtifact:
        cloned_voice = self.clone_voices.get(voice)
        if cloned_voice is None:
            model = self.settings.mimo_tts_model
            audio = {"format": "wav", "voice": voice}
        else:
            # 音色复刻：参考音频以 data URI 放进 voice 字段，不需要参考文本
            encoded = base64.b64encode(cloned_voice.audio_path.read_bytes()).decode("ascii")
            model = self.settings.mimo_tts_clone_model
            audio = {"format": "wav", "voice": f"data:audio/wav;base64,{encoded}"}
        return self._request_audio(
            segment_id=segment_id,
            model=model,
            user_content=instruction,
            text=text,
            audio=audio,
            output_path=output_path,
        )

    def _request_audio(
        self,
        *,
        segment_id: int,
        model: str,
        user_content: str,
        text: str,
        audio: dict[str, Any],
        output_path: Path,
    ) -> AudioArtifact:
        diagnostic_path = _tts_diagnostic_path(output_path)
        try:
            response = self._post_audio(
                model,
                {
                    "model": model,
                    "messages": [
                        {"role": "user", "content": user_content},
                        {"role": "assistant", "content": text},
                    ],
                    "audio": audio,
                },
                output_path,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            _save_tts_response(diagnostic_path, error.response)
            response_excerpt = _compact_excerpt(error.response.text)
            detail = f"MiMo TTS 请求失败（HTTP {status}）"
            if response_excerpt:
                detail += f"；服务商响应：{response_excerpt}"
            detail += f"；原始响应已保存：{diagnostic_path}"
            raise ProviderError(
                detail,
                retryable=status >= 500,
                response_path=str(diagnostic_path),
            ) from error
        except httpx.RequestError as error:
            raise ProviderError("MiMo TTS 网络请求失败", retryable=True) from error

        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            _save_tts_response(diagnostic_path, response)
            raise ProviderError(
                f"MiMo TTS 返回不是有效 JSON；原始响应已保存：{diagnostic_path}",
                retryable=True,
                response_path=str(diagnostic_path),
            ) from error

        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            logger.info("MiMo 用量 model=%s usage=%s", model, payload["usage"])

        try:
            encoded_audio = self._extract_audio_data(payload)
        except ProviderError as error:
            _save_tts_response(diagnostic_path, response)
            detail = f"{error}；原始响应已保存：{diagnostic_path}"
            raise ProviderError(
                detail,
                retryable=error.retryable,
                response_path=str(diagnostic_path),
                content_rejected=error.content_rejected,
            ) from error

        try:
            raw_audio = base64.b64decode(encoded_audio, validate=True)
        except (binascii.Error, ValueError) as error:
            _save_tts_response(diagnostic_path, response)
            raise ProviderError(
                f"MiMo TTS 的 audio.data 不是有效 Base64；原始响应已保存：{diagnostic_path}",
                retryable=True,
                response_path=str(diagnostic_path),
            ) from error
        if not raw_audio:
            _save_tts_response(diagnostic_path, response)
            raise ProviderError(
                f"MiMo TTS 返回了空音频；原始响应已保存：{diagnostic_path}",
                retryable=True,
                response_path=str(diagnostic_path),
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path = output_path.with_suffix(".raw-response")
        raw_path.write_bytes(raw_audio)
        try:
            _normalize_audio(raw_path, output_path)
            duration_ms = _probe_duration_ms(output_path)
        finally:
            raw_path.unlink(missing_ok=True)
        return AudioArtifact(segment_id=segment_id, path=output_path, duration_ms=duration_ms)

    @staticmethod
    def _extract_audio_data(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise ProviderError("MiMo TTS 返回不是 JSON 对象", retryable=True)
        if payload.get("error"):
            error = payload["error"]
            detail = json.dumps(error, ensure_ascii=False) if isinstance(error, dict) else str(error)
            raise ProviderError(
                f"MiMo TTS 服务商拒绝请求：{_compact_excerpt(detail)}",
                retryable=False,
            )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("MiMo TTS 返回缺少 choices", retryable=True)
        choice = choices[0] if isinstance(choices[0], dict) else None
        if choice and choice.get("finish_reason") == "content_filter":
            message = choice.get("message")
            detail = message.get("content") if isinstance(message, dict) else None
            suffix = f"：{_compact_excerpt(detail)}" if isinstance(detail, str) and detail.strip() else ""
            raise ProviderError(
                f"MiMo TTS 内容审核拒绝了当前分段{suffix}",
                retryable=False,
                content_rejected=True,
            )
        message = choice.get("message") if choice else None
        audio = message.get("audio") if isinstance(message, dict) else None
        encoded_audio = audio.get("data") if isinstance(audio, dict) else None
        if not isinstance(encoded_audio, str) or not encoded_audio:
            raise ProviderError(
                "MiMo TTS 返回缺少 choices[0].message.audio.data",
                retryable=True,
            )
        return encoded_audio



class MlxAudioTtsAdapter:
    def __init__(self, settings: Settings):
        if settings.mlx_audio_chunk_target_chars <= 0:
            raise ProviderError("MLX_AUDIO_CHUNK_TARGET_CHARS 必须大于 0")
        if settings.mlx_audio_chunk_max_chars < settings.mlx_audio_chunk_target_chars:
            raise ProviderError(
                "MLX_AUDIO_CHUNK_MAX_CHARS 不能小于 MLX_AUDIO_CHUNK_TARGET_CHARS"
            )
        self.settings = settings
        self.clone_voices = CloneVoiceStore(settings.clone_voices_dir)

    def synthesize(
        self,
        segment: SegmentInput,
        instruction: str,
        voice: str,
        output_path: Path,
    ) -> AudioArtifact:
        # 克隆模式不接受朗读指令，发音提示无法传给本地模型
        return self._synthesize_text(
            segment_id=segment.id,
            text=segment.text,
            voice=voice,
            output_path=output_path,
        )

    def synthesize_preview(self, text: str, voice: str, output_path: Path) -> Path:
        artifact = self._synthesize_text(
            segment_id=0,
            text=text,
            voice=voice,
            output_path=output_path,
        )
        return artifact.path

    def _synthesize_text(
        self,
        *,
        segment_id: int,
        text: str,
        voice: str,
        output_path: Path,
    ) -> AudioArtifact:
        cloned_voice = self.clone_voices.get(voice)
        if cloned_voice is None:
            raise ProviderError(
                f"找不到克隆音色 {voice}，请在配音设置中重新选择音色", retryable=False
            )
        chunks = split_text(
            text,
            target_chars=self.settings.mlx_audio_chunk_target_chars,
            max_chars=self.settings.mlx_audio_chunk_max_chars,
        )
        if not chunks:
            raise ProviderError("本地 TTS 输入文本为空")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostic_path = _tts_diagnostic_path(output_path)
        with tempfile.TemporaryDirectory(
            dir=output_path.parent,
            prefix=f".{output_path.stem}-mlx-",
        ) as temporary_dir:
            working_dir = Path(temporary_dir)
            parts: list[Path] = []
            for chunk_index, chunk in enumerate(chunks, start=1):
                part_path = working_dir / f"part-{chunk_index:04d}.wav"
                content = self._request_chunk(
                    text=chunk.text,
                    cloned_voice=cloned_voice,
                    chunk_index=chunk_index,
                    chunk_count=len(chunks),
                    diagnostic_path=diagnostic_path,
                )
                part_path.write_bytes(content)
                parts.append(part_path)
            _concatenate_audio(parts, output_path, working_dir)

        diagnostic_path.unlink(missing_ok=True)
        return AudioArtifact(
            segment_id=segment_id,
            path=output_path,
            duration_ms=_probe_duration_ms(output_path),
        )

    def _request_chunk(
        self,
        *,
        text: str,
        cloned_voice: ClonedVoice,
        chunk_index: int,
        chunk_count: int,
        diagnostic_path: Path,
    ) -> bytes:
        try:
            response = httpx.post(
                _speech_endpoint(self.settings.mlx_audio_base_url),
                headers={"Content-Type": "application/json"},
                # Qwen3-TTS Base 克隆模式：ref_audio 是 mlx-audio 所在机器上的文件路径，
                # 不能再带 voice/instruct，否则服务端会拒绝或放弃批处理
                json={
                    "model": self.settings.mlx_audio_tts_model,
                    "input": text,
                    "ref_audio": str(cloned_voice.audio_path.resolve()),
                    "ref_text": cloned_voice.ref_text,
                    "lang_code": self.settings.mlx_audio_language,
                    "response_format": "wav",
                    "stream": False,
                    "max_tokens": self.settings.mlx_audio_max_tokens,
                },
                timeout=300,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            detail = _compact_excerpt(error.response.text) or "服务未返回错误详情"
            _save_mlx_diagnostic(
                diagnostic_path,
                chunk_index=chunk_index,
                chunk_count=chunk_count,
                status_code=status,
                detail=detail,
            )
            raise ProviderError(
                f"mlx-audio 请求失败（分块 {chunk_index}/{chunk_count}，HTTP {status}）："
                f"{detail}；诊断信息已保存：{diagnostic_path}",
                retryable=status in {429, 503, 504},
                response_path=str(diagnostic_path),
            ) from error
        except httpx.RequestError as error:
            detail = f"{type(error).__name__}: {error}"
            _save_mlx_diagnostic(
                diagnostic_path,
                chunk_index=chunk_index,
                chunk_count=chunk_count,
                status_code=None,
                detail=detail,
            )
            raise ProviderError(
                f"无法连接 mlx-audio（{self.settings.mlx_audio_base_url}，"
                f"分块 {chunk_index}/{chunk_count}）；诊断信息已保存：{diagnostic_path}",
                retryable=True,
                response_path=str(diagnostic_path),
            ) from error

        content = response.content
        if not _is_wav(content):
            detail = _compact_excerpt(content.decode("utf-8", errors="replace"))
            _save_mlx_diagnostic(
                diagnostic_path,
                chunk_index=chunk_index,
                chunk_count=chunk_count,
                status_code=response.status_code,
                detail=detail or "服务返回了空音频",
            )
            raise ProviderError(
                f"mlx-audio 未返回有效 WAV（分块 {chunk_index}/{chunk_count}）；"
                f"诊断信息已保存：{diagnostic_path}",
                retryable=False,
                response_path=str(diagnostic_path),
            )
        return content


def create_tts_adapter(provider: str, settings: Settings) -> SpeechSynthesizer:
    if provider == "mimo":
        return MimoTtsAdapter(settings)
    if provider == "mlx_audio":
        return MlxAudioTtsAdapter(settings)
    raise ProviderError(f"不支持的 TTS 供应商：{provider}")


def _probe_duration_ms(path: Path) -> int:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return int(float(result.stdout.strip()) * 1000)
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as error:
        raise ProviderError(f"无法读取音频时长：{path.name}") from error
