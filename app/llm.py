# app/llm.py
import json
import logging
import re
from typing import Any, Dict, List

import google.generativeai as genai

from .config import get_settings

logger = logging.getLogger(__name__)


class LLMClient:
    """
    Wrapper around Gemini.

    Main method:
        solve_quiz(question_text, data_repr, kind) -> {"answer": "..."}
    """

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY is not configured")

        genai.configure(api_key=settings.llm_api_key)

        requested = settings.llm_model or "models/gemini-2.5-flash"
        self.model_name = self._pick_model(requested)
        logger.info("Using Gemini model: %s", self.model_name)

        # Ask model to reply in JSON
        self.model = genai.GenerativeModel(
            self.model_name,
            generation_config={
                "response_mime_type": "application/json",
            },
        )

    # ---------- model selection ----------

    def _pick_model(self, requested: str) -> str:
        """
        Choose a model that supports generateContent.

        Priority:
        1) requested (if valid)
        2) models/gemini-2.5-flash
        3) models/gemini-2.0-flash
        4) first model that supports generateContent
        """
        try:
            all_models: List[Any] = list(genai.list_models())
        except Exception as e:
            logger.warning("Could not list models; falling back to requested: %s", e)
            return requested

        gen_models = [
            m for m in all_models
            if "generateContent" in getattr(m, "supported_generation_methods", [])
        ]

        names = {m.name for m in gen_models}

        if requested in names:
            return requested

        for candidate in [
            "models/gemini-2.5-flash",
            "models/gemini-2.0-flash",
            "models/gemini-flash-latest",
        ]:
            if candidate in names:
                return candidate

        if gen_models:
            return gen_models[0].name

        # Last resort: just return requested, may 404 but we tried
        return requested

    # ---------- internal call ----------

    def _call_model(self, prompt: str) -> Dict[str, Any]:
        """
        Call Gemini and parse JSON from response.text.
        """
        resp = self.model.generate_content(prompt)
        text = (getattr(resp, "text", None) or "").strip()

        # First try plain JSON
        try:
            return json.loads(text)
        except Exception:
            pass

        # Try to extract JSON object from inside text
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass

        raise ValueError(f"LLM returned non-JSON: {text[:200]!r}")

    # ---------- public API ----------

    def solve_quiz(self, question_text: str, data_repr: str = "", kind: str = "text") -> Dict[str, Any]:
        """
        kind: "csv", "pdf_text", or "text"
        data_repr: CSV text / PDF text / extra context

        Returns a dict like:
            { "answer": "<string>" }
        """
        if kind == "csv":
            data_block = f"```csv\n{data_repr}\n```"
        else:
            data_block = f"```text\n{data_repr}\n```" if data_repr else "(no extra data)"

        prompt = f"""
You are solving an automatic data quiz.

1. Question and instructions:

\"\"\"text
{question_text}
\"\"\"


2. Extra data ({kind}):

{data_block}

Your task: compute the correct answer using ONLY this information.

Return a single JSON object EXACTLY in this format:

{{
  "answer": "<answer as a string>"
}}

No explanations, no other keys.
"""
        return self._call_model(prompt)
