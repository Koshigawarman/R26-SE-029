# AI Backend Builder — VS Code Extension

An autonomous multi-agent AI system that **plans**, **generates**, and **debugs** production-ready Node.js/Express backend applications — all from a single text prompt.

## ✨ Features

- **🧠 4 Specialized AI Agents** — Planner, Code Generator, Debugger, and Orchestrator.
- **🏗️ Full MVC Architecture** — Generates Models, Controllers, Routes, and Middleware.
- **🔄 Self-Healing Debug Loop** — Automatically detects and fixes runtime errors during generation.
- **🤖 Local AI Models** — Uses Ollama via a FastAPI proxy (no cloud dependency required).
- **☁️ OpenAI Fallback** — Optional fallback to OpenAI API when local models are unavailable.
- **📦 Production-Ready Code** — ES6 modules, async/await, Mongoose, and robust error handling.
- **📊 Progress Tracking** — Real-time status updates via VS Code notifications.

## 🚀 Quick Start

### 1. Set up the Python Backend Proxy

```bash
# 1. Navigate to the backend directory
cd python-backend

# 2. Set up environment
# [Mac/Linux]
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# [Windows]
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Ensure Ollama is running
ollama serve

# 4. Pull recommended models
ollama pull llama3.1:8b
ollama pull qwen2.5-coder:7b

# 5. Start the proxy server
uvicorn main:app --host 0.0.0.0 --port 5000 --reload
```

### 2. Install & Run the Extension

```bash
# 1. Navigate to the extension root
cd ai-backend-builder

# 2. Install dependencies
npm install

# 3. Compile the extension
npm run compile
```

- Press **F5** in VS Code to launch the **Extension Development Host**.
- Open a workspace folder in the new window.
- Open the Command Palette (`Cmd+Shift+P`) and run **"Build Backend with AI"**.

## ⚙️ Configuration

Open VS Code Settings and search for `aiBackendBuilder`:

| Setting          | Default                 | Description                        |
| ---------------- | ----------------------- | ---------------------------------- |
| `backendUrl`     | `http://localhost:5000` | Local API proxy URL                |
| `openaiApiKey`   | _(empty)_               | OpenAI API key (optional fallback) |
| `openaiModel`    | `gpt-4`                 | OpenAI model for fallback          |
| `models.planner` | `llama3.1:8b`           | Local model for planning           |
| `models.codegen` | `qwen2.5-coder:7b`      | Local model for code generation    |
| `models.debug`   | `llama3.1:8b`           | Local model for debugging          |
| `maxRetries`     | `3`                     | Max debug-fix retry attempts       |

## 📁 Project Structure

```
.
├── ai-backend-builder/     # VS Code Extension (TypeScript)
│   ├── src/
│   │   ├── agents/         # AI Agent logic
│   │   ├── services/       # AI Client & File Manager
│   │   └── extension.ts    # Extension entry point
│   └── package.json
└── python-backend/         # API Proxy Server (FastAPI)
    ├── main.py             # FastAPI entry point
    ├── agents/             # Backend agent orchestration
    └── requirements.txt
```

## License

MIT
