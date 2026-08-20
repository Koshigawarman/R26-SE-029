/**
 * AI Backend Builder — VS Code Extension Entry Point
 *
 * Registers the sidebar panel with interactive human-in-the-loop
 * build workflow including approval gates at each major step.
 */

import * as vscode from "vscode";
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
  if (logger) {
    logger.info("AI Backend Builder extension deactivated");
    logger.dispose();
  }
}
