# app/solver.py
import base64
import logging
import re
from io import BytesIO, StringIO
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import httpx
import numpy as np
import pandas as pd
import pdfplumber
from bs4 import BeautifulSoup
from pydub import AudioSegment

from .config import get_settings
from .llm import LLMClient

logger = logging.getLogger(__name__)


class QuizSolver:
    """
    Main engine:

    - Fetch HTML
    - Decode base64/atob embedded questions
    - Detect quiz type (data / audio / simple)
    - For data & text quizzes: try Gemini (if configured),
      but ALWAYS have deterministic fallbacks for CSV/XLSX/PDF.
    - Submit answer if instructions are present.
    """

    def __init__(self, student_email: str, student_secret: str) -> None:
        self.student_email = student_email
        self.student_secret = student_secret
        self.http_client = httpx.AsyncClient(timeout=30.0)

        settings = get_settings()
        self.llm: Optional[LLMClient] = None
        if settings.llm_provider == "gemini" and settings.llm_api_key:
            try:
                self.llm = LLMClient()
            except Exception:
                logger.exception("Failed to initialize LLMClient")

    async def close(self) -> None:
        await self.http_client.aclose()

    # ------------------------ fetch + atob ------------------------

    async def _fetch_html(self, url: str) -> str:
        resp = await self.http_client.get(url)
        resp.raise_for_status()
        return resp.text

    def _decode_embedded_atob(self, html: str) -> str:
        """
        For pages that use: document.innerHTML = atob(`....`);
        append the decoded content to the HTML so BeautifulSoup can see it.
        """
        m = re.search(r"atob\(([`'])([^`']+)\1\)", html)
        if not m:
            return html

        b64_raw = m.group(2)
        b64_clean = re.sub(r"\s+", "", b64_raw)

        try:
            decoded = base64.b64decode(b64_clean).decode("utf-8", errors="ignore")
            return html + "\n<!-- decoded_from_atob -->\n" + decoded
        except Exception as e:
            logger.warning("Failed to decode atob() payload: %s", e)
            return html

    # ------------------------ quiz type ------------------------

    def _detect_quiz_type(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        links = [a.get("href") or "" for a in soup.find_all("a")]

        for href in links:
            low = href.lower()
            if low.endswith((".csv", ".xlsx", ".pdf")):
                return "data"
            if low.endswith((".wav", ".mp3", ".ogg")):
                return "audio"

        # also check raw HTML (JS strings)
        if re.search(r'\.(csv|xlsx|pdf)["\']', html, flags=re.IGNORECASE):
            return "data"
        if re.search(r'\.(mp3|wav|ogg)["\']', html, flags=re.IGNORECASE):
            return "audio"

        return "simple"

    # ------------------------ audio ------------------------

    async def _solve_audio(self, html: str, base_url: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        candidates: List[str] = []

        for tag in soup.find_all(["audio", "a"]):
            src = tag.get("src") or tag.get("href")
            if not src:
                continue
            low = src.lower()
            if low.endswith((".mp3", ".wav", ".ogg")):
                candidates.append(urljoin(base_url, src))

        if not candidates:
            # raw HTML search
            for m in re.finditer(
                r'href=["\']([^"\']+\.(?:mp3|wav|ogg))["\']',
                html,
                flags=re.IGNORECASE,
            ):
                candidates.append(urljoin(base_url, m.group(1)))

        if not candidates:
            logger.warning("No audio links found")
            return "UNKNOWN"

        file_url = candidates[0]
        resp = await self.http_client.get(file_url)
        resp.raise_for_status()

        audio = AudioSegment.from_file(BytesIO(resp.content))
        duration_seconds = len(audio) / 1000.0
        return f"{duration_seconds:.2f}"

    # ------------------------ data helpers ------------------------

    async def _download_dataframe(self, file_url: str) -> pd.DataFrame:
        resp = await self.http_client.get(file_url)
        resp.raise_for_status()
        if file_url.lower().endswith(".csv"):
            return pd.read_csv(StringIO(resp.text))
        return pd.read_excel(BytesIO(resp.content))

    async def _extract_pdf_text(self, file_url: str) -> str:
        resp = await self.http_client.get(file_url)
        resp.raise_for_status()
        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            pages_text = []
            for page in pdf.pages:
                pages_text.append(page.extract_text() or "")
            return "\n\n".join(pages_text)

    def _parse_pdf_instruction(self, text: str) -> Tuple[Optional[str], Optional[int]]:
        """
        Parse instructions like:
          'What is the sum of the "value" column in the table on page 2?'
        Returns (column_name, page_number) or (None, None) if not matched.
        """
        pattern = r'sum of the\s+[“"\']([^”"\']+)[”"\']\s+column.*page\s+(\d+)'
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if not m:
            return None, None
        col_name = m.group(1).strip()
        page_num = int(m.group(2))
        return col_name, page_num

    async def _solve_pdf_data(self, html: str, base_url: str, file_url: str) -> str:
        """
        Deterministic PDF solver: find tables and sum the "value"-like column.
        Works even if LLM is down.
        """
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(" ", strip=True)

        col_name, page_num = self._parse_pdf_instruction(page_text)
        if not col_name:
            col_name = "value"  # fallback default

        logger.info("PDF target column: '%s', requested page: %s", col_name, page_num or "ANY")

        resp = await self.http_client.get(file_url)
        resp.raise_for_status()

        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            num_pages = len(pdf.pages)
            if num_pages == 0:
                return "UNKNOWN"

            values: List[float] = []

            # which pages to scan first
            page_indexes: List[int] = []
            if page_num and 1 <= page_num <= num_pages:
                page_indexes.append(page_num - 1)
            page_indexes.extend(i for i in range(num_pages) if i not in page_indexes)

            # table-based extraction
            for page_idx in page_indexes:
                page = pdf.pages[page_idx]
                tables = page.extract_tables() or []

                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    headers = [(h or "").strip().lower() for h in table[0]]

                    target_idx: Optional[int] = None
                    try:
                        target_idx = headers.index(col_name.lower())
                    except ValueError:
                        for i, h in enumerate(headers):
                            if "value" in h:
                                target_idx = i
                                break
                    if target_idx is None:
                        continue

                    for row in table[1:]:
                        if target_idx >= len(row):
                            continue
                        cell = row[target_idx]
                        if cell is None:
                            continue
                        cell_str = str(cell).strip().replace(",", "")
                        try:
                            v = float(cell_str)
                            values.append(v)
                        except ValueError:
                            continue

            # text-based fallback
            if not values:
                for page_idx in range(num_pages):
                    page = pdf.pages[page_idx]
                    text = page.extract_text() or ""
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    if not lines:
                        continue

                    header_tokens = lines[0].split()
                    header_lower = [h.lower() for h in header_tokens]

                    col_idx: Optional[int] = None
                    try:
                        col_idx = header_lower.index(col_name.lower())
                    except ValueError:
                        for i, h in enumerate(header_lower):
                            if "value" in h:
                                col_idx = i
                                break

                    if col_idx is None:
                        continue

                    for line in lines[1:]:
                        tokens = line.split()
                        if len(tokens) <= col_idx:
                            continue
                        cell_str = tokens[col_idx].replace(",", "")
                        try:
                            v = float(cell_str)
                            values.append(v)
                        except ValueError:
                            continue

            if not values:
                logger.warning("No numeric values found in PDF column '%s'", col_name)
                return "UNKNOWN"

            total = sum(values)
            if abs(total - round(total)) < 1e-9:
                return str(int(round(total)))
            return f"{total:.4f}"

    # ------------------------ LLM helper ------------------------

    def _llm_answer(self, question_text: str, data_repr: str, kind: str) -> Optional[str]:
        if not self.llm:
            return None
        try:
            result = self.llm.solve_quiz(question_text=question_text, data_repr=data_repr, kind=kind)
            ans = str(result.get("answer", "")).strip()
            return ans or None
        except Exception:
            logger.exception("LLM call failed")
            return None

    # ------------------------ data main ------------------------

    async def _solve_data(self, html: str, base_url: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        links = [a.get("href") or "" for a in soup.find_all("a")]

        file_url: Optional[str] = None
        file_ext: Optional[str] = None

        # first look at <a> tags
        for href in links:
            low = href.lower()
            if low.endswith((".csv", ".xlsx", ".pdf")):
                file_url = urljoin(base_url, href)
                if low.endswith(".csv"):
                    file_ext = "csv"
                elif low.endswith(".xlsx"):
                    file_ext = "xlsx"
                else:
                    file_ext = "pdf"
                break

        # fallback raw HTML search
        if not file_url:
            m = re.search(
                r'href=["\']([^"\']+\.(?:csv|xlsx|pdf))["\']',
                html,
                flags=re.IGNORECASE,
            )
            if m:
                href = m.group(1)
                file_url = urljoin(base_url, href)
                low = href.lower()
                if low.endswith(".csv"):
                    file_ext = "csv"
                elif low.endswith(".xlsx"):
                    file_ext = "xlsx"
                else:
                    file_ext = "pdf"

        if not file_url or not file_ext:
            logger.warning("No data file found on page")
            return "UNKNOWN"

        question_text = soup.get_text(" ", strip=True)

        # CSV / XLSX
        if file_ext in {"csv", "xlsx"}:
            df = await self._download_dataframe(file_url)
            if df.empty:
                return "0"

            csv_repr = df.to_csv(index=False)

            # 1) try LLM
            ans = self._llm_answer(question_text, csv_repr, kind="csv")
            if ans:
                return ans

            # 2) deterministic fallback (e.g., sum of numeric column)
            numeric_cols = df.select_dtypes(include=[np.number]).columns
            if len(numeric_cols) == 0:
                return "UNKNOWN"
            col = numeric_cols[0]
            series = df[col].dropna()

            text_lower = (question_text or "").lower()
            combined = text_lower + " " + html.lower()

            if "sum" in combined or "total" in combined:
                value = float(series.sum())
            elif "average" in combined or "mean" in combined:
                value = float(series.mean())
            elif "max" in combined or "maximum" in combined or "highest" in combined:
                value = float(series.max())
            elif "min" in combined or "minimum" in combined or "lowest" in combined:
                value = float(series.min())
            elif "count" in combined or "how many" in combined:
                value = float(series.count())
            else:
                value = float(len(df))

            if abs(value - round(value)) < 1e-9:
                return str(int(round(value)))
            return f"{value:.4f}"

        # PDF
        if file_ext == "pdf":
            pdf_text = await self._extract_pdf_text(file_url)

            # 1) try LLM with PDF text
            ans = self._llm_answer(question_text, pdf_text, kind="pdf_text")
            if ans:
                return ans

            # 2) deterministic fallback (tables)
            return await self._solve_pdf_data(html, base_url, file_url)

        return "UNKNOWN"

    # ------------------------ simple (LLM-based) ------------------------

    async def _solve_simple(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        question_text = soup.get_text(" ", strip=True)

        ans = self._llm_answer(question_text, data_repr="", kind="text")
        if ans:
            return ans

        # tiny heuristic fallback
        lower = question_text.lower()
        markers = ["answer is", "correct answer:", "the answer:", "answer:"]
        for m in markers:
            idx = lower.find(m)
            if idx != -1:
                after = question_text[idx + len(m):].strip()
                token = after.split()[0]
                return token.strip(".,;:()[]")
        return "UNKNOWN"

    # ------------------------ submission ------------------------

    async def _maybe_submit_answer(self, html: str, quiz_url: str, answer: str) -> Optional[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)

        m = re.search(r"Post your answer to\s+(https?://\S+)", text, flags=re.IGNORECASE)
        if not m:
            return None

        submit_url = m.group(1).rstrip(" .)")

        payload = {
            "email": self.student_email,
            "secret": self.student_secret,
            "url": quiz_url,
            "answer": answer,
        }

        try:
            resp = await self.http_client.post(submit_url, json=payload, timeout=10.0)
            result: Dict[str, Any] = {
                "submit_url": submit_url,
                "status_code": resp.status_code,
            }
            try:
                body_json = resp.json()
                result["response_json"] = body_json
                if isinstance(body_json, dict):
                    result["next_url"] = body_json.get("url")
                    result["correct"] = body_json.get("correct")
            except Exception:
                result["body_preview"] = resp.text[:200]
            return result
        except Exception as e:
            logger.exception("Error submitting answer to %s", submit_url)
            return {"submit_url": submit_url, "error": str(e)}

    # ------------------------ public API ------------------------

    async def solve_single_quiz(self, url: str) -> Dict[str, Any]:
        logger.info("Solving quiz at %s", url)
        try:
            raw_html = await self._fetch_html(url)
            html = self._decode_embedded_atob(raw_html)

            quiz_type = self._detect_quiz_type(html)
            logger.info("Detected quiz type: %s", quiz_type)

            if quiz_type == "data":
                answer = await self._solve_data(html, url)
            elif quiz_type == "audio":
                answer = await self._solve_audio(html, url)
            else:
                answer = await self._solve_simple(html)

            submission_result: Optional[Dict[str, Any]] = None
            next_url: Optional[str] = None

            if answer != "UNKNOWN":
                submission_result = await self._maybe_submit_answer(html, url, answer)
                if submission_result and isinstance(submission_result, dict):
                    next_url = submission_result.get("next_url")

            return {
                "step_url": url,
                "success": answer != "UNKNOWN",
                "answer": answer,
                "details": {
                    "quiz_type": quiz_type,
                    "submission": submission_result,
                },
                "next_url": next_url,
            }

        except Exception as e:
            logger.exception("Error while solving quiz at %s", url)
            return {
                "step_url": url,
                "success": False,
                "answer": None,
                "details": {"error": str(e)},
                "next_url": None,
            }

    async def solve_chain(self, start_url: str, max_steps: int = 10) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        current_url: Optional[str] = start_url
        steps = 0

        while current_url and steps < max_steps:
            step_result = await self.solve_single_quiz(current_url)
            results.append(step_result)
            steps += 1

            current_url = step_result.get("next_url")
            if not current_url:
                break

        all_ok = all(r.get("success") for r in results)
        return {
            "success": all_ok,
            "total_steps": steps,
            "steps": results,
        }
