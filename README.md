# LLM Quiz Solver

A FastAPI service that automatically solves data-analysis quizzes using the Google Gemini API with deterministic fallbacks.

## 🎯 Overview

The solver receives a quiz URL, fetches and parses the page, determines the quiz type, computes an answer, and submits it back to the quiz server. It can follow chains of quizzes by reading the `next_url` from each submission response.

Supported quiz types:

| Type | Description |
|------|-------------|
| **data** | Downloads CSV, Excel, or PDF files and performs statistical analysis |
| **audio** | Downloads an audio file and returns its duration in seconds |
| **simple** | Passes the page text to the LLM for a free-form answer |

## 🏗️ Architecture

```
POST /solve
    │
    ▼
FastAPI endpoint  (app/main.py)
    │  validates secret
    ▼
QuizSolver        (app/solver.py)
    │  fetch HTML → detect type → solve → submit
    ▼
LLMClient         (app/llm.py)
    │  Gemini API (with deterministic fallbacks for CSV/XLSX/PDF)
    ▼
Quiz server  ←  POST answer
```

## ✅ Features

- Validates secret and email on every request
- Decodes `atob()`-obfuscated quiz pages
- Solves CSV / Excel quizzes with Gemini, falling back to pandas statistics
- Solves PDF quizzes with Gemini, falling back to pdfplumber table extraction
- Solves audio quizzes by measuring file duration with pydub
- Automatically follows quiz chains (up to 10 steps)
- Structured JSON responses for every step

## 🚀 Setup

### Prerequisites

- Python 3.11+
- A Google Gemini API key ([Google AI Studio](https://aistudio.google.com/))

### Local development

```bash
# 1. Clone and enter the repo
git clone https://github.com/Gseyal/llm_solver.git
cd llm_solver

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set environment variables
export STUDENT_EMAIL="your-email@example.com"
export STUDENT_SECRET="your-secret"
export LLM_API_KEY="your-gemini-api-key"

# 5. Start the server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

You can also put the variables in a `.env` file in the project root — `python-dotenv` will load it automatically.

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `STUDENT_EMAIL` | ✅ | — | Your email, included in answer submissions |
| `STUDENT_SECRET` | ✅ | — | Secret that callers must supply in `/solve` requests |
| `LLM_API_KEY` | ✅ | — | Google Gemini API key |
| `LLM_PROVIDER` | | `gemini` | LLM backend (currently only `gemini` is supported) |
| `LLM_MODEL` | | `models/gemini-2.5-flash` | Gemini model name |
| `LOG_LEVEL` | | `INFO` | Python logging level |

### Docker

```bash
docker build -t llm-quiz-solver .

docker run -d \
  -p 8000:8000 \
  -e STUDENT_EMAIL="your-email@example.com" \
  -e STUDENT_SECRET="your-secret" \
  -e LLM_API_KEY="your-gemini-api-key" \
  --name quiz-solver \
  llm-quiz-solver
```

## 📝 API

### `POST /solve`

Solves one quiz or a chain of quizzes.

**Request body**

```json
{
  "email": "student@example.com",
  "secret": "your-secret",
  "url": "https://example.com/quiz-123"
}
```

**Success response (200)**

```json
{
  "success": true,
  "total_steps": 2,
  "steps": [
    {
      "step_url": "https://example.com/quiz-123",
      "success": true,
      "answer": "42",
      "details": {
        "quiz_type": "data",
        "submission": { "submit_url": "...", "status_code": 200, "correct": true }
      },
      "next_url": "https://example.com/quiz-124"
    }
  ]
}
```

**Error responses**

| Code | Meaning |
|------|---------|
| 400 | Invalid JSON or missing/invalid fields |
| 403 | Wrong secret |
| 500 | Unexpected server error |

---

### `GET /health`

```json
{
  "status": "ok",
  "email": "student@example.com",
  "llm_provider": "gemini"
}
```

## 🧪 Quick test

```bash
curl -X POST http://localhost:8000/solve \
  -H "Content-Type: application/json" \
  -d '{
    "email": "your-email@example.com",
    "secret": "your-secret",
    "url": "https://example.com/quiz-demo"
  }'
```

## 📚 Key dependencies

| Library | Purpose |
|---------|---------|
| `fastapi` + `uvicorn` | Web framework and ASGI server |
| `google-generativeai` | Gemini LLM API |
| `httpx` | Async HTTP client |
| `pandas` + `numpy` + `openpyxl` | CSV / Excel analysis |
| `pdfplumber` | PDF table extraction |
| `pydub` | Audio file duration |
| `beautifulsoup4` | HTML parsing |

## 🐛 Troubleshooting

**Missing environment variables on startup**

```
RuntimeError: Missing required environment variables: STUDENT_EMAIL, LLM_API_KEY
```

Set the variables listed in the [Environment variables](#environment-variables) table.

**`ImportError` or missing package**

```bash
pip install -r requirements.txt --upgrade
```

**LLM call fails / returns non-JSON**

The solver automatically falls back to deterministic logic for CSV, Excel, and PDF quizzes, so most quizzes will still be answered even without a working LLM connection.

## 📄 License

MIT — see [LICENSE](LICENSE).

