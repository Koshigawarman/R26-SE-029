import re
from typing import Any, Dict, List


class CodeGenOutputValidator:
    """Observes generated code for file-boundary and MVC violations."""

    def validate(self, path: str, code: str) -> Dict[str, Any]:
        issues: List[Dict[str, Any]] = []

        self._validate_common(path, code, issues)

        if path.startswith("models/"):
            self._validate_model(path, code, issues)
        elif path.startswith("controllers/"):
            self._validate_controller(path, code, issues)
        elif path.startswith("routes/"):
            self._validate_route(path, code, issues)
        elif path == "app.js":
            self._validate_app(path, code, issues)
        elif path == "middleware/auth.js":
            self._validate_auth_middleware(path, code, issues)
        elif path == "middleware/errorHandler.js":
            self._validate_error_handler(path, code, issues)
        elif path == "config/db.js":
            self._validate_db_config(path, code, issues)

        return {
            "target_file": path,
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

    @staticmethod
    def _is_auth_file(path: str) -> bool:
        return "auth" in path.lower() or "user" in path.lower()

    @staticmethod
    def _add(issues: List[Dict[str, Any]], severity: str, code: str, message: str) -> None:
        issues.append(
            {
                "severity": severity,
                "code": code,
                "message": message,
            }
        )
