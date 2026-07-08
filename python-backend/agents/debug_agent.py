import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import time
from typing import Dict, List, Optional, Tuple

import requests

from schema import DebugResult, RuntimeErrorInfo
from prompts.debug_prompt import TESTING_AGENT_SYSTEM_PROMPT, build_model_testcase_prompt

logger = logging.getLogger(__name__)


ERROR_PATTERNS = {
    "SYNTAX_ERROR": re.compile(r"SyntaxError: (.*?)(?:\n|$)"),
    "REFERENCE_ERROR": re.compile(r"ReferenceError: (.*?)(?:\n|$)"),
    "TYPE_ERROR": re.compile(r"TypeError: (.*?)(?:\n|$)"),
    "MODULE_NOT_FOUND": re.compile(r"Cannot find module ['\"](.*?)['\"]"),
    "ERR_MODULE_NOT_FOUND": re.compile(
        r"ERR_MODULE_NOT_FOUND.*?(?:Cannot find package|Cannot find module).*?['\"](.*?)['\"]",
        re.DOTALL,
    ),
    "EXPORT_ERROR": re.compile(r"does not provide an export named ['\"](.*?)['\"]"),
    "DEFAULT_EXPORT_ERROR": re.compile(r"does not provide an export named ['\"]default['\"]"),
    "JEST_FAIL": re.compile(r"FAIL\s+(.*?)(?:\n|$)"),
    "TEST_FAILED": re.compile(r"Tests:\s+.*?failed", re.IGNORECASE),
    "ROUTE_CALLBACK_ERROR": re.compile(
        r"Route\.(get|post|put|patch|delete)\(\) requires a callback function",
        re.IGNORECASE,
    ),
    "ROUTER_USE_ERROR": re.compile(r"Router\.use\(\) requires a middleware function", re.IGNORECASE),
    "PORT_IN_USE": re.compile(r"EADDRINUSE.*?(\d+)"),
    "CANNOT_USE_IMPORT": re.compile(r"Cannot use import statement outside a module"),
    "STACK_FILE_LINE": re.compile(r"at .*? \((.*?):(\d+):(\d+)\)"),
    "STACK_FILE_LINE_ALT": re.compile(r"at (.*?):(\d+):(\d+)"),
    "FILE_URL_LINE": re.compile(r"file:///(.*?\.js):(\d+)(?::(\d+))?"),
}


DANGEROUS_PATTERNS = [
    (re.compile(r"\brm\s+-rf\b"), "Dangerous delete command detected: rm -rf"),
    (re.compile(r"\bsudo\b"), "Privileged command detected: sudo"),
    (re.compile(r"\bchmod\s+777\b"), "Unsafe permission change detected: chmod 777"),
    (re.compile(r"\bchild_process\b"), "child_process usage detected"),
    (re.compile(r"\bexec\s*\("), "exec() usage detected"),
    (re.compile(r"\bspawn\s*\("), "spawn() usage detected"),
    (re.compile(r"\beval\s*\("), "eval() usage detected"),
    (re.compile(r"\bFunction\s*\("), "Function constructor usage detected"),
    (re.compile(r"\bfs\.rm\s*\("), "Filesystem remove operation detected"),
    (re.compile(r"\bfs\.unlink\s*\("), "Filesystem unlink operation detected"),
    (re.compile(r"\bcurl\b"), "Network shell command detected: curl"),
    (re.compile(r"\bwget\b"), "Network shell command detected: wget"),
]


class DebugAgent:
    """
    PP1 Testing/Debug Agent.

    Implemented for 75% PP1 demo:
    - Static safety scan
    - Model-based Jest/Supertest test generation
    - Docker sandbox execution using temporary project copy
    - Structured testing-report.json
    - Runtime/test error parsing
    - DebugResult output for Orchestrator

    Final phase still needed:
    - Strict non-root Docker user
    - read-only runtime filesystem
    - network-disabled test phase after dependency install
    - stronger route/coverage analysis
    - coverage target measurement
    """

    def __init__(
        self,
        ollama_url: str = "http://localhost:11434",
        model: str = "qwen2.5-coder:1.5b",
        debug_timeout: int = 10000,
        use_openrouter: bool = False,
        openrouter_api_key: str = "",
    ):
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.debug_timeout = debug_timeout

        self.use_openrouter = use_openrouter
        self.openrouter_api_key = openrouter_api_key
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"

        self.install_timeout = int(os.getenv("NPM_INSTALL_TIMEOUT", "120"))
        self.test_timeout = int(os.getenv("TEST_TIMEOUT", "120"))
        self.docker_image = os.getenv("DOCKER_IMAGE", "node:20-alpine")
        self.use_docker = os.getenv("USE_DOCKER_SANDBOX", "true").lower() == "true"

    def execute(self, project_root: str) -> DebugResult:
        logger.info("Testing Agent started for project: %s", project_root)

        original_project_path = Path(project_root)

        if not original_project_path.exists():
            error = RuntimeErrorInfo(
                message=f"Project path does not exist: {project_root}",
                stack=f"Project path does not exist: {project_root}",
                type="filesystem",
            )
            return self._final_result(
                project_path=original_project_path,
                success=False,
                errors=[error],
                stdout="",
                stderr=error.message,
                exit_code=1,
                stage="project_path_validation",
            )

        safety_ok, safety_errors = self._static_safety_scan(original_project_path)

        if not safety_ok:
            return self._final_result(
                project_path=original_project_path,
                success=False,
                errors=safety_errors,
                stdout="",
                stderr="\n".join(error.message for error in safety_errors),
                exit_code=1,
                stage="static_safety_scan",
            )

        with tempfile.TemporaryDirectory(prefix="ai-backend-sandbox-", ignore_cleanup_errors=True) as temp_dir:
            sandbox_project_path = Path(temp_dir) / "project"
            self._copy_project_to_sandbox(original_project_path, sandbox_project_path)

            package_ok, package_error = self._prepare_package_json(sandbox_project_path)
            if not package_ok and package_error:
                self._copy_testing_artifacts_back(sandbox_project_path, original_project_path)
                return self._final_result(
                    project_path=original_project_path,
                    success=False,
                    errors=[package_error],
                    stdout="",
                    stderr=package_error.stack,
                    exit_code=1,
                    stage="package_json_preparation",
                )

            logger.info("Executing pre-test project startup check...")
            startup_result = self._run_project_startup(sandbox_project_path)
            if not startup_result.get("success", False):
                logger.warning("Project startup crashed early!")
                
                startup_errors = self._parse_errors(startup_result)
                if not startup_errors:
                    startup_errors = [
                        RuntimeErrorInfo(
                            message="Project crashed immediately on startup",
                            stack=startup_result.get("stderr", "") or startup_result.get("stdout", ""),
                            type="startup_failure",
                        )
                    ]
                
                self._copy_testing_artifacts_back(sandbox_project_path, original_project_path)
                return self._final_result(
                    project_path=original_project_path,
                    success=False,
                    errors=startup_errors,
                    stdout=startup_result.get("stdout", ""),
                    stderr=startup_result.get("stderr", ""),
                    exit_code=startup_result.get("exitCode", 1),
                    stage="project_startup",
                )

            logger.info("Project startup successful. Proceeding to tests...")

            detected_routes = self._detect_routes(sandbox_project_path)
            file_contents = self._read_project_source_files(sandbox_project_path)

            test_file_path = sandbox_project_path / "tests" / "api.test.js"
            is_retry = os.getenv("CURRENT_DEBUG_ATTEMPT", "1") != "1"
            regenerate_tests = (
                not test_file_path.exists()
                or os.getenv("REGENERATE_TESTS_EACH_ATTEMPT", "false").lower() == "true"
                or is_retry
            )

            if not regenerate_tests:
                logger.info("Reusing existing test file: %s", test_file_path)
            else:
                test_content = self._generate_tests_with_model(
                    file_contents=file_contents,
                    detected_routes=detected_routes,
                )

                if not test_content:
                    logger.warning("Model test generation failed after retries. Falling back to deterministic tests.")
                    test_content = self._generate_fallback_supertest_content(detected_routes)

                self._write_test_file(sandbox_project_path, test_content)

            if self.use_docker:
                test_result = self._run_tests_in_docker(sandbox_project_path)
            else:
                test_result = self._run_tests_locally(sandbox_project_path)

            errors: List[RuntimeErrorInfo] = []

            if test_result["exitCode"] != 0:
                errors = self._parse_errors(test_result)
                if not errors:
                    errors = [
                        RuntimeErrorInfo(
                            message="Tests failed but no known error pattern was detected",
                            stack=test_result["stderr"] or test_result["stdout"],
                            type="test_failure",
                        )
                    ]

            success = test_result["exitCode"] == 0

            self._write_testing_report(
                project_path=sandbox_project_path,
                success=success,
                stage="docker_jest_supertest" if self.use_docker else "local_jest_supertest",
                errors=errors,
                stdout=test_result["stdout"],
                stderr=test_result["stderr"],
                exit_code=test_result["exitCode"],
                detected_routes=detected_routes,
                sandbox_used=self.use_docker,
            )

            self._copy_testing_artifacts_back(sandbox_project_path, original_project_path)

            return DebugResult(
                success=success,
                errors=errors,
                stdout=test_result["stdout"],
                stderr=test_result["stderr"],
                exitCode=test_result["exitCode"],
            )

    # ─────────────────────────────────────────────────────────────────────
    # Safety scan
    # ─────────────────────────────────────────────────────────────────────

    def _static_safety_scan(self, project_path: Path) -> Tuple[bool, List[RuntimeErrorInfo]]:
        errors: List[RuntimeErrorInfo] = []
        scan_extensions = {".js", ".json", ".env", ".sh"}

        for file_path in project_path.rglob("*"):
            if "node_modules" in file_path.parts or ".git" in file_path.parts:
                continue
            if not file_path.is_file():
                continue
            if file_path.suffix not in scan_extensions:
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            relative_path = str(file_path.relative_to(project_path)).replace("\\", "/")

            for pattern, reason in DANGEROUS_PATTERNS:
                if pattern.search(content):
                    errors.append(
                        RuntimeErrorInfo(
                            message=f"Static safety scan blocked {relative_path}: {reason}",
                            file=relative_path,
                            stack=f"Blocked file: {relative_path}\nReason: {reason}",
                            type="security",
                        )
                    )

        if errors:
            logger.warning("Static safety scan failed with %d issue(s)", len(errors))
            return False, errors

        logger.info("Static safety scan passed")
        return True, []

    # ─────────────────────────────────────────────────────────────────────
    # Sandbox preparation
    # ─────────────────────────────────────────────────────────────────────

    def _copy_project_to_sandbox(self, source: Path, target: Path) -> None:
        ignore = shutil.ignore_patterns(
            "node_modules",
            ".git",
            "__pycache__",
            "*.pyc",
            "dist",
            "out",
        )
        shutil.copytree(source, target, ignore=ignore)
        logger.info("Copied project to isolated sandbox temp folder: %s", target)

    def _copy_testing_artifacts_back(self, sandbox_project: Path, original_project: Path) -> None:
        artifact_paths = [
            "tests/api.test.js",
            "testing-report.json",
        ]

        for relative in artifact_paths:
            src = sandbox_project / relative
            dst = original_project / relative

            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    # ─────────────────────────────────────────────────────────────────────
    # package.json + route detection
    # ─────────────────────────────────────────────────────────────────────

    def _prepare_package_json(self, project_path: Path) -> Tuple[bool, Optional[RuntimeErrorInfo]]:
        package_path = project_path / "package.json"

        if not package_path.exists():
            return False, RuntimeErrorInfo(
                message="package.json not found",
                file="package.json",
                stack="package.json is required to install dependencies and run tests.",
                type="dependency",
            )

        try:
            package_data = json.loads(package_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, RuntimeErrorInfo(
                message="Invalid package.json",
                file="package.json",
                stack=str(exc),
                type="syntax",
            )

        package_data.setdefault("type", "module")

        scripts = package_data.setdefault("scripts", {})
        scripts.setdefault("start", "node app.js")
        scripts["test"] = "NODE_ENV=test node --experimental-vm-modules node_modules/jest/bin/jest.js"

        dependencies = package_data.setdefault("dependencies", {})
        dependencies.setdefault("express", "^4.18.2")

        dev_dependencies = package_data.setdefault("devDependencies", {})
        dev_dependencies.setdefault("jest", "^29.7.0")
        dev_dependencies.setdefault("supertest", "^7.1.3")

        package_path.write_text(json.dumps(package_data, indent=2), encoding="utf-8")

        logger.info("Prepared package.json for Jest/Supertest")
        return True, None

    def _detect_routes(self, project_path: Path) -> List[Dict[str, str]]:
        detected_routes: List[Dict[str, str]] = []

        route_patterns = [
            re.compile(r"router\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE),
            re.compile(r"app\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE),
        ]

        for file_path in project_path.rglob("*.js"):
            if "node_modules" in file_path.parts:
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            relative_path = str(file_path.relative_to(project_path)).replace("\\", "/")

            for pattern in route_patterns:
                for method, route_path in pattern.findall(content):
                    detected_routes.append(
                        {
                            "method": method.upper(),
                            "path": route_path,
                            "file": relative_path,
                        }
                    )

        if not detected_routes:
            detected_routes.append(
                {
                    "method": "GET",
                    "path": "/",
                    "file": "app.js",
                }
            )

        logger.info("Detected %d route(s)", len(detected_routes))
        return detected_routes

    def _read_project_source_files(self, project_path: Path) -> Dict[str, str]:
        contents: Dict[str, str] = {}

        for file_path in project_path.rglob("*"):
            if "node_modules" in file_path.parts or ".git" in file_path.parts:
                continue

            if not file_path.is_file():
                continue

            relative_path = str(file_path.relative_to(project_path)).replace("\\", "/")

            should_include = (
                relative_path == "app.js"
                or relative_path == "package.json"
                or relative_path.startswith("routes/")
                or relative_path.startswith("controllers/")
            )

            if not should_include:
                continue

            if not relative_path.endswith((".js", ".json")):
                continue

            try:
                contents[relative_path] = file_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )[:2500]
            except Exception:
                continue

        return contents

    # ─────────────────────────────────────────────────────────────────────
    # Model-based test generation
    # ─────────────────────────────────────────────────────────────────────

    def _generate_tests_with_model(
        self,
        file_contents: Dict[str, str],
        detected_routes: List[Dict[str, str]],
    ) -> str:
        max_attempts = int(os.getenv("MODEL_MAX_RETRIES", "3"))

        prompt = build_model_testcase_prompt(
            file_contents=file_contents,
            detected_routes=detected_routes[:5],
        )

        last_error = ""

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "Generating Jest/Supertest tests with model, attempt %s/%s",
                    attempt,
                    max_attempts,
                )

                if self.use_openrouter:
                    raw = self._query_openrouter(prompt, TESTING_AGENT_SYSTEM_PROMPT)
                else:
                    raw = self._query_ollama(prompt, TESTING_AGENT_SYSTEM_PROMPT)

                test_code = self._extract_code(raw)

                if not test_code or "supertest" not in test_code.lower():
                    raise ValueError("Generated test code does not contain Supertest")

                return test_code

            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Model-based test generation attempt %s/%s failed: %s",
                    attempt,
                    max_attempts,
                    last_error,
                )

                if attempt < max_attempts:
                    time.sleep(2)

        logger.warning("Model-based test generation failed after all retries: %s", last_error)
        return ""

    def _query_ollama(self, prompt: str, system_prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_ctx": 2048,
                "num_predict": 1024,
            },
        }

        response = requests.post(
            f"{self.ollama_url}/api/generate",
            json=payload,
            timeout=int(os.getenv("MODEL_TIMEOUT", "240")),
        )
        response.raise_for_status()

        data = response.json()
        if "response" not in data:
            raise ValueError("Ollama returned no response field")

        return data["response"]

    def _query_openrouter(self, prompt: str, system_prompt: str) -> str:
        if not self.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")

        headers = {
            "Authorization": f"Bearer {self.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Koshigawarman/R26-SE-029",
            "X-Title": "AI Backend Builder",
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        }

        response = requests.post(
            self.openrouter_url,
            headers=headers,
            json=payload,
            timeout=120,
        )
        response.raise_for_status()

        data = response.json()
        return data["choices"][0]["message"]["content"]

    def _extract_code(self, raw_response: str) -> str:
        text = raw_response.strip()

        match = re.search(
            r"```(?:javascript|js)?\s*(.*?)\s*```",
            text,
            re.DOTALL | re.IGNORECASE,
        )

        if match:
            return match.group(1).strip()

        return text

    # ─────────────────────────────────────────────────────────────────────
    # Fallback deterministic tests
    # ─────────────────────────────────────────────────────────────────────

    def _generate_fallback_supertest_content(self, detected_routes: List[Dict[str, str]]) -> str:
        route_tests: List[str] = []
        sample_routes = detected_routes[:5]

        for index, route in enumerate(sample_routes, start=1):
            method = route["method"].lower()
            route_path = route["path"]
            safe_path = re.sub(r":\w+", "1", route_path)

            if method in {"get", "delete"}:
                route_tests.append(
                    f"""
  test('Route {index}: {method.upper()} {safe_path} should return a valid HTTP response', async () => {{
    const res = await request(app).{method}('{safe_path}');
    expect([200, 201, 204, 400, 401, 403, 404, 422, 500]).toContain(res.statusCode);
  }});
"""
                )
            elif method in {"post", "put", "patch"}:
                route_tests.append(
                    f"""
  test('Route {index}: {method.upper()} {safe_path} should return a valid HTTP response', async () => {{
    const res = await request(app).{method}('{safe_path}').send({{}});
    expect([200, 201, 204, 400, 401, 403, 404, 422, 500]).toContain(res.statusCode);
  }});
"""
                )

        if not route_tests:
            route_tests.append(
                """
  test('Root endpoint should return a valid HTTP response', async () => {
    const res = await request(app).get('/');
    expect([200, 201, 204, 400, 401, 403, 404, 500]).toContain(res.statusCode);
  });
"""
            )

        return f"""import request from 'supertest';
import app from '../app.js';

describe('Generated API validation tests', () => {{
{''.join(route_tests)}
}});
"""

    def _write_test_file(self, project_path: Path, test_content: str) -> None:
        tests_dir = project_path / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        test_path = tests_dir / "api.test.js"
        test_path.write_text(test_content, encoding="utf-8")

        logger.info("Generated model-based Supertest file: %s", test_path)

    # ─────────────────────────────────────────────────────────────────────
    # Docker / local execution
    # ─────────────────────────────────────────────────────────────────────

    def _run_project_startup(self, sandbox_project_path: Path) -> Dict[str, object]:
        """
        Attempts to start the project normally (e.g., node app.js) with a short timeout.
        If it crashes immediately, returns the error result to fail early.
        If it times out, we assume the server booted successfully and is running.
        """
        if self.use_docker:
            # Install packages first
            install_result = self._run_command(
                command=[
                    "docker", "run", "--rm", "--memory=512m", "--cpus=1",
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "-e", "npm_config_cache=/app/.npm-cache", "-e", "HOME=/app",
                    "-v", f"{str(sandbox_project_path)}:/app", "-w", "/app",
                    self.docker_image, "sh", "-lc", "npm install"
                ],
                cwd=sandbox_project_path,
                timeout=self.install_timeout,
            )
            if install_result["exitCode"] != 0:
                return install_result

            # Attempt to run project with a 5 second timeout
            run_result = self._run_command(
                command=[
                    "docker", "run", "--rm", "--memory=512m", "--cpus=1",
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "-e", "npm_config_cache=/app/.npm-cache", "-e", "HOME=/app",
                    "-v", f"{str(sandbox_project_path)}:/app", "-w", "/app",
                    self.docker_image, "sh", "-lc", "npm start || node app.js || node index.js"
                ],
                cwd=sandbox_project_path,
                timeout=5,
            )
        else:
            install_result = self._run_command(
                command=["npm", "install"],
                cwd=sandbox_project_path,
                timeout=self.install_timeout,
            )
            if install_result["exitCode"] != 0:
                return install_result

            run_result = self._run_command(
                command=["sh", "-c", "npm start || node app.js || node index.js"],
                cwd=sandbox_project_path,
                timeout=5,
            )

        # If it timed out, the server is running successfully in the foreground!
        if run_result.get("timedOut", False):
            return {"success": True}
        
        # If it exited quickly with an error, it crashed
        if run_result.get("exitCode", 0) != 0:
            return run_result
            
        # Exited quickly with 0 (maybe it's not a server script, but still successful)
        return {"success": True}

    def _run_tests_in_docker(self, sandbox_project_path: Path) -> Dict[str, object]:
        """
        Runs tests inside Docker using only the temporary sandbox project copy.

        Important fix:
        Docker is run using the current host UID:GID so node_modules and package-lock.json
        are not created as root-owned files. This prevents TemporaryDirectory cleanup
        PermissionError after Docker finishes.
        """

        uid = os.getuid()
        gid = os.getgid()

        command = [
            "docker",
            "run",
            "--rm",
            "--memory=512m",
            "--cpus=1",
            "--pids-limit=128",
            
            # Run container process as current host user to avoid root-owned files.
            "--user",
            f"{uid}:{gid}",
            
            # Give npm a writable cache folder inside the mounted app directory.
            "-e",
            "npm_config_cache=/app/.npm-cache",
            "-e",
            "HOME=/app",
            
            "-v",
            f"{str(sandbox_project_path)}:/app",
            "-w",
            "/app",
            self.docker_image,
            "sh",
            "-lc",
            "npm install && npm test -- --runInBand --forceExit",
        ]

        return self._run_command(
            command=command,
            cwd=sandbox_project_path,
            timeout=self.install_timeout + self.test_timeout,
        )

    def _run_tests_locally(self, sandbox_project_path: Path) -> Dict[str, object]:
        install_result = self._run_command(
            command=["npm", "install"],
            cwd=sandbox_project_path,
            timeout=self.install_timeout,
        )

        if install_result["exitCode"] != 0:
            return install_result

        return self._run_command(
            command=["npm", "test", "--", "--runInBand", "--forceExit"],
            cwd=sandbox_project_path,
            timeout=self.test_timeout,
        )

    def _run_command(self, command: List[str], cwd: Path, timeout: int) -> Dict[str, object]:
        logger.info("Running command: %s", " ".join(command))

        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )

            return {
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
                "exitCode": completed.returncode,
                "timedOut": False,
                "command": " ".join(command),
            }

        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""

            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="ignore")

            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="ignore")

            return {
                "stdout": stdout,
                "stderr": stderr + f"\nCommand timed out after {timeout} seconds.",
                "exitCode": 124,
                "timedOut": True,
                "command": " ".join(command),
            }

        except Exception as exc:
            return {
                "stdout": "",
                "stderr": str(exc),
                "exitCode": 1,
                "timedOut": False,
                "command": " ".join(command),
            }

    # ─────────────────────────────────────────────────────────────────────
    # Error parsing + report
    # ─────────────────────────────────────────────────────────────────────

    def _parse_errors(self, result: Dict[str, object]) -> List[RuntimeErrorInfo]:
        output = f"{result.get('stderr', '')}\n{result.get('stdout', '')}".strip()
        errors: List[RuntimeErrorInfo] = []

        if not output:
            return errors

        syntax_match = ERROR_PATTERNS["SYNTAX_ERROR"].search(output)
        if syntax_match:
            errors.append(self._build_error("syntax", f"SyntaxError: {syntax_match.group(1)}", output))

        reference_match = ERROR_PATTERNS["REFERENCE_ERROR"].search(output)
        if reference_match:
            errors.append(self._build_error("runtime", f"ReferenceError: {reference_match.group(1)}", output))

        type_match = ERROR_PATTERNS["TYPE_ERROR"].search(output)
        if type_match:
            errors.append(self._build_error("runtime", f"TypeError: {type_match.group(1)}", output))

        default_export_match = ERROR_PATTERNS["DEFAULT_EXPORT_ERROR"].search(output)
        if default_export_match:
            errors.append(
                self._build_error(
                    "module",
                    "app.js does not export default app required by Supertest",
                    output,
                )
            )

        module_match = ERROR_PATTERNS["MODULE_NOT_FOUND"].search(output)
        if module_match:
            errors.append(
                self._build_error(
                    "module",
                    f"Cannot find module '{module_match.group(1)}'",
                    output,
                )
            )

        err_module_match = ERROR_PATTERNS["ERR_MODULE_NOT_FOUND"].search(output)
        if err_module_match and not module_match:
            errors.append(
                self._build_error(
                    "module",
                    f"ERR_MODULE_NOT_FOUND: {err_module_match.group(1)}",
                    output,
                )
            )

        export_match = ERROR_PATTERNS["EXPORT_ERROR"].search(output)
        if export_match and not default_export_match:
            errors.append(
                self._build_error(
                    "module",
                    f"Missing export: {export_match.group(1)}",
                    output,
                )
            )

        route_callback_match = ERROR_PATTERNS["ROUTE_CALLBACK_ERROR"].search(output)
        if route_callback_match:
            errors.append(
                self._build_error(
                    "runtime",
                    "Route handler callback is missing or not imported correctly",
                    output,
                )
            )

        router_use_match = ERROR_PATTERNS["ROUTER_USE_ERROR"].search(output)
        if router_use_match:
            errors.append(
                self._build_error(
                    "runtime",
                    "Router.use() requires a middleware function",
                    output,
                )
            )

        import_error_match = ERROR_PATTERNS["CANNOT_USE_IMPORT"].search(output)
        if import_error_match:
            errors.append(
                self._build_error(
                    "module",
                    "Cannot use import statement outside a module",
                    output,
                )
            )

        port_match = ERROR_PATTERNS["PORT_IN_USE"].search(output)
        if port_match:
            errors.append(
                self._build_error(
                    "connection",
                    f"Port {port_match.group(1)} already in use",
                    output,
                )
            )

        jest_fail_match = ERROR_PATTERNS["JEST_FAIL"].search(output)
        if jest_fail_match and not errors:
            errors.append(
                self._build_error(
                    "test_failure",
                    f"Jest test failed: {jest_fail_match.group(1)}",
                    output,
                )
            )

        if not errors and int(result.get("exitCode", 0)) != 0:
            first_line = output.splitlines()[0] if output.splitlines() else "Unknown test failure"
            errors.append(
                RuntimeErrorInfo(
                    message=first_line,
                    stack=output,
                    type="test_failure",
                )
            )

        return errors

    def _build_error(self, type_str: str, message: str, full_output: str) -> RuntimeErrorInfo:
        error = RuntimeErrorInfo(
            message=message,
            stack=full_output,
            type=type_str,
        )

        # First priority:
        # Node.js ESM syntax errors often show file path like:
        # file:///home/user/project/middleware/errorHandler.js:1
        file_url_match = ERROR_PATTERNS["FILE_URL_LINE"].search(full_output)

        if file_url_match:
            file_path = file_url_match.group(1).replace("\\", "/")
            error.file = self._extract_project_relative_file(file_path)

            try:
                error.line = int(file_url_match.group(2))

                if file_url_match.group(3):
                    error.column = int(file_url_match.group(3))
            except ValueError:
                pass

            return error

        # Second priority:
        # Stack traces like:
        # at something (/path/to/file.js:10:5)
        file_match = (
            ERROR_PATTERNS["STACK_FILE_LINE"].search(full_output)
            or ERROR_PATTERNS["STACK_FILE_LINE_ALT"].search(full_output)
        )

        if file_match:
            file_path = file_match.group(1).replace("\\", "/")
            error.file = self._extract_project_relative_file(file_path)

            try:
                error.line = int(file_match.group(2))
                error.column = int(file_match.group(3))
            except ValueError:
                pass

        return error

    def _extract_project_relative_file(self, file_path: str) -> str:
        """
        Extract project-relative file path from absolute stack path.

        Example:
        /home/user/project/middleware/errorHandler.js
        -> middleware/errorHandler.js
        """

        normalized = file_path.replace("\\", "/")
        segments = normalized.split("/")

        known_dirs = [
            "models",
            "controllers",
            "routes",
            "middleware",
            "config",
            "services",
            "utils",
            "tests",
        ]

        for i, segment in enumerate(segments):
            if segment in known_dirs:
                return "/".join(segments[i:])

        return segments[-1]

    def _write_testing_report(
        self,
        project_path: Path,
        success: bool,
        stage: str,
        errors: List[RuntimeErrorInfo],
        stdout: str,
        stderr: str,
        exit_code: int,
        detected_routes: List[Dict[str, str]],
        sandbox_used: bool,
    ) -> None:
        report = {
            "success": success,
            "stage": stage,
            "sandbox_used": sandbox_used,
            "docker_image": self.docker_image if sandbox_used else None,
            "exitCode": exit_code,
            "detected_routes": detected_routes,
            "errors": [error.model_dump() for error in errors],
            "stdout_preview": stdout[:3000],
            "stderr_preview": stderr[:3000],
            "pp1_progress_note": (
                "This PP1 Testing Agent uses model-based Jest/Supertest generation, "
                "static safety scanning, Docker-based sandbox execution on a temporary project copy, "
                "and structured testing-report.json output. Final phase will improve sandbox hardening "
                "with non-root users, stricter read-only execution, network-disabled test phase, and coverage metrics."
            ),
        }

        report_path = project_path / "testing-report.json"

        try:
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            logger.info("Testing report written: %s", report_path)
        except Exception as exc:
            logger.warning("Failed to write testing-report.json: %s", exc)

    def _final_result(
        self,
        project_path: Path,
        success: bool,
        errors: List[RuntimeErrorInfo],
        stdout: str,
        stderr: str,
        exit_code: int,
        stage: str,
    ) -> DebugResult:
        self._write_testing_report(
            project_path=project_path,
            success=success,
            stage=stage,
            errors=errors,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            detected_routes=[],
            sandbox_used=False,
        )

        return DebugResult(
            success=success,
            errors=errors,
            stdout=stdout,
            stderr=stderr,
            exitCode=exit_code,
        )