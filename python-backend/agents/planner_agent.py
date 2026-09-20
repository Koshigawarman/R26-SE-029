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
from services.plan_memory import PlanMemory

logger = logging.getLogger(__name__)

MANDATORY_FILES = [
    'package.json',
    'README.md',
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
        self.plan_memory = PlanMemory()

    def execute(self, user_prompt: str, cancel_token: Optional[Callable[[], bool]] = None) -> PlannerOutput:
        logger.info("Starting project planning...")
        plan = None
        last_error = None
        
        # RAG: Retrieve similar past approved plan
        similar_plan_json = self.plan_memory.retrieve_similar_plan(user_prompt)

        for attempt in range(self.MAX_JSON_RETRIES + 1):
            try:
                if attempt == 0:
                    prompt = build_planner_prompt(user_prompt, similar_plan_json)
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

        plan = self._sanitize_entity_fields(plan, user_prompt)
        # Apply SRS overrides BEFORE generic inference so the SRS is the source of truth
        plan = self._apply_srs_overrides(plan, user_prompt)
        plan = self._enforce_architecture_selection(plan, user_prompt)
        plan = self._ensure_mandatory_files(plan, user_prompt)

        # ── Agentic Plan Sanitisation ──────────────────────────────────────────────
        plan = self._deduplicate_files(plan)                          # remove AI duplicates first
        plan = self._enforce_path_conventions(plan)
        plan = self._ensure_auth_files(plan, user_prompt)
        plan = self._enforce_architecture_file_structure(plan)
        plan = self._remove_virtual_entity_models(plan)
        plan = self._remove_srs_shadowed_files(plan, user_prompt)
        plan = self._prune_phantom_files(plan)
        plan = self._deduplicate_files(plan)                          # final pass: catch any late-added duplicates
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

    def _apply_srs_overrides(self, plan: PlannerOutput, user_prompt: str) -> PlannerOutput:
        """
        When an SRS document is appended to the prompt, extract its explicitly declared
        architecture, file list, and entity names, then override the AI plan accordingly.
        Runs BEFORE generic inference so the SRS is always the source of truth.

        Entity extraction uses multiple strategies (in priority order) to be robust against
        different PDF-to-text conversion formats:
          1. model file paths  (models/Cart.js → Cart)  ← most reliable
          2. numbered section headers (2.1 User, 2.2 Product)
          3. markdown headers (### User)
          4. capitalized entity names in known table contexts
        """
        from schema import FileSpec, Entity, EntityField
        srs_marker = "--- SRS Document Content ---"
        if srs_marker not in user_prompt:
            return plan

        srs_section = user_prompt.split(srs_marker, 1)[1]
        srs_lower = srs_section.lower()

        # ── Step 1: Lock architecture from SRS ───────────────────────────────
        srs_patterns = [
            ("clean-architecture", ["clean architecture", "clean-architecture"]),
            ("modular-monolith",   ["modular monolith", "modular-monolith"]),
            ("service-repository", ["service-repository", "service repository"]),
            ("mvc",                ["mvc", "model-view-controller",
                                   "model, controller", "models, controllers, routes"]),
        ]
        for pattern, signals in srs_patterns:
            if any(s in srs_lower for s in signals):
                if plan.architecture.pattern != pattern:
                    logger.info(
                        "[planner] SRS override: architecture changed from '%s' to '%s'",
                        plan.architecture.pattern, pattern,
                    )
                    plan.architecture.pattern = pattern
                break  # First match wins

        # ── Step 2: Extract all file paths from SRS ──────────────────────────
        existing_paths = {f.path for f in plan.files}
        allowed_prefixes = (
            "models/", "controllers/", "routes/", "middleware/",
            "config/", "services/", "repositories/", "utils/",
            "helpers/", "interfaces/", "application/", "infrastructure/",
            "domain/", "modules/",
        )
        file_pattern = re.compile(r"[\|\s\-]*([\w./]+\.(?:js|ts))")
        srs_file_paths = []
        for match in file_pattern.finditer(srs_section):
            raw_path = match.group(1).strip()
            if not any(raw_path.startswith(prefix) for prefix in allowed_prefixes):
                continue
            srs_file_paths.append(raw_path)
            if raw_path not in existing_paths:
                logger.info("[planner] SRS override: auto-adding SRS-specified file '%s'", raw_path)
                plan.files.append(FileSpec(
                    path=raw_path,
                    description=f"File specified in the SRS document: {raw_path}.",
                ))
                existing_paths.add(raw_path)

        # ── Step 3: Multi-strategy entity extraction ──────────────────────────
        existing_entity_names_lower = {e.name.lower() for e in (plan.entities or [])}
        plan.entities = list(plan.entities or [])

        generic_words = {
            "purpose", "scope", "technology", "stack", "authentication",
            "authorization", "strategy", "project", "overview", "api",
            "endpoints", "environment", "variables", "file", "structure",
            "functional", "requirements", "section", "notes", "type",
            "field", "required", "jwt", "role", "models", "controllers",
            "routes", "middleware", "config", "services", "database",
            "repositories", "description", "access", "document", "content",
        }

        def inject_entity(name: str) -> None:
            if not name or name.lower() in generic_words:
                return
            if not name[0].isupper():
                return
            if name.lower() in existing_entity_names_lower:
                return
            logger.info("[planner] SRS override: injecting missing entity '%s'", name)
            plan.entities.append(Entity(
                name=name,
                fields=[EntityField(name="id", type="ObjectId", required=False)],
                description=f"Entity extracted from SRS document: {name}.",
            ))
            existing_entity_names_lower.add(name.lower())

        # Strategy A: Derive entities from model file paths (most reliable)
        # models/Cart.js → "Cart"
        model_path_pattern = re.compile(r"models/([A-Z][a-zA-Z]+)\.js")
        for m in model_path_pattern.finditer(srs_section):
            inject_entity(m.group(1))

        # Also extract from all collected SRS file paths (covers e.g. domain/entities/Cart.js)
        entity_from_path_pattern = re.compile(
            r"(?:models|domain/entities|infrastructure/database)/([A-Z][a-zA-Z]+)(?:Model)?\.js"
        )
        for path in srs_file_paths:
            m = entity_from_path_pattern.match(path)
            if m:
                inject_entity(m.group(1))

        # Strategy B: Numbered section headers — "2.1 User", "2.2 Cart"
        numbered_header = re.compile(r"^\s*\d+\.\d+\s+([A-Z][a-zA-Z]+)\s*$", re.MULTILINE)
        for m in numbered_header.finditer(srs_section):
            inject_entity(m.group(1).strip())

        # Strategy C: Markdown / text headers — "### User", "## Cart"
        md_header = re.compile(r"^#{1,4}\s+([A-Z][a-zA-Z]+)\s*$", re.MULTILINE)
        for m in md_header.finditer(srs_section):
            inject_entity(m.group(1).strip())

        # Strategy D: Table/section labels — "Model: Cart", "Entity: Review"
        label_pattern = re.compile(r"(?:model|entity|schema|resource)\s*[:\-]\s*([A-Z][a-zA-Z]+)", re.IGNORECASE)
        for m in label_pattern.finditer(srs_section):
            inject_entity(m.group(1).strip())

        # ── Step 4: Extract non-entity feature controller/route pairs from endpoints ──
        # e.g. SRS has "/api/admin/users" → adminController.js + adminRoutes.js
        # These are cross-cutting features (not Mongoose models) that get missed by
        # entity-based logic above.
        existing_paths = {f.path for f in plan.files}
        entity_names_lower_set = {e.name.lower() for e in plan.entities}

        # Routes to skip — already handled by dedicated methods
        skip_routes = {"auth"}

        def is_entity_route(name: str) -> bool:
            """Return True if this route name maps to an already-known entity.
            Handles both singular and plural forms: 'products' → 'product'.
            """
            if name in entity_names_lower_set:
                return True
            # Try stripping a trailing 's' (simple plural)
            if name.endswith("s") and name[:-1] in entity_names_lower_set:
                return True
            return False

        api_route_pattern = re.compile(r"/api/([a-z][a-z_-]+)(?:/|$)", re.MULTILINE)
        added_non_entity_routes = set()

        for m in api_route_pattern.finditer(srs_section):
            route_name = m.group(1).lower().replace("-", "")
            if route_name in skip_routes:
                continue
            if is_entity_route(route_name):
                continue
            if route_name in added_non_entity_routes:
                continue
            added_non_entity_routes.add(route_name)

            ctrl_path = f"controllers/{route_name}Controller.js"
            route_path = f"routes/{route_name}Routes.js"

            if ctrl_path not in existing_paths:
                logger.info("[planner] SRS override: auto-adding non-entity controller '%s'", ctrl_path)
                plan.files.append(FileSpec(
                    path=ctrl_path,
                    description=f"Controller for /api/{route_name} endpoints as specified in the SRS.",
                ))
                existing_paths.add(ctrl_path)
            if route_path not in existing_paths:
                logger.info("[planner] SRS override: auto-adding non-entity routes '%s'", route_path)
                plan.files.append(FileSpec(
                    path=route_path,
                    description=f"Express router for /api/{route_name} endpoints as specified in the SRS.",
                ))
                existing_paths.add(route_path)

            # Inject a virtual entity for this route name so _prune_phantom_files
            # does NOT prune the controller/routes we just added.
            if route_name not in existing_entity_names_lower:
                logger.info("[planner] SRS override: injecting virtual entity '%s' to protect routes from pruning", route_name)
                plan.entities.append(Entity(
                    name=route_name.capitalize(),
                    fields=[EntityField(name="id", type="ObjectId", required=False)],
                    description=f"Virtual entity for non-model API group '{route_name}' from SRS.",
                ))
                existing_entity_names_lower.add(route_name)

        return plan

    def _enforce_architecture_selection(self, plan: PlannerOutput, user_prompt: str) -> PlannerOutput:
        """Use deterministic prompt signals to correct under-specified planner choices.
        NOTE: This runs AFTER _apply_srs_overrides, so SRS-declared architecture is
        already locked in — we only change the pattern if NO SRS was attached.
        """
        plan.architecture = normalize_architecture(plan.architecture)

        # If SRS declared the architecture, don't override it with keyword inference
        srs_marker = "--- SRS Document Content ---"
        if srs_marker in user_prompt:
            logger.info("[planner] Skipping architecture keyword inference — SRS is the source of truth.")
            return plan

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

        explicit_layering_signals = {
            "service repository",
            "service-repository",
            "repository layer",
            "service layer",
            "layered architecture",
            "separate business logic",
            "separate database access",
        }
        if any(signal in prompt for signal in explicit_layering_signals):
            return "service-repository"

        strong_domain_signals = {
            "business rule",
            "business rules",
            "business logic",
            "workflow",
            "workflows",
            "multi-step",
            "approval workflow",
            "eligibility",
            "risk score",
            "audit trail",
            "audit history",
            "state transition",
            "state transitions",
            "prevent",
            "constraint",
            "constraints",
            "ledger",
            "transactional",
            "consistency",
            "rollback",
            "reconciliation",
        }
        strong_signal_count = sum(1 for signal in strong_domain_signals if signal in prompt)

        calculation_or_reporting = any(
            signal in prompt
            for signal in {"calculate", "calculation", "computed", "derived", "aggregate", "analytics", "reporting", "report"}
        )
        has_guard_rule = any(signal in prompt for signal in {"prevent", "constraint", "must not", "cannot", "no negative", "threshold"})
        has_approval = any(signal in prompt for signal in {"approve", "reject", "approval", "rejection", "eligibility", "risk score"})

        if strong_signal_count >= 2:
            return "service-repository"
        if calculation_or_reporting and (has_guard_rule or has_approval):
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

    def _sanitize_entity_fields(self, plan: PlannerOutput, user_prompt: str) -> PlannerOutput:
        """Remove fields that commonly break generated Mongo/Mongoose CRUD contracts."""
        auth_required = self._requires_auth(plan, user_prompt)

        for entity in plan.entities or []:
            sanitized_fields = []
            for field in entity.fields or []:
                field_name = (field.name or "").strip()
                field_lower = field_name.lower()
                if field_lower in {"id", "_id"}:
                    logger.info("[planner] Removing custom Mongo identity field '%s.%s'; MongoDB provides _id.", entity.name, field_name)
                    continue
                if field_lower == "password" and not auth_required:
                    logger.info("[planner] Removing password field from '%s' because authentication was not requested.", entity.name)
                    continue
                sanitized_fields.append(field)
            entity.fields = sanitized_fields

        return plan

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
            'package.json': f"NPM package manifest for {project_name}. Sets type to 'module' for ES modules, lists runtime dependencies, and includes start, dev, and test scripts. Uses nodemon as a devDependency for the dev script. Includes dependencies express, mongoose, dotenv, cors, bcryptjs, and jsonwebtoken.",
            'README.md': f"Official project documentation for {project_name}. MUST include: 1. Project overview and purpose. 2. List of all API endpoints with HTTP methods and descriptions. 3. Key functions and features. 4. How to setup the environment (.env variables). 5. How to install dependencies and run the project.",
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
        import re
        fixed = []
        for f in plan.files:
            original = f.path
            # Normalise slashes and strip leading ./ or /
            f.path = re.sub(r'^(?:\./|/)+', '', f.path.replace("\\", "/"))
            f.path = self._canonicalize_entity_layer_path(f.path, plan)
            if f.path != original:
                logger.info("[planner] Path normalised: '%s' → '%s'", original, f.path)
            fixed.append(f)
        plan.files = fixed
        return plan

    def _canonicalize_entity_layer_path(self, path: str, plan: PlannerOutput) -> str:
        """Canonicalize generated layer filenames so imports work on case-sensitive systems."""
        if not path.endswith(".js"):
            return path

        entity_names = {entity.name.lower(): entity.name for entity in (plan.entities or [])}
        for entity_lower, entity_name in entity_names.items():
            entity_var = entity_name[0].lower() + entity_name[1:] if entity_name else ""
            base_map = {
                f"{entity_lower}controller.js": f"{entity_var}Controller.js",
                f"{entity_lower}routes.js": f"{entity_var}Routes.js",
                f"{entity_lower}service.js": f"{entity_var}Service.js",
                f"{entity_lower}repository.js": f"{entity_var}Repository.js",
                f"{entity_lower}usecases.js": f"{entity_var}UseCases.js",
                f"{entity_lower}model.js": f"{entity_name}Model.js",
                f"{entity_lower}.js": f"{entity_name}.js",
            }
            directory, filename = path.rsplit("/", 1) if "/" in path else ("", path)
            canonical_filename = base_map.get(filename.lower())
            if not canonical_filename:
                continue
            if directory in {"controllers", "routes", "services", "repositories", "models"}:
                return f"{directory}/{canonical_filename}"
            if directory in {
                "interfaces/controllers",
                "interfaces/routes",
                "application/use-cases",
                "infrastructure/repositories",
                "infrastructure/database",
                "domain/entities",
            }:
                return f"{directory}/{canonical_filename}"

        return path

    def _remove_srs_shadowed_files(self, plan: PlannerOutput, user_prompt: str) -> PlannerOutput:
        """
        When an SRS is attached, remove auto-generated controller/route files for entities
        that the SRS intentionally covers via a differently-named controller.

        Example:
          - SRS specifies authController.js for the User entity (not userController.js)
          - _enforce_architecture_file_structure still auto-generates userController.js
          - This method detects that 'userController' is NOT in the SRS file list
            and removes it to prevent duplicate code.
        """
        import re
        srs_marker = "--- SRS Document Content ---"
        if srs_marker not in user_prompt:
            return plan

        srs_section = user_prompt.split(srs_marker, 1)[1]

        # Collect every controller stem the SRS explicitly names
        # e.g. "authController.js" → "authController"
        srs_controllers = set(
            m.group(1) for m in re.finditer(r"controllers/(\w+Controller)\.js", srs_section)
        )
        if not srs_controllers:
            return plan

        paths_to_remove = set()

        for entity in (plan.entities or []):
            # Virtual entities are handled separately
            if "Virtual entity for non-model" in (entity.description or ""):
                continue

            entity_name = entity.name
            entity_var = entity_name[0].lower() + entity_name[1:] if entity_name else ""
            standard_ctrl_stem = f"{entity_var}Controller"  # e.g. "userController"

            # If SRS explicitly names this entity's standard controller → keep it
            if standard_ctrl_stem in srs_controllers:
                continue

            # SRS does NOT mention this entity's standard controller.
            # The entity is covered by a different SRS-named controller (e.g. authController).
            # Remove the auto-generated pair to prevent duplicate code.
            ctrl_path = f"controllers/{entity_var}Controller.js"
            route_path = f"routes/{entity_var}Routes.js"
            paths_to_remove.add(ctrl_path)
            paths_to_remove.add(route_path)
            logger.info(
                "[planner] SRS shadow: removing auto-generated '%s' — '%s' covers this entity instead",
                ctrl_path,
                [c for c in srs_controllers if entity_var.lower() in c.lower() or c == "authController"],
            )

        if paths_to_remove:
            plan.files = [f for f in plan.files if f.path not in paths_to_remove]

        return plan

    def _remove_virtual_entity_models(self, plan: PlannerOutput) -> PlannerOutput:
        """
        After _enforce_architecture_file_structure runs, remove any models/ files
        that were generated for virtual (non-Mongoose) entities injected by Strategy E.
        Virtual entities only need controller + route files, not Mongoose model files.
        """
        VIRTUAL_MARKER = "Virtual entity for non-model"
        virtual_names = {
            e.name.lower()
            for e in (plan.entities or [])
            if VIRTUAL_MARKER in (e.description or "")
        }
        if not virtual_names:
            return plan

        kept = []
        for f in plan.files:
            if f.path.startswith("models/"):
                # e.g. models/Admin.js → stem = "admin"
                stem = f.path[len("models/"):].replace(".js", "").lower()
                if stem in virtual_names:
                    logger.info("[planner] Removing virtual entity model file: %s", f.path)
                    continue
            kept.append(f)
        plan.files = kept
        return plan

    def _enforce_architecture_file_structure(self, plan: PlannerOutput) -> PlannerOutput:
        """Ensure every entity has the expected files for the selected architecture pattern.
        Skips model file generation for virtual (non-Mongoose) entities injected by Strategy E.
        """
        plan.architecture = normalize_architecture(plan.architecture)
        profile = get_architecture_profile(plan.architecture)
        existing_paths = {f.path for f in plan.files}
        entity_templates = profile.get("entity_files", [])

        # Virtual entities need controller/route files but NOT a Mongoose model
        VIRTUAL_MARKER = "Virtual entity for non-model"

        for entity in plan.entities or []:
            entity_name = entity.name
            entity_var = entity_name[0].lower() + entity_name[1:] if entity_name else ""
            is_virtual = VIRTUAL_MARKER in (entity.description or "")

            for template in entity_templates:
                expected_path = template.format(Entity=entity_name, entity=entity_var)

                # Virtual entities must not get a Mongoose model file
                if is_virtual and expected_path.startswith("models/"):
                    continue

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
