# AI Backend Builder — VS Code Extension

An autonomous multi-agent AI system that **plans**, **generates**, and **debugs** production-ready Node.js/Express backend applications — all from a single text prompt.

## ✨ Features

- **🧠 4 Specialized AI Agents** — Planner, Code Generator, Debugger, and Orchestrator
- **🏗️ Full MVC Architecture** — Models, Controllers, Routes, Middleware
- **🔄 Self-Healing Debug Loop** — Automatically detects and fixes runtime errors (up to 3 retries)
- **🤖 Local AI Models** — Uses Ollama via a Flask API proxy (no cloud dependency)
- **☁️ OpenAI Fallback** — Falls back to OpenAI API when local models are unavailable
- **📦 Production-Ready Code** — ES6 modules, async/await, Mongoose, dotenv, error handling
- **🔍 Context-Aware** — Reads existing files before generating, avoids blind overwrites
- **📊 Progress Tracking** — Real-time VS Code notifications during generation

## 🚀 Quick Start

### 1. Set up the Flask API Proxy

```bash
cd flask-api
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Ensure Ollama is running
ollama serve

# Pull required models
ollama pull mistral:7b
ollama pull codellama:13b

# Start the proxy
python app.py
```

### 2. Install the Extension

```bash
# From the extension root
npm install
npm run compile
```

Then press **F5** in VS Code to launch the Extension Development Host.

### 3. Use It

1. Open a workspace folder in VS Code
2. Open the Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`)
3. Run **"Build Backend with AI"**
4. Describe your backend (e.g., _"E-commerce API with users, products, orders, and authentication"_)
5. Watch it build! 🎉

## 🧱 Architecture

```
┌──────────────────────────────────────────────────────┐
│                   VS Code Extension                  │
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │              Orchestrator Agent                │  │
│  │    Coordinates the entire build pipeline       │  │
│  └───────┬──────────┬─────────────┬───────────────┘  │
│          │          │             │                  │
│  ┌───────▼──┐ ┌─────▼──────┐ ┌────▼────────┐         │
│  │ Planner  │ │  Code Gen  │ │   Debug     │         │
│  │  Agent   │ │   Agent    │ │   Agent     │         │
│  └────┬─────┘ └─────┬──────┘ └──────┬──────┘         │
│       └─────────────┼───────────────┘                │
│                     │                                │
│              ┌──────▼──────┐                         │
│              │  AI Client  │                         │
│              │ Flask→OpenAI│                         │
│              └──────┬──────┘                         │
└─────────────────────┼────────────────────────────────┘
                      │
               ┌──────▼──────┐     ┌──────────────┐
               │  Flask API  │────▶│   Ollama     │
               │  (Python)   │     │ (Local LLMs) │
               └─────────────┘     └──────────────┘
```

### Pipeline Flow

```
User Prompt → Planner → File Specs → Code Generator → Debug Agent → ✅ Done
                                                          │
                                                          ▼ (if errors)
                                                    Fix → Retry (×3)
```

## ⚙️ Configuration

Open VS Code Settings and search for `aiBackendBuilder`:

| Setting | Default | Description |
|---------|---------|-------------|
| `flaskUrl` | `http://localhost:5000` | Flask API proxy URL |
| `openaiApiKey` | _(empty)_ | OpenAI API key (fallback) |
| `openaiModel` | `gpt-4` | OpenAI model for fallback |
| `models.planner` | `mistral:7b` | Local model for planning |
| `models.codegen` | `codellama:13b` | Local model for code generation |
| `models.debug` | `mistral:7b` | Local model for debugging |
| `maxRetries` | `3` | Max debug-fix retry attempts |
| `debugTimeout` | `10000` | Process timeout (ms) |
| `aiRequestTimeout` | `120000` | AI request timeout (ms) |

## 📁 Generated Project Structure

Every generated project follows this MVC structure:

```
project-name/
├── app.js               # Express entry point
├── package.json          # Dependencies (ES modules)
├── .env                  # Environment variables
├── config/
│   └── db.js            # MongoDB connection
├── models/
│   ├── User.js          # Mongoose schemas
│   └── ...
├── controllers/
│   ├── userController.js # CRUD logic
│   └── ...
├── routes/
│   ├── userRoutes.js     # Express routers
│   └── ...
└── middleware/
    ├── auth.js           # JWT authentication
    └── errorHandler.js   # Error handling
```

## 🛠️ Development

```bash
# Watch mode (recompile on changes)
npm run watch

# One-time compile
npm run compile

# Debug: press F5 in VS Code
```

## 📂 Extension Source Structure

```
src/
├── extension.ts              # Entry point & command registration
├── agents/
│   ├── base-agent.ts         # Abstract base agent
│   ├── planner-agent.ts      # Planner Agent
│   ├── codegen-agent.ts      # Code Generator Agent
│   ├── debug-agent.ts        # Debug Agent
│   └── orchestrator-agent.ts # Orchestrator (master controller)
├── services/
│   ├── ai-client.ts          # Unified AI client (Flask → OpenAI)
│   ├── file-manager.ts       # File system operations
│   └── process-runner.ts     # Child process execution
├── state/
│   └── memory.ts             # Shared memory / state store
├── prompts/
│   ├── planner-prompt.ts     # Planner system/user prompts
│   ├── codegen-prompt.ts     # Code gen prompts with context
│   └── debug-prompt.ts       # Debug analysis prompts
├── types/
│   └── index.ts              # All TypeScript interfaces
└── utils/
    ├── logger.ts             # VS Code OutputChannel logger
    └── constants.ts          # Config defaults & patterns
```

## License

MIT

# flask-api
