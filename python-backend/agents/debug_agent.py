import logging
import re
from typing import List

from schema import DebugResult, RuntimeErrorInfo
from services.process_runner import ProcessRunner

logger = logging.getLogger(__name__)


ERROR_PATTERNS = {
    "SYNTAX_ERROR": re.compile(r"SyntaxError: (.*?)(?:\n|$)"),
    "REFERENCE_ERROR": re.compile(r"ReferenceError: (.*?)(?:\n|$)"),
    "TYPE_ERROR": re.compile(r"TypeError: (.*?)(?:\n|$)"),
    "MODULE_NOT_FOUND": re.compile(r"Cannot find module '(.*?)'"),
    "MODULE_NOT_FOUND_ALT": re.compile(r"ERR_MODULE_NOT_FOUND.*?Cannot find.*?['\"](.*?)['\"]"),
    "ESM_ERROR": re.compile(
        r"Warning: To load an ES module.*?|SyntaxError: Cannot use import statement outside a module"
    ),
    "MONGO_CONNECTION": re.compile(r"Mongo(?:Network)?Error: (.*?)(?:\n|$)"),
    "PORT_IN_USE": re.compile(r"EADDRINUSE.*?(\d+)"),
    "EXPRESS_ROUTER_ERROR": re.compile(r"Router\.use\(\) requires a middleware function.*?(?:\n|$)"),
    "CANNOT_GET": re.compile(r"Cannot GET (.*?)(?:\n|$)"),
    "UNDEFINED_PROPERTY": re.compile(r"Cannot read properties of undefined.*?(?:\n|$)"),
    "STACK_FILE_LINE": re.compile(r"at .*? \((.*?):(\d+):(\d+)\)"),
    "STACK_FILE_LINE_ALT": re.compile(r"at (.*?):(\d+):(\d+)")
}


ENVIRONMENT_ERRORS = [
    re.compile(r"ECONNREFUSED"),
    re.compile(r"EADDRINUSE"),
    re.compile(r"MongoNetworkError"),
    re.compile(r"MongoServerSelectionError")
]


class DebugAgent:
    """
    Debug Agent for PP1 workflow.

    Responsibility:
    - Run generated Node.js project
    - Capture stdout/stderr
    - Parse runtime errors
    - Return DebugResult to Orchestrator

    Important:
    This agent does NOT call AI.
    This agent does NOT generate fixing strategies.
    This agent does NOT generate fixed code.
    """

    def __init__(self, debug_timeout: int = 10000):
        self.debug_timeout = debug_timeout
        self.process_runner = ProcessRunner()

    def execute(self, project_root: str) -> DebugResult:
        logger.info(f"Debug Agent running project at: {project_root}")

        logger.info("Running npm install...")
        install_result = self.process_runner.run_npm_install(project_root)

        if self._has_npm_install_error(install_result):
            logger.error("npm install failed")

            error = RuntimeErrorInfo(
                message="npm install failed",
                stack=install_result["stderr"] or install_result["stdout"],
                type="dependency"
            )

            return DebugResult(
                success=False,
                errors=[error],
                suggestions=[],
                stdout=install_result["stdout"],
                stderr=install_result["stderr"],
                exitCode=install_result["exitCode"]
            )

        logger.info(f"Running node app.js with timeout {self.debug_timeout}ms...")
        run_result = self.process_runner.run_node(
            project_root,
            "app.js",
            self.debug_timeout
        )

        if self.process_runner.is_server_start_success(run_result):
            logger.info("Application started successfully")

            return DebugResult(
                success=True,
                errors=[],
                suggestions=[],
                stdout=run_result["stdout"],
                stderr=run_result["stderr"],
                exitCode=run_result["exitCode"]
            )

        errors = self._parse_errors(run_result)
        logger.info(f"Debug Agent found {len(errors)} error(s)")

        env_errors = [err for err in errors if self._is_environment_error(err)]
        code_errors = [err for err in errors if not self._is_environment_error(err)]

        # For PP1, if only MongoDB/port/environment issue appears,
        # we can treat app structure as acceptable but still report the environment issue.
        if env_errors and not code_errors:
            logger.warning("Only environment errors detected")

            return DebugResult(
                success=True,
                errors=env_errors,
                suggestions=[],
                stdout=run_result["stdout"],
                stderr=run_result["stderr"],
                exitCode=run_result["exitCode"]
            )

        if not code_errors:
            logger.warning("Process failed, but no known error pattern was detected")

            fallback_error = RuntimeErrorInfo(
                message=f"Process exited with code {run_result['exitCode']}",
                stack=run_result["stderr"] or run_result["stdout"],
                type="unknown"
            )

            return DebugResult(
                success=False,
                errors=[fallback_error],
                suggestions=[],
                stdout=run_result["stdout"],
                stderr=run_result["stderr"],
                exitCode=run_result["exitCode"]
            )

        return DebugResult(
            success=False,
            errors=code_errors,
            suggestions=[],
            stdout=run_result["stdout"],
            stderr=run_result["stderr"],
            exitCode=run_result["exitCode"]
        )

    def _has_npm_install_error(self, install_result: dict) -> bool:
        if install_result["exitCode"] == 0:
            return False

        stderr = install_result["stderr"] or ""

        # npm often prints warnings. Warnings should not be treated as real failure.
        real_error = any(
            line.lower().startswith("npm error") or line.startswith("npm ERR!")
            for line in stderr.splitlines()
        )

        return real_error

    def _parse_errors(self, result: dict) -> List[RuntimeErrorInfo]:
        errors: List[RuntimeErrorInfo] = []
        output = result["stderr"] or result["stdout"] or ""

        if not output:
            return errors

        syntax_match = ERROR_PATTERNS["SYNTAX_ERROR"].search(output)
        if syntax_match:
            errors.append(
                self._build_error("syntax", f"SyntaxError: {syntax_match.group(1)}", output)
            )

        ref_match = ERROR_PATTERNS["REFERENCE_ERROR"].search(output)
        if ref_match:
            errors.append(
                self._build_error("runtime", f"ReferenceError: {ref_match.group(1)}", output)
            )

        type_match = ERROR_PATTERNS["TYPE_ERROR"].search(output)
        if type_match:
            errors.append(
                self._build_error("runtime", f"TypeError: {type_match.group(1)}", output)
            )

        module_match = ERROR_PATTERNS["MODULE_NOT_FOUND"].search(output)
        if module_match:
            errors.append(
                self._build_error(
                    "module",
                    f"Cannot find module '{module_match.group(1)}'",
                    output
                )
            )

        module_alt_match = ERROR_PATTERNS["MODULE_NOT_FOUND_ALT"].search(output)
        if module_alt_match and not module_match:
            errors.append(
                self._build_error(
                    "module",
                    f"Cannot find module '{module_alt_match.group(1)}'",
                    output
                )
            )

        esm_match = ERROR_PATTERNS["ESM_ERROR"].search(output)
        if esm_match:
            errors.append(
                self._build_error("module", esm_match.group(0), output)
            )

        mongo_match = ERROR_PATTERNS["MONGO_CONNECTION"].search(output)
        if mongo_match:
            errors.append(
                self._build_error("connection", mongo_match.group(0), output)
            )

        port_match = ERROR_PATTERNS["PORT_IN_USE"].search(output)
        if port_match:
            errors.append(
                self._build_error(
                    "connection",
                    f"Port {port_match.group(1)} already in use",
                    output
                )
            )

        router_match = ERROR_PATTERNS["EXPRESS_ROUTER_ERROR"].search(output)
        if router_match:
            errors.append(
                self._build_error("runtime", router_match.group(0), output)
            )

        undefined_match = ERROR_PATTERNS["UNDEFINED_PROPERTY"].search(output)
        if undefined_match:
            errors.append(
                self._build_error("runtime", undefined_match.group(0), output)
            )

        if not errors and result["exitCode"] != 0:
            first_line = output.splitlines()[0] if output.splitlines() else "Unknown error"

            errors.append(
                RuntimeErrorInfo(
                    message=first_line,
                    stack=output,
                    type="unknown"
                )
            )

        return errors

    def _build_error(self, type_str: str, message: str, full_output: str) -> RuntimeErrorInfo:
        error = RuntimeErrorInfo(
            message=message,
            stack=full_output,
            type=type_str
        )

        file_match = (
            ERROR_PATTERNS["STACK_FILE_LINE"].search(full_output)
            or ERROR_PATTERNS["STACK_FILE_LINE_ALT"].search(full_output)
        )

        if file_match:
            file_path = file_match.group(1)
            segments = file_path.replace("\\", "/").split("/")

            known_dirs = [
                "models",
                "controllers",
                "routes",
                "middleware",
                "config",
                "services",
                "utils"
            ]

            dir_index = next(
                (i for i, segment in enumerate(segments) if segment in known_dirs),
                -1
            )

            if dir_index != -1:
                error.file = "/".join(segments[dir_index:])
            else:
                error.file = segments[-1]

            try:
                error.line = int(file_match.group(2))
                error.column = int(file_match.group(3))
            except ValueError:
                pass

        return error

    def _is_environment_error(self, error: RuntimeErrorInfo) -> bool:
        if error.type == "connection":
            return True

        for pattern in ENVIRONMENT_ERRORS:
            if pattern.search(error.message) or pattern.search(error.stack):
                return True

        return False