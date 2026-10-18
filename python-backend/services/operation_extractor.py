import re
from typing import Dict, List


class OperationExtractor:
    """Extracts user-requested non-CRUD operations from free-text requirements."""

    CRUD_WORDS = {"create", "read", "update", "delete", "crud", "get", "list"}
    DOMAIN_VERBS = {
        "approve",
        "reject",
        "borrow",
        "return",
        "deposit",
        "withdraw",
        "transfer",
        "calculate",
        "generate",
        "track",
        "assign",
        "invite",
        "pay",
        "refund",
        "ship",
        "cancel",
        "publish",
        "archive",
        "restore",
        "verify",
        "activate",
        "deactivate",
        "login",
        "register",
        "protect",
        "authenticate",
        "authorize",
        "report",
        "search",
        "filter",
    }

    PHRASE_PATTERNS = [
        (r"\bview\s+([a-z0-9 -]+?history)\b", "query"),
        (r"\b([a-z0-9 -]+?report)\b", "query"),
        (r"\b([a-z0-9 -]+?listing)\b", "query"),
        (r"\b([a-z0-9 -]+?calculation)\b", "domain_action"),
        (r"\bprevent\s+([a-z0-9 -]+?)(?:\.|,|;| and |$)", "business_rule"),
    ]

    def extract(self, user_prompt: str) -> List[Dict[str, str]]:
        prompt = (user_prompt or "").lower()
        operations: List[Dict[str, str]] = []

        for pattern, op_type in self.PHRASE_PATTERNS:
            for match in re.finditer(pattern, prompt):
                self._append_unique(operations, match.group(0).strip(), op_type)

        for verb in sorted(self.DOMAIN_VERBS):
            if re.search(rf"\b{re.escape(verb)}(?:s|ed|ing)?\b", prompt):
                op_type = "query" if verb in {"report", "search", "filter"} else "domain_action"
                if verb in {"login", "register", "protect", "authenticate", "authorize"}:
                    op_type = "authentication"
                self._append_unique(operations, verb, op_type)

        return operations

    @staticmethod
    def _append_unique(operations: List[Dict[str, str]], name: str, op_type: str) -> None:
        normalized = re.sub(r"\s+", " ", name.strip("- .,:;")).strip()
        normalized = re.sub(r"^(and|or|to|the|a|an)\s+", "", normalized).strip()
        if not normalized:
            return
        if normalized in OperationExtractor.CRUD_WORDS:
            return
        if any(op["name"] == normalized for op in operations):
            return
        operations.append({"name": normalized, "type": op_type})
