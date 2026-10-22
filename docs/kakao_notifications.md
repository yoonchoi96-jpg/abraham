# KakaoTalk self-notifications (opt-in)

This is an **outbound-only** notification channel from Abraham to the OAuth account holder's **나와의 채팅**. It cannot read arbitrary KakaoTalk conversations, receive chat messages, or post to arbitrary group chats. No additional server or Python package is required.

Official API references:
- https://developers.kakao.com/docs/ko/kakaotalk-message/rest-api
- https://developers.kakao.com/docs/ko/kakaologin/rest-api
- https://developers.kakao.com/docs/ko/message-template/default

## Owner setup (required before any real notification)

1. Create an app at https://developers.kakao.com/ (or reuse your own approved app).
2. Enable Kakao Login and register your chosen OAuth redirect URI. Configure the **카카오톡 메시지 전송** consent item (scope **talk_message**).
3. Register the actual HTTPS link domain under **제품 링크 관리 → 웹 도메인**. The message button URL must use this exact registered domain.
4. Complete Kakao Login **authorization-code** flow as the **recipient's own account**, consenting to message sending. Exchange authorization code using the token endpoint. Preserve the resulting refresh token securely. Do not paste tokens into chat, Git commits, CI log output, or issue comments.
5. In this repository's **Settings → Secrets and variables → Actions**:
   - **Secrets**: KAKAO_REST_API_KEY, KAKAO_REFRESH_TOKEN, and KAKAO_CLIENT_SECRET (required if the Kakao app has client-secret verification ON).
   - **Variables**: KAKAO_LINK_URL (registered HTTPS URL), KAKAO_NOTIFY_ENABLED=true.
6. Manually dispatch the Abraham Core workflow to verify. Only after consent and those settings are configured can an outbound message be sent.

The Kakao REST API defaults to client-secret verification ON for newly created app keys. **Refresh tokens can rotate** near expiration. GitHub Actions cannot silently update repository Secrets with the current permissions. When rotation is reported, re-authorize and replace the secret using a trusted private process. Do not copy token values into logs or artifacts. Plan periodic reauthorization to avoid stale credentials.

## Test, interface, and safety

Dry-run (no secrets or API calls):

    python3 scripts/kakao_notify.py --event success

Real send (requires environment credentials, OAuth consent, and registered link domain):

    python3 scripts/kakao_notify.py --event failure --send

The reusable functions are \`refresh_access_token()\` and \`send_to_me()\` in \`scripts/kakao_notify.py\`.

The GitHub Actions integration is disabled unless **KAKAO_NOTIFY_ENABLED=true** and credentials and the link variable are present. When enabled, it sends a short workflow status after the run. The Kakao notification step uses \`continue-on-error\` so a messaging outage does not change the state of the data pipeline.

Kakao text templates permit **at most 200 characters** and require a link whose domain is registered in Product Link settings. For detailed findings, store the report elsewhere and link to it.

**Not supported by this integration:** sending to friends or groups, reading messages, unlimited proactive promotional messaging, or live trading signals. Those require a different design and possibly additional Kakao permissions. Authentication is user consent, not an Admin key shortcut.
