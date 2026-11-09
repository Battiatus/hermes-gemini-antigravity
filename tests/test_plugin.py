"""Loads the plugin the way Hermes does (spec_from_file_location on the folder) against minimal stand-ins of the
Hermes modules it imports, and checks registration and the request it sends to Google."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
registered = []


def stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class ProviderProfile:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class GeminiAPIError(Exception):
    def __init__(self, message, *, code="", status_code=None, retry_after=None, **_):
        super().__init__(message)
        self.code, self.status_code, self.retry_after = code, status_code, retry_after


class GeminiNativeClient:
    def __init__(self, *, api_key, base_url=None, **_):
        self.api_key, self.base_url = api_key, base_url
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create_chat_completion))


def build_gemini_request(*, messages, model, tools_as_json_schema, max_tokens=None, thinking_config=None, **_):
    build_gemini_request.last = {"model": model, "json_schema": tools_as_json_schema}
    config = {"maxOutputTokens": max_tokens or 65535}
    if thinking_config:
        config["thinkingConfig"] = thinking_config
    return {"contents": [{"role": "user", "parts": [{"text": messages[-1]["content"]}]}],
            "systemInstruction": {"role": "system", "parts": [{"text": "be brief"}]}, "generationConfig": config}


stub("providers", register_provider=registered.append)
stub("providers.base", ProviderProfile=ProviderProfile)
stub("agent")
stub("agent.gemini_native_adapter", GeminiAPIError=GeminiAPIError, GeminiNativeClient=GeminiNativeClient,
     build_gemini_request=build_gemini_request, translate_gemini_response=lambda resp, model: ("reply", resp, model),
     translate_stream_event=lambda event, model, indices: [("chunk", event)])

spec = importlib.util.spec_from_file_location("hermes_plugin_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


class Plugin(unittest.TestCase):
    def setUp(self):
        import hermes_plugin_under_test.client as client
        self.client_module = client
        self.sent = []
        client._catalog.update(at=1e18, models={"claude-sonnet-4-6": {"maxOutputTokens": 64000}, "gemini-3.8-flash-low": {}})

        def generate(access, model, request, *, stream, request_id, timeout, on_connection):
            self.sent.append({"access": access, "model": model, "request": request, "stream": stream})
            return iter([{"candidates": []}]) if stream else {"candidates": []}

        self._generate = client.ag.generate
        client.ag.generate = generate
        client.ag._projects.clear()

    def tearDown(self):
        self.client_module.ag.generate = self._generate

    def test_registers_one_oauth_profile(self):
        self.assertEqual(len(registered), 1)
        profile = registered[0]
        self.assertEqual(profile.name, "gemini-antigravity")
        self.assertEqual(profile.auth_type, "oauth_external")
        self.assertTrue(callable(profile.auth_handler) and callable(profile.refresh_credential))
        self.assertEqual(profile.supported_reasoning_efforts("gemini-3.8-flash-high"), ())
        self.assertEqual(profile.build_extra_body(session_id="s1"), {"antigravity_session": "s1"})

    def test_gemini_request_gets_thinking_session_and_plain_system(self):
        client = registered[0].create_client(api_key="tok", base_url=None, unknown="ignored")
        reply = client.chat.completions.create(model="gemini-3.8-flash-low", messages=[{"role": "user", "content": "hi"}],
                                               extra_body={"antigravity_session": "conv"})
        sent = self.sent[-1]
        self.assertEqual(reply[0], "reply")
        self.assertEqual(sent["access"], "tok")
        self.assertEqual(sent["request"]["generationConfig"]["thinkingConfig"], {"includeThoughts": True, "thinkingBudget": 1000})
        self.assertEqual(sent["request"]["systemInstruction"], {"parts": [{"text": "be brief"}]})
        self.assertTrue(sent["request"]["sessionId"])
        self.assertEqual(build_gemini_request.last, {"model": "gemini-3.8-flash-low", "json_schema": False})

    def test_claude_uses_json_schema_tool_ids_and_catalog_output_cap(self):
        client = registered[0].create_client(api_key="tok")
        chunks = list(client.chat.completions.create(model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}], stream=True))
        self.assertEqual(chunks, [("chunk", {"candidates": []})])
        self.assertEqual(self.sent[-1]["request"]["generationConfig"]["maxOutputTokens"], 64000)
        self.assertEqual(build_gemini_request.last, {"model": "gemini-3", "json_schema": True})

    def test_quota_error_becomes_gemini_api_error(self):
        client_module = self.client_module

        def refuse(*args, **kwargs):
            raise client_module.ag.failure(429, '{"error":{"message":"spent","details":[{"retryDelay":"60s"}]}}')

        client_module.ag.generate = refuse
        client = registered[0].create_client(api_key="tok")
        with self.assertRaises(GeminiAPIError) as caught:
            client.chat.completions.create(model="gemini-3.8-flash-low", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual((caught.exception.status_code, caught.exception.code, caught.exception.retry_after), (429, "antigravity_quota", 60))


if __name__ == "__main__":
    unittest.main()
