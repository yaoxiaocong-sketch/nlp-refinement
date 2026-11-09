from __future__ import annotations

import unittest

from nlp_refinement.sensitive_data import RedactionRequiredError, redact_for_model


class SensitiveDataTests(unittest.TestCase):
    def test_high_confidence_tokens_are_replaced_without_retaining_values(self) -> None:
        api_key = "sk-proj-abcdefghijklmnopqrstuvwxyz"
        github_token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        private_key = "-----BEGIN PRIVATE KEY-----\nprivate-body-must-not-leave-memory\n-----END PRIVATE KEY-----"
        value = f"first: {api_key}\nsecond: {api_key}\ngithub: {github_token}\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz123456\nkey: {private_key}\n"

        result = redact_for_model(value)

        self.assertNotIn(api_key, result.text)
        self.assertNotIn(github_token, result.text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", result.text)
        self.assertNotIn("private-body-must-not-leave-memory", result.text)
        self.assertEqual(result.replacement_counts, {"api-key": 2, "bearer-token": 1, "github-token": 1, "private-key": 1})
        self.assertEqual(result.text.count("<REDACTED:api-key:1>"), 2)

    def test_ambiguous_secret_assignment_requires_a_user_redacted_copy(self) -> None:
        with self.assertRaisesRegex(RedactionRequiredError, "ambiguous-secret-assignment"):
            redact_for_model("password: hunter2\n")

    def test_incomplete_private_key_requires_a_user_redacted_copy(self) -> None:
        with self.assertRaisesRegex(RedactionRequiredError, "incomplete-private-key"):
            redact_for_model("-----BEGIN PRIVATE KEY-----\nnot-a-complete-key\n")
