"""
AI Backend Builder — Programmatic Code Validator & Auto-Fixer

This module acts as the "agentic brain" between LLM output and file system writes.
The LLM generates code; this module enforces correctness without trusting the LLM.

Capabilities:
1. Import path auto-fixer: Corrects wrong casing, missing .js extensions, and wrong
   relative directory depth by comparing against the actual project file_list.
2. Partial generation guard: Detects if the LLM output only a diff/patch instead of
   the full file, and flags it as a truncation failure so the orchestrator can retry.
3. Markdown artifact cleaner: Strips any trailing or leading code fences that small
   LLMs sometimes leave inside their output.
4. Structural sanity check: Ensures a JS file has at least one import/export statement,
   signaling it is a real module and not a stray comment or empty shell.
"""

import difflib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass returned by the validator
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """Outcome of running the validator on one generated file."""
    is_valid: bool
    final_code: str                          # Potentially auto-fixed code
    auto_fixes: List[str] = field(default_factory=list)   # Human-readable fix descriptions
    fatal_errors: List[str] = field(default_factory=list) # Issues that block saving the file


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def validate_and_fix(
    file_path: str,
    code: str,
    file_list: List[str],
    original_code: Optional[str] = None,
) -> ValidationResult:
    """
    Run all programmatic checks and auto-fixes on a piece of generated/fixed code.

    Args:
        file_path:     Relative path of the target file (e.g. 'routes/menuRoutes.js').
        code:          Raw LLM output string.
        file_list:     Authoritative list of planned file paths from the Planner Agent.
        original_code: If this is a *fix* pass, supply the original file so the
                       partial-generation guard can compare lengths.

    Returns:
        ValidationResult with .is_valid, .final_code (auto-fixed), .auto_fixes, .fatal_errors
    """
    auto_fixes: List[str] = []
    fatal_errors: List[str] = []

    # Step 1 – Strip markdown artifacts (safe, always first)
    code, stripped = _strip_markdown(code)
    if stripped:
        auto_fixes.append("Stripped markdown code fences from LLM output")

    # Step 2 – Partial-generation guard (no point going further if truncated)
    if original_code is not None:
        is_truncated, reason = _is_truncated(code, original_code)
        if is_truncated:
            fatal_errors.append(f"PARTIAL_GENERATION: {reason}")
            return ValidationResult(is_valid=False, final_code=code, auto_fixes=auto_fixes, fatal_errors=fatal_errors)

    # Step 3 – JS module syntax checks
    if file_path.endswith(".js"):
        # Check for require/module.exports mixins
        errors, warnings = _check_module_syntax(code, file_path)
        fatal_errors.extend(errors)
        for w in warnings:
            logger.warning("[validator] %s: %s", file_path, w)

        # Step 4 – Import path auto-fixer (only run if file_list available)
        if file_list:
            code, fixes = _fix_import_paths(file_path, code, file_list)
            auto_fixes.extend(fixes)

    is_valid = len(fatal_errors) == 0
    return ValidationResult(
        is_valid=is_valid,
        final_code=code,
        auto_fixes=auto_fixes,
        fatal_errors=fatal_errors,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 – Markdown artifact cleaner
# ─────────────────────────────────────────────────────────────────────────────

def _strip_markdown(code: str) -> Tuple[str, bool]:
    """Remove leading/trailing markdown code fences that LLMs sometimes output."""
    stripped = False

    # Match optional language hint at opening fence (```javascript, ```js, ```)
    open_fence = re.compile(r"^```(?:javascript|js|json|typescript|ts)?\s*\n?", re.IGNORECASE)
    close_fence = re.compile(r"\n?```\s*$")

    clean = code.strip()
    if open_fence.match(clean):
        clean = open_fence.sub("", clean, count=1)
        stripped = True
    if close_fence.search(clean):
        clean = close_fence.sub("", clean)
        stripped = True

    # Also remove any inline ``` that appear mid-file (LLM confusion artefacts)
    if "```" in clean:
        clean = clean.replace("```javascript", "").replace("```js", "").replace("```", "")
        stripped = True

    return clean.strip(), stripped


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 – Partial-generation guard
# ─────────────────────────────────────────────────────────────────────────────

_PARTIAL_RATIO_THRESHOLD = 0.40   # New code < 40 % of original → suspect truncation
_MINIMUM_LINE_COUNT = 5           # Any JS module should have at least 5 lines


def _is_truncated(new_code: str, original_code: str) -> Tuple[bool, str]:
    """
    Heuristic check: was the LLM's output suspiciously shorter than the original?

    Returns (is_truncated, reason_string).
    """
    orig_lines = [l for l in original_code.splitlines() if l.strip()]
    new_lines  = [l for l in new_code.splitlines() if l.strip()]

    if len(new_lines) < _MINIMUM_LINE_COUNT:
        return True, f"Generated only {len(new_lines)} non-empty lines (minimum: {_MINIMUM_LINE_COUNT})"

    if orig_lines:
        ratio = len(new_lines) / len(orig_lines)
        if ratio < _PARTIAL_RATIO_THRESHOLD:
            return True, (
                f"Generated {len(new_lines)} lines but original had {len(orig_lines)} lines "
                f"({ratio:.0%} of original — looks like a partial patch, not a full file)"
            )

    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 – JS module syntax checks
# ─────────────────────────────────────────────────────────────────────────────

def _check_module_syntax(code: str, file_path: str) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    has_require  = "require(" in code
    has_import   = "import "  in code
    has_mod_exp  = "module.exports" in code
    has_export   = "export "  in code

    if has_require and has_import:
        errors.append("Mixed require() and import statements — use ES modules only.")
    elif has_require:
        errors.append("Uses require() instead of ES module import — convert to import/export.")

    if has_mod_exp:
        errors.append("Uses module.exports instead of ES module export — convert to export default / export const.")

    if "// TODO" in code or "/* TODO" in code:
        warnings.append("Contains TODO placeholders that may indicate incomplete generation.")

    return errors, warnings


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 – Import path auto-fixer
# ─────────────────────────────────────────────────────────────────────────────

# Matches:  import Foo from './some/path'   or   import { Foo } from "../some/path.js"
_IMPORT_RE = re.compile(r"""(import\s+[^'"]+from\s+)(['"])(\.\.?/[^'"]+)(\2)""")


def _fix_import_paths(
    file_path: str,
    code: str,
    file_list: List[str],
) -> Tuple[str, List[str]]:
    """
    For every local import in `code`, verify the path exists in `file_list`.
    If not, use difflib to find the closest real file and rewrite the import.
    Also ensures .js extension is always present.

    Returns (fixed_code, list_of_auto_fix_descriptions).
    """
    fixes: List[str] = []

    # Build a lookup: lowercase-normalised basename → list of full relative paths
    # e.g. { "menuitemcontroller.js": ["controllers/menuItemController.js"] }
    basename_index: Dict[str, List[str]] = {}
    for fp in file_list:
        bn = os.path.basename(fp).lower()
        basename_index.setdefault(bn, []).append(fp)

    all_js_paths = [f for f in file_list if f.endswith(".js") or f.endswith(".json")]
    file_dir = os.path.dirname(file_path)  # directory of the file being fixed

    def _replace_import(m: re.Match) -> str:
        prefix    = m.group(1)   # "import Foo from "
        quote     = m.group(2)   # ' or "
        raw_path  = m.group(3)   # "./controllers/MenuItemController"

        # --- Step A: ensure .js extension ---
        if not raw_path.endswith(".js") and not raw_path.endswith(".json"):
            raw_path_with_ext = raw_path + ".js"
        else:
            raw_path_with_ext = raw_path

        # Resolve the import to an absolute-like relative path from project root
        # file_dir is e.g. "routes" for routes/menuRoutes.js
        resolved = os.path.normpath(os.path.join(file_dir, raw_path_with_ext))
        # Normalise to forward slashes and strip leading ./
        resolved = resolved.replace("\\", "/").lstrip("./")

        # --- Step B: check if resolved path is in the file_list (case-sensitive) ---
        if resolved in file_list:
            # Path is fine; only fix the .js extension if it was missing
            if raw_path != (raw_path_with_ext if raw_path_with_ext != raw_path else raw_path):
                fixes.append(f"Added missing .js extension to import '{raw_path}'")
            corrected_import_path = _make_relative(file_dir, resolved)
            return f"{prefix}{quote}{corrected_import_path}{quote}"

        # --- Step C: case-insensitive basename lookup ---
        resolved_basename = os.path.basename(resolved).lower()
        candidates = basename_index.get(resolved_basename, [])

        if candidates:
            # Found same basename with different casing/path
            correct_path = candidates[0]  # take first (usually only one)
            corrected_import_path = _make_relative(file_dir, correct_path)
            fixes.append(
                f"Fixed import casing/path: '{raw_path}' → '{corrected_import_path}' "
                f"(actual file: {correct_path})"
            )
            return f"{prefix}{quote}{corrected_import_path}{quote}"

        # --- Step D: fuzzy match on full list ---
        closest = difflib.get_close_matches(
            resolved_basename,
            [os.path.basename(f).lower() for f in all_js_paths],
            n=1,
            cutoff=0.6,
        )
        if closest:
            matched_bn = closest[0]
            matched_paths = basename_index.get(matched_bn, [])
            if matched_paths:
                correct_path = matched_paths[0]
                corrected_import_path = _make_relative(file_dir, correct_path)
                fixes.append(
                    f"Auto-resolved fuzzy import: '{raw_path}' → '{corrected_import_path}' "
                    f"(closest match: {correct_path})"
                )
                return f"{prefix}{quote}{corrected_import_path}{quote}"

        # --- Step E: no fix found — at minimum ensure .js extension ---
        if not raw_path.endswith(".js") and not raw_path.endswith(".json"):
            fixes.append(f"Added missing .js extension to unresolved import '{raw_path}'")
            corrected = raw_path + ".js"
            return f"{prefix}{quote}{corrected}{quote}"

        # Nothing to change
        return m.group(0)

    fixed_code = _IMPORT_RE.sub(_replace_import, code)

    for fix in fixes:
        logger.info("[validator] AUTO-FIX: %s", fix)

    return fixed_code, fixes


def _make_relative(from_dir: str, to_path: str) -> str:
    """
    Compute the relative import path from `from_dir` to `to_path` (project-root relative).
    Ensures the result starts with './' or '../' and ends with the filename.

    Example:
        from_dir = "routes"
        to_path  = "controllers/menuItemController.js"
        → "../controllers/menuItemController.js"
    """
    if not from_dir:
        # file is at project root (e.g. app.js)
        rel = to_path
    else:
        rel = os.path.relpath(to_path, from_dir).replace("\\", "/")

    if not rel.startswith("."):
        rel = "./" + rel

    return rel
