from typing import List, Optional, Dict
from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# Planner Agent Schemas
# ─────────────────────────────────────────────────────────────────────────────

class EntityField(BaseModel):
    name: str
    type: str
    required: bool
    unique: Optional[bool] = False
    default: Optional[str] = None

class Entity(BaseModel):
    name: str
    fields: List[EntityField]
    description: Optional[str] = None

class Feature(BaseModel):
    name: str
    description: str

class FileSpec(BaseModel):
    path: str
    description: str

class PlannerOutput(BaseModel):
    projectName: str
    entities: List[Entity]
    features: List[Feature]
    files: List[FileSpec]

# ─────────────────────────────────────────────────────────────────────────────
# Code Generator Agent Schemas
# ─────────────────────────────────────────────────────────────────────────────

class GeneratedFile(BaseModel):
    path: str
    content: str
    status: str = 'pending' # 'pending', 'generated', 'error', 'fixed'
    errorMessage: Optional[str] = None

class CodeGenContext(BaseModel):
    projectName: str
    entities: List[Entity]
    features: List[Feature]
    allFiles: List[FileSpec]
    existingFileContents: Dict[str, str]

# ─────────────────────────────────────────────────────────────────────────────
# Debug Agent Schemas
# ─────────────────────────────────────────────────────────────────────────────

class RuntimeErrorInfo(BaseModel):
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    stack: str
    type: str # 'syntax', 'runtime', 'module', 'connection', 'unknown'

class FixSuggestion(BaseModel):
    file: str
    issue: str
    fix: str
    regenerate: bool

class DebugResult(BaseModel):
    success: bool
    errors: List[RuntimeErrorInfo]
    suggestions: List[FixSuggestion]
    stdout: str
    stderr: str
    exitCode: Optional[int] = None

# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator Request / Response
# ─────────────────────────────────────────────────────────────────────────────

class BuildRequest(BaseModel):
    prompt: str
    workspace_uri: str
    planner_model: Optional[str] = None
    codegen_model: Optional[str] = None
    debug_model: Optional[str] = None

class BuildResponse(BaseModel):
    success: bool
    projectName: str
    projectRoot: str
    filesGenerated: int
    debugAttempts: int
    errors: List[str]
    duration: float
