import json
import logging
import os
import re
import requests
from typing import Optional, Callable, Dict, Any, List

from schema import PlannerOutput, FileSpec
from services.http_settings import get_ssl_verify_setting
from services.openai_compatible_http import build_provider_headers, raise_for_provider_error
from services.architecture_profile_registry import get_architecture_profile, normalize_architecture
from prompts.planner_prompt import PLANNER_SYSTEM_PROMPT, build_planner_prompt

logger = logging.getLogger(__name__)

MANDATORY_FILES = [
    'package.json',
    'app.js',
    'config/db.js',
]

class PlannerAgent:
    MAX_JSON_RETRIES = 2

    def __init__(
        self,
        ollama_url: str,
        model: str,
        use_openai_compatible: bool = False,
        openai_compatible_url: str = "",
        openai_compatible_api_key: str = "",
        openai_compatible_provider: str = "openai-compatible",
    ):
        self.ollama_url = ollama_url
        self.model = model
        self.use_openai_compatible = use_openai_compatible
        self.openai_compatible_api_key = openai_compatible_api_key
        self.openai_compatible_url = openai_compatible_url
        self.openai_compatible_provider = openai_compatible_provider
        self.last_request_trace: Dict[str, Any] = {}

    def execute(self, user_prompt: str, cancel_token: Optional[Callable[[], bool]] = None) -> PlannerOutput:
        logger.info("Starting project planning...")
        plan = None
        last_error = None

        for attempt in range(self.MAX_JSON_RETRIES + 1):
            try:
                if attempt == 0:
                    prompt = build_planner_prompt(user_prompt)
                else:
                    prompt = self._build_retry_prompt(user_prompt, str(last_error))

                logger.info(f"Querying AI (attempt {attempt + 1})...")
                if self.use_openai_compatible:
                    raw_response = self._query_openai_compatible(prompt, PLANNER_SYSTEM_PROMPT, cancel_token)
                    provider = self.openai_compatible_provider
                else:
                    raw_response = self._query_ollama(prompt, PLANNER_SYSTEM_PROMPT, cancel_token)
                    provider = "ollama"

                self.last_request_trace = {
                    "agent": "planner",
                    "provider": provider,
                    "model": self.model,
                    "attempt": attempt + 1,
                    "system_prompt": PLANNER_SYSTEM_PROMPT,
                    "built_prompt": prompt,
                    "raw_output": raw_response,
                }

                plan = self._parse_and_validate(raw_response)
                break
            except Exception as e:
                last_error = e
                logger.warning(f"Attempt {attempt + 1} failed: {e}")
                if attempt == self.MAX_JSON_RETRIES:
                    logger.error("All planning attempts failed")
                    raise RuntimeError(f"Planner Agent failed after {self.MAX_JSON_RETRIES + 1} attempts: {e}")

        if not plan:
            raise RuntimeError("Planner Agent produced no output")

        plan = self._enforce_architecture_selection(plan, user_prompt)
        plan = self._ensure_mandatory_files(plan, user_prompt)

        # ── Agentic Plan Sanitisation ──────────────────────────────────────────────
        plan = self._deduplicate_files(plan)
        plan = self._enforce_path_conventions(plan)
        plan = self._ensure_auth_files(plan, user_prompt)
        plan = self._enforce_architecture_file_structure(plan)
        plan = self._prune_phantom_files(plan)
        # ────────────────────────────────────────────────────────────

        logger.info(f"Planning complete: '{plan.projectName}' — {len(plan.files)} files")
        return plan

    def _query_ollama(self, prompt: str, system_prompt: str, cancel_token: Optional[Callable[[], bool]] = None) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": True,
            "options": {
                "temperature": 0.3,
                "num_predict": 4096,
            }
        }
        resp = requests.post(
            f"{self.ollama_url}/api/generate",
            json=payload,
            timeout=int(os.getenv("MODEL_TIMEOUT", "240")),
            stream=True
        )
        resp.raise_for_status()
        content = ""
        for line in resp.iter_lines():
            if cancel_token and cancel_token():
                resp.close()
                raise Exception("Generation cancelled by user")
            if line:
                data = json.loads(line)
                if "response" in data:
                    content += data["response"]
        return content

    def _query_openai_compatible(self, prompt: str, system_prompt: str, cancel_token: Optional[Callable[[], bool]] = None) -> str:
        if not self.openai_compatible_url:
            raise ValueError("OPENAI_COMPATIBLE_URL is not set")

        headers = build_provider_headers(self.openai_compatible_api_key)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 4096,
            "stream": True
        }
        resp = requests.post(
            self.openai_compatible_url,
            headers=headers,
            json=payload,
            timeout=int(os.getenv("MODEL_TIMEOUT", "240")),
            verify=get_ssl_verify_setting(),
            stream=True
        )
        raise_for_provider_error(resp, self.openai_compatible_provider, self.openai_compatible_url)
        content = ""
        for line in resp.iter_lines():
            if cancel_token and cancel_token():
                resp.close()
                raise Exception("Generation cancelled by user")
            if line:
                line_str = line.decode('utf-8').strip()
                if line_str.startswith("data: ") and line_str != "data: [DONE]":
                    try:
                        data = json.loads(line_str[6:])
                        if data.get("choices") and data["choices"][0].get("delta", {}).get("content"):
                            content += data["choices"][0]["delta"]["content"]
                    except json.JSONDecodeError:
                        pass
        return content

    def _parse_and_validate(self, raw_response: str) -> PlannerOutput:
        # Extract JSON if the model wrapped it in markdown
        json_match = re.search(r'```json\s*(.*?)\s*```', raw_response, re.DOTALL)
        if json_match:
            raw_response = json_match.group(1)
        else:
            # Fallback to finding the first { and last }
            start = raw_response.find('{')
            end = raw_response.rfind('}')
            if start != -1 and end != -1:
                raw_response = raw_response[start:end+1]

        data = json.loads(raw_response)

        # Pydantic validation
        parsed = PlannerOutput(**data)

        # Sanitize project name
        parsed.projectName = re.sub(r'[^a-z0-9]+', '-', parsed.projectName.lower()).strip('-')

        # Clean paths
        for file in parsed.files:
            file.path = re.sub(r'^\.?/', '', file.path)

        return parsed

    def _enforce_architecture_selection(self, plan: PlannerOutput, user_prompt: str) -> PlannerOutput:
        """Use deterministic prompt signals to correct under-specified planner choices."""
        plan.architecture = normalize_architecture(plan.architecture)
        inferred_pattern = self._infer_architecture_pattern(user_prompt, plan)

        if inferred_pattern and inferred_pattern != plan.architecture.pattern:
            logger.info(
                "[planner] Architecture pattern corrected from '%s' to '%s' based on requirement signals",
                plan.architecture.pattern,
                inferred_pattern,
            )
            plan.architecture.pattern = inferred_pattern

        return plan

    def _infer_architecture_pattern(self, user_prompt: str, plan: PlannerOutput) -> Optional[str]:
        prompt = (user_prompt or "").lower()

        clean_signals = {
            "clean architecture",
            "domain/application/infrastructure",
            "domain, application, infrastructure",
            "separate domain",
            "use cases",
            "use-cases",
            "interface controllers",
            "interface routes",
            "persistence adapter",
        }
        if any(signal in prompt for signal in clean_signals):
            return "clean-architecture"

        modular_signals = {
            "modular monolith",
            "domain modules",
            "business modules",
            "module folder",
            "modules",
            "each module",
        }
        if any(signal in prompt for signal in modular_signals):
            return "modular-monolith"

        service_score = 0
        weighted_signal_groups = [
            (2, {"business rule", "business rules", "business logic", "domain operation", "domain operations"}),
            (2, {"workflow", "workflows", "process", "processes"}),
            (2, {"calculate", "calculation", "computed", "derived", "aggregate"}),
            (2, {"report", "reports", "reporting", "analytics", "summary"}),
            (2, {"validate rule", "constraint", "prevent", "limit", "threshold"}),
            (2, {"state transition", "status change", "status update", "approve", "reject"}),
            (2, {"audit", "history", "ledger", "transaction"}),
            (1, {"payment", "permission", "role", "policy"}),
        ]

        for weight, signals in weighted_signal_groups:
            if any(signal in prompt for signal in signals):
                service_score += weight

        operation_words = re.findall(r"\b(?:create|update|delete|get|list|search|filter|sort|assign|track|process|calculate|approve|reject|verify|send|receive|transfer|import|export|generate)\b", prompt)
        if len(set(operation_words)) >= 4:
            service_score += 1

        if len(plan.entities or []) >= 4 and not self._looks_like_crud_only(prompt):
            service_score += 1

        if service_score >= 2:
            return "service-repository"

        return None

    def _looks_like_crud_only(self, prompt: str) -> bool:
        prompt = prompt.lower()
        crud_terms = {"crud", "create", "read", "update", "delete", "get", "list"}
        non_crud_terms = {
            "rule",
            "workflow",
            "calculate",
            "calculation",
            "report",
            "prevent",
            "approve",
            "reject",
            "audit",
            "transaction",
            "payment",
            "permission",
            "role-based",
        }
        return any(term in prompt for term in crud_terms) and not any(term in prompt for term in non_crud_terms)

    def _ensure_mandatory_files(self, plan: PlannerOutput, user_prompt: str = "") -> PlannerOutput:
        existing_paths = {f.path for f in plan.files}
        auth_required = self._requires_auth(plan, user_prompt)

        for mandatory_path in MANDATORY_FILES:
            if mandatory_path not in existing_paths:
                description = self._get_default_description(mandatory_path, plan.projectName)
                plan.files.append(FileSpec(path=mandatory_path, description=description))

        if '.env' not in existing_paths:
            plan.files.append(FileSpec(
                path='.env',
                description=f"Environment variables: PORT, MONGODB_URI for {plan.projectName}, NODE_ENV" + (", JWT_SECRET" if auth_required else "")
            ))

        if 'middleware/errorHandler.js' not in existing_paths:
            plan.files.append(FileSpec(
                path='middleware/errorHandler.js',
                description='Centralized Express error handling middleware that catches all errors and returns formatted JSON responses'
            ))

        return plan

    def _requires_auth(self, plan: PlannerOutput, user_prompt: str) -> bool:
        prompt = (user_prompt or "").lower()
        auth_terms = [
            "auth",
            "authentication",
            "authorization",
            "login",
            "register",
            "registration",
            "jwt",
            "password",
            "protected",
            "role-based",
            "rbac",
        ]
        feature_text = " ".join(
            f"{getattr(feature, 'name', '')} {getattr(feature, 'description', '')}"
            for feature in (plan.features or [])
        ).lower()
        return any(term in prompt or term in feature_text for term in auth_terms)

    def _ensure_auth_files(self, plan: PlannerOutput, user_prompt: str) -> PlannerOutput:
        """Authentication-critical files should be planned when auth is requested."""
        if not self._requires_auth(plan, user_prompt):
            return plan

        existing_paths = {f.path for f in plan.files}
        auth_files = [
            (
                "middleware/auth.js",
                "JWT authentication middleware. Imports jsonwebtoken, verifies Bearer token using JWT_SECRET, attaches decoded user data to req.user, and exports named protect middleware.",
            ),
            (
                "controllers/authController.js",
                "Authentication controller. Imports bcryptjs, jsonwebtoken, and models/User.js. Exports named registerUser, loginUser, and getProfile async handlers.",
            ),
            (
                "routes/authRoutes.js",
                "Authentication routes. Imports express, auth controller functions, and protect middleware. Defines POST /register, POST /login, and GET /profile protected route. Exports default router.",
            ),
        ]

        for path, description in auth_files:
            if path not in existing_paths:
                logger.info("[planner] Auth required: auto-adding '%s'", path)
                plan.files.append(FileSpec(path=path, description=description))
                existing_paths.add(path)

        for f in plan.files:
            if f.path == "package.json":
                if "bcryptjs" not in f.description or "jsonwebtoken" not in f.description:
                    f.description = (
                        f.description.rstrip(".")
                        + ". Includes dependencies express, mongoose, dotenv, cors, bcryptjs, and jsonwebtoken."
                    )
            elif f.path == "app.js" and "routes/authRoutes.js" not in f.description:
                f.description = (
                    f.description.rstrip(".")
                    + ". Imports routes/authRoutes.js and mounts it under /api/auth."
                )

        return plan

    def _get_default_description(self, path: str, project_name: str) -> str:
        descriptions = {
            'app.js': f"Main Express application entry point for {project_name}. Imports dotenv/config, sets up Express middleware (json, cors), connects to MongoDB, mounts all route files, adds error handling middleware, and starts the server on PORT from environment.",
            'package.json': f"NPM package manifest for {project_name}. Sets type to 'module' for ES modules, lists dependencies: express, mongoose, dotenv, cors, bcryptjs, jsonwebtoken. Includes start script.",
            'config/db.js': "MongoDB connection configuration. Exports an async connectDB function that uses mongoose.connect() with MONGODB_URI from process.env. Logs success/failure.",
        }
        return descriptions.get(path, f"Configuration file for {project_name}")

    def _build_retry_prompt(self, user_prompt: str, error_message: str) -> str:
        return f"""Your previous response was not valid JSON. Error: {error_message}

Please try again. Analyze this requirement and output ONLY valid JSON matching the schema in your system prompt.

## USER REQUIREMENT
{user_prompt}

## ARCHITECTURE REQUIREMENT
Include an architecture object with fixed stack values:
{{
  "stack": "node-express-mongoose",
  "pattern": "mvc",
  "language": "javascript",
  "moduleSystem": "esm",
  "database": "mongodb",
  "orm": "mongoose"
}}

Only pattern may change. Allowed pattern values are "mvc", "service-repository", "clean-architecture", and "modular-monolith". If unsure, use "mvc".

Remember: Output ONLY the JSON object. No markdown fences, no explanations, no extra text."""
    # ─────────────────────────────────────────────────────────────────
    # Agentic Plan Sanitisation
    # ─────────────────────────────────────────────────────────────────

    def _deduplicate_files(self, plan: PlannerOutput) -> PlannerOutput:
        """Remove duplicate file paths, keeping the first occurrence."""
        seen = set()
        unique_files = []
        for f in plan.files:
            if f.path not in seen:
                seen.add(f.path)
                unique_files.append(f)
            else:
                logger.warning("[planner] Duplicate file removed from plan: %s", f.path)
        plan.files = unique_files
        return plan

    def _enforce_path_conventions(self, plan: PlannerOutput) -> PlannerOutput:
        """
        Enforce consistent file path conventions:
        - Strip leading ./ from all paths
        - Convert backslashes to forward slashes
        - Warn about files that should be in subdirectories but aren't
        """
        fixed = []
        for f in plan.files:
            original = f.path
            # Normalise slashes and strip leading ./
            f.path = f.path.replace("\\", "/").lstrip("./").lstrip("/")
            # Lowercase the filename (not directory) part for JS files
            parts = f.path.rsplit("/", 1)
            if len(parts) == 2 and f.path.endswith(".js"):
                # Keep directory casing, only check base is reasonable
                pass
            if f.path != original:
                logger.info("[planner] Path normalised: '%s' → '%s'", original, f.path)
            fixed.append(f)
        plan.files = fixed
        return plan

    def _enforce_architecture_file_structure(self, plan: PlannerOutput) -> PlannerOutput:
        """Ensure every entity has the expected files for the selected architecture pattern."""
        plan.architecture = normalize_architecture(plan.architecture)
        profile = get_architecture_profile(plan.architecture)
        existing_paths = {f.path for f in plan.files}
        entity_templates = profile.get("entity_files", [])

        for entity in plan.entities or []:
            entity_name = entity.name
            entity_var = entity_name[0].lower() + entity_name[1:] if entity_name else ""

            for template in entity_templates:
                expected_path = template.format(Entity=entity_name, entity=entity_var)
                if expected_path in existing_paths:
                    continue

                logger.info(
                    "[planner] %s structure: auto-adding missing file '%s' for entity '%s'",
                    plan.architecture.pattern,
                    expected_path,
                    entity_name,
                )
                plan.files.append(
                    FileSpec(
                        path=expected_path,
                        description=self._architecture_file_description(
                            path=expected_path,
                            entity_name=entity_name,
                            entity_var=entity_var,
                            pattern=plan.architecture.pattern,
                        ),
                    )
                )
                existing_paths.add(expected_path)

        return plan

    def _architecture_file_description(
        self,
        path: str,
        entity_name: str,
        entity_var: str,
        pattern: str,
    ) -> str:
        if pattern == "service-repository":
            if path.startswith("repositories/"):
                return f"Repository layer for {entity_name}. Imports models/{entity_name}.js and exports database access functions. Must not use req/res."
            if path.startswith("services/"):
                return f"Service layer for {entity_name}. Imports repositories/{entity_var}Repository.js and exports business logic functions. Must not use req/res."
            if path.startswith("controllers/"):
                return f"HTTP controller for {entity_name}. Imports services/{entity_var}Service.js and exports named async handlers. Must not import models directly."

        if pattern == "clean-architecture":
            if path.startswith("domain/entities/"):
                return f"Domain entity for {entity_name}. Contains domain representation and simple invariants without Express or Mongoose dependencies."
            if path.startswith("application/use-cases/"):
                return f"Application use cases for {entity_name}. Coordinates repository operations and business rules."
            if path.startswith("infrastructure/database/"):
                return f"Infrastructure Mongoose model for {entity_name}. Defines schema and exports default model."
            if path.startswith("infrastructure/repositories/"):
                return f"Infrastructure repository for {entity_name}. Imports infrastructure/database/{entity_name}Model.js and exports persistence functions."
            if path.startswith("interfaces/controllers/"):
                return f"Interface HTTP controller for {entity_name}. Imports application/use-cases/{entity_var}UseCases.js and exports named async handlers."
            if path.startswith("interfaces/routes/"):
                return f"Interface Express router for {entity_name}. Imports controller functions and exports default router."

        if pattern == "modular-monolith":
            module_prefix = f"modules/{entity_var}/"
            if path == f"{module_prefix}model.js":
                return f"Module-local Mongoose model for {entity_name}. Defines schema and exports default model."
            if path == f"{module_prefix}repository.js":
                return f"Module-local repository for {entity_name}. Imports ./model.js and exports database access functions. Must not use req/res."
            if path == f"{module_prefix}service.js":
                return f"Module-local service for {entity_name}. Imports ./repository.js and exports business logic functions. Must not use req/res."
            if path == f"{module_prefix}controller.js":
                return f"Module-local HTTP controller for {entity_name}. Imports ./service.js and exports named async handlers."
            if path == f"{module_prefix}routes.js":
                return f"Module-local Express router for {entity_name}. Imports ./controller.js functions and exports default router."

        if path.startswith("models/"):
            return f"Mongoose model for {entity_name}. Defines schema and exports default model."
        if path.startswith("controllers/"):
            return f"HTTP controller for {entity_name}. Imports models/{entity_name}.js and exports named CRUD async handlers."
        if path.startswith("routes/"):
            return f"Express routes for {entity_name}. Imports named controller functions from controllers/{entity_var}Controller.js and exports default router."

        return f"Architecture-specific file for {entity_name} in {pattern} pattern."

    def _prune_phantom_files(self, plan: PlannerOutput) -> PlannerOutput:
        """
        Remove files whose names reference entities
        that are NOT in the plan's entity list. This prevents hallucinated entity files.
        """
        entity_names_lower = {e.name.lower() for e in (plan.entities or [])}
        if not entity_names_lower:
            return plan  # No entity list to cross-check against

        cross_cutting_files = {
            "controllers/authController.js",
            "routes/authRoutes.js",
            "middleware/auth.js",
            "middleware/errorHandler.js",
        }
        pruned = []
        for f in plan.files:
            if f.path in cross_cutting_files:
                pruned.append(f)
                continue

            path_lower = f.path.lower()
            is_entity_file = (
                f.path.startswith("models/")
                or f.path.startswith("controllers/")
                or f.path.startswith("routes/")
                or f.path.startswith("repositories/")
                or f.path.startswith("services/")
                or f.path.startswith("domain/entities/")
                or f.path.startswith("application/use-cases/")
                or f.path.startswith("infrastructure/database/")
                or f.path.startswith("infrastructure/repositories/")
                or f.path.startswith("interfaces/controllers/")
                or f.path.startswith("interfaces/routes/")
                or f.path.startswith("modules/")
            )
            if not is_entity_file:
                pruned.append(f)
                continue

            # Extract the entity stem from the filename
            base = os.path.basename(path_lower).replace(".js", "")
            # Strip common suffixes
            if f.path.startswith("modules/"):
                parts = f.path.split("/")
                stem = parts[1].lower() if len(parts) > 2 else ""
            else:
                stem = re.sub(
                    r"(controller|controllers|route|routes|model|models|repository|repositories|service|services|usecases|usecase)$",
                    "",
                    base,
                ).strip()

            if not stem:
                pruned.append(f)
                continue

            # Check if stem roughly matches any known entity
            matched = any(stem in ename or ename in stem for ename in entity_names_lower)
            if matched:
                pruned.append(f)
            else:
                logger.warning(
                    "[planner] Pruning phantom file '%s' (entity '%s' not in plan entities: %s)",
                    f.path, stem, list(entity_names_lower)
                )

        plan.files = pruned
        return plan
