"""Offline regression checks for the OpenAI request boundary and converter."""

import copy
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.converter.openai2gemini import convert_openai_to_gemini_request
from src.converter.utils import merge_system_messages
from src.models import OpenAIChatCompletionRequest, model_to_dict


_MISSING = object()


def tool_history(arguments="{}", name="lookup", result='{"value":"OK"}'):
    function = {"name": name}
    if arguments is not _MISSING:
        function["arguments"] = copy.deepcopy(arguments)
    return [
        {"role": "user", "content": "Look up a value."},
        {
            "role": "assistant",
            "tool_calls": [{"id": "call_1", "type": "function", "function": function}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": copy.deepcopy(result)},
    ]


def content_parts(request, key):
    return [
        part[key] for content in request["contents"] for part in content["parts"] if key in part
    ]


class TestOpenAIMessageConversion(unittest.IsolatedAsyncioTestCase):
    async def convert(self, messages, **options):
        request = OpenAIChatCompletionRequest.model_validate(
            {"model": "gemini-3.8-flash-high", "messages": messages, **options}
        )
        return await convert_openai_to_gemini_request(model_to_dict(request))

    async def test_tool_arguments_survive_request_validation(self):
        for arguments in ('{"key":"test"}', {"key": "test"}):
            with self.subTest(arguments=arguments):
                converted = await self.convert(tool_history(arguments))
                self.assertEqual(
                    content_parts(converted, "functionCall")[0]["args"], {"key": "test"}
                )
                self.assertEqual(content_parts(converted, "functionResponse")[0]["id"], "call_1")

    async def test_malformed_arguments_keep_upstream_recovery(self):
        cases = [
            (' {"key":"first"}{"key":"second"}', {"key": "first"}),
            ("{oops", {}),
            (_MISSING, {}),
        ]
        for arguments, expected in cases:
            with (
                self.subTest(arguments=arguments),
                patch("src.converter.openai2gemini.log.warning"),
            ):
                converted = await self.convert(tool_history(arguments))
                self.assertEqual(content_parts(converted, "functionCall")[0]["args"], expected)
                self.assertEqual(len(content_parts(converted, "functionResponse")), 1)

    async def test_interleaved_instructions_remain_instructions(self):
        for role in ("system", "developer"):
            with self.subTest(role=role):
                converted = await self.convert(
                    [
                        {"role": "system", "content": "Global rule."},
                        {"role": "user", "content": "Hello."},
                        {"role": "assistant", "content": "Hi."},
                        {"role": role, "content": [{"type": "text", "text": "Answer briefly."}]},
                        {"role": "user", "content": "Continue."},
                    ]
                )
                self.assertEqual(
                    converted["systemInstruction"]["parts"],
                    [{"text": "Global rule."}, {"text": "Answer briefly."}],
                )
                self.assertEqual(
                    [content["role"] for content in converted["contents"]],
                    ["user", "model", "user"],
                )
                self.assertNotIn("Answer briefly.", content_parts(converted, "text"))

    async def test_tool_result_text_parts_are_joined(self):
        converted = await self.convert(
            tool_history(
                result=[
                    {"type": "text", "text": "first "},
                    {"type": "text", "text": "second"},
                ]
            )
        )
        self.assertEqual(
            content_parts(converted, "functionResponse")[0]["response"],
            {"result": "first second"},
        )

    async def test_tool_names_match_declarations_calls_results_and_choice(self):
        converted = await self.convert(
            tool_history('{"key":"test"}', name="1lookup"),
            tools=[{"type": "function", "function": {"name": "1lookup"}}],
            tool_choice={"type": "function", "function": {"name": "1lookup"}},
        )
        names = [
            converted["tools"][0]["functionDeclarations"][0]["name"],
            content_parts(converted, "functionCall")[0]["name"],
            content_parts(converted, "functionResponse")[0]["name"],
            converted["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"][0],
        ]
        self.assertEqual(names, ["_1lookup"] * 4)

    async def test_incomplete_tool_history_keeps_upstream_tolerance(self):
        duplicate = tool_history()
        duplicate[1]["tool_calls"].append(copy.deepcopy(duplicate[1]["tool_calls"][0]))
        missing_result = tool_history()[:2] + [{"role": "user", "content": "Continue."}]
        orphan_result = [
            {"role": "user", "content": "Continue."},
            {"role": "tool", "name": "lookup", "tool_call_id": "unknown", "content": "OK"},
        ]
        cases = [
            (duplicate, 2, 1),
            (missing_result, 1, 0),
            (orphan_result, 0, 1),
            (tool_history()[:2], 1, 0),
        ]
        for messages, calls, results in cases:
            with self.subTest(messages=messages):
                converted = await self.convert(messages)
                self.assertEqual(len(content_parts(converted, "functionCall")), calls)
                self.assertEqual(len(content_parts(converted, "functionResponse")), results)

    async def test_assistant_refusal_is_preserved_as_text(self):
        for fields in (
            {"refusal": "I cannot help."},
            {"content": [{"type": "refusal", "refusal": "I cannot help."}]},
        ):
            with self.subTest(fields=fields):
                converted = await self.convert(
                    [
                        {"role": "user", "content": "First request."},
                        {"role": "assistant", **fields},
                        {"role": "user", "content": "Next request."},
                    ]
                )
                self.assertEqual(
                    converted["contents"][1],
                    {"role": "model", "parts": [{"text": "I cannot help."}]},
                )

    async def test_image_encoding_and_content_boundaries(self):
        png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8"
            "AAwMCAO+jB0QAAAAASUVORK5CYII="
        )
        converted = await self.convert(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image."},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png}},
                    ],
                }
            ]
        )
        self.assertEqual(
            content_parts(converted, "inlineData"), [{"mimeType": "image/png", "data": png}]
        )
        unsupported = [
            [{"type": "image_url", "image_url": {"url": "data:image/png,aGVsbG8="}}],
            [{"type": "image_url", "image_url": {"url": "data:image/png;base64,???"}}],
            [{"type": "input_audio", "input_audio": {"data": "aGVsbG8=", "format": "wav"}}],
            [{"type": "file", "file": {"filename": "example.txt", "file_data": "aGVsbG8="}}],
            [{"type": "unknown", "text": "unsupported"}],
            {"text": "not a content array"},
            "",
        ]
        for content in unsupported:
            with self.subTest(content=content), self.assertRaises(ValueError):
                await self.convert([{"role": "user", "content": content}])

    async def test_tool_choice_modes_and_invalid_choices(self):
        messages = [{"role": "user", "content": "Hello."}]
        tools = [{"type": "function", "function": {"name": "lookup"}}]
        for choice, mode in (("auto", "AUTO"), ("none", "NONE"), ("required", "ANY")):
            with self.subTest(choice=choice):
                converted = await self.convert(messages, tools=tools, tool_choice=choice)
                self.assertEqual(converted["toolConfig"]["functionCallingConfig"]["mode"], mode)
        for choice in ("auto", "none"):
            with self.subTest(choice=choice, tools=None):
                self.assertNotIn("toolConfig", await self.convert(messages, tool_choice=choice))
        invalid = [
            {"tool_choice": "required"},
            {"tools": tools, "tool_choice": "unknown"},
            {"tools": tools, "tool_choice": {"type": "function", "function": {"name": "missing"}}},
            {"tools": tools, "tool_choice": {"type": "function", "function": "lookup"}},
            {"tools": tools, "tool_choice": {"type": "function", "function": {"name": ["lookup"]}}},
            {
                "tools": [
                    {"type": "function", "function": {"name": "1lookup"}},
                    {"type": "function", "function": {"name": "_1lookup"}},
                ]
            },
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                await self.convert(messages, **options)

    async def test_anthropic_top_level_system_is_preserved(self):
        for system in ("Top-level rule.", [{"type": "text", "text": "Top-level rule."}]):
            with self.subTest(system=system):
                messages = [{"role": "user", "content": "Hello."}]
                converted = await merge_system_messages({"system": system, "messages": messages})
                self.assertEqual(
                    converted["systemInstruction"]["parts"], [{"text": "Top-level rule."}]
                )
                self.assertEqual(converted["messages"], messages)


class TestOpenAIRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Importing the router initializes the CLI version. Keep that lookup offline.
        release = Mock(json=lambda: {"tag_name": "v1.4.7"})
        with patch("httpx.get", return_value=release):
            from src.router.antigravity.openai import router
            from src.utils import authenticate_bearer

        cls.app = FastAPI()
        cls.app.include_router(router)
        cls.app.dependency_overrides[authenticate_bearer] = lambda: "test"

    def setUp(self):
        self.backend = AsyncMock(
            return_value=JSONResponse(
                content={
                    "response": {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": "OK"}]},
                                "finishReason": "STOP",
                            }
                        ]
                    },
                }
            )
        )

    def request(self, messages, **options):
        with (
            patch("src.api.antigravity.non_stream_request", new=self.backend),
            patch(
                "src.converter.antigravity_fix._get_model_rewrite_rules",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "src.converter.antigravity_fix._get_sensitive_word_patterns",
                new=AsyncMock(return_value=None),
            ),
            TestClient(self.app) as client,
        ):
            return client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.8-flash-high",
                    "messages": messages,
                    **options,
                },
            )

    def test_instructions_and_tool_arguments_reach_backend(self):
        messages = [
            {"role": "system", "content": "Global rule."},
            {"role": "user", "content": "Hello."},
            {"role": "assistant", "content": "Hi."},
            {"role": "developer", "content": "Answer briefly."},
            *tool_history('{"key":"test"}'),
        ]
        response = self.request(messages)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["role"], "assistant")
        self.backend.assert_awaited_once()
        payload = self.backend.await_args.kwargs["body"]["request"]
        self.assertEqual(
            payload["systemInstruction"]["parts"],
            [{"text": "Global rule."}, {"text": "Answer briefly."}],
        )
        self.assertTrue(
            all(content["role"] in ("user", "model") for content in payload["contents"])
        )
        self.assertEqual(content_parts(payload, "functionCall")[0]["args"], {"key": "test"})

    def test_remote_image_is_rejected_before_backend(self):
        response = self.request(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image."},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/image.png"},
                        },
                    ],
                }
            ]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {
                "detail": "Antigravity requires image_url content as a base64 image data URL",
            },
        )
        self.backend.assert_not_awaited()

    def test_request_model_rejects_unsupported_roles_and_fields(self):
        cases = [
            ([{"role": "unknown", "content": "Hello."}], {}),
            ([{"role": "function", "name": "lookup", "content": "OK"}], {}),
            (
                [
                    {
                        "role": "developer",
                        "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                    }
                ],
                {},
            ),
            ([{"role": "user", "tool_calls": tool_history()[1]["tool_calls"]}], {}),
            ([{"role": "assistant", "audio": {"id": "audio_example"}}], {}),
            ([{"role": "assistant", "function_call": {"name": "lookup", "arguments": "{}"}}], {}),
            (
                [{"role": "user", "content": "Hello."}],
                {
                    "tools": [{"type": "custom", "function": {"name": "lookup"}}],
                },
            ),
        ]
        for messages, options in cases:
            with self.subTest(messages=messages, options=options):
                self.assertEqual(self.request(messages, **options).status_code, 422)
                self.backend.assert_not_awaited()

    def test_outgoing_role_guard_stops_invalid_normalized_role(self):
        normalized = {
            "model": "gemini-3.8-flash",
            "contents": [
                {"role": "developer", "parts": [{"text": "Unexpected normalized role."}]},
            ],
        }
        with patch(
            "src.converter.antigravity_fix.normalize_antigravity_request",
            new=AsyncMock(return_value=normalized),
        ):
            response = self.request([{"role": "user", "content": "Hello."}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(), {"detail": "Unrepresentable content role for Antigravity"}
        )
        self.backend.assert_not_awaited()

    def test_malformed_tool_choice_is_rejected_before_backend(self):
        for function in ("lookup", {"name": ["lookup"]}):
            with self.subTest(function=function):
                response = self.request(
                    [{"role": "user", "content": "Hello."}],
                    tools=[{"type": "function", "function": {"name": "lookup"}}],
                    tool_choice={"type": "function", "function": function},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json(), {"detail": "Unsupported tool_choice for Antigravity"}
                )
                self.backend.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
