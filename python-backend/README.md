# AI Backend Builder — FastAPI Proxy Server

This is the proxy server for the AI Backend Builder VS Code extension. It handles requests from the extension and orchestrates local AI models (via Ollama) to plan, generate, and debug backend applications.

## Prerequisites

- Python 3.10+
- [Ollama](https://ollama.ai/) installed and running

## Quick Start

```bash
# 1. Navigate to this directory
cd python-backend

# 2. Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Ensure Ollama is running
ollama serve

# 5. Pull the recommended models
ollama pull llama3.1:8b
ollama pull qwen2.5-coder:7b

# 6. Start the API server
uvicorn main:app --host 0.0.0.0 --port 5000 --reload
```

The server will start at `http://localhost:5000`.

## Environment Variables

Create a `.env` file in this directory (refer to `.env.example`):

```env
OLLAMA_URL=http://localhost:11434
API_HOST=0.0.0.0
API_PORT=5000
PLANNER_MODEL=llama3.1:8b
CODEGEN_MODEL=qwen2.5-coder:7b
DEBUG_MODEL=llama3.1:8b
REQUEST_TIMEOUT=120
```

## API Endpoints

### `POST /api/build`

Start the full autonomous multi-agent pipeline (Streaming SSE).

### `POST /api/generate`

Generate text from a local AI model.

### `GET /api/health`

Health check — also verifies Ollama connectivity.

### `GET /api/models`

List available Ollama models.

## Troubleshooting

- **"Cannot connect to Ollama"** — Make sure `ollama serve` is running.
- **"Model not found"** — Pull the model first: `ollama pull <model_name>`.
- **Timeout errors** — Increase `REQUEST_TIMEOUT` in `.env` or extension settings.
