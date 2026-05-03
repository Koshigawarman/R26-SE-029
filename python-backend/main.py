"""
AI Backend Builder — FastAPI Proxy Server

Proxies AI requests from the VS Code extension to locally running
AI models (Ollama or compatible). Acts as a unified interface so
the extension doesn't need to handle multiple AI model APIs directly.

Endpoints:
  POST /api/generate  — Generate text from a local AI model
  GET  /api/health    — Health check
  GET  /api/models    — List available models
  POST /api/build     — Run the full autonomous multi-agent pipeline

Usage:
  1. Ensure Ollama is running: `ollama serve`
  2. Install dependencies: `pip install -r requirements.txt`
  3. Run the server: `uvicorn main:app --host 0.0.0.0 --port 5000 --reload`
  4. Extension connects to http://localhost:5000
"""

import os
import time
import logging
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from sse_starlette.sse import EventSourceResponse

from schema import BuildRequest
from agents.orchestrator_agent import OrchestratorAgent

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Backend Builder API")

# Allow requests from VS Code extension
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Ollama API base URL (default: localhost:11434)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# Default models per agent role (can be overridden per request)
DEFAULT_MODELS = {
    "planner": os.getenv("PLANNER_MODEL", "llama3.1:8b"),
    "codegen": os.getenv("CODEGEN_MODEL", "qwen2.5-coder:7b"),
    "debug": os.getenv("DEBUG_MODEL", "llama3.1:8b"),
}

# Request timeout for Ollama API calls (seconds)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    model: str
    prompt: str
    system: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    """Health check endpoint. Also verifies Ollama connectivity."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        ollama_status = "connected" if resp.ok else "unreachable"
    except requests.exceptions.RequestException:
        ollama_status = "unreachable"

    return {
        "status": "ok",
        "ollama": ollama_status,
        "ollama_url": OLLAMA_URL,
        "timestamp": time.time(),
    }


@app.get("/api/models")
async def list_models():
    """List available models from Ollama."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        if resp.ok:
            data = resp.json()
            models = [m.get("name", "unknown") for m in data.get("models", [])]
            return {
                "models": models,
                "default_models": DEFAULT_MODELS,
            }
        else:
            raise HTTPException(status_code=502, detail=f"Ollama returned {resp.status_code}")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach Ollama: {str(e)}")


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    """Generate text from a local AI model."""
    ollama_payload = {
        "model": req.model,
        "prompt": req.prompt,
        "system": req.system,
        "stream": False,
        "options": {
            "temperature": req.temperature,
            "num_predict": req.max_tokens,
        },
    }

    logger.info(f"Generating with model '{req.model}' (prompt: {len(req.prompt)} chars)")

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=ollama_payload,
            timeout=REQUEST_TIMEOUT,
        )

        if not resp.ok:
            error_text = resp.text[:500]
            logger.error(f"Ollama error ({resp.status_code}): {error_text}")
            raise HTTPException(status_code=502, detail=f"Ollama returned {resp.status_code}: {error_text}")

        result = resp.json()
        response_text = result.get("response", "")

        if not response_text:
            raise HTTPException(status_code=502, detail="Ollama returned empty response")

        logger.info(f"Generated {len(response_text)} chars with model '{req.model}'")

        return {
            "response": response_text,
            "model": req.model,
            "done": result.get("done", True),
        }

    except requests.exceptions.Timeout:
        logger.error(f"Ollama request timed out after {REQUEST_TIMEOUT}s")
        raise HTTPException(status_code=504, detail=f"Request timed out after {REQUEST_TIMEOUT} seconds")

    except requests.exceptions.ConnectionError:
        logger.error(f"Cannot connect to Ollama at {OLLAMA_URL}")
        raise HTTPException(status_code=503, detail=f"Cannot connect to Ollama at {OLLAMA_URL}. Is Ollama running?")

    except requests.exceptions.RequestException as e:
        logger.error(f"Ollama request failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Request failed: {str(e)}")


@app.post("/api/build")
async def build_project(req: BuildRequest, request: Request):
    """
    Start the autonomous backend build process.
    Returns Server-Sent Events (SSE) stream.
    """
    # Fallback to default models if not provided
    if not req.planner_model: req.planner_model = DEFAULT_MODELS["planner"]
    if not req.codegen_model: req.codegen_model = DEFAULT_MODELS["codegen"]
    if not req.debug_model: req.debug_model = DEFAULT_MODELS["debug"]

    orchestrator = OrchestratorAgent(
        ollama_url=OLLAMA_URL,
        models={
            "planner": req.planner_model,
            "codegen": req.codegen_model,
            "debug": req.debug_model,
        },
        max_retries=int(os.getenv("MAX_RETRIES", "3"))
    )

    async def event_generator():
        # The orchestrator's execute_stream yields SSE formatted strings like:
        # data: {"type": "status", "data": {...}}\n\n
        # sse_starlette expects a dict or string without 'data: ' and '\n\n'
        # Let's adapt the generator for EventSourceResponse
        
        try:
            for chunk in orchestrator.execute_stream(req):
                # The orchestrator is already formatting as SSE. 
                # We can just yield the raw string, but sse_starlette prefers dicts.
                # Let's clean the prefix and suffix added by the orchestrator so sse-starlette can format it.
                if chunk.startswith("data: "):
                    content = chunk[6:].strip()
                    yield {"data": content}
                
                # Check for client disconnect
                if await request.is_disconnected():
                    logger.info("Client disconnected from build stream.")
                    break
        except Exception as e:
            logger.error(f"Error in stream generator: {str(e)}")
            import json
            yield {"data": json.dumps({"type": "status", "data": {"message": f"❌ Stream Error: {str(e)}", "progress": 100}})}

    return EventSourceResponse(event_generator())
