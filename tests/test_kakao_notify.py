"""Unit tests for Kakao OAuth refresh and safe self-notifications."""
import io
import json
import unittest
from urllib import error, parse
from scripts.kakao_notify import (
    KakaoNotificationError, TOKEN_URL, SEND_URL,
    refresh_access_token, send_to_me, validate_message, format_abraham_event,
)


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class KakaoNotificationTests(unittest.TestCase):
    def test_refresh_token_form_and_optional_secret(self):
        calls = []
        def fake(req, timeout):
            calls.append((req, timeout))
            return Reply(b'{"access_token":"fake-access","expires_in":43000}')
        access, rotated = refresh_access_token("app-key", "fake-refresh", "app-secret", opener=fake)
        self.assertEqual(access, "fake-access")
        self.assertFalse(rotated)
        req, timeout = calls[0]
        self.assertEqual(req.full_url, TOKEN_URL)
        self.assertEqual(timeout, 15)
        body = parse.parse_qs(req.data.decode())
        self.assertEqual(body["grant_type"], ["refresh_token"])
        self.assertEqual(body["client_id"], ["app-key"])
        self.assertEqual(body["client_secret"], ["app-secret"])

    def test_rotated_refresh_token_is_detected_without_output(self):
        def fake(req, timeout):
            return Reply(b'{"access_token":"access","refresh_token":"new-secret"}')
        self.assertEqual(refresh_access_token("key", "refresh", opener=fake), ("access", True))

    def test_message_template_and_bearer(self):
        calls = []
        def fake(req, timeout):
            calls.append(req)
            return Reply(b'{"result_code":0}')
        self.assertTrue(send_to_me("fake-access", "테스트 완료", "https://example.com/status", opener=fake))
        req = calls[0]
        self.assertEqual(req.full_url, SEND_URL)
        self.assertEqual(req.get_header("Authorization"), "Bearer fake-access")
        obj = json.loads(parse.parse_qs(req.data.decode())["template_object"][0])
        self.assertEqual(obj["object_type"], "text")
        self.assertEqual(obj["text"], "테스트 완료")
        self.assertEqual(obj["link"]["web_url"], "https://example.com/status")

    def test_deny_oversized_or_insecure_message(self):
        for message, link in [("", "https://example.com"), ("가" * 201, "https://example.com"), ("ok", "http://example.com")]:
            with self.assertRaises(KakaoNotificationError):
                validate_message(message, link)

    def test_does_not_expose_api_error_body(self):
        def fake(req, timeout):
            raise error.HTTPError(req.full_url, 401, "No", {}, io.BytesIO(b"secret-response-token"))
        with self.assertRaisesRegex(KakaoNotificationError, "HTTP 401") as err:
            refresh_access_token("app", "refresh-secret", opener=fake)
        self.assertNotIn("secret-response", str(err.exception))
        self.assertNotIn("refresh-secret", str(err.exception))

    def test_event_message(self):
        self.assertIn("실패", format_abraham_event("failure", "42"))
        self.assertIn("42", format_abraham_event("failure", "42"))


if __name__ == "__main__":
    unittest.main()
