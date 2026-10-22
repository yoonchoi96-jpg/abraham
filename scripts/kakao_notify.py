"""Opt-in KakaoTalk 'send to me' notifications. Standard library only.

Never print OAuth tokens, response bodies, or Authorization headers.
Actual sends require --send and the Kakao account owner's OAuth consent.
"""
from __future__ import annotations

import argparse
import json
import os
from urllib import error, parse, request


TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"


class KakaoNotificationError(RuntimeError):
    """Safe-to-log error that omits API response bodies and secrets."""


def _post_form(url, fields, *, access_token=None, opener=None):
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
    if access_token:
        headers["Authorization"] = "Bearer " + access_token

    payload = parse.urlencode(fields).encode("utf-8")
    req = request.Request(url, data=payload, headers=headers, method="POST")
    call = opener or request.urlopen
    try:
        with call(req, timeout=15) as response:
            data = response.read()
    except error.HTTPError as exc:
        raise KakaoNotificationError("Kakao API returned HTTP " + str(exc.code)) from None
    except error.URLError:
        raise KakaoNotificationError("Unable to reach Kakao API") from None

    try:
        result = json.loads(data.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("not an object")
        return result
    except (UnicodeError, ValueError):
        raise KakaoNotificationError("Kakao API returned an invalid response") from None


def refresh_access_token(rest_key, refresh_token, client_secret="", *, opener=None):
    """Return (access token, rotated-refresh-token-present); never log token values."""
    if not rest_key or not refresh_token:
        raise KakaoNotificationError("Kakao REST API key and refresh token are required")
    fields = {
        "grant_type": "refresh_token",
        "client_id": rest_key,
        "refresh_token": refresh_token,
    }
    if client_secret:
        fields["client_secret"] = client_secret
    response = _post_form(TOKEN_URL, fields, opener=opener)
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise KakaoNotificationError("Kakao did not return an access token")
    return access_token, bool(response.get("refresh_token"))


def validate_message(message, link_url):
    if not isinstance(message, str) or not 1 <= len(message) <= 200:
        raise KakaoNotificationError("Kakao text template must contain 1-200 characters")
    try:
        url = parse.urlsplit(link_url)
    except (TypeError, ValueError):
        raise KakaoNotificationError("Kakao message link must be an HTTPS URL") from None
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise KakaoNotificationError("Kakao message link must be an HTTPS URL")
    # Kakao also requires the URL's domain to be registered in Product Link settings.
    return link_url


def send_to_me(access_token, message, link_url, *, opener=None):
    """Send a text-template notification to the authenticated user's My Chat."""
    validate_message(message, link_url)
    if not access_token:
        raise KakaoNotificationError("Kakao access token is required")
    template = {
        "object_type": "text",
        "text": message,
        "link": {"web_url": link_url, "mobile_web_url": link_url},
        "button_title": "자세히 보기",
    }
    response = _post_form(
        SEND_URL,
        {"template_object": json.dumps(template, ensure_ascii=False)},
        access_token=access_token,
        opener=opener,
    )
    if response.get("result_code") != 0:
        raise KakaoNotificationError("Kakao message was not accepted")
    return True


def format_abraham_event(event, run_number=""):
    descriptions = {
        "success": "작업 정상 완료",
        "failure": "작업 실패 - 로그 확인 필요",
        "cancelled": "작업 취소",
        "skipped": "핵심 작업 미실행 - 이전 단계 확인 필요",
    }
    description = descriptions.get(event, "작업 상태 확인 필요")
    suffix = ("\n실행 번호: " + str(run_number)) if run_number else ""
    return "[Abraham] " + description + suffix


def main(argv=None):
    parser = argparse.ArgumentParser(description="Opt-in KakaoTalk notification (dry-run by default)")
    parser.add_argument("--event", choices=("success", "failure", "cancelled", "skipped", "unknown"), required=True)
    parser.add_argument("--send", action="store_true", help="Actually send. Default is dry-run.")
    args = parser.parse_args(argv)

    message = format_abraham_event(args.event, os.getenv("GITHUB_RUN_NUMBER", ""))
    if not args.send:
        print("Kakao notification dry-run: " + message.replace("\n", " / "))
        return 0

    rest_key = os.getenv("KAKAO_REST_API_KEY", "")
    refresh_token = os.getenv("KAKAO_REFRESH_TOKEN", "")
    client_secret = os.getenv("KAKAO_CLIENT_SECRET", "")
    link_url = os.getenv("KAKAO_LINK_URL", "")
    validate_message(message, link_url)
    access_token, rotated = refresh_access_token(rest_key, refresh_token, client_secret)
    send_to_me(access_token, message, link_url)
    print("Kakao self-notification accepted")
    if rotated:
        print("WARNING: Kakao rotated the refresh token. Re-authorize/update the stored secret before expiry; token value is never logged.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KakaoNotificationError as exc:
        print("Kakao notification failed: " + str(exc))
        raise SystemExit(1) from None
