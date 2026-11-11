import time

from .answers import normalize_answer
from .config import Settings
from .providers import (
    ModelProvider,
    ProviderError,
    SearchProvider,
    build_model_provider,
    build_search_provider,
)
from .schemas import Citation, DraftAnswer, SolveResponse


class Solver:
    def __init__(
        self,
        settings: Settings,
        model: ModelProvider | None = None,
        search: SearchProvider | None = None,
    ):
        self.settings = settings
        self.model = model or build_model_provider(settings)
        self.search = search if search is not None else build_search_provider(settings)

    @staticmethod
    def _append(warnings: list[str], message: str) -> None:
        if message and message not in warnings:
            warnings.append(message)

    async def solve(
        self,
        question_text: str = "",
        image_data_url: str | None = None,
        *,
        options: dict[str, str] | None = None,
        question_type: str = "unknown",
        search_mode: str = "auto",
    ) -> SolveResponse:
        started = time.perf_counter()
        options = options or {}
        warnings: list[str] = []
        if self.settings.demo_mode:
            warnings.append("当前为演示模式，答案与检索不能作为真实能力或准确率依据")

        stage = time.perf_counter()
        draft = await self.model.draft(question_text, image_data_url, options, question_type)
        timings = {"draft": round(time.perf_counter() - stage, 3)}
        final = draft.model_copy(deep=True)
        sources: list[Citation] = []
        search_failed = False
        review_failed = False

        if draft.transcription_warning:
            self._append(warnings, f"题目识别不完整：{draft.transcription_warning}")

        if search_mode == "auto" and draft.needs_search:
            queries = draft.search_queries or [draft.question_text or question_text]
            stage = time.perf_counter()
            if self.search is None:
                search_failed = True
                self._append(warnings, "未配置联网搜索，本题未完成外部来源核对")
            else:
                try:
                    outcome = await self.search.search(queries)
                    sources = outcome.results
                    for warning in outcome.warnings:
                        self._append(warnings, warning)
                    if not sources:
                        search_failed = True
                        self._append(warnings, "未获得有效来源，本题未完成外部来源核对")
                except ProviderError as exc:
                    search_failed = True
                    self._append(warnings, f"联网核对失败：{exc}")
            timings["search"] = round(time.perf_counter() - stage, 3)

            if sources:
                stage = time.perf_counter()
                try:
                    reviewed = await self.model.review(
                        draft.question_text or question_text, draft, sources
                    )
                    if normalize_answer(reviewed.answer, reviewed.question_type) != normalize_answer(
                        draft.answer, draft.question_type
                    ):
                        self._append(warnings, "联网复核改变了初答，请重点人工核对")
                    final = reviewed
                except ProviderError as exc:
                    review_failed = True
                    self._append(warnings, f"答案复核失败：{exc}")
                timings["review"] = round(time.perf_counter() - stage, 3)
        elif search_mode == "off" and draft.needs_search:
            search_failed = True
            self._append(warnings, "联网核对已关闭，答案未经过外部来源验证")

        final.question_text = question_text or final.question_text or draft.question_text
        final.options = options or final.options or draft.options
        if final.question_type == "unknown" and question_type != "unknown":
            final.question_type = question_type
        if not final.explanation:
            final.explanation = draft.explanation
        final.answer = normalize_answer(final.answer, final.question_type)

        if final.source_conflict:
            self._append(warnings, "检索来源相互冲突，请人工核对原文")
        if final.confidence < self.settings.confidence_threshold:
            self._append(warnings, "模型置信度较低，请人工核对")
        if not final.answer:
            self._append(warnings, "模型未能给出有效答案")
        if final.question_type in {"single", "multiple"} and final.options:
            labels = final.answer.split(",")
            if any(label not in final.options for label in labels):
                self._append(warnings, "答案与题目选项不匹配")

        needs_review = bool(
            self.settings.demo_mode
            or final.confidence < self.settings.confidence_threshold
            or final.transcription_warning
            or draft.transcription_warning
            or final.source_conflict
            or not final.answer
            or search_failed
            or review_failed
            or any("选项不匹配" in warning or "改变了初答" in warning for warning in warnings)
        )
        timings["total"] = round(time.perf_counter() - started, 3)
        return SolveResponse(
            question_text=final.question_text,
            question_type=final.question_type,
            options=final.options,
            answer=final.answer,
            explanation=final.explanation,
            confidence=final.confidence,
            needs_review=needs_review,
            sources=sources,
            warnings=warnings,
            timings=timings,
            demo_mode=self.settings.demo_mode,
        )

    async def close(self) -> None:
        close_model = getattr(self.model, "close", None)
        if close_model:
            await close_model()
        close_search = getattr(self.search, "close", None)
        if close_search:
            await close_search()
