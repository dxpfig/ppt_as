#!/usr/bin/env python3
"""Probe a local OpenAI-compatible Qwen service without exposing its key."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values


def reject_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("模型地址必须是有效的 HTTP(S) URL。")
    if parsed.username or parsed.password:
        raise ValueError("模型地址不能包含内嵌凭证。")
    return value.rstrip("/")


def probe(env_file: Path, image: Path | None) -> dict:
    values = {**dotenv_values(env_file), **os.environ}
    provider = values.get("PPT_LLM_PROVIDER") or "openai"
    model = values.get("PPT_LLM_MODEL") or values.get("OPENAI_MODEL")
    base_url = reject_url(values.get("OPENAI_BASE_URL") or "")
    key = values.get("OPENAI_API_KEY") or ""
    if provider != "openai" or not model or not key:
        raise ValueError("Qwen 检查需要 openai provider、模型名和 API Key。")
    headers = {"Authorization": f"Bearer {key}"}
    timeout = httpx.Timeout(120, connect=10)
    result = {"baseUrl": base_url, "requestedModel": model, "checks": {}}
    with httpx.Client(headers=headers, timeout=timeout, trust_env=False) as client:
        response = client.get(base_url + "/models")
        response.raise_for_status()
        models = response.json().get("data") or []
        selected = next((item for item in models if item.get("id") == model), None)
        if selected is None:
            raise ValueError(f"模型列表中没有 {model}。")
        result["checks"]["models"] = {
            "status": "passed",
            "ownedBy": selected.get("owned_by"),
            "maxModelLen": selected.get("max_model_len"),
            "root": selected.get("root"),
        }

        tokenize = client.post(
            base_url.rsplit("/v1", 1)[0] + "/tokenize",
            json={"model": model, "prompt": "本地 Qwen 连通性检查"},
        )
        tokenize.raise_for_status()
        token_data = tokenize.json()
        result["checks"]["tokenize"] = {
            "status": "passed",
            "count": token_data.get("count"),
            "maxModelLen": token_data.get("max_model_len"),
        }

        structured = client.post(
            base_url + "/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "返回本地模型检查结果。"}],
                "max_tokens": 128,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "local_probe",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
        )
        structured.raise_for_status()
        payload = structured.json()
        content = payload["choices"][0]["message"].get("content") or ""
        parsed = json.loads(content)
        if parsed != {"ok": True}:
            raise ValueError("结构化输出内容不符合检查契约。")
        result["checks"]["structuredOutput"] = {
            "status": "passed",
            "finishReason": payload["choices"][0].get("finish_reason"),
            "usage": payload.get("usage"),
        }

        if image:
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            visual = client.post(
                base_url + "/chat/completions",
                json={
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用一句中文描述图片主色调。"},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/png;base64,{encoded}",
                                "detail": "low",
                            }},
                        ],
                    }],
                    "max_tokens": 64,
                    "temperature": 0,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
            visual.raise_for_status()
            visual_payload = visual.json()
            description = visual_payload["choices"][0]["message"].get("content") or ""
            if not description.strip():
                raise ValueError("图片输入没有返回描述。")
            result["checks"]["vision"] = {
                "status": "passed",
                "description": description[:200],
                "usage": visual_payload.get("usage"),
            }
    result["status"] = "passed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--image", type=Path)
    args = parser.parse_args()
    if not args.env.is_file():
        print(f"环境文件不存在：{args.env}", file=sys.stderr)
        return 2
    if args.image and not args.image.is_file():
        print(f"图片文件不存在：{args.image}", file=sys.stderr)
        return 2
    try:
        result = probe(args.env, args.image)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)[:1000]}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
