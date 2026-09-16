"""Smoke-test the configured LLM: python scripts/check_llm.py [--model ollama/llama3.1]"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm import chat, resolve  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--model", default=None)
args = ap.parse_args()

cfg = resolve(args.model)
print(f"model   : {cfg.model}")
print(f"api_base: {cfg.api_base or '(provider default)'}")
print(f"api_key : {'set' if cfg.api_key else 'none'}")
print("reply   :", chat([{"role": "user", "content": "Reply with exactly one word: pong"}], model=args.model))
