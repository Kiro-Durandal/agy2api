"""Opt-in live checks for the OpenAI adapter.

The test key is read only from AGY2API_TEST_KEY. Nothing runs during pytest or
unittest discovery; invoke this file explicitly after configuring a test host.
The default Gemini endpoint tests locally converted requests without deploying
the patch. Use --endpoint openai after deploying it to test the full route.
"""

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.converter.openai2gemini import convert_openai_to_gemini_request
from src.models import OpenAIChatCompletionRequest, model_to_dict


CASES = {
    "system": [
        {"role": "system", "content": "Answer in one short sentence."},
        {"role": "user", "content": "Say hello."},
    ],
    "developer": [
        {"role": "developer", "content": "Answer in one short sentence."},
        {"role": "user", "content": "Say hello."},
    ],
    "interleaved_developer": [
        {"role": "user", "content": "Say hello."},
        {"role": "assistant", "content": "Hello."},
        {"role": "developer", "content": "Answer in one short sentence."},
        {"role": "user", "content": "Say goodbye."},
    ],
    "tool_history": [
        {"role": "user", "content": "Look up a value."},
        {"role": "assistant", "tool_calls": [{
            "id": "call_smoke_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"key":"test"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_smoke_1", "content": '{"value":"OK"}'},
        {"role": "user", "content": "Summarize the returned value."},
    ],
}

CASE_OPTIONS = {
    "tool_history": {
        "tools": [{
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Return the value for a key",
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            },
        }],
    },
}


async def convert(model: str, messages: list[dict], options: dict) -> dict:
    parsed = OpenAIChatCompletionRequest.model_validate({
        "model": model, "messages": messages, **options,
    })
    return await convert_openai_to_gemini_request(model_to_dict(parsed))


def request_once(url: str, key: str, body: dict, timeout: float) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            data = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        status = exc.code
        data = exc.read(1024 * 1024)
    try:
        return status, json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, {"error": {"message": "Non-JSON response"}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="agy2api origin, without the /v1 path")
    parser.add_argument("--model", required=True, help="model exposed by the agy2api server")
    parser.add_argument("--endpoint", choices=("gemini", "openai"), default="gemini")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--case", choices=[*CASES, "all"], default="all")
    args = parser.parse_args()

    key = os.environ.get("AGY2API_TEST_KEY")
    if not key:
        parser.error("Set AGY2API_TEST_KEY in the environment; do not pass a key on the command line")

    model_path = urllib.parse.quote(args.model, safe="")
    if args.endpoint == "gemini":
        url = args.base_url.rstrip("/") + f"/v1/models/{model_path}:generateContent"
    else:
        url = args.base_url.rstrip("/") + "/v1/chat/completions"
    selected = CASES.items() if args.case == "all" else [(args.case, CASES[args.case])]
    failed = False
    for name, messages in selected:
        options = CASE_OPTIONS.get(name, {})
        if args.endpoint == "gemini":
            body = asyncio.run(convert(args.model, messages, options))
            roles = [item.get("role") for item in body.get("contents", [])]
            if not roles or any(role not in {"user", "model"} for role in roles):
                print(f"{name}: conversion produced an invalid role sequence: {roles}")
                failed = True
                continue
        else:
            body = {"model": args.model, "messages": messages, **options}
            roles = [item["role"] for item in messages]
        status, response = request_once(url, key, body, args.timeout)
        result_key = "candidates" if args.endpoint == "gemini" else "choices"
        if status == 200 and response.get(result_key):
            print(f"{name}: HTTP 200, {result_key} present, request roles={roles}")
        else:
            error = response.get("error", {})
            message = error.get("message", "No candidates or unrecognized response")
            safe_message = str(message).replace(key, "[REDACTED]")[:300]
            print(f"{name}: HTTP {status}, {safe_message}, request roles={roles}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
