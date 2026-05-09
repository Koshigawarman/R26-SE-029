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
  4. Extension connects to http://localhost:5000 (configured via API_PORT)
"""

import os
import time
import logging
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from sse_starlette.sse import EventSourceResponse
import threading
from agents.orchestrator_agent import BuildSession

from schema import BuildRequest, GenerateRequest, ApprovalRequest
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
    "planner": os.getenv("PLANNER_MODEL", "qwen2.5-coder:3b"),
    "codegen": os.getenv("CODEGEN_MODEL", "qwen2.5-coder:3b"),
    "debug": os.getenv("DEBUG_MODEL", "qwen2.5-coder:3b"),
    "critic": os.getenv("CRITIC_MODEL", "qwen2.5-coder:3b"),
}

# Request timeout for Ollama API calls (seconds)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# OpenRouter Settings
USE_OPENROUTER = os.getenv("USE_OPENROUTER", "false").lower() == "true"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────
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
    
    logger.info(f"Generating with model '{req.model}' (prompt: {len(req.prompt)} chars)")

    if USE_OPENROUTER:
        if not OPENROUTER_API_KEY:
            raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY is not set")
        
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Koshigawarman/R26-SE-029", # Optional
            "X-Title": "AI Backend Builder", # Optional
        }
        
        payload = {
            "model": req.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.prompt}
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        
        logger.info("\n" + "="*50)
        logger.info(f"OPENROUTER API REQUEST | Model: {req.model}")
        logger.info(f"--- SYSTEM PROMPT ---\n{req.system}")
        logger.info(f"--- USER PROMPT ---\n{req.prompt[:1000]}{'...' if len(req.prompt) > 1000 else ''}")
        logger.info("="*50)
        
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            response_text = data['choices'][0]['message']['content']
            
            logger.info("\n" + "="*50)
            logger.info(f"OPENROUTER API RESPONSE | Length: {len(response_text)}")
            logger.info(f"--- CONTENT ---\n{response_text[:1000]}{'...' if len(response_text) > 1000 else ''}")
            logger.info("="*50)
            
            return {
                "response": response_text,
                "model": req.model,
                "done": True,
            }
        except Exception as e:
            logger.error(f"OpenRouter request failed: {str(e)}")
            raise HTTPException(status_code=502, detail=f"OpenRouter error: {str(e)}")

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
    
    logger.info("\n" + "="*50)
    logger.info(f"OLLAMA API REQUEST | Model: {req.model}")
    logger.info(f"--- SYSTEM PROMPT ---\n{req.system}")
    logger.info(f"--- USER PROMPT ---\n{req.prompt[:1000]}{'...' if len(req.prompt) > 1000 else ''}")
    logger.info("="*50)

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

        logger.info("\n" + "="*50)
        logger.info(f"OLLAMA API RESPONSE | Length: {len(response_text)}")
        logger.info(f"--- CONTENT ---\n{response_text[:1000]}{'...' if len(response_text) > 1000 else ''}")
        logger.info("="*50)
        
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
    if not req.critic_model: req.critic_model = DEFAULT_MODELS["critic"]
    if not req.max_retries: req.max_retries = MAX_RETRIES

    orchestrator = OrchestratorAgent(
        ollama_url=OLLAMA_URL,
        models={
            "planner": req.planner_model,
            "codegen": req.codegen_model,
            "debug": req.debug_model,
            "critic": req.critic_model,
        },
        max_retries=req.max_retries,
        use_openrouter=USE_OPENROUTER,
        openrouter_api_key=OPENROUTER_API_KEY
    )
    
    session = BuildSession()
    _active_sessions[session.id] = session
    logger.info(f"Created build session: {session.id}")

    # Run the orchestrator in a background thread so it can block at
    # approval gates without freezing the async event loop.
    thread = threading.Thread(
        target=orchestrator.execute_interactive,
        args=(req, session),
        daemon=True,
    )
    thread.start()

    async def event_generator():
        import asyncio, json as _json

        # First event: send the session ID so the client can POST approvals
        yield {"data": _json.dumps({"type": "session", "data": {"sessionId": session.id}})}

        while session.active:
            try:
                event = session.event_queue.get(timeout=0.3)
                yield {"data": _json.dumps(event)}

                if event.get("type") == "complete":
                    break
            except Exception:
                # queue.Empty — just keep polling
                if await request.is_disconnected():
                    logger.info("Client disconnected from build stream.")
                    session.active = False
                    break
                await asyncio.sleep(0.1)

        # Cleanup
        _active_sessions.pop(session.id, None)
        logger.info(f"Build session {session.id} ended.")

    return EventSourceResponse(event_generator())

# ─────────────────────────────────────────────────────────────────────────────
# Session Management & Approval Endpoint
# ─────────────────────────────────────────────────────────────────────────────

# Active build sessions (keyed by session ID)
_active_sessions: dict = {}

     # optional extra data from the user


@app.post("/api/build/{session_id}/approve")
async def approve_build_step(session_id: str, body: ApprovalRequest):
    """
    Approve or reject the current approval gate for a running build.
    The orchestrator thread is blocking on session.approval_event — this
    endpoint sets the action and unblocks it.
    """
    from agents.orchestrator_agent import BuildSession

    session: BuildSession | None = _active_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Build session not found or already completed.")

    logger.info(f"Approval received for session {session_id}: action={body.action}")
    session.approval_action = body.action
    session.approval_data = body.data
    session.approval_event.set()

    return {"status": "ok", "action": body.action}


@app.get("/api/build/sessions")
async def list_sessions():
    """List active build sessions (for debugging)."""
    return {
        "sessions": [
            {"id": sid, "active": s.active}
            for sid, s in _active_sessions.items()
        ]
    }