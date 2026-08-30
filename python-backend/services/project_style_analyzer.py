import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


class ProjectStyleAnalyzer:
    """Extracts deterministic style signals from an existing Node/Express project."""

    IGNORED_DIRS = {
        ".git",
        "node_modules",
        "dist",
        "build",
        "coverage",
        ".next",
        ".turbo",
        "__pycache__",
        "_research_artifacts",
    }
    TARGET_SUFFIXES = {".js", ".mjs", ".cjs", ".ts"}
    TARGET_PARTS = {
        "models",
        "controllers",
        "routes",
        "middleware",
        "config",
        "services",
        "repositories",
        "modules",
    }

    def analyze(self, source_path: Optional[str], max_files: int = 200) -> Dict[str, Any]:
        if not source_path:
            return self._empty_profile(source_path, "style source was not provided")

        if self._is_git_url(source_path):
            return self._analyze_git_url(source_path, max_files=max_files)

        return self._analyze_local_path(source_path, max_files=max_files)

    def _analyze_git_url(self, source_url: str, max_files: int) -> Dict[str, Any]:
        normalized_url = self._normalize_git_url(source_url)
        if not normalized_url:
            return self._empty_profile(source_url, "unsupported git URL")

        git_path = shutil.which("git")
        if not git_path:
            return self._empty_profile(source_url, "git executable not found")

        with tempfile.TemporaryDirectory(prefix="style-source-") as temp_dir:
            clone_dir = Path(temp_dir) / "repo"
            try:
                subprocess.run(
                    [
                        git_path,
                        "clone",
                        "--depth",
                        "1",
                        "--filter=blob:none",
                        normalized_url,
                        str(clone_dir),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=90,
                )
            except subprocess.TimeoutExpired:
                return self._empty_profile(source_url, "git clone timed out")
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                reason = f"git clone failed: {stderr[-500:]}" if stderr else "git clone failed"
                return self._empty_profile(source_url, reason)

            profile = self._analyze_local_path(
                str(clone_dir),
                max_files=max_files,
                source_type="github",
                original_source=source_url,
            )
            profile["cloned_from"] = normalized_url
            return profile

    def _analyze_local_path(
        self,
        source_path: str,
        max_files: int = 200,
        source_type: str = "local",
        original_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        root = Path(source_path or "").expanduser()
        if not source_path or not root.exists() or not root.is_dir():
            return self._empty_profile(original_source or source_path, "style source folder not found")

        files = self._collect_files(root, max_files=max_files)
        if not files:
            return self._empty_profile(original_source or str(root), "no supported JavaScript/TypeScript files found")

        samples = []
        file_cases = Counter()
        quote_styles = Counter()
        indentation = Counter()
        semicolons = Counter()
        controller_patterns = Counter()
        router_variables = Counter()
        model_exports = Counter()
        error_patterns = Counter()
        route_patterns = Counter()
        middleware_patterns = Counter()
        import_patterns = Counter()
        schema_patterns = Counter()

        for file in files:
            rel = str(file.relative_to(root)).replace("\\", "/")
            content = file.read_text(encoding="utf-8", errors="ignore")
            samples.append(rel)

            file_cases[self._detect_file_case(file.name)] += 1
            quote_styles[self._detect_quote_style(content)] += 1
            indentation[self._detect_indentation(content)] += 1
            semicolons["semicolons" if self._uses_semicolons(content) else "no_semicolons"] += 1

            if self._is_controller(rel):
                controller_patterns.update(self._extract_controller_patterns(content))
                error_patterns.update(self._extract_error_patterns(content))
            if self._is_route(rel):
                route_patterns.update(self._extract_route_patterns(content))
                router_variables.update(self._extract_router_variables(content))
                middleware_patterns.update(self._extract_middleware_patterns(content))
            if self._is_model(rel):
                model_exports.update(self._extract_model_patterns(content))
                schema_patterns.update(self._extract_schema_patterns(content))
            import_patterns.update(self._extract_import_patterns(content))

        profile = {
            "source_path": str(root),
            "source": original_source or str(root),
            "source_type": source_type,
            "file_count": len(files),
            "sample_files": samples[:30],
            "naming": {
                "fileCase": self._top(file_cases, "unknown"),
                "controllerFunctionPattern": self._top(controller_patterns, "named async exports"),
                "routerVariablePattern": self._top(router_variables, "entityRouter"),
                "modelExportPattern": self._top(model_exports, "default mongoose model"),
            },
            "formatting": {
                "quoteStyle": self._top(quote_styles, "single"),
                "indentation": self._top(indentation, "2 spaces"),
                "semicolons": self._top(semicolons, "semicolons") == "semicolons",
            },
            "imports": {
                "usesJsExtension": self._top(import_patterns, "uses .js extension") == "uses .js extension",
                "usesDefaultModelImports": "default model import" in import_patterns,
                "usesNamedControllerImports": "named controller import" in import_patterns,
            },
            "controllerStyle": {
                "exportStyle": self._top(controller_patterns, "named async exports"),
                "usesTryCatch": "try/catch" in error_patterns,
                "usesNextError": "next(error)" in error_patterns,
                "responsePattern": (
                    "res.status(...).json(...)"
                    if "res.status(...).json(...)" in error_patterns
                    else self._top(error_patterns, "res.status(...).json(...)")
                ),
            },
            "routeStyle": {
                "thinRoutes": "thin routes" in route_patterns,
                "routerVariablePattern": self._top(router_variables, "entityRouter"),
                "routeOrder": self._top(route_patterns, "static-before-id"),
                "authMiddlewarePattern": self._top(middleware_patterns, "none"),
            },
            "modelStyle": {
                "usesTimestamps": "timestamps true" in schema_patterns,
                "usesTrimForStrings": "string trim" in schema_patterns,
                "usesEnumForStatus": "status enum" in schema_patterns,
                "modelExport": self._top(model_exports, "default mongoose model"),
            },
            "confidence": self._confidence(len(files)),
        }
        profile["summary"] = self.to_prompt_summary(profile)
        return profile

    def for_target_file(self, profile: Dict[str, Any], target_path: str) -> Dict[str, Any]:
        if not profile or not profile.get("file_count"):
            return {}

        common = {
            "formatting": profile.get("formatting", {}),
            "imports": profile.get("imports", {}),
            "confidence": profile.get("confidence", 0),
        }
        if self._is_model(target_path):
            common["modelStyle"] = profile.get("modelStyle", {})
            common["naming"] = {
                "fileCase": profile.get("naming", {}).get("fileCase"),
                "modelExportPattern": profile.get("naming", {}).get("modelExportPattern"),
            }
        elif self._is_controller(target_path):
            common["controllerStyle"] = profile.get("controllerStyle", {})
            common["naming"] = {
                "fileCase": profile.get("naming", {}).get("fileCase"),
                "controllerFunctionPattern": profile.get("naming", {}).get("controllerFunctionPattern"),
            }
        elif self._is_route(target_path):
            common["routeStyle"] = profile.get("routeStyle", {})
            common["naming"] = {
                "fileCase": profile.get("naming", {}).get("fileCase"),
                "routerVariablePattern": profile.get("naming", {}).get("routerVariablePattern"),
            }
        elif target_path == "middleware/errorHandler.js" or "/middleware/" in target_path:
            common["controllerStyle"] = profile.get("controllerStyle", {})
            common["routeStyle"] = profile.get("routeStyle", {})
        else:
            common["naming"] = profile.get("naming", {})
        return common

    def to_prompt_summary(self, profile: Dict[str, Any]) -> str:
        if not profile or not profile.get("file_count"):
            return "No existing project style profile was detected."

        lines = [
            f"Source files analyzed: {profile.get('file_count', 0)}",
            f"File naming: {profile.get('naming', {}).get('fileCase', 'unknown')}",
            f"Formatting: {profile.get('formatting', {}).get('quoteStyle', 'single')} quotes, {profile.get('formatting', {}).get('indentation', '2 spaces')}, semicolons={profile.get('formatting', {}).get('semicolons', True)}",
            f"Imports: .js extensions={profile.get('imports', {}).get('usesJsExtension', True)}, named controller imports={profile.get('imports', {}).get('usesNamedControllerImports', False)}",
            f"Controllers: {json.dumps(profile.get('controllerStyle', {}), ensure_ascii=False)}",
            f"Routes: {json.dumps(profile.get('routeStyle', {}), ensure_ascii=False)}",
            f"Models: {json.dumps(profile.get('modelStyle', {}), ensure_ascii=False)}",
        ]
        return "\n".join(lines)

    def validate_generated_style(self, path: str, code: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        if not profile or not profile.get("file_count"):
            return {"score": None, "matched": [], "mismatched": [], "reason": "no style profile"}

        matched: List[str] = []
        mismatched: List[str] = []
        fmt = profile.get("formatting", {})
        imports = profile.get("imports", {})

        self._check("quote_style", self._detect_quote_style(code) == fmt.get("quoteStyle"), matched, mismatched)
        self._check("indentation", self._detect_indentation(code) == fmt.get("indentation"), matched, mismatched)
        self._check("semicolons", self._uses_semicolons(code) == fmt.get("semicolons"), matched, mismatched)

        if imports.get("usesJsExtension"):
            self._check("js_import_extensions", not re.search(r"from\s+['\"]\.[^'\"]+(?<!\.js)(?<!\.json)['\"]", code), matched, mismatched)

        if self._is_controller(path):
            style = profile.get("controllerStyle", {})
            self._check("controller_try_catch", ("try {" in code) == style.get("usesTryCatch"), matched, mismatched)
            self._check("controller_next_error", ("next(error)" in code) == style.get("usesNextError"), matched, mismatched)
        elif self._is_route(path):
            self._check("thin_routes", "Model.find" not in code and "try {" not in code, matched, mismatched)
        elif self._is_model(path):
            style = profile.get("modelStyle", {})
            self._check("model_timestamps", ("timestamps: true" in code) == style.get("usesTimestamps"), matched, mismatched)

        total = len(matched) + len(mismatched)
        score = round(len(matched) / total, 3) if total else None
        return {"score": score, "matched": matched, "mismatched": mismatched}

    def _collect_files(self, root: Path, max_files: int) -> List[Path]:
        files: List[Path] = []
        for file in root.rglob("*"):
            if len(files) >= max_files:
                break
            if not file.is_file() or file.suffix not in self.TARGET_SUFFIXES:
                continue
            if any(part in self.IGNORED_DIRS for part in file.parts):
                continue
            rel_parts = set(file.relative_to(root).parts)
            if file.name in {"app.js", "server.js", "index.js"} or rel_parts.intersection(self.TARGET_PARTS):
                files.append(file)
        return sorted(files)

    @staticmethod
    def _empty_profile(source_path: Optional[str], reason: str) -> Dict[str, Any]:
        return {
            "source_path": source_path,
            "source": source_path,
            "source_type": "unknown",
            "file_count": 0,
            "sample_files": [],
            "confidence": 0,
            "reason": reason,
            "summary": "No existing project style profile was detected.",
        }

    @staticmethod
    def _is_git_url(value: str) -> bool:
        value = (value or "").strip()
        return (
            value.startswith("https://github.com/")
            or value.startswith("http://github.com/")
            or value.startswith("git@github.com:")
        )

    @staticmethod
    def _normalize_git_url(value: str) -> Optional[str]:
        value = (value or "").strip()
        if value.startswith("git@github.com:"):
            return value if value.endswith(".git") else f"{value}.git"

        match = re.match(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:\.git)?/?$", value)
        if not match:
            return None
        owner, repo = match.groups()
        if repo.endswith(".git"):
            repo = repo[:-4]
        return f"https://github.com/{owner}/{repo}.git"

    @staticmethod
    def _detect_file_case(name: str) -> str:
        stem = Path(name).stem
        if "-" in stem:
            return "kebab-case"
        if "_" in stem:
            return "snake_case"
        if stem[:1].isupper():
            return "PascalCase"
        return "camelCase"

    @staticmethod
    def _detect_quote_style(content: str) -> str:
        single = len(re.findall(r"'[^'\n]*'", content))
        double = len(re.findall(r'"[^"\n]*"', content))
        return "double" if double > single else "single"

    @staticmethod
    def _detect_indentation(content: str) -> str:
        two = len(re.findall(r"(?m)^  \S", content))
        four = len(re.findall(r"(?m)^    \S", content))
        tabs = len(re.findall(r"(?m)^\t\S", content))
        if tabs > max(two, four):
            return "tabs"
        return "4 spaces" if four > two else "2 spaces"

    @staticmethod
    def _uses_semicolons(content: str) -> bool:
        statement_lines = [line.strip() for line in content.splitlines() if re.search(r"\b(import|export|const|let|return|res\.)\b", line)]
        if not statement_lines:
            return True
        semi = sum(1 for line in statement_lines if line.endswith(";"))
        return semi / max(len(statement_lines), 1) >= 0.5

    @staticmethod
    def _extract_controller_patterns(content: str) -> Iterable[str]:
        if re.search(r"export\s+const\s+\w+\s*=\s*async", content):
            yield "named async exports"
        if re.search(r"export\s+default", content):
            yield "default export"
        if re.search(r"async\s+function\s+\w+", content):
            yield "async function exports"

    @staticmethod
    def _extract_error_patterns(content: str) -> Iterable[str]:
        if "try {" in content and "catch" in content:
            yield "try/catch"
        if "next(error)" in content:
            yield "next(error)"
        if re.search(r"res\.status\s*\([^)]*\)\.json", content):
            yield "res.status(...).json(...)"
        if "success: false" in content:
            yield "success boolean responses"

    @staticmethod
    def _extract_route_patterns(content: str) -> Iterable[str]:
        if "express.Router" in content:
            yield "express router"
        if not re.search(r"\brouter\.(get|post|put|patch|delete)\s*\([^)]*async\s*\(", content, re.S):
            yield "thin routes"
        id_match = re.search(r"\.get\s*\(\s*['\"]/:id['\"]", content)
        static_after_id = id_match and re.search(r"\.(get|post|put|patch|delete)\s*\(\s*['\"]/(?!:id)", content[id_match.end():])
        yield "id-before-static" if static_after_id else "static-before-id"

    @staticmethod
    def _extract_router_variables(content: str) -> Iterable[str]:
        for name in re.findall(r"const\s+(\w+)\s*=\s*express\.Router\s*\(", content):
            if name == "router":
                yield "router"
            elif name.endswith("Router"):
                yield "entityRouter"
            else:
                yield "custom router variable"

    @staticmethod
    def _extract_middleware_patterns(content: str) -> Iterable[str]:
        if re.search(r"\bprotect\b", content):
            yield "protect"
        if re.search(r"\bauthenticate(Token|User)?\b", content):
            yield "authenticate"

    @staticmethod
    def _extract_model_patterns(content: str) -> Iterable[str]:
        if "export default mongoose.model" in content:
            yield "default mongoose model"
        if re.search(r"export\s+default\s+\w+", content):
            yield "default named model"

    @staticmethod
    def _extract_schema_patterns(content: str) -> Iterable[str]:
        if "timestamps: true" in content:
            yield "timestamps true"
        if re.search(r"trim\s*:\s*true", content):
            yield "string trim"
        if re.search(r"\bstatus\b[\s\S]{0,180}enum\s*:", content):
            yield "status enum"

    @staticmethod
    def _extract_import_patterns(content: str) -> Iterable[str]:
        if re.search(r"from\s+['\"]\.[^'\"]+\.js['\"]", content):
            yield "uses .js extension"
        if re.search(r"import\s+\w+\s+from\s+['\"]\.\./models/", content):
            yield "default model import"
        if re.search(r"import\s+\{[^}]+\}\s+from\s+['\"]\.\./controllers/", content):
            yield "named controller import"

    @staticmethod
    def _is_model(path: str) -> bool:
        return path.startswith("models/") or path.endswith("/model.js") or "/models/" in path

    @staticmethod
    def _is_controller(path: str) -> bool:
        return path.startswith("controllers/") or path.endswith("/controller.js") or "/controllers/" in path

    @staticmethod
    def _is_route(path: str) -> bool:
        return path.startswith("routes/") or path.endswith("/routes.js") or "/routes/" in path

    @staticmethod
    def _top(counter: Counter, default: str) -> str:
        return counter.most_common(1)[0][0] if counter else default

    @staticmethod
    def _confidence(file_count: int) -> float:
        if file_count <= 0:
            return 0
        if file_count >= 20:
            return 0.9
        if file_count >= 8:
            return 0.75
        return 0.5

    @staticmethod
    def _check(name: str, passed: bool, matched: List[str], mismatched: List[str]) -> None:
        (matched if passed else mismatched).append(name)
