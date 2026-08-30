from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from schema import Architecture


SUPPORTED_ARCHITECTURE_PATTERNS = {
    "mvc",
    "service-repository",
    "clean-architecture",
    "modular-monolith",
}

DEFAULT_STACK = {
    "stack": "node-express-mongoose",
    "language": "javascript",
    "moduleSystem": "esm",
    "database": "mongodb",
    "orm": "mongoose",
}


ARCHITECTURE_PROFILES: Dict[str, Dict[str, Any]] = {
    "mvc": {
        "id": "mvc",
        "name": "Model View Controller",
        "description": "Simple Express MVC backend where controllers contain request handling and CRUD business logic.",
        "folders": ["models", "controllers", "routes", "middleware", "config"],
        "required_files": [
            "package.json",
            "README.md",
            ".env",
            "app.js",
            "config/db.js",
            "middleware/errorHandler.js",
        ],
        "entity_files": [
            "models/{Entity}.js",
            "controllers/{entity}Controller.js",
            "routes/{entity}Routes.js",
        ],
        "dependency_flow": [
            "routes -> controllers",
            "controllers -> models",
            "app -> routes",
            "app -> config/db",
            "app -> middleware/errorHandler",
        ],
        "file_roles": {
            "model": "Defines Mongoose schema and exports default model.",
            "controller": "Imports model directly and exports named async request handlers.",
            "route": "Imports controller functions and maps HTTP routes.",
            "middleware": "Contains Express middleware only.",
            "config": "Contains infrastructure configuration such as database connection.",
            "app": "Composes middleware, routes, database connection, and server startup.",
        },
        "allowed_file_types": ["package", "readme", "env", "app", "config", "model", "controller", "route", "middleware"],
    },
    "service-repository": {
        "id": "service-repository",
        "name": "Service Repository",
        "description": "Layered Express backend where controllers call services, services call repositories, and repositories call models.",
        "folders": ["models", "repositories", "services", "controllers", "routes", "middleware", "config"],
        "required_files": [
            "package.json",
            "README.md",
            ".env",
            "app.js",
            "config/db.js",
            "middleware/errorHandler.js",
        ],
        "entity_files": [
            "models/{Entity}.js",
            "repositories/{entity}Repository.js",
            "services/{entity}Service.js",
            "controllers/{entity}Controller.js",
            "routes/{entity}Routes.js",
        ],
        "dependency_flow": [
            "routes -> controllers",
            "controllers -> services",
            "services -> repositories",
            "repositories -> models",
            "app -> routes",
            "app -> config/db",
            "app -> middleware/errorHandler",
        ],
        "file_roles": {
            "model": "Defines Mongoose schema and exports default model.",
            "repository": "Imports model and exposes database query functions. Must not use req/res.",
            "service": "Imports repository and contains business rules. Must not use req/res.",
            "controller": "Imports service and handles HTTP req/res/next only. Must not import models directly.",
            "route": "Imports controller functions and maps HTTP routes.",
            "middleware": "Contains Express middleware only.",
            "config": "Contains infrastructure configuration such as database connection.",
            "app": "Composes middleware, routes, database connection, and server startup.",
        },
        "allowed_file_types": [
            "package",
            "readme",
            "env",
            "app",
            "config",
            "model",
            "repository",
            "service",
            "controller",
            "route",
            "middleware",
        ],
    },
    "clean-architecture": {
        "id": "clean-architecture",
        "name": "Clean Architecture",
        "description": "Layered backend with domain, application, infrastructure, and interface boundaries.",
        "folders": ["domain", "application", "infrastructure", "interfaces", "middleware", "config"],
        "required_files": [
            "package.json",
            "README.md",
            ".env",
            "app.js",
            "config/db.js",
            "middleware/errorHandler.js",
        ],
        "entity_files": [
            "domain/entities/{Entity}.js",
            "application/use-cases/{entity}UseCases.js",
            "infrastructure/database/{Entity}Model.js",
            "infrastructure/repositories/{entity}Repository.js",
            "interfaces/controllers/{entity}Controller.js",
            "interfaces/routes/{entity}Routes.js",
        ],
        "dependency_flow": [
            "interfaces/routes -> interfaces/controllers",
            "interfaces/controllers -> application/use-cases",
            "application/use-cases -> infrastructure/repositories",
            "infrastructure/repositories -> infrastructure/database models",
            "domain/entities has no outer-layer dependencies",
        ],
        "file_roles": {
            "domain_entity": "Defines domain entity shape and domain-level invariants without Express or Mongoose coupling.",
            "use_case": "Contains application use-case logic and coordinates repositories.",
            "repository": "Implements persistence operations against Mongoose models.",
            "model": "Defines infrastructure Mongoose schema/model.",
            "controller": "Handles HTTP req/res and calls use cases.",
            "route": "Maps HTTP routes to controller functions.",
            "middleware": "Contains Express middleware only.",
            "config": "Contains infrastructure configuration such as database connection.",
            "app": "Composes middleware, routes, database connection, and server startup.",
        },
        "allowed_file_types": [
            "package",
            "readme",
            "env",
            "app",
            "config",
            "domain_entity",
            "use_case",
            "model",
            "repository",
            "controller",
            "route",
            "middleware",
        ],
    },
    "modular-monolith": {
        "id": "modular-monolith",
        "name": "Modular Monolith",
        "description": "Single deployable Express backend organized by business modules.",
        "folders": ["modules", "shared", "middleware", "config"],
        "required_files": [
            "package.json",
            "README.md",
            ".env",
            "app.js",
            "config/db.js",
            "middleware/errorHandler.js",
        ],
        "entity_files": [
            "modules/{entity}/model.js",
            "modules/{entity}/repository.js",
            "modules/{entity}/service.js",
            "modules/{entity}/controller.js",
            "modules/{entity}/routes.js",
        ],
        "dependency_flow": [
            "modules/<module>/routes -> modules/<module>/controller",
            "modules/<module>/controller -> modules/<module>/service",
            "modules/<module>/service -> modules/<module>/repository",
            "modules/<module>/repository -> modules/<module>/model",
            "app -> modules/<module>/routes",
            "shared code may be imported by modules",
        ],
        "file_roles": {
            "model": "Defines module-local Mongoose schema/model.",
            "repository": "Contains module-local database operations. Must not use req/res.",
            "service": "Contains module business logic. Must not use req/res.",
            "controller": "Handles HTTP req/res and calls module service.",
            "route": "Maps module HTTP routes to controller functions.",
            "shared": "Contains reusable cross-module helpers only when planned.",
            "middleware": "Contains Express middleware only.",
            "config": "Contains infrastructure configuration such as database connection.",
            "app": "Composes middleware, module routes, database connection, and server startup.",
        },
        "allowed_file_types": [
            "package",
            "readme",
            "env",
            "app",
            "config",
            "model",
            "repository",
            "service",
            "controller",
            "route",
            "shared",
            "middleware",
        ],
    },
}


def normalize_architecture(architecture: Architecture | Dict[str, Any] | None) -> Architecture:
    """Return a supported architecture object with fixed stack defaults."""
    raw: Dict[str, Any] = {}
    if isinstance(architecture, Architecture):
        raw = architecture.model_dump()
    elif isinstance(architecture, dict):
        raw = dict(architecture)

    pattern = str(raw.get("pattern") or "mvc").strip().lower()
    if pattern not in SUPPORTED_ARCHITECTURE_PATTERNS:
        pattern = "mvc"

    return Architecture(
        stack=DEFAULT_STACK["stack"],
        pattern=pattern,
        language=DEFAULT_STACK["language"],
        moduleSystem=DEFAULT_STACK["moduleSystem"],
        database=DEFAULT_STACK["database"],
        orm=DEFAULT_STACK["orm"],
    )


def get_architecture_profile(architecture: Architecture | Dict[str, Any] | str | None) -> Dict[str, Any]:
    """Resolve a deep-copied architecture profile by Architecture object, dict, or pattern string."""
    if isinstance(architecture, str):
        pattern = architecture.strip().lower()
    else:
        pattern = normalize_architecture(architecture).pattern

    if pattern not in ARCHITECTURE_PROFILES:
        pattern = "mvc"

    profile = deepcopy(ARCHITECTURE_PROFILES[pattern])
    profile["stack"] = deepcopy(DEFAULT_STACK)
    return profile


def list_supported_patterns() -> List[str]:
    return sorted(SUPPORTED_ARCHITECTURE_PATTERNS)


def detect_file_type(path: str, pattern: str = "mvc") -> str:
    """Classify a planned file path into an architecture-aware file type."""
    if path == "package.json":
        return "package"
    if path == "README.md":
        return "readme"
    if path == ".env":
        return "env"
    if path == "app.js":
        return "app"
    if path.startswith("config/"):
        return "config"
    if path.startswith("middleware/"):
        return "middleware"

    if pattern == "clean-architecture":
        if path.startswith("domain/entities/"):
            return "domain_entity"
        if path.startswith("application/use-cases/"):
            return "use_case"
        if path.startswith("infrastructure/database/"):
            return "model"
        if path.startswith("infrastructure/repositories/"):
            return "repository"
        if path.startswith("interfaces/controllers/"):
            return "controller"
        if path.startswith("interfaces/routes/"):
            return "route"

    if pattern == "modular-monolith":
        if path.startswith("modules/"):
            filename = path.rsplit("/", 1)[-1]
            if filename == "model.js":
                return "model"
            if filename == "repository.js":
                return "repository"
            if filename == "service.js":
                return "service"
            if filename == "controller.js":
                return "controller"
            if filename == "routes.js":
                return "route"
        if path.startswith("shared/"):
            return "shared"

    if path.startswith("models/"):
        return "model"
    if path.startswith("repositories/"):
        return "repository"
    if path.startswith("services/"):
        return "service"
    if path.startswith("controllers/"):
        return "controller"
    if path.startswith("routes/"):
        return "route"

    return "unknown"
