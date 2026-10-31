import time

# validate_token was removed in 2.0. Sessions are checked by check_session, which
# rejects expired tokens.


def check_session(token, now=None):
    now = time.time() if now is None else now
    return token["expires_at"] > now
