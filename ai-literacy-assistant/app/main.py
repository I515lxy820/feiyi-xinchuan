from contextlib import asynccontextmanager
import asyncio
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .answers import answers_match, classify_error, normalize_answer
from .config import BASE_DIR, Settings
from .db import Database
from .imaging import ImageValidationError, prepare_image
from .providers import ProviderError
from .question_io import question_hash, validate_record
from .schemas import (
    AnswerConfirm,
    FeedbackCreate,
    HealthResponse,
    ImportItemResult,
    ImportQuestion,
    ImportRequest,
    ImportResult,
    QuestionPublic,
    QuestionRecord,
    RunCreate,
    SolveResponse,
)
from .solver import Solver


STATIC_DIR = BASE_DIR / "static"
MAX_IMPORT_QUESTIONS = 50


def create_app(settings: Settings | None, solver: Solver | None = None) -> FastAPI:
    settings = settings or Settings()
    database = Database(settings.database_path)
    if solver is None:
        try:
            solver = Solver(settings)
        except ProviderError:
            solver = None
    solve_jobs: dict[str, dict] = {}
    solve_tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialize()
        yield
        for task in solve_tasks:
            task.cancel()
        if solve_tasks:
            await asyncio.gather(*solve_tasks, return_exceptions=True)
        if app.state.solver:
            await app.state.solver.close()

    app = FastAPI(title="知衡 AI 信息素养训练助手", version="1.0.0", lifespan=lifespan)
    app.state.solver = solver
    app.state.settings = settings
    app.state.database = database

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(ProviderError)
    async def provider_error_handler(request, exc: ProviderError):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    def require_solver() -> Solver:
        if app.state.solver is None:
            raise HTTPException(status_code=503, detail="模型或搜索配置不完整，请检查服务端环境变量")
        return app.state.solver

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok" if settings.model_configured and settings.search_configured else "needs_configuration",
            demo_mode=settings.demo_mode,
            model_provider=settings.model_provider,
            model_configured=settings.model_configured,
            search_provider=settings.search_provider,
            search_configured=settings.search_configured,
            prompt_version=settings.prompt_version,
        )

    async def prepare_solve_input(text: str, image: UploadFile | None) -> tuple[str, str | None]:
        question_text = text.strip()
        if len(question_text) > 12000:
            raise HTTPException(status_code=400, detail="题目文字不能超过 12000 字")
        if not question_text and image is None:
            raise HTTPException(status_code=400, detail="请粘贴题目或上传一张题目截图")
        image_data_url = None
        if image is not None:
            data = await image.read(5 * 1024 * 1024 + 1)
            await image.close()
            try:
                image_data_url = prepare_image(data)
            except ImageValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return question_text, image_data_url

    async def execute_solve(
        question_text: str, image_data_url: str | None, search_mode: Literal["auto", "off"]
    ) -> SolveResponse:
        response = await require_solver().solve(
            question_text, image_data_url, search_mode=search_mode
        )
        input_type = "mixed" if question_text and image_data_url else "image" if image_data_url else "text"
        response.attempt_id = database.record_attempt(
            response,
            input_type=input_type,
            model_version=settings.model_name or "mock",
            prompt_version=settings.prompt_version,
        )
        return response

    @app.post("/api/solve", response_model=SolveResponse)
    async def solve(
        text: str = Form(""),
        search_mode: Literal["auto", "off"] = Form("auto"),
        image: UploadFile | None = File(None),
    ) -> SolveResponse:
        question_text, image_data_url = await prepare_solve_input(text, image)
        return await execute_solve(question_text, image_data_url, search_mode)

    @app.post("/api/solve/jobs", status_code=202)
    async def create_solve_job(
        text: str = Form(""),
        search_mode: Literal["auto", "off"] = Form("auto"),
        image: UploadFile | None = File(None),
    ) -> dict[str, str]:
        question_text, image_data_url = await prepare_solve_input(text, image)
        require_solver()
        job_id = uuid4().hex
        solve_jobs[job_id] = {"status": "running", "created_at": time.time()}

        async def run() -> None:
            try:
                response = await execute_solve(question_text, image_data_url, search_mode)
                solve_jobs[job_id] = {
                    "status": "completed",
                    "created_at": time.time(),
                    "result": response.model_dump(mode="json"),
                }
            except ProviderError as exc:
                solve_jobs[job_id] = {"status": "failed", "created_at": time.time(), "detail": str(exc)}
            except Exception:
                solve_jobs[job_id] = {
                    "status": "failed",
                    "created_at": time.time(),
                    "detail": "解题服务暂时失败，请重试",
                }

        task = asyncio.create_task(run())
        solve_tasks.add(task)
        task.add_done_callback(solve_tasks.discard)
        return {"job_id": job_id, "status": "running"}

    @app.get("/api/solve/jobs/{job_id}")
    async def get_solve_job(job_id: str) -> dict:
        cutoff = time.time() - 3600
        for stale_id in [key for key, value in solve_jobs.items() if value["created_at"] < cutoff]:
            solve_jobs.pop(stale_id, None)
        job = solve_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="解题任务不存在或已过期")
        return {key: value for key, value in job.items() if key != "created_at"}

    @app.post("/api/practice/runs")
    async def create_practice_run(payload: RunCreate) -> dict[str, int]:
        run_id = database.create_run(
            payload.topic.strip()[:100], payload.wrong_only, f"{settings.model_name or 'mock'}:{settings.prompt_version}"
        )
        return {"run_id": run_id}

    @app.get("/api/practice/next", response_model=QuestionPublic | None)
    async def next_practice_question(
        run_id: int = Query(ge=1),
        topic: str = "",
        wrong_only: bool = False,
    ) -> QuestionPublic | None:
        question = database.next_question(run_id, topic.strip(), wrong_only)
        return QuestionPublic.model_validate(question) if question else None

    @app.get("/api/practice/{question_id}/image", include_in_schema=False)
    async def practice_image(question_id: int) -> FileResponse:
        question = database.get_question(question_id, include_answer=True)
        if not question or not question["image_path"]:
            raise HTTPException(status_code=404, detail="该题没有图片")
        path = Path(question["image_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="题目图片不存在")
        return FileResponse(path)

    @app.post("/api/practice/{question_id}/solve", response_model=SolveResponse)
    async def solve_practice_question(
        question_id: int,
        run_id: int = Query(ge=1),
        search_mode: Literal["auto", "off"] = "auto",
    ) -> SolveResponse:
        question = database.get_question(question_id, include_answer=True)
        if not question:
            raise HTTPException(status_code=404, detail="练习题不存在")
        image_data_url = None
        if question["image_path"]:
            try:
                image_data_url = prepare_image(Path(question["image_path"]).read_bytes())
            except (OSError, ImageValidationError) as exc:
                raise HTTPException(status_code=400, detail=f"题目图片不可用：{exc}") from exc
        response = await require_solver().solve(
            question["question_text"],
            image_data_url,
            options=question["options"],
            question_type=question["question_type"],
            search_mode=search_mode,
        )
        response.is_correct = answers_match(
            response.answer, question["answer"], question["question_type"]
        )
        response.correct_answer = question["answer"]
        response.answer_status = question["answer_status"]
        response.run_id = run_id
        response.error_category = classify_error(response)
        input_type = (
            "mixed" if question["question_text"] and image_data_url else "image" if image_data_url else "text"
        )
        response.attempt_id = database.record_attempt(
            response,
            question_id=question_id,
            run_id=run_id,
            input_type=input_type,
            is_correct=response.is_correct,
            error_category=response.error_category,
            model_version=settings.model_name or "mock",
            prompt_version=settings.prompt_version,
        )
        return response

    @app.post("/api/attempts/{attempt_id}/feedback")
    async def feedback(attempt_id: int, payload: FeedbackCreate) -> dict[str, int]:
        if not database.attempt_exists(attempt_id):
            raise HTTPException(status_code=404, detail="作答记录不存在")
        feedback_id = database.add_feedback(
            attempt_id,
            payload.correct,
            payload.corrected_answer.strip()[:2000],
            payload.note.strip()[:2000],
        )
        return {"feedback_id": feedback_id}

    @app.get("/api/mistakes")
    async def mistakes(topic: str = "") -> list[dict]:
        return database.list_mistakes(topic.strip())

    @app.post("/api/import/questions", response_model=ImportResult)
    async def import_questions(payload: ImportRequest) -> ImportResult:
        if not payload.questions:
            raise HTTPException(status_code=400, detail="请至少提供一道题目")
        if len(payload.questions) > MAX_IMPORT_QUESTIONS:
            raise HTTPException(
                status_code=400, detail=f"单次最多导入 {MAX_IMPORT_QUESTIONS} 道题"
            )

        semaphore = asyncio.Semaphore(3)
        seen: set[str] = set()

        def failure(
            text: str,
            message: str,
            question_type: str = "unknown",
            options: dict[str, str] | None = None,
        ) -> tuple[ImportItemResult, None]:
            return (
                ImportItemResult(
                    question_text=text,
                    question_type=question_type,
                    options=options or {},
                    status="failed",
                    message=message,
                ),
                None,
            )

        async def handle(item: ImportQuestion) -> tuple[ImportItemResult, QuestionRecord | None]:
            text = item.question_text.strip()
            options = {
                str(key).strip().upper(): str(value).strip()
                for key, value in item.options.items()
                if str(value).strip()
            }
            question_type = item.question_type
            answer = item.answer.strip()
            explanation = item.explanation.strip()
            digest = question_hash(text)

            if digest in seen:
                return (
                    ImportItemResult(
                        question_text=text,
                        status="skipped",
                        message="与本次导入中的另一题重复",
                    ),
                    None,
                )
            seen.add(digest)
            if database.question_exists(digest):
                return (
                    ImportItemResult(
                        question_text=text, status="skipped", message="题库中已存在相同题目"
                    ),
                    None,
                )

            if not answer:
                if not payload.auto_solve:
                    return failure(text, "未提供答案且未开启自动作答", question_type, options)
                if app.state.solver is None:
                    return failure(text, "模型未配置，无法自动作答，请手工提供答案", question_type, options)
                async with semaphore:
                    try:
                        solved = await app.state.solver.solve(
                            text,
                            options=options,
                            question_type=question_type,
                            search_mode=payload.search_mode,
                        )
                    except ProviderError as exc:
                        return failure(text, f"自动作答失败：{exc}", question_type, options)
                if not solved.answer:
                    return failure(text, "自动作答未返回答案，请手工提供答案", question_type, options)
                if solved.question_type != "unknown":
                    question_type = solved.question_type
                options = options or solved.options
                answer = solved.answer
                explanation = explanation or solved.explanation

            try:
                record = validate_record(
                    QuestionRecord(
                        question_text=text,
                        question_type=question_type,  # type: ignore[arg-type]
                        options=options,
                        answer=answer,
                        topic=item.topic.strip()[:100] or "导入题",
                        explanation=explanation,
                        source=item.source.strip()[:500],
                    )
                )
            except (ValidationError, ValueError) as exc:
                return failure(text, f"题目校验失败：{exc}", question_type, options)

            record.answer_status = "confirmed" if item.answer.strip() else "pending"
            return (
                ImportItemResult(
                    question_text=record.question_text,
                    question_type=record.question_type,
                    options=record.options,
                    answer=record.answer,
                    status="confirmed" if record.answer_status == "confirmed" else "pending",
                ),
                record,
            )

        pairs = await asyncio.gather(*(handle(item) for item in payload.questions))
        items = [pair[0] for pair in pairs]
        records = [
            (pair[1], question_hash(pair[1].question_text)) for pair in pairs if pair[1] is not None
        ]
        if records:
            database.insert_questions(records)
        return ImportResult(
            total=len(items),
            confirmed=sum(1 for item in items if item.status == "confirmed"),
            pending=sum(1 for item in items if item.status == "pending"),
            skipped=sum(1 for item in items if item.status == "skipped"),
            failed=sum(1 for item in items if item.status == "failed"),
            items=items,
        )

    @app.post("/api/questions/{question_id}/answer")
    async def confirm_question_answer(question_id: int, payload: AnswerConfirm) -> dict:
        question = database.get_question(question_id, include_answer=True)
        if not question:
            raise HTTPException(status_code=404, detail="题目不存在")
        answer = payload.answer.strip()
        question_type = question["question_type"]
        normalized = normalize_answer(answer, question_type)
        if question_type in {"single", "multiple"}:
            labels = normalized.split(",") if normalized else []
            if not labels or any(label not in question["options"] for label in labels):
                raise HTTPException(status_code=400, detail="答案必须是题目中的有效选项标签")
            if question_type == "single" and len(labels) != 1:
                raise HTTPException(status_code=400, detail="单选题只能填写一个选项标签")
        elif question_type == "boolean" and normalized not in {"正确", "错误"}:
            raise HTTPException(status_code=400, detail="判断题答案必须是“正确”或“错误”")
        updated = database.confirm_answer(question_id, answer)
        return {
            "id": question_id,
            "answer": updated["answer"],
            "answer_status": updated["answer_status"],
        }

    @app.get("/api/stats")
    async def stats() -> dict:
        return database.stats()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app(None)
