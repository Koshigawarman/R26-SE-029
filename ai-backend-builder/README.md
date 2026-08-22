# AI Backend Builder

The **AI Backend Builder** is an autonomous multi-agent system that helps you plan, generate, and debug production-ready Node.js/Express backend applications entirely inside VS Code using AI.

## Features

- 🏗 **Multi-Agent Architecture**: Uses three distinct agents (Planner, Coder, Debugger) to iteratively generate backend projects.
- 🚀 **Full Project Generation**: Generates standard Express.js folder structure, including routes, controllers, and services.
- 🔧 **Autonomous Debugging**: The extension will automatically test and fix syntax and runtime errors for you.
- 🧠 **Local or Remote Models**: Supports local open-source LLMs (via Ollama) for maximum privacy and cost efficiency, or cloud APIs (OpenAI) for maximum performance.

## Prerequisites

By default, the AI Backend Builder connects to a Python proxy API. Ensure your backend server is running locally on port 5000, or point the extension to a cloud-hosted URL.

Alternatively, you can provide an OpenAI API key in the extension settings to bypass the proxy.

## Extension Settings

This extension contributes the following settings:

* `aiBackendBuilder.backendUrl`: Base URL for the Python Backend API (Default: `http://localhost:5000`).
* `aiBackendBuilder.openaiApiKey`: OpenAI API Key to use if bypassing the Python proxy.
* `aiBackendBuilder.maxRetries`: Maximum number of times the agent will try to debug errors automatically.
* `aiBackendBuilder.models.*`: Allows specifying the Ollama local models to use for each agent.

## How to use

1. Open the Command Palette (`Ctrl+Shift+P` / `Cmd+Shift+P`).
2. Type **AI Backend Builder: Build Backend with AI**.
3. Follow the interactive panel to provide your project specifications!

## Known Issues

- Currently relies on a separate Python backend for multi-agent logic coordination.

## Release Notes

### 0.1.0
Initial beta release!
