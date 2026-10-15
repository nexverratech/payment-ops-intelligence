"""Password hashing and policy."""

import pytest

from payops.security import passwords
from payops.security.passwords import PasswordPolicyError, ScryptParams, check_policy, hash_password, needs_rehash, verify_password


def test_production_uses_the_owasp_scrypt_minimum():
    assert passwords.PRODUCTION == ScryptParams(log2_n=17, r=8, p=1)


def test_right_and_wrong_password():
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h)
    assert not verify_password("correct horse batterY", h)


def test_same_password_gives_different_hashes():
    assert hash_password("correct horse battery") != hash_password("correct horse battery")   # random salt


def test_hash_does_not_contain_the_password():
    assert "battery" not in hash_password("correct horse battery")


@pytest.mark.parametrize("stored", ["", "plain-text", "scrypt$99$8$1$AAAA$AAAA", "bcrypt$2b$12$abc"])
def test_garbage_or_absurd_hashes_never_verify(stored):
    assert verify_password("anything", stored) is False


def test_weaker_old_hashes_are_flagged_for_upgrade(monkeypatch):
    old = hash_password("correct horse battery", ScryptParams(log2_n=10))
    monkeypatch.setattr(passwords, "CURRENT", ScryptParams(log2_n=11))
    assert needs_rehash(old) and not needs_rehash(hash_password("correct horse battery"))


@pytest.mark.parametrize("pw, why", [("short", "at least 12"), ("x" * 129, "at most 128"), ("alice-the-user", "username"),
                                     ("aaaaaaaaaaaa", "repetitive")])
def test_policy(pw, why):
    with pytest.raises(PasswordPolicyError, match=why):
        check_policy(pw, "alice-the-user")


def test_a_long_passphrase_is_fine():
    check_policy("purple elephants dance at noon", "alice")
