"""
magic.py — signed, time-limited login tokens for one-click email deep links.

The Flask app + the offline email script (which runs on GitHub Actions, not
inside Flask) share a single signing helper here so both ends agree on the
token format. The signing key is read from SECRET_KEY — make sure the
Railway env var and the GitHub Actions secret hold the same value, or
tokens minted in one place will fail to verify in the other.

Tokens carry only the user_id. The destination (the pool deep-link) rides
along as a separate `?next=...` query param so the same token works for
multiple deep-link targets.
"""
import os
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired


MAGIC_SALT = "magic-login-v1"
DEFAULT_MAX_AGE_SECONDS = 14 * 24 * 60 * 60  # 14 days


def _serializer():
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        raise RuntimeError(
            "SECRET_KEY env var is required to sign/verify magic tokens"
        )
    return URLSafeTimedSerializer(secret, salt=MAGIC_SALT)


def make_magic_token(user_id):
    return _serializer().dumps(user_id)


def verify_magic_token(token, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Returns (user_id, None) on success or (None, reason) on failure."""
    try:
        return _serializer().loads(token, max_age=max_age_seconds), None
    except SignatureExpired:
        return None, "expired"
    except BadSignature:
        return None, "invalid"
