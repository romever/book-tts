from typing import Literal

from pydantic import BaseModel, Field, model_validator


class HintCreate(BaseModel):
    segment_id: int
    surface: str = Field(min_length=1)
    occurrence_index: int = Field(default=0, ge=0)
    pronunciation: str = Field(min_length=1)
    kind: str = Field(default="manual", min_length=1)


class HintPatch(BaseModel):
    surface: str | None = Field(default=None, min_length=1)
    occurrence_index: int | None = Field(default=None, ge=0)
    pronunciation: str | None = Field(default=None, min_length=1)
    kind: str | None = Field(default=None, min_length=1)
    active: bool | None = None


class VoicePreviewRequest(BaseModel):
    provider: Literal["mimo", "mlx_audio"]
    voice: str = Field(min_length=1)


class VoiceDesignRequest(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=200)


SpeechStyle = Literal["faithful", "narrated"]


class SpeechStylePatch(BaseModel):
    speech_style: SpeechStyle


class TtsConfigPatch(BaseModel):
    provider: Literal["mimo", "mlx_audio"]
    voice: str = Field(min_length=1)


StageName = Literal["precheck", "synthesize", "package"]
SelectionMode = Literal["chapters", "characters", "advanced"]
OutputMode = Literal["none", "sample", "chapters"]


class ExecutionRequest(BaseModel):
    selection_mode: SelectionMode = "characters"
    chapter_start: int | None = Field(default=None, ge=1)
    chapter_end: int | None = Field(default=None, ge=1)
    target_characters: int | None = Field(default=None, ge=1, le=10_000_000)
    start_batch: int | None = Field(default=None, ge=1)
    end_batch: int | None = Field(default=None, ge=1)
    start_segment: int | None = Field(default=None, ge=1)
    end_segment: int | None = Field(default=None, ge=1)
    start_sequence: int | None = Field(default=None, ge=0)
    start_stage: StageName = "precheck"
    end_stage: StageName = "precheck"
    output_mode: OutputMode = "none"

    @model_validator(mode="after")
    def validate_stage_and_output(self):
        order = {"precheck": 0, "synthesize": 1, "package": 2}
        if order[self.end_stage] < order[self.start_stage]:
            raise ValueError("结束阶段不能早于开始阶段")
        if self.output_mode == "sample" and self.end_stage != "package":
            raise ValueError("样例音频需要把结束阶段设为生成章节音频")
        if self.output_mode == "chapters" and self.end_stage != "package":
            raise ValueError("章节成品需要把结束阶段设为生成章节音频")
        if self.selection_mode == "chapters" and self.chapter_start is None:
            raise ValueError("按章节执行时必须填写开始章节")
        if self.selection_mode == "characters" and self.target_characters is None:
            raise ValueError("按字数执行时必须填写目标字数")
        if self.selection_mode == "advanced" and not any(
            value is not None
            for value in (
                self.start_batch,
                self.end_batch,
                self.start_segment,
                self.end_segment,
                self.chapter_start,
                self.chapter_end,
            )
        ):
            raise ValueError("高级范围必须填写批次、分段或章节范围")
        return self


class PauseRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class ContinueRequest(BaseModel):
    target_characters: int | None = Field(default=None, ge=1, le=10_000_000)
