from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .answers import answers_match, classify_error
from .config import BASE_DIR, Settings
from .db import Database
from .imaging import ImageValidationError, prepare_image
from .providers import ProviderError
from .schemas import FeedbackCreate, HealthResponse, QuestionPublic, RunCreate, SolveResponse
from .solver import Solver


STATIC_DIR = BASE_DIR / "static"


def create_app(settings: Settings | None, solver: Solver | None = None) -> FastAPI:
    settings = settings or Settings()
    database = Database(settings.database_path)
    if solver is None:
        try:
            solver = Solver(settings)
        except ProviderError:
            solver = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialize()
        yield
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

    @app.post("/api/solve", response_model=SolveResponse)
    async def solve(
        text: str = Form(""),
        search_mode: Literal["auto", "off"] = Form("auto"),
        image: UploadFile | None = File(None),
    ) -> SolveResponse:
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

    @app.get("/api/stats")
    async def stats() -> dict:
        return database.stats()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app(None)
