# Flask AI API Proxy — Setup Guide

## Prerequisites

- Python 3.10+
- [Ollama](https://ollama.ai/) installed and running

## Quick Start

```bash
# 1. Navigate to this directory
cd flask-api

# 2. Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Ensure Ollama is running
ollama serve

# 5. Pull the required models
ollama pull mistral:7b
ollama pull codellama:13b

# 6. Start the Flask API
python app.py
```

The server will start at `http://localhost:5000`.

## Environment Variables

Create a `.env` file in this directory to customize:

```env
OLLAMA_URL=http://localhost:11434
FLASK_HOST=0.0.0.0
FLASK_PORT=5000
PLANNER_MODEL=mistral:7b
CODEGEN_MODEL=codellama:13b
DEBUG_MODEL=mistral:7b
REQUEST_TIMEOUT=120
```

## API Endpoints

### `POST /api/generate`

Generate text from a local AI model.

**Request:**
```json
{
  "model": "mistral:7b",
  "prompt": "Your prompt here",
  "system": "Optional system prompt",
  "temperature": 0.3,
  "max_tokens": 4096
}
```

**Response:**
```json
{
  "response": "Generated text...",
  "model": "mistral:7b",
  "done": true
}
```

### `GET /api/health`

Health check — also verifies Ollama connectivity.

### `GET /api/models`

List available Ollama models.

## Troubleshooting

- **"Cannot connect to Ollama"** — Make sure `ollama serve` is running
- **"Model not found"** — Pull the model first: `ollama pull modelname`
- **Timeout errors** — Increase `REQUEST_TIMEOUT` in `.env` or VS Code settings
