"""
AI Backend Builder — FastAPI Proxy Server

Acts as the orchestration hub between the VS Code extension and the
local AI model provider (Ollama) or a cloud router (OpenRouter).

Endpoints:
  GET  /api/health              — Health check + Ollama connectivity
  GET  /api/info                — Server info, uptime, default models
  GET  /api/models              — List available models from Ollama
  POST /api/generate            — Direct text generation from a model
  POST /api/build               — Run the full autonomous pipeline (SSE stream)
  POST /api/build/{id}/approve  — Approve/reject a build checkpoint
  GET  /api/build/sessions      — List active build sessions

Quick Start:
  1. Ensure Ollama is running:  ollama serve
  2. Install Python deps:       pip install -r requirements.txt
  3. Start server:              python main.py
"""

import os
import time
import logging
import requests
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from sse_starlette.sse import EventSourceResponse
import threading
from agents.orchestrator_agent import BuildSession

from schema import BuildRequest, GenerateRequest, ApprovalRequest
from pydantic import BaseModel
from typing import Dict, Any

class DiagramRequest(BaseModel):
    type: str
    plan: Dict[str, Any]

from agents.orchestrator_agent import OrchestratorAgent
from agents.diagram_agent import DiagramAgent
from services.http_settings import get_ssl_verify_setting
from services.openai_compatible_http import build_provider_headers, raise_for_provider_error

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SERVER_START_TIME = time.time()

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

# # Ollama API base URL (default: localhost:11434)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# OLLAMA_URL="https://sivabavithran16--multi-agent-ollama-serve-ollama.modal.run"

# # Default models per agent role (can be overridden per request)
DEFAULT_MODELS = {
    "planner": os.getenv("PLANNER_MODEL", "qwen2.5-coder:1.5b"),
    "codegen": os.getenv("CODEGEN_MODEL", "qwen2.5-coder:1.5b"),
    "debug": os.getenv("DEBUG_MODEL", "qwen2.5-coder:1.5b"),
    "critic": os.getenv("CRITIC_MODEL", "qwen2.5-coder:1.5b"),
}

# Request timeout for Ollama API calls (seconds)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# OpenAI-compatible chat-completions settings.
# Modal vLLM and many hosted LLMs can use this same shape.
USE_OPENAI_COMPATIBLE = os.getenv("USE_OPENAI_COMPATIBLE", "false").lower() == "true"
OPENAI_COMPATIBLE_URL = os.getenv(
    "OPENAI_COMPATIBLE_URL",
    "",
)
OPENAI_COMPATIBLE_API_KEY = os.getenv(
    "OPENAI_COMPATIBLE_API_KEY",
    "",
)
OPENAI_COMPATIBLE_PROVIDER = os.getenv(
    "OPENAI_COMPATIBLE_PROVIDER",
    "openai-compatible",
)

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health_check():
    """Health check endpoint. Also verifies Ollama connectivity."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        ollama_status = "connected" if resp.ok else "unreachable"
    except requests.exceptions.RequestException:
        ollama_status = "unreachable"

    return {
        "status": "ok",
        "ollama": ollama_status,
        "ollama_url": OLLAMA_URL,
        "timestamp": time.time(),
    }


@app.get("/api/info")
async def server_info():
    """Return server metadata: version, uptime, config, model defaults."""
    return {
        "name": "AI Backend Builder",
        "version": "1.0.0",
        "uptime_seconds": round(time.time() - SERVER_START_TIME, 1),
        "use_openrouter": USE_OPENROUTER,
        "max_retries": MAX_RETRIES,
        "default_models": DEFAULT_MODELS,
        "active_sessions": len(_active_sessions),
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

    if USE_OPENAI_COMPATIBLE:
        headers = build_provider_headers(OPENAI_COMPATIBLE_API_KEY)
        
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
        logger.info(f"OPENAI-COMPATIBLE API REQUEST | Provider: {OPENAI_COMPATIBLE_PROVIDER} | Model: {req.model}")
        logger.info(f"--- SYSTEM PROMPT ---\n{req.system}")
        logger.info(f"--- USER PROMPT ---\n{req.prompt[:1000]}{'...' if len(req.prompt) > 1000 else ''}")
        logger.info("="*50)
        
        try:
            resp = requests.post(
                OPENAI_COMPATIBLE_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
                verify=get_ssl_verify_setting(),
            )
            raise_for_provider_error(resp, OPENAI_COMPATIBLE_PROVIDER, OPENAI_COMPATIBLE_URL)
            data = resp.json()
            response_text = data['choices'][0]['message']['content']
            
            logger.info("\n" + "="*50)
            logger.info(f"OPENAI-COMPATIBLE API RESPONSE | Length: {len(response_text)}")
            logger.info(f"--- CONTENT ---\n{response_text[:1000]}{'...' if len(response_text) > 1000 else ''}")
            logger.info("="*50)
            
            return {
                "response": response_text,
                "model": req.model,
                "done": True,
            }
        except Exception as e:
            logger.error(f"OpenAI-compatible request failed: {str(e)}")
            raise HTTPException(status_code=502, detail=f"OpenAI-compatible error: {str(e)}")

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


@app.post("/api/diagrams/generate")
async def generate_diagram(req: DiagramRequest):
    """Generate a Mermaid diagram (class or usecase) based on the plan."""
    logger.info(f"Generating diagram: {req.type}")
    
    diagram_agent = DiagramAgent(
        ollama_url=OLLAMA_URL,
        model=DEFAULT_MODELS["planner"],
        use_openai_compatible=USE_OPENAI_COMPATIBLE,
        openai_compatible_url=OPENAI_COMPATIBLE_URL,
        openai_compatible_api_key=OPENAI_COMPATIBLE_API_KEY,
        openai_compatible_provider=OPENAI_COMPATIBLE_PROVIDER,
    )
    
    try:
        mermaid_code = diagram_agent.generate(req.type, req.plan)
        return {"status": "ok", "mermaid": mermaid_code}
    except Exception as e:
        logger.error(f"Failed to generate diagram: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
        use_openai_compatible=USE_OPENAI_COMPATIBLE,
        openai_compatible_url=OPENAI_COMPATIBLE_URL,
        openai_compatible_api_key=OPENAI_COMPATIBLE_API_KEY,
        openai_compatible_provider=OPENAI_COMPATIBLE_PROVIDER,
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

        while session.active or not session.event_queue.empty():
            try:
                event = session.event_queue.get(timeout=0.3)
                yield {"data": _json.dumps(event)}

                if event.get("type") == "complete":
                    break
            except Exception:
                if await request.is_disconnected():
                    logger.info("Client disconnected from build stream.")
                    session.active = False
                    session.approval_action = "cancel"
                    session.approval_event.set()
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

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "5000"))
    logger.info(f"Starting embedded backend on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
