"""Regression checks for OpenAI Chat Completions message adaptation."""

import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from src.converter.openai2gemini import convert_openai_to_gemini_request
from src.converter.antigravity_fix import normalize_antigravity_request
from src.models import OpenAIChatCompletionRequest, model_to_dict


class OpenAIMessageConversionTests(unittest.IsolatedAsyncioTestCase):
    async def convert(self, messages, **options):
        parsed = OpenAIChatCompletionRequest.model_validate(
            {"model": "gemini-3-flash", "messages": messages, **options}
        )
        return await convert_openai_to_gemini_request(model_to_dict(parsed))

    async def test_interleaved_developer_and_tool_round_trip(self):
        result = await self.convert([
            {"role": "system", "content": "Global rule"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "developer", "content": [{"type": "text", "text": "Current rule"}]},
            {"role": "user", "content": "Find Shanghai weather"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_weather",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city":"Shanghai"}',
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "call_weather", "content": '{"temperature":20}'},
            {"role": "user", "content": "Summarize it"},
        ])

        self.assertEqual(
            result["systemInstruction"]["parts"],
            [{"text": "Global rule"}, {"text": "Current rule"}],
        )
        self.assertTrue(all(item["role"] in {"user", "model"} for item in result["contents"]))
        self.assertFalse(
            any(part.get("text") == "Current rule" for item in result["contents"]
                for part in item["parts"])
        )
        calls = [part["functionCall"] for item in result["contents"]
                 for part in item["parts"] if "functionCall" in part]
        responses = [part["functionResponse"] for item in result["contents"]
                     for part in item["parts"] if "functionResponse" in part]
        self.assertEqual(calls[0]["args"], {"city": "Shanghai"})
        self.assertEqual(responses[0], {
            "id": "call_weather",
            "name": "get_weather",
            "response": {"temperature": 20},
        })

        with patch("src.converter.antigravity_fix._get_model_rewrite_rules", new=AsyncMock(return_value=None)), \
             patch("src.converter.antigravity_fix._get_sensitive_word_patterns", new=AsyncMock(return_value=None)):
            outgoing = await normalize_antigravity_request({**result, "model": "gemini-3-flash"})
        self.assertEqual(
            [item["role"] for item in outgoing["contents"]],
            [item["role"] for item in result["contents"]],
        )
        self.assertEqual(outgoing["systemInstruction"], result["systemInstruction"])

    async def test_tool_result_text_parts(self):
        result = await self.convert([
            {"role": "user", "content": "Run a tool"},
            {"role": "assistant", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": [
                {"type": "text", "text": "first "},
                {"type": "text", "text": "second"},
            ]},
        ])
        response = result["contents"][-1]["parts"][0]["functionResponse"]
        self.assertEqual(response["response"], {"result": "first second"})

    async def test_user_text_and_inline_image_are_preserved(self):
        result = await self.convert([{"role": "user", "content": [
            {"type": "text", "text": "Describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
        ]}])
        self.assertEqual(result["contents"][0]["parts"], [
            {"text": "Describe this"},
            {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}},
        ])

    async def test_remote_image_and_audio_are_not_silently_dropped(self):
        for part in (
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
            {"type": "input_audio", "input_audio": {"data": "aGVsbG8=", "format": "wav"}},
        ):
            with self.subTest(part=part["type"]), self.assertRaises(ValueError):
                await self.convert([{"role": "user", "content": [{"type": "text", "text": "Question"}, part]}])

    async def test_assistant_refusal_is_preserved_as_text(self):
        result = await self.convert([
            {"role": "user", "content": "First request"},
            {"role": "assistant", "content": [{"type": "refusal", "refusal": "I cannot help."}]},
            {"role": "user", "content": "Next request"},
        ])
        self.assertEqual(result["contents"][1], {"role": "model", "parts": [{"text": "I cannot help."}]})

    async def test_empty_assistant_history_is_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "no content representable"):
            await self.convert([
                {"role": "user", "content": "First request"},
                {"role": "assistant", "content": None},
                {"role": "user", "content": "Next request"},
            ])

    async def test_nontext_tool_part_is_rejected_locally(self):
        with self.assertRaisesRegex(ValueError, "text parts"):
            await self.convert([
                {"role": "user", "content": "Run a tool"},
                {"role": "assistant", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": [
                    {"type": "text", "text": 123},
                ]},
            ])

    async def test_nonobject_tool_arguments_are_rejected_locally(self):
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            await self.convert([
                {"role": "user", "content": "Run a tool"},
                {"role": "assistant", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "lookup", "arguments": "[]"},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": "done"},
            ])

    async def test_malformed_tool_arguments_are_not_replaced_with_empty_object(self):
        with self.assertRaisesRegex(ValueError, "valid JSON object"):
            await self.convert([
                {"role": "user", "content": "Run a tool"},
                {"role": "assistant", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "lookup", "arguments": "{oops"},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": "done"},
            ])

    async def test_tool_choice_must_name_a_declared_function(self):
        with self.assertRaisesRegex(ValueError, "undeclared function"):
            await self.convert(
                [{"role": "user", "content": "Hello"}],
                tool_choice={"type": "function", "function": {"name": "missing"}},
            )

    async def test_tool_names_must_remain_unique_after_normalization(self):
        with self.assertRaisesRegex(ValueError, "collide"):
            await self.convert(
                [{"role": "user", "content": "Hello"}],
                tools=[
                    {"type": "function", "function": {"name": "one!"}},
                    {"type": "function", "function": {"name": "one?"}},
                ],
            )

    async def test_orphan_tool_result_is_rejected_locally(self):
        with self.assertRaisesRegex(ValueError, "unknown preceding tool_call_id"):
            await self.convert([
                {"role": "user", "content": "Run a tool"},
                {"role": "tool", "tool_call_id": "missing", "content": "result"},
            ])

    async def test_missing_tool_result_is_rejected_locally(self):
        with self.assertRaisesRegex(ValueError, "Missing tool response for: call_1"):
            await self.convert([
                {"role": "user", "content": "Run a tool"},
                {"role": "assistant", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }]},
                {"role": "user", "content": "Continue"},
            ])

    async def test_final_assistant_is_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "final assistant message"):
            await self.convert([
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
            ])

    def test_unknown_role_is_rejected_at_request_boundary(self):
        with self.assertRaises(ValidationError):
            OpenAIChatCompletionRequest.model_validate({
                "model": "gemini-3-flash",
                "messages": [{"role": "invalid", "content": "Hello"}],
            })

    def test_nontext_developer_content_is_rejected_at_request_boundary(self):
        with self.assertRaises(ValidationError):
            OpenAIChatCompletionRequest.model_validate({
                "model": "gemini-3-flash",
                "messages": [{"role": "developer", "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
                ]}],
            })

    def test_legacy_function_call_is_not_silently_dropped(self):
        with self.assertRaises(ValidationError):
            OpenAIChatCompletionRequest.model_validate({
                "model": "gemini-3-flash",
                "messages": [{"role": "assistant", "function_call": {
                    "name": "lookup", "arguments": "{}",
                }}],
            })


if __name__ == "__main__":
    unittest.main()
