#!/usr/bin/env python3
"""Send one bounded worker task to an OpenRouter model without persisting credentials."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

from common import DEFAULT_OPENROUTER_MODEL, USER_AGENT, sanitize_error


ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MAX_PDF_BYTES = 100 * 1024 * 1024


def read_text(path: str | None, inline: str | None) -> str:
    if path:
        return Path(path).expanduser().resolve().read_text(encoding="utf-8")
    if inline is not None:
        return inline
    if sys.stdin.isatty():
        raise ValueError("provide --prompt, --prompt-file, or pipe a prompt on stdin")
    return sys.stdin.read()


def pdf_part(value: str) -> Mapping[str, Any]:
    if value.startswith(("https://", "http://")):
        filename = value.rstrip("/").rsplit("/", 1)[-1] or "document.pdf"
        file_data = value
    else:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"only PDF attachments are supported: {path}")
        size = path.stat().st_size
        if size > MAX_PDF_BYTES:
            raise ValueError(f"PDF exceeds OpenRouter's 100 MB limit: {path}")
        filename = path.name
        file_data = "data:application/pdf;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "file", "file": {"filename": filename, "file_data": file_data}}


def build_messages(
    prompt: str,
    *,
    system: str | None = None,
    pdfs: Sequence[str] = (),
) -> list[Mapping[str, Any]]:
    messages: list[Mapping[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    if pdfs:
        content: list[Mapping[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(pdf_part(value) for value in pdfs)
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})
    return messages


def extract_text(document: Mapping[str, Any]) -> str:
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenRouter response has no choices")
    message = choices[0].get("message", {})
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) for item in content
            if isinstance(item, Mapping) and item.get("type") in {"text", "output_text"}
        ).strip()
    raise RuntimeError("OpenRouter response has no text content")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    prompt = parser.add_mutually_exclusive_group()
    prompt.add_argument("--prompt", help="short inline task; prefer --prompt-file for substantial work")
    prompt.add_argument("--prompt-file", help="UTF-8 task file")
    parser.add_argument("--system-file", help="optional UTF-8 worker instructions")
    parser.add_argument("--pdf", action="append", default=[], help="local PDF path or public PDF URL; repeatable")
    parser.add_argument("--model", help="OpenRouter slug; defaults to MODEL_INTELLIGENCE_OPENROUTER_MODEL")
    parser.add_argument("--pdf-engine", choices=("cloudflare-ai", "mistral-ocr", "native"))
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--reasoning-effort", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--json", action="store_true", help="emit a compact response envelope")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    key = os.environ.get("OPENROUTER_API_KEY")
    model = args.model or os.environ.get("MODEL_INTELLIGENCE_OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
    if not key:
        raise ValueError("missing required OpenRouter credential in the environment")
    if not model:
        raise ValueError("set --model or MODEL_INTELLIGENCE_OPENROUTER_MODEL")
    if args.max_tokens < 1:
        raise ValueError("--max-tokens must be positive")
    if not 0 <= args.temperature <= 2:
        raise ValueError("--temperature must be between 0 and 2")
    task = read_text(args.prompt_file, args.prompt)
    if not task.strip():
        raise ValueError("worker task is empty")
    system = Path(args.system_file).expanduser().resolve().read_text(encoding="utf-8") if args.system_file else None
    payload: dict[str, Any] = {
        "model": model,
        "messages": build_messages(task, system=system, pdfs=args.pdf),
        "temperature": args.temperature,
        "seed": args.seed,
        "max_tokens": args.max_tokens,
        "stream": False,
    }
    if args.reasoning_effort:
        payload["reasoning_effort"] = args.reasoning_effort
    if args.pdf:
        engine = args.pdf_engine or os.environ.get("OPENROUTER_PDF_ENGINE", "cloudflare-ai")
        payload["plugins"] = [{"id": "file-parser", "pdf": {"engine": engine}}]
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            document = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
    content = extract_text(document)
    if args.json:
        envelope = {
            "id": document.get("id"),
            "model": document.get("model", model),
            "content": content,
            "finish_reason": document.get("choices", [{}])[0].get("finish_reason"),
            "usage": document.get("usage"),
        }
        print(json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(content)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"openrouter-worker: {sanitize_error(exc)}", file=sys.stderr)
        raise SystemExit(2)
