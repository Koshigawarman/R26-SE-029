/**
 * AI Backend Builder — Constants & Defaults
 * 
 * Centralized configuration defaults, mandatory file lists,
 * file generation order, and error pattern matchers.
 */

/** Default configuration values matching package.json contributes */
export const DEFAULT_CONFIG = {
  flaskUrl: 'http://localhost:5000',
  openaiApiKey: '',
  openaiModel: 'gpt-4',
  models: {
    planner: 'mistral:7b',
    codegen: 'codellama:13b',
    debug: 'mistral:7b',
  },
  maxRetries: 3,
  debugTimeout: 10_000,
  aiRequestTimeout: 120_000,
} as const;

/** Flask API endpoints */
export const FLASK_ENDPOINTS = {
  generate: '/api/generate',
  health: '/api/health',
  models: '/api/models',
} as const;

/** OpenAI API endpoint */
export const OPENAI_API_URL = 'https://api.openai.com/v1/chat/completions';

/** 
 * Mandatory project files — the Planner Agent MUST include these.
 * If any are missing from the plan, the orchestrator injects them.
 */
export const MANDATORY_FILES = [
  'app.js',
  'package.json',
  'config/db.js',
] as const;

/**
 * Mandatory project directories — created before any files.
 */
export const MANDATORY_DIRECTORIES = [
  'config',
  'models',
  'controllers',
  'routes',
  'middleware',
] as const;

/**
 * File generation order — files are generated in this priority.
 * Files matching earlier prefixes are generated first to ensure
 * dependencies are available when later files reference them.
 */
export const FILE_GENERATION_ORDER: readonly string[] = [
  'package.json',
  'config/',
  '.env',
  'models/',
  'middleware/',
  'controllers/',
  'routes/',
  'app.js',
] as const;

/**
 * Error patterns used by the Debug Agent to classify runtime errors.
 */
export const ERROR_PATTERNS = {
  /** Module not found — usually a missing import or wrong path */
  MODULE_NOT_FOUND: /Cannot find module ['"](.+?)['"]/,

  /** Syntax error — invalid JavaScript */
  SYNTAX_ERROR: /SyntaxError:\s*(.+)/,

  /** Reference error — using undefined variables */
  REFERENCE_ERROR: /ReferenceError:\s*(.+)/,

  /** Type error — wrong type usage */
  TYPE_ERROR: /TypeError:\s*(.+)/,

  /** MongoDB connection error — environment issue, not code bug */
  MONGO_CONNECTION: /MongoServerError|MongooseServerSelectionError|ECONNREFUSED.*27017/,

  /** Port already in use */
  PORT_IN_USE: /EADDRINUSE.*:(\d+)/,

  /** Missing environment variable */
  MISSING_ENV: /Cannot read properties of undefined|process\.env\.(\w+)/,

  /** ES Module import error */
  ESM_ERROR: /ERR_MODULE_NOT_FOUND|ERR_REQUIRE_ESM|Cannot use import statement/,

  /** File path extraction from stack trace */
  STACK_FILE_LINE: /at\s+.+?\((.+?):(\d+):(\d+)\)/,

  /** Alternative stack trace format */
  STACK_FILE_LINE_ALT: /at\s+(.+?):(\d+):(\d+)/,
} as const;

/**
 * Environment issues that should NOT trigger code regeneration.
 * These are infrastructure problems, not bugs in generated code.
 */
export const ENVIRONMENT_ERRORS = [
  ERROR_PATTERNS.MONGO_CONNECTION,
  ERROR_PATTERNS.PORT_IN_USE,
] as const;

/** VS Code output channel name */
export const OUTPUT_CHANNEL_NAME = 'AI Backend Builder';

/** Extension command IDs */
export const COMMANDS = {
  BUILD_BACKEND: 'aiBackendBuilder.buildBackend',
} as const;

/** Maximum length of AI prompt context (characters) to avoid token limits */
export const MAX_CONTEXT_LENGTH = 12_000;

/** Delay between file generation requests (ms) to avoid rate limiting */
export const GENERATION_DELAY = 500;
