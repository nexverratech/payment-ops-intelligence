"""Password hashing with scrypt from Python's standard library.

Why scrypt: OWASP's Password Storage Cheat Sheet lists Argon2id first and scrypt
as the alternative, with minimum parameters N=2^17, r=8, p=1 (128 MiB of memory
per hash). scrypt ships with Python (hashlib, via OpenSSL), so this needs no
extra package. Argon2id would need `argon2-cffi`; see docs/security.md.

Stored format (one string, so parameters can be raised later without breaking
old hashes):   scrypt$<log2 N>$<r>$<p>$<salt, base64>$<hash, base64>

A verify call re-derives the hash with the stored parameters and compares in
constant time. `needs_rehash` reports when a stored hash used weaker parameters
than today's, so it can be upgraded at the next successful login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ScryptParams:
    log2_n: int = 17     # N = 131072
    r: int = 8
    p: int = 1

    @property
    def maxmem(self) -> int:
        return 2 * 128 * self.r * (2 ** self.log2_n) * self.p   # what scrypt needs, with headroom


PRODUCTION = ScryptParams()
CURRENT = PRODUCTION            # tests switch this to cheaper parameters to stay fast
SALT_BYTES = 16
KEY_BYTES = 32
_FORMAT = re.compile(r"^scrypt\$(\d{1,2})\$(\d{1,2})\$(\d{1,2})\$([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+)$")


class PasswordPolicyError(ValueError):
    """The password does not meet the policy; the message says why (never echoes the password)."""


def check_policy(password: str, username: str, min_length: int = 12) -> None:
    """Length-based policy, as NIST SP 800-63B recommends (no forced symbol rules)."""
    if len(password) < min_length:
        raise PasswordPolicyError(f"password must be at least {min_length} characters")
    if len(password) > 128:
        raise PasswordPolicyError("password must be at most 128 characters")
    if password.strip().lower() == username.lower():
        raise PasswordPolicyError("password must not be the username")
    if len(set(password)) < 4:
        raise PasswordPolicyError("password is too repetitive")


def hash_password(password: str, params: ScryptParams | None = None) -> str:
    params = params or CURRENT
    salt = os.urandom(SALT_BYTES)
    key = _derive(password, salt, params)
    return "$".join(["scrypt", str(params.log2_n), str(params.r), str(params.p),
                     base64.b64encode(salt).decode(), base64.b64encode(key).decode()])


def verify_password(password: str, stored: str) -> bool:
    m = _FORMAT.match(stored or "")
    if not m:
        return False
    params = ScryptParams(int(m[1]), int(m[2]), int(m[3]))
    if not (10 <= params.log2_n <= 18 and 1 <= params.r <= 8 and 1 <= params.p <= 4):
        return False              # refuse absurd parameters (a tampered row could otherwise exhaust memory)
    expected = base64.b64decode(m[5])
    return hmac.compare_digest(_derive(password, base64.b64decode(m[4]), params), expected)


def needs_rehash(stored: str) -> bool:
    m = _FORMAT.match(stored or "")
    return not m or (int(m[1]), int(m[2]), int(m[3])) < (CURRENT.log2_n, CURRENT.r, CURRENT.p)


# A real hash of a random password: used to spend the same time when the username does
# not exist, so response time does not reveal which usernames are real.
_DUMMY: dict[ScryptParams, str] = {}


def burn_time(password: str) -> None:
    if CURRENT not in _DUMMY:
        _DUMMY[CURRENT] = hash_password(os.urandom(16).hex())
    verify_password(password, _DUMMY[CURRENT])


def _derive(password: str, salt: bytes, params: ScryptParams) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** params.log2_n, r=params.r, p=params.p,
                          maxmem=params.maxmem, dklen=KEY_BYTES)
