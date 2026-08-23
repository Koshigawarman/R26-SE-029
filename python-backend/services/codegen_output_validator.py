import re
from typing import Any, Dict, List


class CodeGenOutputValidator:
    """Observes generated code for file-boundary and architecture violations."""

    def validate(self, path: str, code: str, architecture: Dict[str, Any] = None) -> Dict[str, Any]:
        issues: List[Dict[str, Any]] = []
        pattern = self._pattern(architecture)

        self._validate_common(path, code, issues)

        if path.startswith("models/") or path.startswith("infrastructure/database/") or path.endswith("/model.js"):
            self._validate_model(path, code, issues)
        elif path.startswith("repositories/") or path.startswith("infrastructure/repositories/") or path.endswith("/repository.js"):
            self._validate_repository(path, code, issues)
        elif path.startswith("services/") or path.endswith("/service.js"):
            self._validate_service(path, code, issues)
        elif path.startswith("domain/entities/"):
            self._validate_domain_entity(path, code, issues)
        elif path.startswith("application/use-cases/"):
            self._validate_use_case(path, code, issues)
        elif path.startswith("controllers/") or path.startswith("interfaces/controllers/") or path.endswith("/controller.js"):
            self._validate_controller(path, code, issues)
        elif path.startswith("routes/") or path.startswith("interfaces/routes/") or path.endswith("/routes.js"):
            self._validate_route(path, code, issues)
        elif path == "app.js":
            self._validate_app(path, code, issues)
        elif path == "middleware/auth.js":
            self._validate_auth_middleware(path, code, issues)
        elif path == "middleware/errorHandler.js":
            self._validate_error_handler(path, code, issues)
        elif path == "config/db.js":
            self._validate_db_config(path, code, issues)

        self._validate_architecture_boundaries(path, code, pattern, issues)

        return {
            "target_file": path,
            "architecture_pattern": pattern,
            "valid": len([issue for issue in issues if issue["severity"] == "error"]) == 0,
            "issue_count": len(issues),
            "error_count": len([issue for issue in issues if issue["severity"] == "error"]),
            "warning_count": len([issue for issue in issues if issue["severity"] == "warning"]),
            "issues": issues,
        }

    def _validate_common(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "```" in code:
            self._add(issues, "error", "markdown_fence", "Output contains markdown code fences.")

        if "require(" in code:
            self._add(issues, "warning", "commonjs_require", "Output uses require(); project expects ES modules.")

        if "module.exports" in code:
            self._add(issues, "warning", "commonjs_exports", "Output uses module.exports; project expects ES modules.")

        if re.search(r"from\s+['\"]\./[^'\"]+(?<!\.js)(?<!\.json)['\"]", code):
            self._add(issues, "warning", "missing_js_extension", "A local import may be missing .js extension.")

        if "bcryptjs" in code and not self._is_auth_file(path):
            self._add(
                issues,
                "warning",
                "auth_logic_leakage",
                "bcryptjs appears in a non-auth target file. This may be authentication logic leakage.",
            )

    def _validate_model(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "mongoose.Schema" not in code and "new Schema" not in code:
            self._add(issues, "warning", "model_missing_schema", "Model file does not appear to define a schema.")

        if "express.Router" in code:
            self._add(issues, "error", "model_contains_router", "Model file contains Express router logic.")

        if "export default mongoose.model" not in code:
            self._add(issues, "warning", "model_missing_default_export", "Model may be missing default mongoose model export.")

    def _validate_controller(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "mongoose.Schema" in code or "mongoose.model(" in code or "new Schema" in code:
            self._add(issues, "error", "controller_contains_schema", "Controller contains model schema/model definition.")

        if "express.Router" in code or re.search(r"\brouter\.(get|post|put|patch|delete)\s*\(", code):
            self._add(issues, "error", "controller_contains_routes", "Controller contains Express route definitions.")

        if "export default router" in code:
            self._add(issues, "error", "controller_exports_router", "Controller exports router instead of named controller functions.")

        if not re.search(r"export\s+const\s+\w+\s*=\s*async|export\s+async\s+function\s+\w+", code):
            self._add(issues, "warning", "controller_missing_named_async_exports", "Controller may be missing named async exports.")

        if "next(error)" in code and not re.search(r"\(\s*req\s*,\s*res\s*,\s*next\s*\)", code):
            self._add(issues, "warning", "next_without_parameter", "Code calls next(error), but a handler may not accept next.")

    def _validate_route(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "mongoose.Schema" in code or "mongoose.model(" in code or "new Schema" in code:
            self._add(issues, "error", "route_contains_schema", "Route file contains model schema/model definition.")

        if not re.search(r"import\s+express\s+from\s+['\"]express['\"]", code):
            self._add(issues, "warning", "route_missing_express_import", "Route file may be missing express import.")

        if "express.Router" not in code:
            self._add(issues, "warning", "route_missing_router", "Route file may be missing express.Router().")

        if "export default router" not in code:
            self._add(issues, "warning", "route_missing_default_router_export", "Route file may be missing default router export.")

        if re.search(r"\b(find|findById|findByIdAndUpdate|findByIdAndDelete|findOne|save|remove|deleteOne|deleteMany)\s*\(", code):
            self._add(issues, "error", "route_contains_database_logic", "Route file appears to contain controller/database logic.")

        if re.search(r"\brouter\.(get|post|put|patch|delete)\s*\([^)]*async\s*\(", code, re.DOTALL):
            self._add(issues, "error", "route_contains_inline_async_handler", "Route file contains inline async handler logic instead of controller references.")

        if "try {" in code and re.search(r"\brouter\.(get|post|put|patch|delete)\s*\(", code):
            self._add(issues, "warning", "route_contains_try_catch", "Route file contains try/catch; controller should own request logic.")

        if "express-validator" in code or "validationResult" in code or re.search(r"\bcheck\s*\(", code):
            self._add(issues, "warning", "route_contains_inline_validation", "Route file contains inline validation instead of separate middleware/controller validation.")

        if re.search(r"import\s+\w+\s+from\s+['\"]\.\./models/", code):
            self._add(issues, "error", "route_imports_model", "Route file imports a model directly; it should import controller functions only.")

        if "next(error)" in code and not re.search(r"\(\s*req\s*,\s*res\s*,\s*next\s*\)", code):
            self._add(issues, "warning", "next_without_parameter", "Code calls next(error), but a route handler may not accept next.")

    def _validate_repository(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "express" in code:
            self._add(issues, "error", "repository_imports_express", "Repository imports Express; repositories must be framework-independent.")

        if self._uses_http_objects(code):
            self._add(issues, "error", "repository_uses_http_objects", "Repository uses req/res/next or HTTP response logic.")

        if "express.Router" in code or re.search(r"\brouter\.(get|post|put|patch|delete)\s*\(", code):
            self._add(issues, "error", "repository_contains_routes", "Repository contains route definitions.")

        if not re.search(r"export\s+(const|async\s+function|function)\s+\w+", code):
            self._add(issues, "warning", "repository_missing_named_exports", "Repository may be missing named exports.")

    def _validate_service(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "express" in code:
            self._add(issues, "error", "service_imports_express", "Service imports Express; services must be framework-independent.")

        if self._uses_http_objects(code):
            self._add(issues, "error", "service_uses_http_objects", "Service uses req/res/next or HTTP response logic.")

        if "mongoose.Schema" in code or "mongoose.model(" in code or "new Schema" in code:
            self._add(issues, "error", "service_contains_schema", "Service contains schema/model definition.")

        if "express.Router" in code or re.search(r"\brouter\.(get|post|put|patch|delete)\s*\(", code):
            self._add(issues, "error", "service_contains_routes", "Service contains route definitions.")

        if not re.search(r"export\s+(const|async\s+function|function)\s+\w+", code):
            self._add(issues, "warning", "service_missing_named_exports", "Service may be missing named exports.")

    def _validate_domain_entity(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "mongoose" in code:
            self._add(issues, "error", "domain_imports_mongoose", "Domain entity imports Mongoose; clean architecture domain must not depend on infrastructure.")

        if "express" in code or self._uses_http_objects(code):
            self._add(issues, "error", "domain_uses_http", "Domain entity uses Express/HTTP objects.")

    def _validate_use_case(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "express" in code:
            self._add(issues, "error", "use_case_imports_express", "Use case imports Express; use cases must be framework-independent.")

        if self._uses_http_objects(code):
            self._add(issues, "error", "use_case_uses_http_objects", "Use case uses req/res/next or HTTP response logic.")

        if "mongoose.Schema" in code or "mongoose.model(" in code or "new Schema" in code:
            self._add(issues, "error", "use_case_contains_schema", "Use case contains schema/model definition.")

    def _validate_app(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "export default app" not in code:
            self._add(issues, "warning", "app_missing_default_export", "app.js may be missing export default app.")

        if "NODE_ENV" not in code or "test" not in code:
            self._add(issues, "warning", "app_missing_test_guard", "app.js may start server during tests.")

    def _validate_error_handler(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if re.search(r"^import\s+", code, re.MULTILINE):
            self._add(issues, "warning", "error_handler_has_imports", "errorHandler should normally have zero imports.")

        if "export default errorHandler" not in code:
            self._add(issues, "warning", "error_handler_missing_default_export", "errorHandler may be missing default export.")

    def _validate_auth_middleware(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "jsonwebtoken" not in code and "jwt.verify" not in code:
            self._add(issues, "warning", "auth_missing_jwt_verification", "Auth middleware may be missing JWT verification.")

        if "jwt.sign" in code:
            self._add(issues, "error", "auth_creates_token", "Auth middleware creates JWT tokens; token creation belongs in auth controller.")

        if "bcrypt" in code or "bcryptjs" in code:
            self._add(issues, "error", "auth_hashes_password", "Auth middleware hashes passwords; password logic belongs in auth controller/service.")

        if "express.Router" in code or re.search(r"\brouter\.(get|post|put|patch|delete)\s*\(", code):
            self._add(issues, "error", "auth_contains_routes", "Auth middleware contains route definitions.")

        if "mongoose.Schema" in code or "mongoose.model(" in code or "new Schema" in code:
            self._add(issues, "error", "auth_contains_schema", "Auth middleware contains model schema/model definition.")

        if "Authorization" not in code and "authorization" not in code:
            self._add(issues, "warning", "auth_missing_authorization_header", "Auth middleware may not read the Authorization header.")

        if "Bearer" not in code:
            self._add(issues, "warning", "auth_missing_bearer_scheme", "Auth middleware may not enforce Bearer token format.")

        if "process.env.JWT_SECRET" not in code:
            self._add(issues, "warning", "auth_missing_jwt_secret", "Auth middleware may not use process.env.JWT_SECRET.")

        if "req.user" not in code:
            self._add(issues, "warning", "auth_missing_req_user", "Auth middleware may not attach decoded user info to req.user.")

        if "next()" not in code:
            self._add(issues, "warning", "auth_missing_next_call", "Auth middleware may not call next() on successful authentication.")

    def _validate_db_config(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if "mongoose" not in code:
            self._add(issues, "warning", "db_config_missing_mongoose", "config/db.js may be missing mongoose.")

        if "export default" not in code:
            self._add(issues, "warning", "db_config_missing_default_export", "config/db.js may be missing default export.")

    def _validate_architecture_boundaries(self, path: str, code: str, pattern: str, issues: List[Dict[str, Any]]) -> None:
        if pattern == "service-repository":
            self._validate_service_repository_boundaries(path, code, issues)
        elif pattern == "clean-architecture":
            self._validate_clean_architecture_boundaries(path, code, issues)
        elif pattern == "modular-monolith":
            self._validate_modular_monolith_boundaries(path, code, issues)

    def _validate_service_repository_boundaries(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if path.startswith("controllers/"):
            if re.search(r"from\s+['\"]\.\./models/", code):
                self._add(issues, "error", "service_repo_controller_imports_model", "Controller imports model directly; it should import service functions.")
            if self._contains_mongoose_query(code):
                self._add(issues, "error", "service_repo_controller_contains_db_logic", "Controller contains database query logic; use service/repository layers.")
            if not re.search(r"from\s+['\"]\.\./services/", code):
                self._add(issues, "warning", "service_repo_controller_missing_service_import", "Controller may be missing service import.")

        if path.startswith("services/"):
            if re.search(r"from\s+['\"]\.\./models/", code):
                self._add(issues, "error", "service_repo_service_imports_model", "Service imports model directly; it should use repository functions.")
            if not re.search(r"from\s+['\"]\.\./repositories/", code):
                self._add(issues, "warning", "service_repo_service_missing_repository_import", "Service may be missing repository import.")

        if path.startswith("repositories/"):
            if not re.search(r"from\s+['\"]\.\./models/", code):
                self._add(issues, "warning", "service_repo_repository_missing_model_import", "Repository may be missing model import.")

        if path.startswith("routes/"):
            if re.search(r"from\s+['\"]\.\./(models|services|repositories)/", code):
                self._add(issues, "error", "service_repo_route_imports_inner_layer", "Route should import controllers only.")

    def _validate_clean_architecture_boundaries(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if path.startswith("domain/"):
            if re.search(r"from\s+['\"].*(infrastructure|interfaces|application)", code):
                self._add(issues, "error", "clean_domain_imports_outer_layer", "Domain layer must not import outer layers.")

        if path.startswith("application/use-cases/"):
            if re.search(r"from\s+['\"].*interfaces/", code):
                self._add(issues, "error", "clean_use_case_imports_interface", "Use case must not import interface layer.")
            if re.search(r"from\s+['\"].*database/.*Model", code):
                self._add(issues, "warning", "clean_use_case_imports_model", "Use case should use repository, not database model directly.")

        if path.startswith("interfaces/controllers/"):
            if re.search(r"from\s+['\"].*infrastructure/(database|repositories)", code):
                self._add(issues, "error", "clean_controller_imports_infrastructure", "Controller should call use cases, not infrastructure directly.")
            if not re.search(r"from\s+['\"].*application/use-cases/", code):
                self._add(issues, "warning", "clean_controller_missing_use_case_import", "Controller may be missing use-case import.")

        if path.startswith("interfaces/routes/"):
            if re.search(r"from\s+['\"].*(application|infrastructure|domain)/", code):
                self._add(issues, "error", "clean_route_imports_inner_layer", "Route should import interface controller only.")

    def _validate_modular_monolith_boundaries(self, path: str, code: str, issues: List[Dict[str, Any]]) -> None:
        if not path.startswith("modules/"):
            return

        if path.endswith("/controller.js"):
            if re.search(r"from\s+['\"]\./(model|repository)\.js['\"]", code):
                self._add(issues, "error", "module_controller_imports_lower_layer_directly", "Module controller should import service only.")
            if not re.search(r"from\s+['\"]\./service\.js['\"]", code):
                self._add(issues, "warning", "module_controller_missing_service_import", "Module controller may be missing ./service.js import.")

        if path.endswith("/service.js"):
            if re.search(r"from\s+['\"]\./model\.js['\"]", code):
                self._add(issues, "error", "module_service_imports_model", "Module service should use repository, not model directly.")
            if not re.search(r"from\s+['\"]\./repository\.js['\"]", code):
                self._add(issues, "warning", "module_service_missing_repository_import", "Module service may be missing ./repository.js import.")

        if path.endswith("/repository.js"):
            if not re.search(r"from\s+['\"]\./model\.js['\"]", code):
                self._add(issues, "warning", "module_repository_missing_model_import", "Module repository may be missing ./model.js import.")

        if path.endswith("/routes.js"):
            if re.search(r"from\s+['\"]\./(model|repository|service)\.js['\"]", code):
                self._add(issues, "error", "module_route_imports_inner_layer", "Module route should import controller only.")
            if not re.search(r"from\s+['\"]\./controller\.js['\"]", code):
                self._add(issues, "warning", "module_route_missing_controller_import", "Module route may be missing ./controller.js import.")

    @staticmethod
    def _is_auth_file(path: str) -> bool:
        return "auth" in path.lower() or "user" in path.lower()

    @staticmethod
    def _pattern(architecture: Dict[str, Any] = None) -> str:
        if not architecture:
            return "mvc"
        pattern = str(architecture.get("pattern") or "mvc").strip().lower()
        if pattern not in {"mvc", "service-repository", "clean-architecture", "modular-monolith"}:
            return "mvc"
        return pattern

    @staticmethod
    def _uses_http_objects(code: str) -> bool:
        return bool(re.search(r"\b(req|res|next)\b|\.status\s*\(|\.json\s*\(", code))

    @staticmethod
    def _contains_mongoose_query(code: str) -> bool:
        return bool(
            re.search(
                r"\b(find|findById|findOne|create|findByIdAndUpdate|findByIdAndDelete|deleteOne|deleteMany|aggregate|countDocuments|save)\s*\(",
                code,
            )
        )

    @staticmethod
    def _add(issues: List[Dict[str, Any]], severity: str, code: str, message: str) -> None:
        issues.append(
            {
                "severity": severity,
                "code": code,
                "message": message,
            }
        )
