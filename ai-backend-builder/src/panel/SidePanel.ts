/**
 * AI Backend Builder — Interactive Side Panel WebviewViewProvider
 *
 * Provides a sidebar panel with human-in-the-loop approval gates.
 * Streams SSE events from the backend, pauses at approval checkpoints,
 * and sends user decisions back via the /api/build/{id}/approve endpoint.
 */

import * as vscode from "vscode";
import * as path from "path";
import * as fs from "fs";
import { Logger } from "../utils/logger.js";
import type { ExtensionConfig } from "../types/index.js";
import { getPanelHtml } from "./panelHtml.js";

function getNonce(): string {
  let text = '';
  const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) {
    text += possible.charAt(Math.floor(Math.random() * possible.length));
  }
  return text;
}

export class SidePanelProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "aiBackendBuilder.sidePanel";

  private _view?: vscode.WebviewView;
  private _logger: Logger;
  private _extensionUri: vscode.Uri;
  private _isBuilding = false;
  private _abortController?: AbortController;
  private _sessionId?: string;

  private _context: vscode.ExtensionContext;

  constructor(context: vscode.ExtensionContext) {
    this._context = context;
    this._extensionUri = context.extensionUri;
    this._logger = Logger.getInstance();
  }

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken,
  ): void {
    this._view = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this._extensionUri],
    };

    const nonce = getNonce();
    webviewView.webview.html = getPanelHtml(
      webviewView.webview,
      this._extensionUri,
      nonce
    );

    // Handle messages from the webview
    webviewView.webview.onDidReceiveMessage(
      async (message) => {
        this._logger.info(`[PANEL] Received message from webview: ${JSON.stringify(message).substring(0, 200)}`);
        switch (message.command) {
          case "startBuild":
            await this._handleBuild(message.prompt);
            break;
          case "cancelBuild":
            this._handleCancel();
            break;
          case "approve":
            await this._handleApproval(message.action, message.data || {});
            break;
          case "checkHealth":
            await this._checkBackendHealth();
            break;
          case "openSettings":
            vscode.commands.executeCommand(
              "workbench.action.openSettings",
              "aiBackendBuilder",
            );
            break;
          case "openFolder":
            if (message.path) {
              const uri = vscode.Uri.file(message.path);
              vscode.commands.executeCommand("vscode.openFolder", uri, {
                forceNewWindow: false,
              });
            }
            break;
          case "showLogs":
            this._logger.show();
            break;
          case "openFile":
            if (message.path) {
              const uri = vscode.Uri.file(message.path);
              vscode.window.showTextDocument(uri, { preview: false });
            }
            break;
          case "savePrompt":
            if (message.prompt) {
              let prompts = this._context.globalState.get<string[]>("promptHistory") || [];
              prompts = [message.prompt, ...prompts.filter(p => p !== message.prompt)].slice(0, 10);
              await this._context.globalState.update("promptHistory", prompts);
              this._postMessage({ command: "promptHistory", prompts });
            }
            break;
          case "getPrompts":
            {
              const prompts = this._context.globalState.get<string[]>("promptHistory") || [];
              this._postMessage({ command: "promptHistory", prompts });
            }
            break;
          case "retryBuild":
            await this._handleBuild(message.prompt);
            break;
          case "viewArtifact":
            await this._handleViewArtifact(message.type, message.plan, message.projectName);
            break;
          case "generateMockData":
            await this._handleGenerateMockData(message.plan, message.projectName);
            break;
        }
      },
      undefined,
      [],
    );

    // Don't call _checkBackendHealth() here — it creates a race condition.
    // The webview script sends its own 'checkHealth' message once loaded,
    // which arrives via onDidReceiveMessage and triggers the check safely.
  }

  private _postMessage(message: any): void {
    this._logger.info(`[PANEL] Sending message to webview: ${JSON.stringify(message).substring(0, 200)}`);
    if (this._view) {
      this._view.webview.postMessage(message).then(
        (ok) => this._logger.info(`[PANEL] postMessage delivered: ${ok}`),
        (err) => this._logger.error(`[PANEL] postMessage failed: ${err}`),
      );
    } else {
      this._logger.error(`[PANEL] Cannot post message — this._view is undefined!`);
    }
  }

  private async _checkBackendHealth(): Promise<void> {
    const config = this._loadConfig();
    try {
      const resp = await fetch(`${config.backendUrl}/api/health`, {
        signal: AbortSignal.timeout(5000),
      });
      if (resp.ok) {
        const data = (await resp.json()) as any;
        this._logger.info(`[PANEL] Health check OK. Ollama: ${data.ollama}. Posting healthStatus message...`);
        this._postMessage({
          command: "healthStatus",
          status: "connected",
          ollama: data.ollama,
          url: config.backendUrl,
        });
      } else {
        this._postMessage({
          command: "healthStatus",
          status: "error",
          url: config.backendUrl,
        });
      }
    } catch {
      this._postMessage({
        command: "healthStatus",
        status: "disconnected",
        url: config.backendUrl,
      });
    }
  }

  /**
   * Send an approval/rejection to the backend for the current session.
   */
  private async _handleApproval(
    action: string,
    data: Record<string, unknown> = {},
  ): Promise<void> {
    if (!this._sessionId) {
      this._logger.warn("No active session to approve");
      return;
    }

    const config = this._loadConfig();
    try {
      const resp = await fetch(
        `${config.backendUrl}/api/build/${this._sessionId}/approve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action, data }),
        },
      );
      if (!resp.ok) {
        this._logger.error(`Approval request failed: ${resp.statusText}`);
      } else {
        this._logger.info(`Sent approval: ${action}`);
      }
    } catch (err: any) {
      this._logger.error(`Approval request error: ${err.message}`);
    }
  }

  private async _handleViewArtifact(type: string, plan: any, projectName?: string): Promise<void> {
    const config = this._loadConfig();
    try {
      let titleName = "Diagram";
      if (type === "class") titleName = "Class Diagram";
      if (type === "usecase") titleName = "Use Case Diagram";
      if (type === "swagger") titleName = "API Contract";
      
      await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `Generating ${titleName}...`,
          cancellable: false
        },
        async (progress) => {
          let codebase = undefined;
          if (type === "swagger") {
            codebase = await this._getCodebaseContext(projectName);
          }

          const resp = await fetch(`${config.backendUrl}/api/artifacts/generate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ type, plan, codebase }),
          });

          if (!resp.ok) {
             let errText = resp.statusText;
             try {
                const errBody = await resp.json() as any;
                if (errBody.detail) errText = errBody.detail;
             } catch(e) {}
             throw new Error(errText);
          }

          const data = await resp.json() as any;
          if (data.content) {
            const { ArtifactPanel } = await import("./ArtifactPanel.js");
            ArtifactPanel.show(this._context, type, data.content);
          }
        }
      );
    } catch (err: any) {
      this._logger.error(`Failed to generate ${type}: ${err.message}`);
      vscode.window.showErrorMessage(`Failed to generate ${type}: ${err.message}`);
    }
  }

  private async _handleGenerateMockData(plan: any, projectName?: string): Promise<void> {
    const config = this._loadConfig();
    try {
      const countInput = await vscode.window.showInputBox({
        prompt: "How many dummy records do you want to generate per entity?",
        value: "10",
        validateInput: (value) => {
          const n = parseInt(value, 10);
          if (isNaN(n) || n <= 0) return "Please enter a positive number";
          if (n > 100) return "Please enter a number less than or equal to 100 to avoid timeouts";
          return null;
        }
      });
      
      if (!countInput) return; // cancelled
      
      const count = parseInt(countInput, 10);

      await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `Generating ${count} mock records per entity...`,
          cancellable: false
        },
        async (progress) => {
          const codebase = await this._getCodebaseContext(projectName);
          const resp = await fetch(`${config.backendUrl}/api/artifacts/generate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ type: "mock_data", plan, count, codebase }),
          });

          if (!resp.ok) {
             let errText = resp.statusText;
             try {
                const errBody = await resp.json() as any;
                if (errBody.detail) errText = errBody.detail;
             } catch(e) {}
             throw new Error(errText);
          }

          const data = await resp.json() as any;
          if (data.content) {
            // Write to workspace
            const workspaceFolders = vscode.workspace.workspaceFolders;
            if (!workspaceFolders || workspaceFolders.length === 0) {
              throw new Error("No open workspace folder to save mock data to.");
            }
            
            const wsPath = workspaceFolders[0].uri;
            const projectDir = projectName ? vscode.Uri.joinPath(wsPath, projectName) : wsPath;
            const mockDataDir = vscode.Uri.joinPath(projectDir, "mock_data");
            
            try {
              await vscode.workspace.fs.createDirectory(mockDataDir);
            } catch(e) { /* ignore if exists */ }

            let parsedContent;
            try {
               parsedContent = JSON.parse(data.content);
            } catch (e) {
               throw new Error("Failed to parse mock data JSON from AI.");
            }

            for (const entityName of Object.keys(parsedContent)) {
              const entityData = parsedContent[entityName];
              const fileUri = vscode.Uri.joinPath(mockDataDir, `${entityName}.json`);
              const buffer = Buffer.from(JSON.stringify(entityData, null, 2), "utf8");
              await vscode.workspace.fs.writeFile(fileUri, new Uint8Array(buffer));
            }

            vscode.window.showInformationMessage(`Mock data generated successfully in ${projectName ? projectName + '/mock_data' : 'mock_data'}/`);
          }
        }
      );
    } catch (err: any) {
      this._logger.error(`Failed to generate mock data: ${err.message}`);
      vscode.window.showErrorMessage(`Failed to generate mock data: ${err.message}`);
    }
  }

  private async _getCodebaseContext(projectName?: string): Promise<string | undefined> {
    try {
      const workspaceFolders = vscode.workspace.workspaceFolders;
      if (!workspaceFolders || workspaceFolders.length === 0) return undefined;
      const wsUri = workspaceFolders[0].uri;
      
      const projectDir = projectName ? vscode.Uri.joinPath(wsUri, projectName) : wsUri;
      let codebase = "";
      const dirsToRead = ["models", "controllers", "routes"];
      
      for (const dir of dirsToRead) {
        try {
          const dirUri = vscode.Uri.joinPath(projectDir, dir);
          const entries = await vscode.workspace.fs.readDirectory(dirUri);
          for (const [name, type] of entries) {
            if (type === vscode.FileType.File && (name.endsWith(".js") || name.endsWith(".ts"))) {
              const fileUri = vscode.Uri.joinPath(dirUri, name);
              const content = await vscode.workspace.fs.readFile(fileUri);
              codebase += `\n// File: ${dir}/${name}\n`;
              codebase += Buffer.from(content).toString("utf8");
              codebase += `\n`;
            }
          }
        } catch(e) {
          // directory might not exist, ignore
        }
      }
      
      return codebase.length > 0 ? codebase : undefined;
    } catch (err) {
      return undefined;
    }
  }

  /**
   * Handle the build request — streams SSE events from the backend.
   */
  private async _handleBuild(prompt: string): Promise<void> {
    if (this._isBuilding) {
      vscode.window.showWarningMessage("A build is already in progress.");
      return;
    }

    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
      this._postMessage({
        command: "buildError",
        error: "No workspace folder open. Please open a folder first.",
      });
      return;
    }

    if (!prompt || prompt.trim().length < 10) {
      this._postMessage({
        command: "buildError",
        error:
          "Please provide a more detailed description (at least 10 characters).",
      });
      return;
    }

    this._isBuilding = true;
    this._abortController = new AbortController();
    this._sessionId = undefined;
    const config = this._loadConfig();
    const workspaceUri = workspaceFolders[0].uri.fsPath;

    this._logger.section("NEW BUILD REQUEST (Interactive)");
    this._logger.info(`User prompt: "${prompt}"`);

    this._postMessage({ 
      command: "buildStarted",
      workspaceRoot: workspaceUri
    });

    try {
      const response = await fetch(`${config.backendUrl}/api/build`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: prompt,
          workspace_uri: workspaceUri,
        }),
        signal: this._abortController.signal,
      });

      if (!response.ok) {
        throw new Error(
          `Backend API error: ${response.status} ${response.statusText}`,
        );
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error("Failed to read response stream");
      }

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n\r?\n/);
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ") || line.includes("data: ")) {
            try {
              const dataStr = line.includes("data: ")
                ? line.substring(line.indexOf("data: ") + 6).trim()
                : line.slice(6).trim();
              const data = JSON.parse(dataStr);

              // Route each event type to the webview
              switch (data.type) {
                case "session":
                  this._sessionId = data.data.sessionId;
                  this._logger.info(`Session: ${this._sessionId}`);
                  break;

                case "status":
                  this._postMessage({
                    command: "buildProgress",
                    message: data.data.message,
                    progress: data.data.progress,
                    state: data.data.state,
                  });
                  this._logger.info(`Status: ${data.data.message}`);
                  break;

                case "approval_needed":
                  this._postMessage({
                    command: "approvalNeeded",
                    step: data.data.step,
                    details: data.data,
                  });
                  this._logger.info(
                    `⏸️ Approval needed: ${data.data.step} — ${data.data.message}`,
                  );
                  break;

                case "memory_retrieved":
                  this._postMessage({
                    command: "memoryRetrieved",
                    ...data.data,
                  });
                  this._logger.info(`🧠 Memory retrieved: ${data.data.count} matches found`);
                  break;

                case "file_generated":
                  if (data.data.content && workspaceUri) {
                    try {
                      const writePath = data.data.full_path || data.data.path;
                      const fullPath = path.join(workspaceUri, writePath);
                      fs.mkdirSync(path.dirname(fullPath), { recursive: true });
                      fs.writeFileSync(fullPath, data.data.content, "utf8");
                      this._logger.info(`☁️ Synced file from cloud: ${writePath}`);
                    } catch (e: any) {
                      this._logger.error(`Failed to sync file: ${e.message}`);
                    }
                  }
                  this._postMessage({
                    command: "fileGenerated",
                    ...data.data,
                  });
                  break;

                case "fix_applied":
                  if (data.data.content && workspaceUri) {
                    try {
                      const writePath = data.data.full_path || data.data.file;
                      const fullPath = path.join(workspaceUri, writePath);
                      fs.mkdirSync(path.dirname(fullPath), { recursive: true });
                      fs.writeFileSync(fullPath, data.data.content, "utf8");
                      this._logger.info(`☁️ Synced fix from cloud: ${writePath}`);
                    } catch (e: any) {
                      this._logger.error(`Failed to sync fix: ${e.message}`);
                    }
                  }
                  this._postMessage({
                    command: "fixApplied",
                    ...data.data,
                  });
                  break;

                case "complete":
                  this._postMessage({
                    command: "buildComplete",
                    result: data.data,
                  });
                  this._logger.info(
                    `Build ${data.data.success ? "succeeded" : "failed"}`,
                  );
                  break;
              }
            } catch (e) {
              this._logger.warn(`Failed to parse SSE: ${line}`);
            }
          }
        }
      }
    } catch (error: unknown) {
      if (error instanceof Error && error.name === "AbortError") {
        this._postMessage({ command: "buildCancelled" });
        this._logger.warn("Build cancelled by user");
      } else {
        const msg = error instanceof Error ? error.message : String(error);
        this._postMessage({ command: "buildError", error: msg });
        this._logger.error(`Build error: ${msg}`);
      }
    } finally {
      this._isBuilding = false;
      this._abortController = undefined;
      this._sessionId = undefined;
    }
  }

  private _handleCancel(): void {
    if (this._abortController) {
      this._abortController.abort();
    }
  }

  private _loadConfig(): ExtensionConfig {
    const config = vscode.workspace.getConfiguration("aiBackendBuilder");
    return {
      backendUrl: config.get<string>("cloudBackendUrl", "http://13.61.155.52:5000"),
      openaiApiKey: config.get<string>("openaiApiKey", ""),
      openaiModel: config.get<string>("openaiModel", "gpt-4"),
      models: {
        planner: config.get<string>("models.planner", "qwen2.5-coder:1.5b"),
        codegen: config.get<string>("models.codegen", "qwen2.5-coder:1.5b"),
        debug: config.get<string>("models.debug", "qwen2.5-coder:1.5b"),
      },
      maxRetries: config.get<number>("maxRetries", 3),
      debugTimeout: config.get<number>("debugTimeout", 10_000),
      aiRequestTimeout: config.get<number>("aiRequestTimeout", 120_000),
    };
  }
}
