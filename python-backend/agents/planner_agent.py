import json
import logging
import os
import re
import requests

from schema import PlannerOutput, FileSpec
from services.http_settings import get_ssl_verify_setting
from services.openai_compatible_http import build_provider_headers, raise_for_provider_error
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

    def execute(self, user_prompt: str) -> PlannerOutput:
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
                    raw_response = self._query_openai_compatible(prompt, PLANNER_SYSTEM_PROMPT)
                    provider = self.openai_compatible_provider
                else:
                    raw_response = self._query_ollama(prompt, PLANNER_SYSTEM_PROMPT)
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

        plan = self._ensure_mandatory_files(plan)

        # ── Agentic Plan Sanitisation ──────────────────────────────────────────────
        plan = self._deduplicate_files(plan)
        plan = self._enforce_path_conventions(plan)
        plan = self._enforce_mvc_pairing(plan)
        plan = self._prune_phantom_files(plan)
        # ────────────────────────────────────────────────────────────

        logger.info(f"Planning complete: '{plan.projectName}' — {len(plan.files)} files")
        return plan

    def _query_ollama(self, prompt: str, system_prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "options": {
                "temperature": 0.3,
                "num_predict": 4096,
            }
        }
        resp = requests.post(
            f"{self.ollama_url}/api/generate",
            json=payload,
            timeout=int(os.getenv("MODEL_TIMEOUT", "240"))
        )
        resp.raise_for_status()
        data = resp.json()
        if "response" not in data:
            raise ValueError("Ollama returned no response")
        return data["response"]

    def _query_openai_compatible(self, prompt: str, system_prompt: str) -> str:
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
        }
        resp = requests.post(
            self.openai_compatible_url,
            headers=headers,
            json=payload,
            timeout=int(os.getenv("MODEL_TIMEOUT", "240")),
            verify=get_ssl_verify_setting(),
        )
        raise_for_provider_error(resp, self.openai_compatible_provider, self.openai_compatible_url)
        data = resp.json()
        return data['choices'][0]['message']['content']

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

    def _ensure_mandatory_files(self, plan: PlannerOutput) -> PlannerOutput:
        existing_paths = {f.path for f in plan.files}

        for mandatory_path in MANDATORY_FILES:
            if mandatory_path not in existing_paths:
                description = self._get_default_description(mandatory_path, plan.projectName)
                plan.files.append(FileSpec(path=mandatory_path, description=description))

        if '.env' not in existing_paths:
            plan.files.append(FileSpec(
                path='.env',
                description=f"Environment variables: PORT, MONGODB_URI for {plan.projectName}, NODE_ENV, JWT_SECRET"
            ))

        if 'middleware/errorHandler.js' not in existing_paths:
            plan.files.append(FileSpec(
                path='middleware/errorHandler.js',
                description='Centralized Express error handling middleware that catches all errors and returns formatted JSON responses'
            ))

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

    def _enforce_mvc_pairing(self, plan: PlannerOutput) -> PlannerOutput:
        """
        For every route file routes/X.js, ensure controllers/XController.js exists.
        For every controller file controllers/X.js, ensure models/X.js exists (with fuzzy match).
        Auto-inserts missing paired files into the plan.
        """
        import difflib
        existing_paths = {f.path for f in plan.files}

        routes   = [f for f in plan.files if f.path.startswith("routes/") and f.path.endswith(".js")]
        ctrls    = {f.path for f in plan.files if f.path.startswith("controllers/")}
        models   = {f.path for f in plan.files if f.path.startswith("models/")}

        for route_file in routes:
            # Derive expected controller name: routes/menuRoutes.js → controllers/menuController.js
            base = os.path.basename(route_file.path)  # menuRoutes.js
            # Strip common suffixes to get entity stem
            stem = re.sub(r'(routes|Routes)\.js$', '', base).strip()
            expected_ctrl = f"controllers/{stem}Controller.js"

            if expected_ctrl not in existing_paths:
                # Try fuzzy match among existing controllers
                close = difflib.get_close_matches(expected_ctrl, ctrls, n=1, cutoff=0.6)
                if not close:
                    logger.info(
                        "[planner] MVC pairing: auto-adding missing controller '%s' for route '%s'",
                        expected_ctrl, route_file.path
                    )
                    plan.files.append(FileSpec(
                        path=expected_ctrl,
                        description=f"Express controller for {stem} entity. Exports named CRUD async functions."
                    ))
                    existing_paths.add(expected_ctrl)

        return plan

    def _prune_phantom_files(self, plan: PlannerOutput) -> PlannerOutput:
        """
        Remove files whose names reference entities (models/X, controllers/X, routes/X)
        that are NOT in the plan's entity list. This prevents hallucinated entity files.
        """
        entity_names_lower = {e.name.lower() for e in (plan.entities or [])}
        if not entity_names_lower:
            return plan  # No entity list to cross-check against

        pruned = []
        for f in plan.files:
            path_lower = f.path.lower()
            # Only check model/controller/route files
            is_entity_file = (
                f.path.startswith("models/")
                or f.path.startswith("controllers/")
                or f.path.startswith("routes/")
            )
            if not is_entity_file:
                pruned.append(f)
                continue

            # Extract the entity stem from the filename
            base = os.path.basename(path_lower).replace(".js", "")
            # Strip common suffixes
            stem = re.sub(r"(controller|controllers|route|routes|model|models)$", "", base).strip()

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
