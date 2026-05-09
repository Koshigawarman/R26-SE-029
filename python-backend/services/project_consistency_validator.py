import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Set

logger = logging.getLogger(__name__)


NODE_BUILTINS = {
    "fs", "path", "os", "http", "https", "crypto", "url",
    "stream", "events", "buffer", "process", "child_process"
}


KNOWN_DEPENDENCY_VERSIONS = {
    "express": "^4.18.2",
    "dotenv": "^16.4.5",
    "cors": "^2.8.5",
    "mongoose": "^8.0.0",
    "helmet": "^7.1.0",
    "morgan": "^1.10.0",
    "compression": "^1.7.4",
    "joi": "^17.13.0",
    "bcrypt": "^5.1.1",
    "jsonwebtoken": "^9.0.2",
    "express-validator": "^7.0.1",
    "axios": "^1.6.0",
    "jest": "^29.7.0",
    "supertest": "^6.3.3",
}


class ProjectConsistencyValidator:
    """
    Validates generated Node.js/Express project consistency before testing.

    It checks:
    - Missing local import files
    - Missing named exports
    - External packages imported but missing from package.json
    """

    def validate(self, project_path: str) -> Dict:
        root = Path(project_path)

        issues: List[Dict] = []
        external_imports: Set[str] = set()

        js_files = [
            file for file in root.rglob("*.js")
            if "node_modules" not in file.parts and ".git" not in file.parts
        ]

        package_dependencies = self._read_package_dependencies(root)

        for js_file in js_files:
            relative_source = str(js_file.relative_to(root)).replace("\\", "/")
            content = js_file.read_text(encoding="utf-8", errors="ignore")

            imports = self._extract_imports(content)

            for imp in imports:
                import_path = imp["path"]

                if self._is_local_import(import_path):
                    resolved = self._resolve_local_import(js_file, import_path)

                    if not resolved.exists():
                        issues.append(
                            {
                                "type": "missing_local_file",
                                "source_file": relative_source,
                                "import_path": import_path,
                                "message": f"{relative_source} imports missing file {import_path}",
                            }
                        )
                        continue

                    named_imports = imp.get("named_imports", [])

                    if named_imports:
                        target_content = resolved.read_text(
                            encoding="utf-8",
                            errors="ignore",
                        )

                        exported_names = self._extract_named_exports(target_content)

                        for name in named_imports:
                            if name not in exported_names:
                                issues.append(
                                    {
                                        "type": "missing_named_export",
                                        "source_file": relative_source,
                                        "target_file": str(resolved.relative_to(root)).replace("\\", "/"),
                                        "missing_export": name,
                                        "message": (
                                            f"{relative_source} imports '{name}', "
                                            f"but it is not exported by {str(resolved.relative_to(root)).replace(chr(92), '/')}"
                                        ),
                                    }
                                )

                else:
                    package_name = self._get_package_name(import_path)
                    if package_name == "express":
                        invalid_express_named_imports = [
                            name for name in imp.get("named_imports", [])
                            if name in ["cors"]
                        ]

                        for name in invalid_express_named_imports:
                            issues.append(
                                {
                                    "type": "invalid_named_import",
                                    "source_file": relative_source,
                                    "package": "express",
                                    "import_name": name,
                                    "message": (
                                        f"{relative_source} imports '{name}' from express, "
                                        f"but '{name}' is not exported by express. "
                                        f"Use import cors from 'cors' only in app.js if cors is needed."
                                    ),
                                }
                            )

                    if package_name not in NODE_BUILTINS:
                        external_imports.add(package_name)

        missing_dependencies = sorted(
            pkg for pkg in external_imports
            if pkg not in package_dependencies
        )

        for pkg in missing_dependencies:
            issues.append(
                {
                    "type": "missing_dependency",
                    "package": pkg,
                    "message": f"External package '{pkg}' is imported but missing from package.json",
                }
            )

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "missing_dependencies": missing_dependencies,
        }

    def sync_package_dependencies(self, project_path: str, packages: List[str]) -> List[str]:
        """
        Adds missing external imports to package.json dependencies.

        Example:
        import helmet from "helmet"
        but package.json does not include helmet.
        This method adds it.
        """

        root = Path(project_path)
        package_path = root / "package.json"

        if not package_path.exists():
            logger.warning("package.json not found, cannot sync dependencies")
            return []

        try:
            data = json.loads(package_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Invalid package.json, cannot sync dependencies: %s", exc)
            return []

        dependencies = data.setdefault("dependencies", {})
        added: List[str] = []

        for pkg in packages:
            if pkg in NODE_BUILTINS:
                continue

            if pkg not in dependencies:
                dependencies[pkg] = KNOWN_DEPENDENCY_VERSIONS.get(pkg, "*")
                added.append(pkg)

        if added:
            package_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("Added missing dependencies to package.json: %s", added)

        return added

    def _read_package_dependencies(self, root: Path) -> Set[str]:
        package_path = root / "package.json"

        if not package_path.exists():
            return set()

        try:
            data = json.loads(package_path.read_text(encoding="utf-8"))
        except Exception:
            return set()

        dependencies = set(data.get("dependencies", {}).keys())
        dev_dependencies = set(data.get("devDependencies", {}).keys())

        return dependencies.union(dev_dependencies)

    def _extract_imports(self, content: str) -> List[Dict]:
        imports: List[Dict] = []

        # import express from "express";
        # import { getTasks, createTask } from "../controllers/taskController.js";
        from_import_pattern = re.compile(
            r"import\s+(.+?)\s+from\s+['\"]([^'\"]+)['\"]",
            re.DOTALL,
        )

        for match in from_import_pattern.finditer(content):
            import_clause = match.group(1).strip()
            import_path = match.group(2).strip()

            named_imports: List[str] = []

            named_match = re.search(r"\{([^}]+)\}", import_clause)
            if named_match:
                raw_names = named_match.group(1).split(",")

                for raw in raw_names:
                    name = raw.strip()

                    if not name:
                        continue

                    # handle: getTasks as getAllTasks
                    if " as " in name:
                        name = name.split(" as ")[0].strip()

                    named_imports.append(name)

            imports.append(
                {
                    "path": import_path,
                    "named_imports": named_imports,
                }
            )

        # import "./config/db.js";
        side_effect_pattern = re.compile(r"import\s+['\"]([^'\"]+)['\"]")

        for match in side_effect_pattern.finditer(content):
            imports.append(
                {
                    "path": match.group(1).strip(),
                    "named_imports": [],
                }
            )

        return imports

    def _extract_named_exports(self, content: str) -> Set[str]:
        exports: Set[str] = set()

        # export const getTasks = ...
        direct_export_pattern = re.compile(
            r"export\s+(?:const|let|var|function|class)\s+([A-Za-z0-9_]+)"
        )

        for match in direct_export_pattern.finditer(content):
            exports.add(match.group(1))

        # export { getTasks, createTask };
        grouped_export_pattern = re.compile(r"export\s*\{([^}]+)\}")

        for match in grouped_export_pattern.finditer(content):
            raw_names = match.group(1).split(",")

            for raw in raw_names:
                name = raw.strip()

                if not name:
                    continue

                if " as " in name:
                    name = name.split(" as ")[1].strip()

                exports.add(name)

        return exports

    def _is_local_import(self, import_path: str) -> bool:
        return import_path.startswith("./") or import_path.startswith("../")

    def _resolve_local_import(self, source_file: Path, import_path: str) -> Path:
        base_path = source_file.parent / import_path

        # ES module best case: already has .js
        if base_path.suffix:
            return base_path.resolve()

        # If extension missing, assume .js
        js_path = Path(str(base_path) + ".js")
        if js_path.exists():
            return js_path.resolve()

        # If importing a folder, try index.js
        index_path = base_path / "index.js"
        return index_path.resolve()

    def _get_package_name(self, import_path: str) -> str:
        if import_path.startswith("@"):
            parts = import_path.split("/")
            return "/".join(parts[:2])

        return import_path.split("/")[0]