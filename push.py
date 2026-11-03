"""Firebase Cloud Messaging (HTTP v1): wake a vendor's app for an incoming call.

Set FIREBASE_SERVICE_ACCOUNT to the service-account JSON file from the Firebase console
(Project settings -> Service accounts -> Generate new private key). Never commit that file.
Without it, calls still ring over the app's live connection while the app is running.

Signs the OAuth token itself (RS256 with `cryptography`), so no Google SDK is needed.
"""

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from loguru import logger

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_token: dict[str, Any] = {}


def configured() -> bool:
    path = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    return bool(path) and Path(path).is_file()


def _account() -> dict[str, Any]:
    return json.loads(Path(os.environ["FIREBASE_SERVICE_ACCOUNT"]).read_text())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _signed_jwt(account: dict[str, Any]) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": account["private_key_id"]}
    claims = {"iss": account["client_email"], "scope": SCOPE, "aud": account["token_uri"], "iat": now, "exp": now + 3600}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    key = serialization.load_pem_private_key(account["private_key"].encode(), password=None)
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


async def _access_token(client: httpx.AsyncClient, account: dict[str, Any]) -> str:
    if _token.get("expires", 0) > time.time() + 60:
        return _token["value"]
    response = await client.post(
        account["token_uri"],
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": _signed_jwt(account)},
    )
    response.raise_for_status()
    body = response.json()
    _token.update(value=body["access_token"], expires=time.time() + body.get("expires_in", 3600))
    return _token["value"]


async def send(device_token: str, data: dict[str, str]) -> int:
    """Send a high-priority data message (the app shows the incoming-call screen). Returns 1 if sent."""
    if not configured():
        return 0
    account = _account()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            access = await _access_token(client, account)
            response = await client.post(
                f"https://fcm.googleapis.com/v1/projects/{account['project_id']}/messages:send",
                headers={"Authorization": f"Bearer {access}"},
                json={"message": {"token": device_token, "data": data, "android": {"priority": "high", "ttl": "40s"}}},
            )
            response.raise_for_status()
            return 1
    except httpx.HTTPError as e:
        logger.warning(f"Push failed: {e}")
        return 0
