/**
 * AI Backend Builder — VS Code Extension Entry Point
 * 
 * Registers the "Build Backend with AI" command, handles user input,
 * sets up the agent pipeline, and provides progress notifications.
 */

import * as vscode from 'vscode';
import { Logger } from './utils/logger.js';
import { COMMANDS } from './utils/constants.js';
import type { ExtensionConfig } from './types/index.js';


let logger: Logger;

/**
 * Extension activation — called when the extension is first activated.
 * Registers all commands and initializes services.
 */
export function activate(context: vscode.ExtensionContext): void {
  logger = Logger.getInstance();
  logger.info('AI Backend Builder extension activated');

  // Register the main command
  const buildCommand = vscode.commands.registerCommand(
    COMMANDS.BUILD_BACKEND,
    handleBuildBackend
  );

  context.subscriptions.push(buildCommand);
  logger.info(`Registered command: "${COMMANDS.BUILD_BACKEND}"`);
}

/**
 * Extension deactivation — cleanup.
 */
export function deactivate(): void {
  if (logger) {
    logger.info('AI Backend Builder extension deactivated');
    logger.dispose();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Command Handler
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Handle the "Build Backend with AI" command.
 * Prompts the user for a description, validates the workspace,
 * and runs the full orchestration pipeline with progress tracking.
 */
async function handleBuildBackend(): Promise<void> {
  logger.section('NEW BUILD REQUEST');

  // ─── Step 1: Validate workspace ──────────────────────────────────
  const workspaceFolders = vscode.workspace.workspaceFolders;
  if (!workspaceFolders || workspaceFolders.length === 0) {
    vscode.window.showErrorMessage(
      'AI Backend Builder: Please open a workspace folder first.'
    );
    return;
  }

  const workspaceUri = workspaceFolders[0].uri;

  // ─── Step 2: Get user input ──────────────────────────────────────
  const userPrompt = await vscode.window.showInputBox({
    title: 'AI Backend Builder',
    prompt: 'Describe your backend application',
    placeHolder:
      'e.g., E-commerce API with users, products, orders, and authentication',
    ignoreFocusOut: true,
    validateInput: (value) => {
      if (!value || value.trim().length < 10) {
        return 'Please provide a more detailed description (at least 10 characters)';
      }
      return null;
    },
  });

  if (!userPrompt) {
    logger.info('User cancelled input');
    return;
  }

  logger.info(`User prompt: "${userPrompt}"`);

  // ─── Step 3: Run the pipeline with progress ─────────────────────
  const config = loadExtensionConfig();

  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: 'AI Backend Builder',
      cancellable: true,
    },
    async (progress, token) => {
      const reportProgress = (message: string, increment?: number) => {
        progress.report({ message, increment });
      };

      const controller = new AbortController();
      token.onCancellationRequested(() => {
        logger.warn('Build cancelled by user');
        controller.abort();
        vscode.window.showWarningMessage('AI Backend Builder: Build cancelled.');
      });

      try {
        const response = await fetch(`${config.flaskUrl}/api/build`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            prompt: userPrompt,
            workspace_uri: workspaceUri.fsPath
          }),
          signal: controller.signal
        });

        if (!response.ok) {
          throw new Error(`Flask API error: ${response.statusText}`);
        }

        // Parse SSE stream
        const reader = response.body?.getReader();
        if (!reader) throw new Error("Failed to read response stream");

        const decoder = new TextDecoder();
        let buffer = '';
        let result: any = null;
        let lastProgress = 0;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\\n\\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const data = JSON.parse(line.slice(6));
                
                if (data.type === 'status') {
                  const inc = data.data.progress - lastProgress;
                  lastProgress = data.data.progress;
                  reportProgress(data.data.message, inc > 0 ? inc : 0);
                  logger.info(`Status: ${data.data.message}`);
                } else if (data.type === 'complete') {
                  result = data.data;
                }
              } catch (e) {
                logger.warn(`Failed to parse SSE line: ${line}`);
              }
            }
          }
        }

        // ─── Step 5: Show result ─────────────────────────────────
        if (result.success) {
          const action = await vscode.window.showInformationMessage(
            `✅ Backend "${result.projectName}" generated successfully!\n` +
            `${result.filesGenerated} files • ${result.debugAttempts} debug cycles • ` +
            `${(result.duration / 1000).toFixed(1)}s`,
            'Open Project Folder',
            'Show Logs'
          );

          if (action === 'Open Project Folder') {
            const projectUri = vscode.Uri.file(result.projectRoot);
            vscode.commands.executeCommand('vscode.openFolder', projectUri, {
              forceNewWindow: false,
            });
          } else if (action === 'Show Logs') {
            logger.show();
          }
        } else {
          const errorSummary = result.errors.length > 0
            ? result.errors[0].substring(0, 150)
            : 'Unknown error';

          const action = await vscode.window.showErrorMessage(
            `❌ Backend build failed after ${result.debugAttempts} attempts.\n${errorSummary}`,
            'Show Logs',
            'Retry'
          );

          if (action === 'Show Logs') {
            logger.show();
          } else if (action === 'Retry') {
            // Re-run the command
            vscode.commands.executeCommand(COMMANDS.BUILD_BACKEND);
          }
        }
      } catch (error: unknown) {
        const errorMsg = error instanceof Error ? error.message : String(error);
        logger.error(`Fatal error: ${errorMsg}`);
        vscode.window.showErrorMessage(
          `AI Backend Builder: Fatal error — ${errorMsg}`
        );
      }
    }
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Configuration
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Load the full extension configuration from VS Code settings.
 */
function loadExtensionConfig(): ExtensionConfig {
  const config = vscode.workspace.getConfiguration('aiBackendBuilder');

  return {
    flaskUrl: config.get<string>('flaskUrl', 'http://localhost:5000'),
    openaiApiKey: config.get<string>('openaiApiKey', ''),
    openaiModel: config.get<string>('openaiModel', 'gpt-4'),
    models: {
      planner: config.get<string>('models.planner', 'mistral:7b'),
      codegen: config.get<string>('models.codegen', 'codellama:13b'),
      debug: config.get<string>('models.debug', 'mistral:7b'),
    },
    maxRetries: config.get<number>('maxRetries', 3),
    debugTimeout: config.get<number>('debugTimeout', 10_000),
    aiRequestTimeout: config.get<number>('aiRequestTimeout', 120_000),
  };
}
