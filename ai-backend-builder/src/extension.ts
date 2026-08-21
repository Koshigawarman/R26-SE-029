/**
 * AI Backend Builder — VS Code Extension Entry Point
 *
 * Registers the sidebar panel with interactive human-in-the-loop
 * build workflow including approval gates at each major step.
 */

import * as vscode from "vscode";
import * as path from "path";
import * as os from "os";
import { exec, spawn } from "child_process";
import * as fs from "fs";

import { Logger } from "./utils/logger.js";
import { COMMANDS } from "./utils/constants.js";
import { SidePanelProvider } from "./panel/SidePanel.js";
import type { ExtensionConfig } from "./types/index.js";

let logger: Logger;

/**
 * Extension activation — called when the extension is first activated.
 * Registers the sidebar panel provider and all commands.
 */
export function activate(context: vscode.ExtensionContext): void {
  logger = Logger.getInstance();
  logger.info("AI Backend Builder extension activated");

  const workspaceFolders = vscode.workspace.workspaceFolders;
  const workspacePath = workspaceFolders && workspaceFolders.length > 0 ? workspaceFolders[0].uri.fsPath : "";
  
  if (workspacePath) {
    logger.info(`Starting Python backend via local Docker build. Workspace: ${workspacePath}`);
    logger.info("Assuming backend is running locally at http://localhost:5000");
  } else {
    logger.warn("No workspace folder open. Docker backend cannot mount workspace.");
  }

  // Register the WebviewView sidebar panel
  const panelProvider = new SidePanelProvider(context);
  const panelRegistration = vscode.window.registerWebviewViewProvider(
    SidePanelProvider.viewType,
    panelProvider,
    { webviewOptions: { retainContextWhenHidden: true } },
  );
  context.subscriptions.push(panelRegistration);
  logger.info("Registered sidebar panel");

  // Register the build command (focuses the side panel)
  const buildCommand = vscode.commands.registerCommand(
    COMMANDS.BUILD_BACKEND,
    async () => {
      await vscode.commands.executeCommand("aiBackendBuilder.sidePanel.focus");
    },
  );

  context.subscriptions.push(buildCommand);
  logger.info(`Registered command: "${COMMANDS.BUILD_BACKEND}"`);
}

/**
 * Extension deactivation — cleanup.
 */
export function deactivate(): void {
  // No longer stopping Docker backend as it's run manually

  if (logger) {
    logger.info("AI Backend Builder extension deactivated");
    logger.dispose();
  }
}
