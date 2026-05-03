/**
 * AI Backend Builder — Core Type Definitions
 * 
 * Central type definitions shared across all agents, services, and utilities.
 */

// ─────────────────────────────────────────────────────────────────────────────
// Agent Roles
// ─────────────────────────────────────────────────────────────────────────────

export type AgentRole = 'planner' | 'codegen' | 'debug';

// ─────────────────────────────────────────────────────────────────────────────
// Planner Agent Types
// ─────────────────────────────────────────────────────────────────────────────

/** A single field within an entity (e.g., "email" of type "String") */
export interface EntityField {
  name: string;
  type: string;
  required: boolean;
  unique?: boolean;
  default?: string;
}

/** A data entity extracted from the user's requirements (e.g., User, Product) */
export interface Entity {
  name: string;
  fields: EntityField[];
  description?: string;
}

/** A feature identified from the user's requirements (e.g., CRUD, Auth) */
export interface Feature {
  name: string;
  description: string;
}

/** A file specification produced by the Planner Agent */
export interface FileSpec {
  path: string;
  description: string;
}

/** The structured output of the Planner Agent */
export interface PlannerOutput {
  projectName: string;
  entities: Entity[];
  features: Feature[];
  files: FileSpec[];
}

// ─────────────────────────────────────────────────────────────────────────────
// Code Generator Agent Types
// ─────────────────────────────────────────────────────────────────────────────

/** Status of a generated file */
export type FileStatus = 'pending' | 'generated' | 'error' | 'fixed';

/** A file that has been generated (or attempted) by the Code Generator Agent */
export interface GeneratedFile {
  path: string;
  content: string;
  status: FileStatus;
  errorMessage?: string;
}

/** Context passed to the Code Generator for a single file */
export interface CodeGenContext {
  projectName: string;
  entities: Entity[];
  features: Feature[];
  allFiles: FileSpec[];
  existingFileContents: Map<string, string>;
}

// ─────────────────────────────────────────────────────────────────────────────
// Debug Agent Types
// ─────────────────────────────────────────────────────────────────────────────

/** A runtime error captured during debug execution */
export interface RuntimeError {
  message: string;
  file?: string;
  line?: number;
  column?: number;
  stack: string;
  type: 'syntax' | 'runtime' | 'module' | 'connection' | 'unknown';
}

/** A suggested fix produced by the Debug Agent */
export interface FixSuggestion {
  file: string;
  issue: string;
  fix: string;
  regenerate: boolean;
}

/** The output of the Debug Agent */
export interface DebugResult {
  success: boolean;
  errors: RuntimeError[];
  suggestions: FixSuggestion[];
  stdout: string;
  stderr: string;
  exitCode: number | null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Process Runner Types
// ─────────────────────────────────────────────────────────────────────────────

/** Result from running a child process */
export interface ProcessResult {
  exitCode: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}

// ─────────────────────────────────────────────────────────────────────────────
// AI Client Types
// ─────────────────────────────────────────────────────────────────────────────

/** Request payload for the Flask AI API */
export interface AIRequest {
  model: string;
  prompt: string;
  system?: string;
  temperature?: number;
  max_tokens?: number;
}

/** Response from the Flask AI API */
export interface AIResponse {
  response: string;
  model?: string;
  done?: boolean;
  error?: string;
}

/** Configuration for the AI client */
export interface AIClientConfig {
  flaskBaseUrl: string;
  openaiApiKey: string;
  openaiModel: string;
  localModels: Record<AgentRole, string>;
  requestTimeout: number;
}

// ─────────────────────────────────────────────────────────────────────────────
// System State Types
// ─────────────────────────────────────────────────────────────────────────────

/** Overall system execution status */
export type SystemStatus = 
  | 'idle' 
  | 'planning' 
  | 'generating' 
  | 'debugging' 
  | 'fixing' 
  | 'complete' 
  | 'failed';

/** The entire system state, maintained by the Memory module */
export interface SystemState {
  plan: PlannerOutput | null;
  generatedFiles: Map<string, GeneratedFile>;
  debugHistory: DebugResult[];
  currentAttempt: number;
  status: SystemStatus;
  userPrompt: string;
  projectRoot: string;
  startTime: number;
  errors: string[];
}

// ─────────────────────────────────────────────────────────────────────────────
// Extension Configuration Types
// ─────────────────────────────────────────────────────────────────────────────

/** Full extension configuration derived from VS Code settings */
export interface ExtensionConfig {
  flaskUrl: string;
  openaiApiKey: string;
  openaiModel: string;
  models: Record<AgentRole, string>;
  maxRetries: number;
  debugTimeout: number;
  aiRequestTimeout: number;
}

// ─────────────────────────────────────────────────────────────────────────────
// Orchestrator Types
// ─────────────────────────────────────────────────────────────────────────────

/** Progress callback for the orchestrator to report status to VS Code */
export type ProgressCallback = (message: string, increment?: number) => void;

/** Final result returned by the Orchestrator */
export interface OrchestratorResult {
  success: boolean;
  projectName: string;
  projectRoot: string;
  filesGenerated: number;
  debugAttempts: number;
  errors: string[];
  duration: number;
}
