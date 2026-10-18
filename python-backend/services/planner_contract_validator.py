from typing import Any, Dict, List

from schema import PlannerOutput
from services.operation_extractor import OperationExtractor


class PlannerContractValidator:
    """Checks whether the planner contract covers user-visible requirements."""

    def __init__(self) -> None:
        self.operation_extractor = OperationExtractor()

    def validate(self, user_prompt: str, plan: PlannerOutput) -> Dict[str, Any]:
        operations = self.operation_extractor.extract(user_prompt)
        issues: List[Dict[str, Any]] = []
        files_text = " ".join(f"{f.path} {f.description}" for f in plan.files).lower()
        features_text = " ".join(f"{f.name} {f.description}" for f in plan.features).lower()
        contract_text = f"{files_text} {features_text}"

        self._validate_auth_contract(user_prompt, plan, issues)
        self._validate_operation_coverage(operations, contract_text, issues)
        self._validate_architecture_shape(plan, issues)

        return {
            "valid": not any(issue["severity"] == "error" for issue in issues),
            "operations": operations,
            "issues": issues,
            "issue_count": len(issues),
            "error_count": len([issue for issue in issues if issue["severity"] == "error"]),
            "warning_count": len([issue for issue in issues if issue["severity"] == "warning"]),
        }

    def _validate_auth_contract(self, user_prompt: str, plan: PlannerOutput, issues: List[Dict[str, Any]]) -> None:
        text = (
            user_prompt
            + " "
            + " ".join(f"{f.name} {f.description}" for f in plan.features)
        ).lower()
        auth_required = any(
            term in text
            for term in ["auth", "authentication", "login", "register", "jwt", "password", "protected", "role-based", "rbac"]
        )
        if not auth_required:
            return

        paths = {f.path for f in plan.files}
        for required in ["middleware/auth.js", "controllers/authController.js", "routes/authRoutes.js"]:
            if required not in paths:
                issues.append(
                    {
                        "severity": "error",
                        "code": "missing_auth_file",
                        "target_file": required,
                        "message": f"Authentication is required, but {required} is not in planner files.",
                    }
                )

        package_text = self._description_for(plan, "package.json")
        for dep in ["bcryptjs", "jsonwebtoken"]:
            if dep not in package_text:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "auth_dependency_not_planned",
                        "target_file": "package.json",
                        "message": f"Authentication is required, but package.json description does not mention {dep}.",
                    }
                )

    def _validate_operation_coverage(
        self,
        operations: List[Dict[str, str]],
        contract_text: str,
        issues: List[Dict[str, Any]],
    ) -> None:
        for operation in operations:
            name = operation["name"]
            if name in {"login", "register", "protect", "authenticate", "authorize"}:
                continue
            key_terms = [part for part in name.replace("-", " ").split() if len(part) >= 3]
            if key_terms and not any(term in contract_text for term in key_terms):
                issues.append(
                    {
                        "severity": "warning",
                        "code": "operation_not_planned",
                        "operation": operation,
                        "message": f"Requested operation '{name}' is not clearly represented in planner features or file descriptions.",
                    }
                )

    def _validate_architecture_shape(self, plan: PlannerOutput, issues: List[Dict[str, Any]]) -> None:
        pattern = plan.architecture.pattern
        paths = {f.path for f in plan.files}

        for entity in plan.entities:
            name = entity.name
            entity_var = name[0].lower() + name[1:] if name else ""
            if pattern == "service-repository":
                expected = [
                    f"models/{name}.js",
                    f"repositories/{entity_var}Repository.js",
                    f"services/{entity_var}Service.js",
                    f"controllers/{entity_var}Controller.js",
                    f"routes/{entity_var}Routes.js",
                ]
            elif pattern == "clean-architecture":
                expected = [
                    f"domain/entities/{name}.js",
                    f"application/use-cases/{entity_var}UseCases.js",
                    f"infrastructure/database/{name}Model.js",
                    f"infrastructure/repositories/{entity_var}Repository.js",
                    f"interfaces/controllers/{entity_var}Controller.js",
                    f"interfaces/routes/{entity_var}Routes.js",
                ]
            elif pattern == "modular-monolith":
                expected = [
                    f"modules/{entity_var}/model.js",
                    f"modules/{entity_var}/repository.js",
                    f"modules/{entity_var}/service.js",
                    f"modules/{entity_var}/controller.js",
                    f"modules/{entity_var}/routes.js",
                ]
            else:
                expected = [
                    f"models/{name}.js",
                    f"controllers/{entity_var}Controller.js",
                    f"routes/{entity_var}Routes.js",
                ]

            for path in expected:
                if path not in paths:
                    issues.append(
                        {
                            "severity": "error",
                            "code": "missing_architecture_file",
                            "target_file": path,
                            "message": f"{pattern} plan is missing expected file {path}.",
                        }
                    )

    @staticmethod
    def _description_for(plan: PlannerOutput, path: str) -> str:
        for file_spec in plan.files:
            if file_spec.path == path:
                return file_spec.description.lower()
        return ""
