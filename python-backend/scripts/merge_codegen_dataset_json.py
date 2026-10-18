#!/usr/bin/env python3
"""
Merge per-record CodeGen dataset JSON files into one JSONL file.

Example:
  python scripts/merge_codegen_dataset_json.py \
    --input-dir datasets/codegen_dataset \
    --output datasets/codegen_synthetic.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir)
    output_path = Path(args.output)

    if not input_dir.exists():
        raise FileNotFoundError(input_dir)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    records = load_records(input_dir)

    if args.only_valid:
        records = [
            record for record in records
            if record.get("metadata", {}).get("validation", {}).get("passed") is True
        ]

    with output_path.open("w", encoding="utf-8") as out:
        for record in records:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"Merged {len(records)} record(s) into {output_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge per-record CodeGen dataset JSON files into JSONL.")
    parser.add_argument("--input-dir", default="datasets/codegen_dataset", help="Directory containing per-record JSON files.")
    parser.add_argument("--output", default="datasets/codegen_synthetic.jsonl", help="Output JSONL path.")
    parser.add_argument("--only-valid", action="store_true", help="Include only records whose validation.passed is true.")
    return parser.parse_args()


def load_records(input_dir: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    seen_ids = set()

    for path in sorted(input_dir.rglob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[skip] Invalid JSON {path}: {exc}")
            continue

        record_id = record.get("id")
        if not record_id:
            print(f"[skip] Missing id: {path}")
            continue
        if record_id in seen_ids:
            print(f"[skip] Duplicate id: {path}")
            continue

        messages = record.get("messages")
        if not isinstance(messages, list) or len(messages) < 3:
            print(f"[skip] Missing chat messages: {path}")
            continue

        seen_ids.add(record_id)
        records.append(record)

    return records


if __name__ == "__main__":
    raise SystemExit(main())
