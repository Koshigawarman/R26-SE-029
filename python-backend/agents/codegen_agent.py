import logging
import os
import re
import json
import requests
from typing import Optional, Callable, Dict, Any, List

from schema import FileSpec, CodeGenContext, GeneratedFile
from services.codegen_output_validator import CodeGenOutputValidator
from services.http_settings import get_ssl_verify_setting
from services.openai_compatible_http import build_provider_headers, raise_for_provider_error
from services.project_style_analyzer import ProjectStyleAnalyzer
from prompts.codegen_prompt import (
    build_codegen_prompt,
    build_code_fix_prompt,
)
from prompts.codegen_prompt_factory import get_architecture_codegen_system_prompt
from services.code_validator import validate_and_fix

logger = logging.getLogger(__name__)

class CodeGenAgent:
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
        self.output_validator = CodeGenOutputValidator()
        self.style_analyzer = ProjectStyleAnalyzer()

    def execute(self, file_spec: FileSpec, context: CodeGenContext, existing_content: str = None, cancel_token: Optional[Callable[[], bool]] = None) -> GeneratedFile:
        logger.info(f"Generating: {file_spec.path}")

        try:
            # Handle special files deterministically
            if file_spec.path == '.env':
                return self._generate_env_file(file_spec, context)
            if file_spec.path == 'package.json':
                return self._generate_package_json(file_spec, context)
            if file_spec.path == 'README.md':
                return self._generate_readme_file(file_spec, context)

            prompt = build_codegen_prompt(
                file_spec=file_spec,
                project_name=context.projectName,
                entities=context.entities,
                features=context.features,
                all_files=context.allFiles,
                existing_contents=context.existingFileContents,
                existing_file_content=existing_content,
                architecture=context.architecture.model_dump(),
                style_profile=context.styleProfile,
            )
            system_prompt = get_architecture_codegen_system_prompt(
                file_spec.path,
                context.architecture,
            )

            if self.use_openai_compatible:
                raw_response = self._query_openai_compatible(prompt, system_prompt, cancel_token)
                provider = self.openai_compatible_provider
            else:
                raw_response = self._query_ollama(prompt, system_prompt, cancel_token)
                provider = "ollama"

            self.last_request_trace = {
                "agent": "codegen",
                "mode": "generate",
                "provider": provider,
                "model": self.model,
                "target_file": file_spec.path,
                "architecture": context.architecture.model_dump(),
                "style_profile": context.styleProfile,
                "style_context": self.style_analyzer.for_target_file(context.styleProfile, file_spec.path),
                "system_prompt": system_prompt,
                "built_prompt": prompt,
                "raw_output": raw_response,
            }

            code = self._extract_code(raw_response)

            if not code or len(code.strip()) < 10:
                raise ValueError(f"Generated code is too short ({len(code)} chars)")

            # ── Agentic Validation Layer ──────────────────────────────────────
            file_list = [f.path for f in (context.allFiles or [])]
            result = validate_and_fix(
                file_path=file_spec.path,
                code=code,
                file_list=file_list,
                original_code=existing_content,
            )

            if not result.is_valid:
                raise ValueError("; ".join(result.fatal_errors))

            if result.auto_fixes:
                logger.info("[codegen] %d auto-fix(es) applied to %s: %s",
                            len(result.auto_fixes), file_spec.path, result.auto_fixes)

            code = result.final_code
            # ─────────────────────────────────────────────────────────────────
            self._validate_code(file_spec.path, code)
            validation_result = self.output_validator.validate(
                file_spec.path,
                code,
                context.architecture.model_dump(),
            )
            validation_result["style_validation"] = self.style_analyzer.validate_generated_style(
                file_spec.path,
                code,
                context.styleProfile,
            )
            self.last_request_trace["output_validation"] = validation_result

            logger.info(f"✓ Generated: {file_spec.path} ({len(code)} chars)")

            return GeneratedFile(
                path=file_spec.path,
                content=code,
                status='generated'
            )

        except Exception as e:
            logger.error(f"✗ Failed to generate {file_spec.path}: {str(e)}")
            return GeneratedFile(
                path=file_spec.path,
                content='',
                status='error',
                errorMessage=str(e)
            )

    def fix_file_with_strategy(
        self,
        file_path: str,
        original_content: str,
        error_log: str,
        critic_strategy: str,
        instructions_for_code_agent: str,
        file_list: list = None,
        architecture: Dict[str, Any] = None,
        cancel_token: Optional[Callable[[], bool]] = None,
    ) -> GeneratedFile:
        """
        Applies the Critic Agent's fixing strategy to one affected file.

        This method belongs to the CodeGenAgent because:
        - CriticAgent diagnoses and creates strategy only.
        - CodeGenAgent generates the actual fixed code.
        - code_validator then programmatically verifies and auto-fixes the output.
        """

        logger.info(f"Applying critic strategy to fix: {file_path}")

        # Allow up to 2 internal retries if the LLM returns a partial file
        _MAX_INTERNAL_RETRIES = 2

        for internal_attempt in range(1, _MAX_INTERNAL_RETRIES + 1):
            try:
                prompt = build_code_fix_prompt(
                    file_path=file_path,
                    original_content=original_content,
                    error_log=error_log,
                    critic_strategy=critic_strategy,
                    instructions_for_code_agent=instructions_for_code_agent,
                )
                system_prompt = get_architecture_codegen_system_prompt(
                    file_path,
                    architecture,
                    mode="fix",
                )

                if self.use_openai_compatible:
                    raw_response = self._query_openai_compatible(prompt, system_prompt, cancel_token)
                    provider = self.openai_compatible_provider
                else:
                    raw_response = self._query_ollama(prompt, system_prompt, cancel_token)
                    provider = "ollama"

                self.last_request_trace = {
                    "agent": "codegen",
                    "mode": "fix",
                    "provider": provider,
                    "model": self.model,
                    "target_file": file_path,
                    "architecture": architecture or {"pattern": "mvc"},
                    "system_prompt": system_prompt,
                    "built_prompt": prompt,
                    "raw_output": raw_response,
                }

                fixed_code = self._extract_code(raw_response)

                self._validate_code(file_path, fixed_code)
                validation_result = self.output_validator.validate(file_path, fixed_code, architecture)
                self.last_request_trace["output_validation"] = validation_result

                if not fixed_code or len(fixed_code.strip()) < 10:
                    raise ValueError("Generated fixed code is too short")

                # ── Agentic Validation Layer ──────────────────────────────────
                result = validate_and_fix(
                    file_path=file_path,
                    code=fixed_code,
                    file_list=file_list or [],
                    original_code=original_content,  # enables partial-generation guard
                )

                if not result.is_valid:
                    if internal_attempt < _MAX_INTERNAL_RETRIES:
                        logger.warning(
                            "[codegen] Fix attempt %d/%d rejected by validator (%s): %s. Retrying...",
                            internal_attempt, _MAX_INTERNAL_RETRIES, file_path,
                            result.fatal_errors
                        )
                        continue  # retry the LLM call
                    else:
                        raise ValueError("; ".join(result.fatal_errors))

                if result.auto_fixes:
                    logger.info(
                        "[codegen] %d auto-fix(es) applied to %s: %s",
                        len(result.auto_fixes), file_path, result.auto_fixes
                    )

                fixed_code = result.final_code
                # ─────────────────────────────────────────────────────────────

                logger.info(f"✓ Fixed file generated: {file_path} ({len(fixed_code)} chars)")

                return GeneratedFile(
                    path=file_path,
                    content=fixed_code,
                    status="fixed"
                )

            except Exception as e:
                if internal_attempt < _MAX_INTERNAL_RETRIES:
                    logger.warning(
                        "[codegen] Fix attempt %d/%d failed (%s): %s. Retrying...",
                        internal_attempt, _MAX_INTERNAL_RETRIES, file_path, e
                    )
                    continue

                logger.error(f"✗ Failed to fix {file_path}: {str(e)}")
                return GeneratedFile(
                    path=file_path,
                    content="",
                    status="error",
                    errorMessage=str(e)
                )

    def _query_ollama(self, prompt: str, system_prompt: str, cancel_token: Optional[Callable[[], bool]] = None) -> str:
        logger.info("\n" + "="*50)
        logger.info(f"OLLAMA REQUEST [CodeGenAgent] | Model: {self.model}")
        logger.info(f"--- SYSTEM PROMPT ---\n{system_prompt}")
        logger.info(f"--- USER PROMPT ---\n{prompt[:1000]}{'...' if len(prompt) > 1000 else ''}")
        logger.info("="*50)

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
        response_text = ""
        for line in resp.iter_lines():
            if cancel_token and cancel_token():
                resp.close()
                raise Exception("Generation cancelled by user")
            if line:
                import json
                data = json.loads(line)
                if "response" in data:
                    response_text += data["response"]
        logger.info("\n" + "="*50)
        logger.info(f"OLLAMA RESPONSE [CodeGenAgent] | Length: {len(response_text)}")
        logger.info(f"--- CONTENT ---\n{response_text[:1000]}{'...' if len(response_text) > 1000 else ''}")
        logger.info("="*50)

        return response_text

    def _query_openai_compatible(self, prompt: str, system_prompt: str, cancel_token: Optional[Callable[[], bool]] = None) -> str:
        
        logger.info("\n" + "="*50)
        logger.info(f"OPENAI-COMPATIBLE REQUEST [CodeGenAgent] | Provider: {self.openai_compatible_provider} | Model: {self.model}")
        logger.info(f"--- SYSTEM PROMPT ---\n{system_prompt}")
        logger.info(f"--- USER PROMPT ---\n{prompt[:1000]}{'...' if len(prompt) > 1000 else ''}")
        logger.info("="*50)

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
        response_text = ""
        for line in resp.iter_lines():
            if cancel_token and cancel_token():
                resp.close()
                raise Exception("Generation cancelled by user")
            if line:
                line_str = line.decode('utf-8').strip()
                if line_str.startswith("data: ") and line_str != "data: [DONE]":
                    try:
                        import json
                        data = json.loads(line_str[6:])
                        if data.get("choices") and data["choices"][0].get("delta", {}).get("content"):
                            response_text += data["choices"][0]["delta"]["content"]
                    except json.JSONDecodeError:
                        pass
        logger.info("\n" + "="*50)
        logger.info(f"OPENAI-COMPATIBLE RESPONSE [CodeGenAgent] | Length: {len(response_text)}")
        logger.info(f"--- CONTENT ---\n{response_text[:1000]}{'...' if len(response_text) > 1000 else ''}")
        logger.info("="*50)

        return response_text

    def _extract_code(self, raw_response: str) -> str:
        """Extracts code block from markdown if present."""
        code_match = re.search(r'```(?:javascript|js)?\s*(.*?)\s*```', raw_response, re.DOTALL | re.IGNORECASE)
        if code_match:
            return code_match.group(1).strip()
        return raw_response.strip()

    def _generate_env_file(self, file_spec: FileSpec, context: CodeGenContext) -> GeneratedFile:
        db_name = re.sub(r'[^a-z0-9]', '-', context.projectName.lower()).strip('-')
        has_auth = any('auth' in f.name.lower() for f in context.features)

        lines = [
            f"# {context.projectName} Environment Variables",
            "PORT=3000",
            f"MONGODB_URI=mongodb://localhost:27017/{db_name}",
            "NODE_ENV=development",
        ]

        if has_auth:
            lines.append("JWT_SECRET=your_jwt_secret_key_change_in_production")
            lines.append("JWT_EXPIRE=7d")

        lines.append("")
        content = "\n".join(lines)
        self.last_request_trace = {
            "agent": "codegen",
            "mode": "deterministic",
            "provider": "local-template",
            "model": None,
            "target_file": file_spec.path,
            "architecture": context.architecture.model_dump(),
            "style_profile": context.styleProfile,
            "system_prompt": get_architecture_codegen_system_prompt(file_spec.path, context.architecture),
            "built_prompt": "Deterministic .env template generated from project name and features.",
            "raw_output": content,
            "output_validation": self.output_validator.validate(file_spec.path, content, context.architecture.model_dump()),
        }

        logger.info(f"✓ Generated: {file_spec.path} (env file — deterministic)")
        return GeneratedFile(path=file_spec.path, content=content, status='generated')

    def _generate_readme_file(self, file_spec: FileSpec, context: CodeGenContext) -> GeneratedFile:
        architecture = context.architecture
        has_auth = any('auth' in f.name.lower() for f in context.features)
        route_summaries = self._readme_route_summaries(context)
        entity_lines = [
            f"- {entity.name}: " + ", ".join(field.name for field in entity.fields)
            for entity in context.entities
        ]
        feature_lines = [
            f"- {feature.name}: {feature.description}"
            for feature in context.features
        ]
        env_lines = [
            "- `PORT` - HTTP server port.",
            "- `MONGODB_URI` - MongoDB connection string.",
            "- `NODE_ENV` - Runtime environment.",
        ]

        if has_auth:
            env_lines.extend([
                "- `JWT_SECRET` - Secret used to sign JWT access tokens.",
                "- `JWT_EXPIRE` - JWT expiry duration.",
            ])

        content = "\n".join([
            f"# {context.projectName}",
            "",
            f"{context.projectName} is a generated Node.js, Express, and MongoDB backend API.",
            "",
            "## Architecture",
            "",
            f"- Pattern: `{architecture.pattern}`",
            f"- Stack: `{architecture.stack}`",
            f"- Language: `{architecture.language}`",
            f"- Module system: `{architecture.moduleSystem}`",
            f"- Database: `{architecture.database}`",
            f"- ORM: `{architecture.orm}`",
            "",
            "## Features",
            "",
            "\n".join(feature_lines) if feature_lines else "- CRUD API endpoints.",
            "",
            "## Entities",
            "",
            "\n".join(entity_lines) if entity_lines else "- No domain entities were planned.",
            "",
            "## Project Structure",
            "",
            "```text",
            *self._readme_structure_lines(context),
            "```",
            "",
            "## Environment Variables",
            "",
            "\n".join(env_lines),
            "",
            "Create a `.env` file using the generated `.env` template before running the app.",
            "",
            "## Scripts",
            "",
            "- `npm install` - Install dependencies.",
            "- `npm run dev` - Start the API with nodemon.",
            "- `npm start` - Start the API with Node.js.",
            "- `npm test` - Run the test suite.",
            "",
            "## API Routes",
            "",
            "\n".join(route_summaries) if route_summaries else "- Routes are defined in the generated route files.",
            "",
            "## Notes",
            "",
            "- The project uses ES Modules, so local imports include `.js` extensions.",
            "- Database connection is configured in `config/db.js`.",
            "- Centralized error handling is configured in `middleware/errorHandler.js`.",
            "",
        ])

        self.last_request_trace = {
            "agent": "codegen",
            "mode": "deterministic",
            "provider": "local-template",
            "model": None,
            "target_file": file_spec.path,
            "architecture": context.architecture.model_dump(),
            "style_profile": context.styleProfile,
            "system_prompt": get_architecture_codegen_system_prompt(file_spec.path, context.architecture),
            "built_prompt": "Deterministic README.md generated from planner contract, architecture, entities, features, and planned route files.",
            "raw_output": content,
            "output_validation": self.output_validator.validate(file_spec.path, content, context.architecture.model_dump()),
        }

        logger.info(f"✓ Generated: {file_spec.path} (README — deterministic)")
        return GeneratedFile(path=file_spec.path, content=content, status='generated')

    def _readme_structure_lines(self, context: CodeGenContext) -> List[str]:
        paths = sorted({file_spec.path for file_spec in context.allFiles})
        return [f"- {path}" for path in paths]

    def _readme_route_summaries(self, context: CodeGenContext) -> List[str]:
        summaries = []
        route_paths = sorted(
            file_spec.path for file_spec in context.allFiles
            if self._is_route_file(file_spec.path)
        )

        for route_path in route_paths:
            resource = self._resource_from_route_path(route_path)
            if resource == "auth":
                summaries.append(f"- `/api/auth` - Authentication routes such as registration, login, and profile access.")
            else:
                summaries.append(f"- `/api/{resource}` - Routes defined in `{route_path}`.")

        return summaries

    def _is_route_file(self, path: str) -> bool:
        lowered = path.lower()
        return (
            "/routes/" in lowered
            or lowered.startswith("routes/")
            or lowered.endswith("/routes.js")
            or lowered.endswith("routes.js")
        )

    def _resource_from_route_path(self, path: str) -> str:
        normalized = path.replace("\\", "/")
        parts = normalized.split("/")

        if len(parts) >= 3 and parts[0] == "modules" and parts[-1] == "routes.js":
            return self._slugify(parts[1])

        stem = os.path.splitext(parts[-1])[0]
        stem = re.sub(r"Routes$", "", stem)
        stem = re.sub(r"routes$", "", stem)
        resource = self._slugify(stem)

        if resource.endswith("y"):
            return resource[:-1] + "ies"
        if resource.endswith("s"):
            return resource
        return resource + "s"

    def _slugify(self, value: str) -> str:
        value = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
        value = re.sub(r"[^a-zA-Z0-9]+", "-", value)
        return value.strip("-").lower() or "resources"

    def _generate_package_json(self, file_spec: FileSpec, context: CodeGenContext) -> GeneratedFile:
        has_auth = any('auth' in f.name.lower() for f in context.features)
        has_validation = any('valid' in f.name.lower() for f in context.features)

        pkg = {
            "name": context.projectName,
            "version": "1.0.0",
            "description": f"{context.projectName} — Generated by AI Backend Builder",
            "type": "module",
            "main": "app.js",
            "scripts": {
                "start": "node app.js",
                "dev": "nodemon app.js",
                "test": "NODE_ENV=test node --experimental-vm-modules node_modules/jest/bin/jest.js"
            },
            "dependencies": {
                "express": "^4.18.2",
                "mongoose": "^8.0.0",
                "dotenv": "^16.3.1",
                "cors": "^2.8.5"
            },
            "devDependencies": {
                "jest": "^29.7.0",
                "supertest": "^7.1.3",
                "nodemon": "^3.1.0"
            }
        }

        if has_auth:
            pkg["dependencies"]["bcryptjs"] = "^2.4.3"
            pkg["dependencies"]["jsonwebtoken"] = "^9.0.2"
        if has_validation:
            pkg["dependencies"]["express-validator"] = "^7.0.1"

        content = json.dumps(pkg, indent=2) + "\n"
        self.last_request_trace = {
            "agent": "codegen",
            "mode": "deterministic",
            "provider": "local-template",
            "model": None,
            "target_file": file_spec.path,
            "architecture": context.architecture.model_dump(),
            "system_prompt": get_architecture_codegen_system_prompt(file_spec.path, context.architecture),
            "built_prompt": "Deterministic package.json template generated from project name and features.",
            "raw_output": content,
            "output_validation": self.output_validator.validate(file_spec.path, content, context.architecture.model_dump()),
        }

        logger.info(f"✓ Generated: {file_spec.path} (package.json — deterministic)")
        return GeneratedFile(path=file_spec.path, content=content, status='generated')

    def _validate_code(self, path: str, code: str):
        warnings = []
        errors = []

        if 'require(' in code and 'import ' in code:
            errors.append("Mixed require() and import statements detected. Use ES modules only (import/export).")
        elif 'require(' in code:
            errors.append("Using require() instead of ES module imports. Use ES modules only (import/export).")

        if 'module.exports' in code:
            errors.append("Using module.exports instead of ES module exports. Use ES modules only (export default / export const).")

        if '// TODO' in code or '/* TODO' in code:
            warnings.append("Contains TODO placeholders")

        import_regex = re.compile(r"from\s+['\"](\.\/.+?)['\"]")
        for match in import_regex.finditer(code):
            import_path = match.group(1)
            if not import_path.endswith('.js') and not import_path.endswith('.json'):
                errors.append(f"Import '{import_path}' missing .js extension")

        for w in warnings:
            logger.warning(f"{path}: {w}")

        if errors:
            raise ValueError("\n".join(errors))
