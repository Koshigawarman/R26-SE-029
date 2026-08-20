"""
AI Backend Builder — Pydantic Schemas

Defines all data models shared across agents, services, and the API layer.
Keep this file as the single source of truth for all data contracts.
"""

from typing import List, Optional, Dict, Any
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
    status: str = "pending"  # pending, generated, error, fixed
    errorMessage: Optional[str] = None


class CodeGenContext(BaseModel):
    projectName: str
    entities: List[Entity]
    features: List[Feature]
    allFiles: List[FileSpec]
    existingFileContents: Dict[str, str]



# ─────────────────────────────────────────────────────────────────────────────
# Debug / Testing Agent Schemas
# ─────────────────────────────────────────────────────────────────────────────

class RuntimeErrorInfo(BaseModel):
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    stack: str
    type: str


class DebugResult(BaseModel):
    success: bool
    errors: List[RuntimeErrorInfo] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exitCode: Optional[int] = None

# ─────────────────────────────────────────────────────────────────────────────
# Validation Schemas
# ─────────────────────────────────────────────────────────────────────────────

class ValidationIssue(BaseModel):
    """One issue found by the ProjectConsistencyValidator."""
    type: str                   # missing_local_file | missing_named_export | missing_dependency | invalid_named_import
    source_file: Optional[str] = None
    target_file: Optional[str] = None
    import_path: Optional[str] = None
    missing_export: Optional[str] = None
    package: Optional[str] = None
    message: str


class ValidationResult(BaseModel):
    """Output of the pre-debug consistency validation gate."""
    valid: bool
    issues: List[ValidationIssue] = Field(default_factory=list)
    missing_dependencies: List[str] = Field(default_factory=list)
    auto_fixed_dependencies: List[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Test Result Schemas
# ─────────────────────────────────────────────────────────────────────────────

class TestResults(BaseModel):
    """Summary of Jest/Supertest execution results."""
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: float = 0.0
    stage: str = ""            # e.g. docker_jest_supertest | local_jest_supertest


# ─────────────────────────────────────────────────────────────────────────────
# Episodic Memory Schemas
# ─────────────────────────────────────────────────────────────────────────────

class MemoryCase(BaseModel):
    """
    One error-to-success memory record.

    PP1:
    - Stored in JSON.

    Final:
    - Can be moved into ChromaDB / FAISS.
    """
    error_pattern: str
    root_cause: str
    fix_strategy: str
    affected_files: List[str] = Field(default_factory=list)
    success: bool = True
    usage_count: int = 0
    created_at: Optional[str] = None


class MemoryMatch(BaseModel):
    """
    One retrieved similar memory case.
    """
    case: MemoryCase
    score: float = 0.0
    matched_pattern: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Critic Agent Schemas
# ─────────────────────────────────────────────────────────────────────────────

class CriticStrategy(BaseModel):
    """
    Output from Diagnostic Critic Agent.

    Important:
    The Critic Agent must NOT generate fixed source code.
    It only diagnoses the error and gives a fixing strategy.
    """
    root_cause: str
    affected_files: List[str] = Field(default_factory=list)
    fixing_strategy: str
    instructions_for_code_agent: str
    confidence: float = 0.0



# ─────────────────────────────────────────────────────────────────────────────
# Orchestration / Retry Tracking Schemas
# ─────────────────────────────────────────────────────────────────────────────

class OrchestrationAttempt(BaseModel):
    """
    Used to record each retry attempt for PP1 evidence and debug-report.md.
    """
    attempt: int
    state: str
    success: bool = False
    errors: List[str] = Field(default_factory=list)
    memory_matches_count: int = 0
    critic_strategy: Optional[CriticStrategy] = None
    fixed_files: List[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# API Request / Response Schemas
# ─────────────────────────────────────────────────────────────────────────────

class BuildRequest(BaseModel):
    prompt: str
    workspace_uri: str

    planner_model: Optional[str] = None
    codegen_model: Optional[str] = None

    # Kept only for VS Code extension compatibility.
    # DebugAgent no longer uses a model.
    debug_model: Optional[str] = None

    # Actual model used for Critic Agent.
    critic_model: Optional[str] = None

    max_retries: Optional[int] = None


class BuildResponse(BaseModel):
    success: bool
    projectName: str
    projectRoot: str
    filesGenerated: int
    debugAttempts: int
    errors: List[str]
    duration: float
    testResults: Optional[TestResults] = None
    validationIssues: List[str] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    model: str
    prompt: str
    system: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    

class ApprovalRequest(BaseModel):
    action: str          # "approve" | "skip" | "retry" | "cancel"
    data: dict = {} 