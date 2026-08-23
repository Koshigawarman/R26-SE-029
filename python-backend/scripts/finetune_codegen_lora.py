#!/usr/bin/env python3
"""
Fine-tune a CodeGen chat model with LoRA/QLoRA using JSONL records.

Expected dataset record:
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "source code"}
  ],
  "metadata": {...}
}

Install example:
  pip install -U "transformers>=4.45.0" "datasets>=2.20.0" \
    "accelerate>=0.33.0" "peft>=0.12.0" "trl>=0.9.6" bitsandbytes

Run example:
  python scripts/finetune_codegen_lora.py \
    --dataset datasets/codegen_synthetic.jsonl \
    --output-dir outputs/qwen-codegen-lora \
    --model Qwen/Qwen2.5-Coder-3B-Instruct \
    --max-seq-length 4096 \
    --epochs 2

After training, the output directory contains LoRA adapter weights. You can
load them with the base model, or merge them later for deployment.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import torch
from datasets import Dataset
from peft import LoraConfig, TaskType
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TrainingArguments,
)
from trl import SFTTrainer


def main() -> int:
    args = parse_args()

    dataset_path = Path(args.dataset)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    tokenizer = AutoTokenizer.from_pretrained(
        args.model,
        trust_remote_code=True,
        use_fast=True,
    )

    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    train_dataset = load_chat_dataset(dataset_path, tokenizer, args.limit)
    split = train_dataset.train_test_split(test_size=args.eval_ratio, seed=args.seed)

    quantization_config = None
    torch_dtype = torch.bfloat16 if args.bf16 else torch.float16

    if args.load_in_4bit:
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch_dtype,
            bnb_4bit_use_double_quant=True,
        )

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        trust_remote_code=True,
        torch_dtype=torch_dtype,
        device_map="auto",
        quantization_config=quantization_config,
    )

    model.config.use_cache = False

    peft_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        target_modules=args.target_modules.split(","),
    )

    training_args = TrainingArguments(
        output_dir=str(output_dir),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.eval_batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.learning_rate,
        warmup_ratio=args.warmup_ratio,
        lr_scheduler_type="cosine",
        logging_steps=args.logging_steps,
        save_steps=args.save_steps,
        eval_steps=args.eval_steps,
        evaluation_strategy="steps",
        save_strategy="steps",
        save_total_limit=args.save_total_limit,
        bf16=args.bf16,
        fp16=not args.bf16,
        optim="paged_adamw_8bit" if args.load_in_4bit else "adamw_torch",
        gradient_checkpointing=True,
        report_to="none",
        remove_unused_columns=False,
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=split["train"],
        eval_dataset=split["test"],
        peft_config=peft_config,
        dataset_text_field="text",
        max_seq_length=args.max_seq_length,
        packing=args.packing,
        args=training_args,
    )

    trainer.train()
    trainer.save_model(str(output_dir))
    tokenizer.save_pretrained(str(output_dir))

    write_training_card(output_dir, args, len(split["train"]), len(split["test"]))

    print(f"Saved LoRA adapter to: {output_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fine-tune CodeGen model with LoRA/QLoRA.")
    parser.add_argument("--dataset", required=True, help="CodeGen JSONL dataset path.")
    parser.add_argument("--output-dir", required=True, help="Output adapter directory.")
    parser.add_argument("--model", default="Qwen/Qwen2.5-Coder-3B-Instruct")
    parser.add_argument("--limit", type=int, default=0, help="Optional record limit for testing.")
    parser.add_argument("--max-seq-length", type=int, default=4096)
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--eval-batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--warmup-ratio", type=float, default=0.03)
    parser.add_argument("--eval-ratio", type=float, default=0.05)
    parser.add_argument("--logging-steps", type=int, default=10)
    parser.add_argument("--save-steps", type=int, default=100)
    parser.add_argument("--eval-steps", type=int, default=100)
    parser.add_argument("--save-total-limit", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--load-in-4bit", action="store_true", default=True)
    parser.add_argument("--no-4bit", action="store_false", dest="load_in_4bit")
    parser.add_argument("--bf16", action="store_true", default=True)
    parser.add_argument("--fp16", action="store_false", dest="bf16")
    parser.add_argument("--packing", action="store_true", help="Pack short examples together.")
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument(
        "--target-modules",
        default="q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj",
        help="Comma-separated LoRA target module names.",
    )
    return parser.parse_args()


def load_chat_dataset(path: Path, tokenizer: Any, limit: int = 0) -> Dataset:
    rows: List[Dict[str, str]] = []

    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if limit and len(rows) >= limit:
                break

            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_no}: {exc}") from exc

            messages = record.get("messages")
            if not isinstance(messages, list):
                raise ValueError(f"Line {line_no} missing messages list.")

            text = tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=False,
            )

            rows.append(
                {
                    "text": text,
                    "target_file": str((record.get("metadata") or {}).get("target_file", "")),
                }
            )

    if len(rows) < 2:
        raise ValueError("Need at least 2 dataset records for train/eval split.")

    return Dataset.from_list(rows)


def write_training_card(output_dir: Path, args: argparse.Namespace, train_size: int, eval_size: int) -> None:
    payload = {
        "base_model": args.model,
        "dataset": args.dataset,
        "train_size": train_size,
        "eval_size": eval_size,
        "method": "LoRA/QLoRA SFT",
        "max_seq_length": args.max_seq_length,
        "epochs": args.epochs,
        "learning_rate": args.learning_rate,
        "lora": {
            "r": args.lora_r,
            "alpha": args.lora_alpha,
            "dropout": args.lora_dropout,
            "target_modules": args.target_modules.split(","),
        },
    }
    (output_dir / "training_card.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
