"""Exercise the patched OpenAI route without deploying or calling Google."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.router.antigravity.openai import router
from src.utils import authenticate_bearer


class OpenAIRouterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[authenticate_bearer] = lambda: "test"
        self.backend_payloads = []

    def request(self, messages):
        async def fake_backend(body, log_ctx):
            self.backend_payloads.append(body)
            return JSONResponse(content={"response": {"candidates": [{
                "content": {"role": "model", "parts": [{"text": "OK"}]},
                "finishReason": "STOP",
            }]}})

        with patch("src.converter.antigravity_fix._get_model_rewrite_rules", new=AsyncMock(return_value=None)), \
             patch("src.converter.antigravity_fix._get_sensitive_word_patterns", new=AsyncMock(return_value=None)), \
             patch("src.api.antigravity.non_stream_request", new=fake_backend), \
             TestClient(self.app) as client:
            return client.post("/v1/chat/completions", json={
                "model": "gemini-3-flash", "messages": messages,
            })

    def test_interleaved_developer_reaches_backend_as_system_instruction(self):
        response = self.request([
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "developer", "content": "Answer briefly"},
            {"role": "user", "content": "Continue"},
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["role"], "assistant")
        payload = self.backend_payloads[0]["request"]
        self.assertEqual(payload["systemInstruction"]["parts"], [{"text": "Answer briefly"}])
        self.assertEqual([item["role"] for item in payload["contents"]], ["user", "model", "user"])

    def test_tool_arguments_and_result_reach_backend(self):
        response = self.request([
            {"role": "user", "content": "Look up a value"},
            {"role": "assistant", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "lookup", "arguments": '{"key":"test"}'},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"value":"OK"}'},
            {"role": "user", "content": "Summarize"},
        ])
        self.assertEqual(response.status_code, 200)
        parts = [part for item in self.backend_payloads[0]["request"]["contents"]
                 for part in item["parts"]]
        call = next(part["functionCall"] for part in parts if "functionCall" in part)
        result = next(part["functionResponse"] for part in parts if "functionResponse" in part)
        self.assertEqual(call["args"], {"key": "test"})
        self.assertEqual(result["response"], {"value": "OK"})

    def test_unrepresentable_final_assistant_stops_before_backend(self):
        response = self.request([
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.backend_payloads, [])


if __name__ == "__main__":
    unittest.main()
