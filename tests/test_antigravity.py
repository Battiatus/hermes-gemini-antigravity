import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import antigravity as ag  # noqa: E402


class Protocol(unittest.TestCase):
    def test_thinking_levels(self):
        self.assertEqual(ag.thinking_for("gemini-3.8-flash-high"), {"includeThoughts": True})
        self.assertEqual(ag.thinking_for("gemini-3.8-flash-medium"), {"includeThoughts": True, "thinkingBudget": 4000})
        self.assertEqual(ag.thinking_for("gemini-3.8-flash-low"), {"includeThoughts": True, "thinkingBudget": 1000})
        self.assertEqual(ag.thinking_for("gemini-4-argon-high"), {"includeThoughts": True, "thinkingLevel": "HIGH"})
        self.assertEqual(ag.thinking_for("claude-sonnet-5-5-low"), {"includeThoughts": True, "thinkingBudget": 0, "thinkingLevel": "LOW"})
        self.assertIsNone(ag.thinking_for("claude-sonnet-4-6"))

    def test_offered_models_keep_google_order_and_drop_unavailable(self):
        catalog = {"models": {"a": {}, "b": {"disabled": True}, "c": {}},
                   "agentModelSorts": [{"groups": [{"modelIds": ["c", "b", "a", "a", "missing"]}]}]}
        self.assertEqual(ag.offered_models(catalog), ["c", "a"])
        self.assertEqual(ag.offered_models({"models": {"x": {"displayName": "X"}, "y": {"isInternal": True, "displayName": "Y"}}}), ["x"])

    def test_quota_pools_put_five_hours_first(self):
        pools = ag.quota_pools({"groups": [{"displayName": "Gemini", "buckets": [
            {"window": "weekly", "remainingFraction": 0.5}, {"window": "5h", "remainingFraction": 2}, {"window": "x", "disabled": True}]}]})
        self.assertEqual(pools, [{"name": "Gemini", "windows": [
            {"window": "5h", "remainingFraction": 1.0, "resetTime": None}, {"window": "weekly", "remainingFraction": 0.5, "resetTime": None}]}])

    def test_failure_quota_and_capacity(self):
        quota = ag.failure(429, json.dumps({"error": {"message": "exhausted", "details": [{"retryDelay": "600s"}]}}))
        self.assertIn("quota reached", str(quota))
        self.assertFalse(quota.retryable)
        self.assertEqual(quota.retry_after, 600)
        self.assertTrue(ag.failure(429, '{"error":{"message":"No capacity available"}}').retryable)
        self.assertTrue(ag.failure(503, "busy").retryable)
        self.assertFalse(ag.failure(400, "bad").retryable)

    def test_code_from_pasted_address(self):
        self.assertEqual(ag.code_from_redirect("http://localhost:51121/oauth-callback?state=s1&code=abc", "s1"), "abc")
        with self.assertRaises(ag.AntigravityError):
            ag.code_from_redirect("http://localhost:51121/oauth-callback?state=other&code=abc", "s1")
        with self.assertRaises(ag.AntigravityError):
            ag.code_from_redirect("http://localhost:51121/oauth-callback?state=s1&error=access_denied", "s1")

    def test_authorize_url_uses_pkce_and_registered_return_address(self):
        url = ag.authorize_url("verifier", "state")
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A51121%2Foauth-callback", url)
        self.assertIn("access_type=offline", url)

    def test_hosts_redirect_is_bypassed_only_for_antigravity_hosts(self):
        ag._resolved.clear()
        ask = lambda host: "142.250.0.1"
        self.assertEqual(ag.public_address("daily-cloudcode-pa.googleapis.com", resolve=lambda h: ["127.0.0.1"], ask_dns=ask), "142.250.0.1")
        self.assertIsNone(ag.public_address("cloudcode-pa.googleapis.com", resolve=lambda h: ["142.250.0.2"], ask_dns=ask))
        self.assertIsNone(ag.public_address("example.com", resolve=lambda h: ["127.0.0.1"], ask_dns=ask))

    def test_browser_return_reaches_the_callback_server(self):
        import http.client, threading, time
        def browser():
            time.sleep(0.5)
            conn = http.client.HTTPConnection("127.0.0.1", ag.CALLBACK_PORT, timeout=5)
            conn.request("GET", "/oauth-callback?state=wrong&code=x")
            self.page = conn.getresponse().read().decode()
        threading.Thread(target=browser, daemon=True).start()
        with self.assertRaises(ag.AntigravityError) as caught:
            ag.login(open_browser=False, timeout=10, prompt=lambda _: time.sleep(30) or "")
        self.assertIn("state mismatch", str(caught.exception))
        self.assertIn("state mismatch", self.page)

    def test_sessions_keep_one_id_per_conversation(self):
        s = ag.Sessions()
        first, r1 = s.begin("conv")
        again, r2 = s.begin("conv")
        other, _ = s.begin("other")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertTrue(r1.endswith("/1") and r2.endswith("/2"))
        self.assertEqual(ag._fnv1a64("hermes:abc"), "8430453108141726085")  # same as the DSH plugin's hash


if __name__ == "__main__":
    unittest.main()
