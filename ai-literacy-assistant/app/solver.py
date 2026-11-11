import asyncio
import re
import time
from urllib.parse import urlsplit, urlunsplit

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
from .web_reader import WebReader, build_web_reader


URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；、）】]+", flags=re.IGNORECASE)
URL_TRAILING_PUNCTUATION = ".,;:!?)]}，。；：！？、）】》"
PAGE_INTENT_PATTERN = re.compile(r"网站|网页|论文|题名|作者|年份|摘要")


class Solver:
    def __init__(
        self,
        settings: Settings,
        model: ModelProvider | None = None,
        search: SearchProvider | None = None,
        web_reader: WebReader | None = None,
    ):
        self.settings = settings
        self.model = model or build_model_provider(settings)
        self.search = search if search is not None else build_search_provider(settings)
        self.web_reader = (
            web_reader if web_reader is not None else build_web_reader(settings)
        )

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
        web_failed = False
        review_failed = False

        if draft.transcription_warning:
            self._append(warnings, f"题目识别不完整：{draft.transcription_warning}")

        combined_text = f"{question_text}\n{draft.question_text}"
        urls: list[str] = []
        for match in URL_PATTERN.finditer(combined_text):
            url = match.group(0).rstrip(URL_TRAILING_PUNCTUATION)
            if url and url not in urls:
                urls.append(url)
            if len(urls) >= self.settings.web_max_pages:
                break
        page_intent = bool(PAGE_INTENT_PATTERN.search(combined_text))
        should_search = draft.needs_search or (page_intent and not urls)
        external_attempted = should_search or page_intent or bool(urls)

        if search_mode == "auto":
            queries = draft.search_queries or [draft.question_text or question_text]
            search_results: list[Citation] = []
            tasks = []
            task_labels: list[tuple[str, str]] = []

            if should_search:
                if self.search is None:
                    search_failed = True
                    self._append(warnings, "未配置联网搜索，本题未完成外部来源核对")
                else:
                    tasks.append(self.search.search(queries))
                    task_labels.append(("search", ""))

            if urls:
                if self.web_reader is None:
                    web_failed = True
                    if self.settings.demo_mode:
                        message = "演示模式未执行真实公开网页读取"
                    elif not self.settings.web_reader_enabled:
                        message = "公开网页读取已关闭，本题未核对指定页面"
                    else:
                        message = "公开网页读取不可用，本题未核对指定页面"
                    self._append(warnings, message)
                else:
                    for url in urls:
                        tasks.append(self.web_reader.read(url))
                        task_labels.append(("web", url))

            if tasks:
                stage = time.perf_counter()
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                elapsed = round(time.perf_counter() - stage, 3)
                if should_search and self.search is not None:
                    timings["search"] = elapsed
                if urls and self.web_reader is not None:
                    timings["web"] = elapsed

                for (kind, url), outcome in zip(task_labels, outcomes):
                    if isinstance(outcome, asyncio.CancelledError):
                        raise outcome
                    if isinstance(outcome, BaseException):
                        if kind == "search":
                            search_failed = True
                            message = "联网核对失败"
                        else:
                            web_failed = True
                            message = f"公开网页读取失败（{url}）"
                        if isinstance(outcome, ProviderError):
                            self._append(warnings, f"{message}：{outcome}")
                        elif isinstance(outcome, Exception):
                            self._append(warnings, f"{message}，请人工打开来源核对")
                        else:
                            raise outcome
                        continue

                    for warning in outcome.warnings:
                        self._append(warnings, warning)
                    if kind == "search":
                        search_results.extend(outcome.results)
                        if not outcome.results:
                            search_failed = True
                            self._append(warnings, "未获得有效来源，本题未完成外部来源核对")
                    elif not outcome.results:
                        web_failed = True
                        self._append(warnings, f"公开网页未返回可用内容（{url}）")
                    sources.extend(outcome.results)

            if not urls and page_intent and search_results:
                candidate = None
                for item in search_results:
                    try:
                        if urlsplit(item.url).scheme.lower() in {"http", "https"}:
                            candidate = item
                            break
                    except ValueError:
                        continue
                if candidate is None:
                    web_failed = True
                    self._append(warnings, "搜索结果没有可读取的公开网页")
                elif self.web_reader is None:
                    web_failed = True
                    if self.settings.demo_mode:
                        message = "演示模式未执行真实公开网页读取"
                    elif not self.settings.web_reader_enabled:
                        message = "公开网页读取已关闭，本题未核对指定页面"
                    else:
                        message = "公开网页读取不可用，本题未核对指定页面"
                    self._append(warnings, message)
                else:
                    stage = time.perf_counter()
                    try:
                        outcome = await self.web_reader.read(candidate.url)
                        sources.extend(outcome.results)
                        for warning in outcome.warnings:
                            self._append(warnings, warning)
                        if not outcome.results:
                            web_failed = True
                            self._append(warnings, "公开网页未返回可用内容")
                    except ProviderError as exc:
                        web_failed = True
                        self._append(warnings, f"公开网页读取失败：{exc}")
                    timings["web"] = round(time.perf_counter() - stage, 3)

            ordered_sources = sorted(
                sources, key=lambda item: 0 if item.kind == "webpage" else 1
            )
            sources = []
            seen_urls: set[str] = set()
            webpage_count = 0
            search_count = 0
            for item in ordered_sources:
                if item.kind == "webpage" and webpage_count >= self.settings.web_max_pages:
                    continue
                if item.kind == "search" and search_count >= 5:
                    continue
                try:
                    parsed = urlsplit(item.url)
                    key = urlunsplit(
                        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
                    )
                except ValueError:
                    key = item.url
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                if item.kind == "webpage":
                    webpage_count += 1
                else:
                    search_count += 1
                item.rank = len(sources) + 1
                sources.append(item)

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
        elif search_mode == "off" and external_attempted:
            search_failed = True
            self._append(warnings, "联网核对已关闭，答案未经过外部来源验证")

        final.question_text = question_text or final.question_text or draft.question_text
        final.options = options or final.options or draft.options
        if final.question_type == "unknown" and question_type != "unknown":
            final.question_type = question_type
        if not final.explanation:
            final.explanation = draft.explanation
        final.answer = normalize_answer(final.answer, final.question_type)
        if external_attempted and not sources and (search_failed or web_failed):
            final.confidence = min(final.confidence, 0.49)

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
            or web_failed
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
