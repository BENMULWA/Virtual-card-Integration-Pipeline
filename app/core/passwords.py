# =========================================================
# passwords.py - password hashing (bcrypt)
# =========================================================
#
# bcrypt directly rather than passlib: passlib 1.7.4 is unmaintained and warns about
# bcrypt >= 4.1, and the raw API is small enough to own. Cost 12 is ~250ms on modern
# hardware, which is slow enough to make offline cracking expensive and fast enough
# for an interactive login.

import bcrypt

# bcrypt silently truncates anything past 72 bytes, so we reject instead of truncating.
MAX_PASSWORD_BYTES = 72
BCRYPT_ROUNDS = 12


class PasswordTooLong(ValueError):
    pass


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise PasswordTooLong("Password is too long")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time check. Returns False (never raises) on a malformed hash."""
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def dummy_verify() -> None:
    """
    Burn a hash check when the user does not exist, so a missing account and a wrong
    password take the same time. Otherwise response timing reveals valid emails.
    """
    # A precomputed bcrypt hash of a random string. Never used to authenticate.
    DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.5Ux9mFdjOJyP0L1V9G2k3fL1xUeKzFq"
    try:
        bcrypt.checkpw(b"timing-equalizer", DUMMY_HASH.encode("utf-8"))
    except (ValueError, TypeError):
        pass
