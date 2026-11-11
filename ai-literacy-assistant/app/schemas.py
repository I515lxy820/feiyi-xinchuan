from typing import Literal

from pydantic import BaseModel, Field


QuestionType = Literal["single", "multiple", "boolean", "fill", "essay", "unknown"]
InputType = Literal["text", "image", "mixed"]


class Citation(BaseModel):
    title: str
    url: str
    snippet: str = ""
    rank: int = 1
    kind: Literal["search", "webpage"] = "search"
    page_text: str = Field(default="", exclude=True, repr=False)


class DraftAnswer(BaseModel):
    question_text: str = ""
    question_type: QuestionType = "unknown"
    options: dict[str, str] = Field(default_factory=dict)
    answer: str = ""
    explanation: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    needs_search: bool = True
    search_queries: list[str] = Field(default_factory=list)
    transcription_warning: str = ""
    source_conflict: bool = False


class SolveResponse(BaseModel):
    attempt_id: int | None = None
    run_id: int | None = None
    question_text: str
    question_type: QuestionType
    options: dict[str, str] = Field(default_factory=dict)
    answer: str
    explanation: str
    confidence: float = Field(ge=0, le=1)
    needs_review: bool
    sources: list[Citation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    demo_mode: bool = False
    is_correct: bool | None = None
    correct_answer: str | None = None
    error_category: str | None = None
    answer_status: Literal["confirmed", "pending"] | None = None


class QuestionRecord(BaseModel):
    question_text: str = Field(min_length=1)
    question_type: QuestionType
    options: dict[str, str] = Field(default_factory=dict)
    answer: str = Field(min_length=1)
    topic: str = "未分类"
    explanation: str = ""
    source: str = ""
    image_path: str = ""
    answer_status: Literal["confirmed", "pending"] = "confirmed"


class QuestionPublic(BaseModel):
    id: int
    question_text: str
    question_type: QuestionType
    options: dict[str, str]
    topic: str
    source: str = ""
    has_image: bool = False
    answer_status: Literal["confirmed", "pending"] = "confirmed"


class ImportQuestion(BaseModel):
    question_text: str = Field(min_length=1)
    question_type: QuestionType = "unknown"
    options: dict[str, str] = Field(default_factory=dict)
    answer: str = ""
    topic: str = "导入题"
    explanation: str = ""
    source: str = ""


class ImportRequest(BaseModel):
    questions: list[ImportQuestion]
    auto_solve: bool = True
    search_mode: Literal["auto", "off"] = "off"


class ImportItemResult(BaseModel):
    question_text: str
    question_type: QuestionType = "unknown"
    options: dict[str, str] = Field(default_factory=dict)
    answer: str = ""
    status: Literal["confirmed", "pending", "skipped", "failed"]
    message: str = ""


class ImportResult(BaseModel):
    total: int
    confirmed: int
    pending: int
    skipped: int
    failed: int
    items: list[ImportItemResult]


class AnswerConfirm(BaseModel):
    answer: str = Field(min_length=1)


class RunCreate(BaseModel):
    topic: str = ""
    wrong_only: bool = False


class FeedbackCreate(BaseModel):
    correct: bool
    corrected_answer: str = ""
    note: str = ""


class HealthResponse(BaseModel):
    status: str
    demo_mode: bool
    model_provider: str
    model_configured: bool
    search_provider: str
    search_configured: bool
    prompt_version: str
