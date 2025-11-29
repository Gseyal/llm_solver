# app/config.py
import os
from functools import lru_cache
from pydantic import BaseModel
from dotenv import load_dotenv

# Load .env only if it exists (local development)
load_dotenv()

class Settings(BaseModel):
    student_email: str = os.getenv("STUDENT_EMAIL", "")
    student_secret: str = os.getenv("STUDENT_SECRET", "")

    llm_provider: str = os.getenv("LLM_PROVIDER", "gemini")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "models/gemini-2.5-flash")

    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    class Config:
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    settings = Settings()

    # Fail fast if important fields missing
    missing = []
    if not settings.student_email:
        missing.append("STUDENT_EMAIL")
    if not settings.student_secret:
        missing.append("STUDENT_SECRET")
    if settings.llm_provider == "gemini" and not settings.llm_api_key:
        missing.append("LLM_API_KEY")

    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    return settings
