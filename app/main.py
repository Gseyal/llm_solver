# app/main.py
import json
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from .config import get_settings
from .models import HealthResponse, SolveChainResponse, SolveRequest
from .solver import QuizSolver

logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(title="Smart Quiz Solver")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        email=settings.student_email,
        llm_provider=settings.llm_provider or "",
    )


@app.post("/solve", response_model=SolveChainResponse)
async def solve_quiz(request: Request) -> SolveChainResponse:
    """
    /solve:
    - 400 for invalid JSON or invalid/missing fields
    - 403 for invalid secret
    - 200 + SolveChainResponse for valid requests
    """
    # 1) Parse raw JSON → handle invalid JSON explicitly
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 2) Validate shape with Pydantic
    try:
        req = SolveRequest(**payload)
    except ValidationError as e:
        logger.warning("Invalid request payload: %s", e)
        raise HTTPException(status_code=400, detail="Invalid request payload")

    # 3) Secret check
    if req.secret != settings.student_secret:
        raise HTTPException(status_code=403, detail="Invalid secret")

    # 4) Delegate to solver
    solver = QuizSolver(
        student_email=settings.student_email,
        student_secret=settings.student_secret,
    )
    try:
        result = await solver.solve_chain(start_url=req.url)
        return SolveChainResponse(**result)
    finally:
        await solver.close()
