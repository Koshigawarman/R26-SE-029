import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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
    "nodemon": "^3.1.0",
}


class ProjectConsistencyValidator:
    """
    Validates generated Node.js/Express project consistency before testing.

    It checks:
    - Missing local import files
    - Missing named exports
    - External packages imported but missing from package.json
    """

    def validate(
        self,
        project_path: str,
        planner_output: Optional[Dict[str, Any]] = None,
        operations: Optional[List[Dict[str, str]]] = None,
    ) -> Dict:
        root = Path(project_path).resolve()

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
            self._validate_file_structure(relative_source, content, issues)
            self._validate_symbol_usage(relative_source, content, imports, issues)

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

                    if not self._has_exact_path_case(root, resolved):
                        issues.append(
                            {
                                "type": "local_import_case_mismatch",
                                "source_file": relative_source,
                                "import_path": import_path,
                                "target_file": str(resolved.relative_to(root)).replace("\\", "/"),
                                "message": f"{relative_source} imports {import_path}, but the path casing does not exactly match the filesystem.",
                            }
                        )

                    named_imports = imp.get("named_imports", [])
                    default_import = imp.get("default_import")
                    target_content = resolved.read_text(
                        encoding="utf-8",
                        errors="ignore",
                    )

                    if default_import and not self._has_default_export(target_content):
                        issues.append(
                            {
                                "type": "missing_default_export",
                                "source_file": relative_source,
                                "target_file": str(resolved.relative_to(root)).replace("\\", "/"),
                                "import_name": default_import,
                                "message": (
                                    f"{relative_source} imports default '{default_import}', "
                                    f"but {str(resolved.relative_to(root)).replace(chr(92), '/')} has no default export."
                                ),
                            }
                        )

                    if named_imports:
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
                    if package_name == "next":
                        issues.append(
                            {
                                "type": "invalid_next_package_import",
                                "source_file": relative_source,
                                "package": "next",
                                "message": (
                                    f"{relative_source} imports the Next.js package. "
                                    "Express next must be used only as a handler parameter: (req, res, next)."
                                ),
                            }
                        )

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

        if planner_output:
            self._validate_model_field_coverage(root, planner_output, issues)
            self._validate_auth_implementation(root, planner_output, issues)
            self._validate_controller_model_field_usage(root, planner_output, issues)
            self._validate_required_model_fields_are_creatable(root, planner_output, issues)

        if operations:
            self._validate_operation_implementation(root, operations, issues)

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "missing_dependencies": missing_dependencies,
        }

    def _validate_file_structure(self, relative_source: str, content: str, issues: List[Dict]) -> None:
        if relative_source == "app.js":
            if re.search(r"\bapp\.use\s*\(\s*json\s*\(", content) and "express.json" not in content:
                issues.append(
                    {
                        "type": "invalid_express_json",
                        "source_file": relative_source,
                        "message": "app.js uses json() instead of express.json(), or json is not clearly imported.",
                    }
                )
            if "export default app" not in content:
                issues.append(
                    {
                        "type": "missing_default_app_export",
                        "source_file": relative_source,
                        "message": "app.js should export default app for Supertest.",
                    }
                )

        if self._is_route_file(relative_source):
            if re.search(r"\bprotect\b", content) and not re.search(r"import\s+(?:\{\s*protect\s*\}|\w+\s*,?\s*\{\s*protect\s*\}|\w+)\s+from\s+['\"][^'\"]*auth\.js['\"]", content):
                issues.append(
                    {
                        "type": "middleware_used_without_import",
                        "source_file": relative_source,
                        "import_name": "protect",
                        "message": f"{relative_source} uses protect middleware but does not import it from auth middleware.",
                    }
                )

            routes = self._extract_route_order(content)
            first_param_index = next((idx for idx, route in enumerate(routes) if route["path"].startswith("/:")), None)
            if first_param_index is not None:
                for idx, route in enumerate(routes[first_param_index + 1 :], start=first_param_index + 1):
                    if not route["path"].startswith("/:"):
                        issues.append(
                            {
                                "type": "unreachable_static_route",
                                "source_file": relative_source,
                                "route": route["path"],
                                "message": f"{relative_source} defines static/action route {route['path']} after a parameter route; it may be unreachable.",
                            }
                        )

        if self._is_controller_file(relative_source) or self._is_route_file(relative_source):
            if "next(error)" in content:
                handlers = self._extract_async_handlers(content)
                for handler in handlers:
                    if "next(error)" in handler["body"] and "next" not in handler["params"]:
                        issues.append(
                            {
                                "type": "next_without_parameter",
                                "source_file": relative_source,
                                "handler": handler["name"],
                                "message": f"{relative_source} handler {handler['name']} calls next(error) but does not accept next.",
                            }
                        )

        if self._is_controller_file(relative_source):
            if "mongoose.Schema" in content or "new Schema" in content or "mongoose.model(" in content:
                issues.append(
                    {
                        "type": "controller_contains_schema",
                        "source_file": relative_source,
                        "message": f"{relative_source} defines a Mongoose schema/model; controllers should import the planned model file.",
                    }
                )

            if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*Schema\.pre\s*\(", content):
                issues.append(
                    {
                        "type": "controller_contains_schema_hook",
                        "source_file": relative_source,
                        "message": (
                            f"{relative_source} defines Mongoose schema middleware hooks. "
                            "Controllers should not define schema hooks; timestamps are handled in models with timestamps: true."
                        ),
                    }
                )

            model_imports = [imp for imp in self._extract_imports(content) if "/models/" in imp.get("path", "")]
            default_model_names = {imp.get("default_import") for imp in model_imports if imp.get("default_import")}
            for model_name in re.findall(r"\b([A-Z][A-Za-z0-9_]*)\.(?:find|findById|findOne|create|findByIdAndUpdate|findByIdAndDelete|deleteOne|deleteMany|aggregate|countDocuments)\s*\(", content):
                if model_name not in default_model_names:
                    issues.append(
                        {
                            "type": "controller_uses_undefined_model_alias",
                            "source_file": relative_source,
                            "symbol": model_name,
                            "message": f"{relative_source} calls {model_name}.* but does not import/define {model_name}.",
                        }
                    )

        if self._is_repository_file(relative_source):
            if "mongoose.Schema" in content or "new Schema" in content or "mongoose.model(" in content:
                issues.append(
                    {
                        "type": "repository_contains_schema",
                        "source_file": relative_source,
                        "message": f"{relative_source} defines a Mongoose schema/model; repositories should import the planned model file.",
                    }
                )

            model_imports = [imp for imp in self._extract_imports(content) if "/models/" in imp.get("path", "")]
            default_model_names = {imp.get("default_import") for imp in model_imports if imp.get("default_import")}
            for model_name in re.findall(r"\b([A-Z][A-Za-z0-9_]*)\.(?:find|findById|findOne|create|findByIdAndUpdate|findByIdAndDelete|deleteOne|deleteMany|aggregate|countDocuments)\s*\(", content):
                if model_name not in default_model_names:
                    issues.append(
                        {
                            "type": "repository_uses_undefined_model_alias",
                            "source_file": relative_source,
                            "symbol": model_name,
                            "message": f"{relative_source} calls {model_name}.* but does not import/define {model_name}.",
                        }
                    )

    def _validate_symbol_usage(self, relative_source: str, content: str, imports: List[Dict], issues: List[Dict]) -> None:
        if not (
            self._is_controller_file(relative_source)
            or self._is_service_file(relative_source)
            or self._is_repository_file(relative_source)
        ):
            return

        imported = set()
        for imp in imports:
            if imp.get("default_import"):
                imported.add(imp["default_import"])
            imported.update(imp.get("named_imports", []))

        local_defs = self._extract_named_exports(content).union(self._extract_local_function_names(content))
        declared = imported.union(local_defs)
        ignored = {
            "Date",
            "Error",
            "Promise",
            "String",
            "Number",
            "Boolean",
            "Object",
            "Array",
            "JSON",
            "parseInt",
            "parseFloat",
        }

        for match in re.finditer(r"(?<![.\w])([A-Za-z_][A-Za-z0-9_]*)\s*\(", content):
            symbol = match.group(1)
            if symbol in ignored or symbol in declared:
                continue
            if symbol in {"if", "for", "while", "switch", "catch", "function"}:
                continue
            if symbol[0].islower():
                issues.append(
                    {
                        "type": "called_function_not_imported",
                        "source_file": relative_source,
                        "symbol": symbol,
                        "message": f"{relative_source} calls {symbol}(), but it is not imported or defined.",
                    }
                )

        for handler in self._extract_async_handlers(content):
            own_name = handler["name"]
            if re.search(rf"\b{re.escape(own_name)}\s*\(", handler["body"]):
                issues.append(
                    {
                        "type": "recursive_handler_call",
                        "source_file": relative_source,
                        "handler": own_name,
                        "message": f"{relative_source} handler {own_name} appears to call itself recursively.",
                    }
                )

    def _validate_model_field_coverage(self, root: Path, planner_output: Dict[str, Any], issues: List[Dict]) -> None:
        for entity in planner_output.get("entities", []) or []:
            entity_name = entity.get("name")
            if not entity_name:
                continue

            model_paths = [
                root / "models" / f"{entity_name}.js",
                root / "infrastructure" / "database" / f"{entity_name}Model.js",
            ]
            model_paths.extend(root.glob(f"modules/*/model.js"))
            model_path = next((path for path in model_paths if path.exists()), None)
            if not model_path:
                continue

            content = model_path.read_text(encoding="utf-8", errors="ignore")
            relative_model = str(model_path.relative_to(root)).replace("\\", "/")
            if re.search(r"\bid\s*:\s*\{[^{}]*required\s*:\s*true", content, re.DOTALL):
                issues.append(
                    {
                        "type": "model_has_required_custom_id",
                        "source_file": relative_model,
                        "field": "id",
                        "message": f"{relative_model} defines required custom id. MongoDB provides _id automatically; avoid required id unless explicitly needed.",
                    }
                )
            for field in entity.get("fields", []) or []:
                field_name = field.get("name")
                if field_name and not re.search(rf"\b{re.escape(field_name)}\s*:", content):
                    issues.append(
                        {
                            "type": "model_missing_planned_field",
                            "source_file": relative_model,
                            "field": field_name,
                            "message": f"{relative_model} is missing planned field '{field_name}'.",
                        }
                    )

    def _validate_auth_implementation(self, root: Path, planner_output: Dict[str, Any], issues: List[Dict]) -> None:
        features_text = " ".join(f"{f.get('name', '')} {f.get('description', '')}" for f in planner_output.get("features", []) or []).lower()
        if not any(term in features_text for term in ["auth", "login", "jwt", "protected", "password"]):
            return

        auth_middleware = root / "middleware" / "auth.js"
        if not auth_middleware.exists():
            issues.append({"type": "missing_auth_middleware", "source_file": "middleware/auth.js", "message": "Authentication feature exists, but middleware/auth.js is missing."})

        user_model = root / "models" / "User.js"
        auth_controller = root / "controllers" / "authController.js"
        if user_model.exists() and auth_controller.exists():
            model_content = user_model.read_text(encoding="utf-8", errors="ignore")
            controller_content = auth_controller.read_text(encoding="utf-8", errors="ignore")
            for field in sorted(self._extract_object_fields_used_with_prefix(controller_content, "user")):
                if field in {"_id", "id", "password", "token"}:
                    continue
                if not re.search(rf"\b{re.escape(field)}\s*:", model_content):
                    issues.append(
                        {
                            "type": "auth_controller_uses_missing_user_field",
                            "source_file": "controllers/authController.js",
                            "target_file": "models/User.js",
                            "field": field,
                            "message": f"authController.js reads/returns user.{field}, but User.js does not define field '{field}'.",
                        }
                    )

            for required_field in self._extract_required_schema_fields_without_default(model_content):
                if required_field in {"email", "password"}:
                    continue
                if not re.search(rf"\b{re.escape(required_field)}\b", controller_content):
                    issues.append(
                        {
                            "type": "auth_registration_missing_required_user_field",
                            "source_file": "controllers/authController.js",
                            "target_file": "models/User.js",
                            "field": required_field,
                            "message": (
                                f"User.js requires field '{required_field}' without a default, "
                                "but authController.js registration does not set or accept it."
                            ),
                        }
                    )

        route_files = list((root / "routes").glob("*.js")) + list((root / "interfaces" / "routes").glob("*.js")) + list(root.glob("modules/*/routes.js"))
        route_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in route_files)
        if "profile" in features_text or "protected" in features_text:
            if "protect" not in route_text and "authMiddleware" not in route_text:
                issues.append(
                    {
                        "type": "protected_route_missing_middleware",
                        "message": "Authentication requires a protected route, but no route appears to use auth middleware.",
                    }
                )

    def _validate_operation_implementation(self, root: Path, operations: List[Dict[str, str]], issues: List[Dict]) -> None:
        code_index = ""
        for js_file in root.rglob("*.js"):
            if "node_modules" in js_file.parts:
                continue
            code_index += "\n" + js_file.read_text(encoding="utf-8", errors="ignore").lower()

        for operation in operations:
            name = operation.get("name", "")
            if operation.get("type") == "authentication":
                continue
            terms = [term for term in re.split(r"[^a-z0-9]+", name.lower()) if len(term) >= 3]
            if terms and not any(term in code_index for term in terms):
                issues.append(
                    {
                        "type": "missing_requested_operation",
                        "operation": operation,
                        "message": f"Requested operation '{name}' is not clearly implemented in generated code.",
                    }
                )

    def _validate_controller_model_field_usage(self, root: Path, planner_output: Dict[str, Any], issues: List[Dict]) -> None:
        model_fields_by_name: Dict[str, Set[str]] = {}
        for entity in planner_output.get("entities", []) or []:
            entity_name = entity.get("name")
            if not entity_name:
                continue
            model_path = root / "models" / f"{entity_name}.js"
            if not model_path.exists():
                continue
            model_content = model_path.read_text(encoding="utf-8", errors="ignore")
            model_fields_by_name[entity_name] = self._extract_schema_field_names(model_content)

        if not model_fields_by_name:
            return

        controller_paths = list((root / "controllers").glob("*.js")) + list((root / "interfaces" / "controllers").glob("*.js"))
        for controller_path in controller_paths:
            content = controller_path.read_text(encoding="utf-8", errors="ignore")
            relative_controller = str(controller_path.relative_to(root)).replace("\\", "/")
            for imp in self._extract_imports(content):
                if "/models/" not in imp.get("path", "") or not imp.get("default_import"):
                    continue
                model_name = Path(imp["path"]).stem
                fields = model_fields_by_name.get(model_name, set())
                if not fields:
                    continue
                alias = imp["default_import"]
                for query_field in self._extract_model_query_fields(content, alias):
                    if query_field not in fields:
                        issues.append(
                            {
                                "type": "controller_queries_missing_model_field",
                                "source_file": relative_controller,
                                "target_file": f"models/{model_name}.js",
                                "field": query_field,
                                "message": f"{relative_controller} queries {alias}.{query_field}, but models/{model_name}.js does not define '{query_field}'.",
                            }
                        )
                for variable in set(re.findall(rf"\b([a-z][A-Za-z0-9_]*)\s*=\s*await\s+{re.escape(alias)}\.", content)):
                    for used_field in self._extract_object_fields_used_with_prefix(content, variable):
                        if used_field in {"_id", "id", "__v"}:
                            continue
                        if used_field not in fields:
                            issues.append(
                                {
                                    "type": "controller_uses_missing_model_field",
                                    "source_file": relative_controller,
                                    "target_file": f"models/{model_name}.js",
                                    "field": used_field,
                                    "message": f"{relative_controller} reads {variable}.{used_field}, but models/{model_name}.js does not define '{used_field}'.",
                                }
                            )

    def _validate_required_model_fields_are_creatable(self, root: Path, planner_output: Dict[str, Any], issues: List[Dict]) -> None:
        for entity in planner_output.get("entities", []) or []:
            entity_name = entity.get("name")
            if not entity_name:
                continue
            model_path = root / "models" / f"{entity_name}.js"
            if not model_path.exists():
                continue
            model_content = model_path.read_text(encoding="utf-8", errors="ignore")
            required_fields = self._extract_required_schema_fields_without_default(model_content)
            if not required_fields:
                continue

            controller_path = root / "controllers" / f"{entity_name[0].lower() + entity_name[1:]}Controller.js"
            if not controller_path.exists():
                continue
            controller_content = controller_path.read_text(encoding="utf-8", errors="ignore")
            relative_controller = str(controller_path.relative_to(root)).replace("\\", "/")
            for required_field in sorted(required_fields):
                if required_field in {"_id", "__v"}:
                    continue
                if not re.search(rf"\b{re.escape(required_field)}\b", controller_content):
                    issues.append(
                        {
                            "type": "controller_create_missing_required_model_field",
                            "source_file": relative_controller,
                            "target_file": str(model_path.relative_to(root)).replace("\\", "/"),
                            "field": required_field,
                            "message": f"{relative_controller} does not appear to set required field '{required_field}' from {model_path.name}.",
                        }
                    )

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
                    "default_import": self._extract_default_import(import_clause),
                    "named_imports": named_imports,
                }
            )

        # import "./config/db.js";
        side_effect_pattern = re.compile(r"import\s+['\"]([^'\"]+)['\"]")

        for match in side_effect_pattern.finditer(content):
            imports.append(
                {
                    "path": match.group(1).strip(),
                    "default_import": None,
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

    @staticmethod
    def _extract_default_import(import_clause: str) -> Optional[str]:
        clause = import_clause.strip()
        if not clause or clause.startswith("{") or clause.startswith("*"):
            return None
        return clause.split(",", 1)[0].strip() or None

    @staticmethod
    def _has_default_export(content: str) -> bool:
        return bool(re.search(r"\bexport\s+default\b", content))

    def _extract_route_order(self, content: str) -> List[Dict[str, str]]:
        routes: List[Dict[str, str]] = []
        pattern = re.compile(r"\brouter\.(get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]+)['\"]")
        for match in pattern.finditer(content):
            routes.append({"method": match.group(1), "path": match.group(2)})
        return routes

    def _extract_async_handlers(self, content: str) -> List[Dict[str, str]]:
        handlers: List[Dict[str, str]] = []
        pattern = re.compile(
            r"export\s+const\s+(\w+)\s*=\s*async\s*\(([^)]*)\)\s*=>\s*\{(.*?)\}\s*;",
            re.DOTALL,
        )
        for match in pattern.finditer(content):
            handlers.append({"name": match.group(1), "params": match.group(2), "body": match.group(3)})
        function_pattern = re.compile(
            r"export\s+async\s+function\s+(\w+)\s*\(([^)]*)\)\s*\{(.*?)\}",
            re.DOTALL,
        )
        for match in function_pattern.finditer(content):
            handlers.append({"name": match.group(1), "params": match.group(2), "body": match.group(3)})
        return handlers

    @staticmethod
    def _extract_local_function_names(content: str) -> Set[str]:
        names = set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(", content))
        names.update(re.findall(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", content))
        names.update(re.findall(r"\basync\s+function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", content))
        return names

    @staticmethod
    def _extract_object_fields_used_with_prefix(content: str, prefix: str) -> Set[str]:
        return set(re.findall(rf"\b{re.escape(prefix)}\.([A-Za-z_][A-Za-z0-9_]*)", content))

    @staticmethod
    def _extract_required_schema_fields_without_default(content: str) -> Set[str]:
        fields: Set[str] = set()
        pattern = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\{([^{}]*)\}", re.DOTALL)
        for match in pattern.finditer(content):
            field_name = match.group(1)
            body = match.group(2)
            if re.search(r"\brequired\s*:\s*true\b", body) and not re.search(r"\bdefault\s*:", body):
                fields.add(field_name)
        return fields

    @staticmethod
    def _extract_schema_field_names(content: str) -> Set[str]:
        return set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\{", content))

    @staticmethod
    def _extract_model_query_fields(content: str, model_alias: str) -> Set[str]:
        fields: Set[str] = set()
        pattern = re.compile(
            rf"\b{re.escape(model_alias)}\.(?:find|findOne|findOneAndUpdate|findOneAndDelete|countDocuments|aggregate)\s*\(\s*\{{(.*?)\}}",
            re.DOTALL,
        )
        for match in pattern.finditer(content):
            body = match.group(1)
            for field in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:", body):
                if field not in {"$or", "$and", "$gte", "$lte", "$gt", "$lt", "$in", "$regex", "$options"}:
                    fields.add(field)
        return fields

    @staticmethod
    def _is_route_file(relative_source: str) -> bool:
        return relative_source.startswith("routes/") or relative_source.startswith("interfaces/routes/") or relative_source.endswith("/routes.js")

    @staticmethod
    def _is_controller_file(relative_source: str) -> bool:
        return relative_source.startswith("controllers/") or relative_source.startswith("interfaces/controllers/") or relative_source.endswith("/controller.js")

    @staticmethod
    def _is_service_file(relative_source: str) -> bool:
        return relative_source.startswith("services/") or relative_source.startswith("application/use-cases/") or relative_source.endswith("/service.js")

    @staticmethod
    def _is_repository_file(relative_source: str) -> bool:
        return relative_source.startswith("repositories/") or relative_source.startswith("infrastructure/repositories/") or relative_source.endswith("/repository.js")

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

    def _has_exact_path_case(self, root: Path, resolved: Path) -> bool:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            return True

        current = root
        for part in relative.parts:
            try:
                names = {child.name for child in current.iterdir()}
            except OSError:
                return True
            if part not in names:
                return False
            current = current / part
        return True

    def _get_package_name(self, import_path: str) -> str:
        if import_path.startswith("@"):
            parts = import_path.split("/")
            return "/".join(parts[:2])

        return import_path.split("/")[0]
