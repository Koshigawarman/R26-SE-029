from __future__ import annotations

from typing import Any, Dict

from schema import Architecture
from services.architecture_profile_registry import detect_file_type, get_architecture_profile, normalize_architecture
from prompts.codegen_profiles import clean_architecture, modular_monolith, mvc, service_repository


PROFILE_MODULES = {
    "mvc": mvc,
    "service-repository": service_repository,
    "clean-architecture": clean_architecture,
    "modular-monolith": modular_monolith,
}


def get_architecture_codegen_system_prompt(
    path: str,
    architecture: Architecture | Dict[str, Any] | None,
    mode: str = "generate",
) -> str:
    """
    Select an architecture-specific CodeGen system prompt.

    Step 5 only introduces the factory. CodeGen integration happens separately.
    """
    normalized = normalize_architecture(architecture)
    profile = get_architecture_profile(normalized)
    pattern = profile["id"]
    prompt_module = PROFILE_MODULES.get(pattern, mvc)

    if mode == "fix":
        file_type = detect_file_type(path, pattern)
        base_prompt = prompt_module.PROMPTS.get(file_type) or prompt_module.PROMPTS["generic"]
        return base_prompt + """

## REPAIR MODE
Generate the complete corrected source code for the target file only.
Preserve working logic where possible.
Do not output a diff or partial file.
Do not include markdown or explanations."""

    file_type = _specialized_file_type(path, pattern)
    return prompt_module.PROMPTS.get(file_type) or prompt_module.PROMPTS["generic"]


def _specialized_file_type(path: str, pattern: str) -> str:
    if path == "middleware/auth.js":
        return "auth_middleware"
    if path == "controllers/authController.js":
        return "auth_controller"
    if path == "routes/authRoutes.js":
        return "auth_route"
    if path == "middleware/errorHandler.js":
        return "error_middleware"
    return detect_file_type(path, pattern)
