# app/models.py
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr


class SolveRequest(BaseModel):
    email: EmailStr
    secret: str
    url: str


class StepResult(BaseModel):
    step_url: str
    success: bool
    answer: Optional[str] = None
    details: Dict[str, Any]
    next_url: Optional[str] = None


class SolveChainResponse(BaseModel):
    success: bool
    total_steps: int
    steps: List[StepResult]


class HealthResponse(BaseModel):
    status: str
    email: str
    llm_provider: str
