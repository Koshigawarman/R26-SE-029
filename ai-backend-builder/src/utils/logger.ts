/**
 * AI Backend Builder — Logger
 * 
 * Provides a structured logging interface using VS Code OutputChannel.
 * All agent activity is logged with timestamps and agent-name prefixes.
 */

import * as vscode from 'vscode';
import { OUTPUT_CHANNEL_NAME } from './constants.js';

export class Logger {
  private static instance: Logger;
  private outputChannel: vscode.OutputChannel;

  private constructor() {
    this.outputChannel = vscode.window.createOutputChannel(OUTPUT_CHANNEL_NAME);
  }

  /** Get or create the singleton Logger instance */
  public static getInstance(): Logger {
    if (!Logger.instance) {
      Logger.instance = new Logger();
    }
    return Logger.instance;
  }

  /** Format a log message with timestamp and optional agent prefix */
  private format(level: string, message: string, agent?: string): string {
    const timestamp = new Date().toISOString().replace('T', ' ').substring(0, 19);
    const prefix = agent ? `[${agent}]` : '';
    return `[${timestamp}] [${level}] ${prefix} ${message}`;
  }

  /** Log an informational message */
  public info(message: string, agent?: string): void {
    const formatted = this.format('INFO', message, agent);
    this.outputChannel.appendLine(formatted);
    console.log(formatted);
  }

  /** Log a warning */
  public warn(message: string, agent?: string): void {
    const formatted = this.format('WARN', message, agent);
    this.outputChannel.appendLine(formatted);
    console.warn(formatted);
  }

  /** Log an error — automatically reveals the output channel */
  public error(message: string, agent?: string): void {
    const formatted = this.format('ERROR', message, agent);
    this.outputChannel.appendLine(formatted);
    console.error(formatted);
    this.outputChannel.show(true); // reveal but don't steal focus
  }

  /** Log a debug message (verbose) */
  public debug(message: string, agent?: string): void {
    const formatted = this.format('DEBUG', message, agent);
    this.outputChannel.appendLine(formatted);
  }

  /** Log a separator/section header for visual clarity */
  public section(title: string): void {
    const line = '─'.repeat(60);
    this.outputChannel.appendLine('');
    this.outputChannel.appendLine(line);
    this.outputChannel.appendLine(`  ${title}`);
    this.outputChannel.appendLine(line);
  }

  /** Log a multi-line block (e.g., generated code, error traces) */
  public block(label: string, content: string, agent?: string): void {
    const prefix = agent ? `[${agent}] ` : '';
    this.outputChannel.appendLine(`${prefix}┌── ${label} ──`);
    for (const line of content.split('\n')) {
      this.outputChannel.appendLine(`${prefix}│ ${line}`);
    }
    this.outputChannel.appendLine(`${prefix}└${'─'.repeat(label.length + 6)}`);
  }

  /** Show the output channel */
  public show(): void {
    this.outputChannel.show();
  }

  /** Dispose the output channel on deactivation */
  public dispose(): void {
    this.outputChannel.dispose();
  }
}
