"""
Experiment 1: Planning Agent Blueprint Validity Evaluation
==========================================================
Measures blueprint validity rate for:
  - RAG-augmented mode (your real system)
  - Zero-shot baseline (no memory retrieval)

Run from python-backend directory:
    python eval/eval_planner.py

Outputs:
  - eval/results/planner_eval_results.json  (raw)
  - eval/results/planner_eval_summary.txt   (human-readable, copy into paper)
"""

import json
import os
import sys
import logging
from pathlib import Path
from datetime import datetime

# Make sure we can import from the backend
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.planner_agent import PlannerAgent
from services.plan_memory import PlanMemory

logging.basicConfig(level=logging.WARNING)  # Suppress verbose logs during eval
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config — edit these to match your Modal / Ollama setup
# ─────────────────────────────────────────────────────────────────────────────
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL      = os.getenv("PLANNER_MODEL", "qwen2.5-coder:7b")

USE_OPENAI_COMPATIBLE   = os.getenv("USE_OPENAI_COMPATIBLE", "false").lower() == "true"
OPENAI_COMPATIBLE_URL   = os.getenv("OPENAI_COMPATIBLE_URL", "")
OPENAI_COMPATIBLE_KEY   = os.getenv("OPENAI_COMPATIBLE_API_KEY", "")
OPENAI_COMPATIBLE_PROV  = os.getenv("OPENAI_COMPATIBLE_PROVIDER", "modal")

# ─────────────────────────────────────────────────────────────────────────────
# 25 held-out test prompts (NOT in approved_plans.json)
# These are fresh prompts the model has never seen
# ─────────────────────────────────────────────────────────────────────────────
TEST_PROMPTS = [
    "Build a library management API to manage Books and Members with borrowing records using MVC.",
    "Create a job board backend with Jobs and Applicants. Use service-repository pattern.",
    "An event ticketing system for Events and Tickets. Standard MVC architecture.",
    "A social media backend with Posts, Users, and Likes. Use clean architecture.",
    "A school management API for Students, Teachers, and Grades. MVC pattern.",
    "An online marketplace for Products and Sellers. Use service-repository pattern.",
    "A news aggregator backend with Articles and Categories. MVC architecture.",
    "A pet adoption platform API with Pets and Adopters. Use clean architecture.",
    "A gym membership system with Members and Subscriptions. Service-repository pattern.",
    "A hotel room booking API with Rooms and Reservations. MVC architecture.",
    "A project management backend with Projects and Tasks assigned to Team Members. Clean architecture.",
    "A courier delivery system tracking Packages and Couriers. Service-repository pattern.",
    "A vehicle fleet management API for Vehicles and Drivers. MVC architecture.",
    "A music streaming backend with Songs and Playlists owned by Users. Clean architecture.",
    "A forum discussion API with Threads, Posts, and Users. Service-repository pattern.",
    "An HR leave management system for Employees and Leave Requests. MVC architecture.",
    "A recipe sharing platform with Recipes and Ingredients authored by Users. Clean architecture.",
    "A logistics tracking system for Shipments and Warehouses. Service-repository pattern.",
    "An auction platform backend with Items and Bids placed by Buyers. MVC architecture.",
    "A hospital bed management API for Beds and Admissions. Clean architecture.",
    "An expense tracker with Expenses and Budgets linked to Users. Service-repository pattern.",
    "A ticketing support desk API for Tickets and Agents. MVC architecture.",
    "A research paper repository backend with Papers and Authors. Clean architecture.",
    "An inventory reorder system for Products with automatic threshold alerts. Service-repository.",
    "A volunteer coordination platform with Volunteers and Events they sign up for. MVC architecture.",
]

REQUIRED_TOP_LEVEL_KEYS = {"projectName", "architecture", "entities", "features", "files"}
REQUIRED_ARCH_KEYS       = {"stack", "pattern", "language", "moduleSystem", "database", "orm"}


def validate_blueprint(json_str: str) -> tuple[bool, str]:
    """Returns (is_valid, reason)."""
    try:
        data = json.loads(json_str)
    except Exception as e:
        return False, f"JSON parse error: {e}"

    missing_top = REQUIRED_TOP_LEVEL_KEYS - set(data.keys())
    if missing_top:
        return False, f"Missing top-level keys: {missing_top}"

    arch = data.get("architecture", {})
    missing_arch = REQUIRED_ARCH_KEYS - set(arch.keys())
    if missing_arch:
        return False, f"Missing architecture keys: {missing_arch}"

    if not data.get("entities") or not isinstance(data["entities"], list) or len(data["entities"]) == 0:
        return False, "No entities defined"

    if not data.get("files") or not isinstance(data["files"], list) or len(data["files"]) == 0:
        return False, "No files defined"

    if not data.get("projectName") or not isinstance(data["projectName"], str):
        return False, "Invalid or missing projectName"

    return True, "OK"


def run_planner(prompt: str, use_rag: bool) -> tuple[bool, str, str]:
    """
    Runs the planner agent on a prompt.
    Returns (is_valid, reason, raw_json_output)
    """
    from services.plan_memory import PlanMemory
    from agents.planner_agent import PlannerAgent
    from prompts.planner_prompt import PLANNER_SYSTEM_PROMPT, build_planner_prompt
    import re

    agent = PlannerAgent(
        ollama_url=OLLAMA_URL,
        model=MODEL,
        use_openai_compatible=USE_OPENAI_COMPATIBLE,
        openai_compatible_url=OPENAI_COMPATIBLE_URL,
        openai_compatible_api_key=OPENAI_COMPATIBLE_KEY,
        openai_compatible_provider=OPENAI_COMPATIBLE_PROV,
    )

    if not use_rag:
        # ── TRUE ZERO-SHOT BASELINE ──
        # Bypass the agentic retry loop and Python sanitizers completely.
        # Just ask the LLM once.
        model_prompt = build_planner_prompt(prompt, None)
        try:
            if USE_OPENAI_COMPATIBLE:
                raw_response = agent._query_openai_compatible(model_prompt, PLANNER_SYSTEM_PROMPT)
            else:
                raw_response = agent._query_ollama(model_prompt, PLANNER_SYSTEM_PROMPT)

            # Extract JSON block
            text = raw_response.strip()
            json_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
            if json_match:
                text = json_match.group(1).strip()
            else:
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end != -1:
                    text = text[start:end+1]

            is_valid, reason = validate_blueprint(text)
            return is_valid, reason, text
        except Exception as e:
            return False, f"LLM Call Failed: {e}", ""

    # ── PROPOSED FRAMEWORK (RAG + AGENTIC LOOP) ──
    try:
        result = agent.execute(prompt)
        raw_json = result.model_dump_json()
        is_valid, reason = validate_blueprint(raw_json)
        return is_valid, reason, raw_json
    except Exception as e:
        return False, f"Agent exception: {e}", ""


def main():
    output_dir = Path(__file__).parent / "results"
    output_dir.mkdir(parents=True, exist_ok=True)

    rag_results      = []
    zeroshot_results = []

    total = len(TEST_PROMPTS)

    print(f"\n{'='*60}")
    print(f"  Planning Agent Evaluation — {total} prompts")
    print(f"  Model: {MODEL}")
    print(f"  OpenAI-Compatible: {USE_OPENAI_COMPATIBLE}")
    print(f"{'='*60}\n")

    for i, prompt in enumerate(TEST_PROMPTS, 1):
        print(f"[{i:02d}/{total}] Prompt: {prompt[:60]}...")

        # ── RAG mode ──
        print(f"        → RAG mode...", end=" ", flush=True)
        rag_valid, rag_reason, rag_json = run_planner(prompt, use_rag=True)
        rag_results.append({"prompt": prompt, "valid": rag_valid, "reason": rag_reason, "output": rag_json[:200]})
        print(f"{'✅ VALID' if rag_valid else '❌ INVALID'} ({rag_reason})")

        # ── Zero-shot mode ──
        print(f"        → Zero-shot mode...", end=" ", flush=True)
        zs_valid, zs_reason, zs_json = run_planner(prompt, use_rag=False)
        zeroshot_results.append({"prompt": prompt, "valid": zs_valid, "reason": zs_reason, "output": zs_json[:200]})
        print(f"{'✅ VALID' if zs_valid else '❌ INVALID'} ({zs_reason})")

        print()

    # ── Compute metrics ──
    rag_valid_count = sum(1 for r in rag_results if r["valid"])
    zs_valid_count  = sum(1 for r in zeroshot_results if r["valid"])

    rag_rate = (rag_valid_count / total) * 100
    zs_rate  = (zs_valid_count / total) * 100

    summary = f"""
Planning Agent Blueprint Validity Evaluation
============================================
Date:           {datetime.now().strftime('%Y-%m-%d %H:%M')}
Model:          {MODEL}
Total Prompts:  {total}

RAG-Augmented Mode:
  Valid blueprints:   {rag_valid_count} / {total}
  Validity Rate:      {rag_rate:.1f}%

Zero-Shot Baseline:
  Valid blueprints:   {zs_valid_count} / {total}
  Validity Rate:      {zs_rate:.1f}%

Improvement (RAG over Zero-Shot): +{rag_rate - zs_rate:.1f}%

--- For TABLE II (System Evaluation) ---
Planning Agent | Blueprint Validity Rate (RAG) | {rag_rate:.1f}%

--- For TABLE III (Baseline Comparison) ---
Zero-Shot Single LLM | Blueprint Valid | {zs_rate:.1f}%
Proposed Framework   | Blueprint Valid | {rag_rate:.1f}%
"""

    # ── Save results ──
    raw_results = {
        "timestamp": datetime.now().isoformat(),
        "model": MODEL,
        "total_prompts": total,
        "rag_validity_rate": round(rag_rate, 1),
        "zeroshot_validity_rate": round(zs_rate, 1),
        "rag_results": rag_results,
        "zeroshot_results": zeroshot_results,
    }

    json_path = output_dir / "planner_eval_results.json"
    txt_path  = output_dir / "planner_eval_summary.txt"

    json_path.write_text(json.dumps(raw_results, indent=2))
    txt_path.write_text(summary)

    print(summary)
    print(f"Results saved to:")
    print(f"  {json_path}")
    print(f"  {txt_path}")


if __name__ == "__main__":
    main()
