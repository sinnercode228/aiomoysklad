from __future__ import annotations

import base64

import pytest

from aiomoysklad import AuthKind, BasicAuth, BearerToken


def test_basic_auth_sends_base64_of_login_and_password() -> None:
    auth = BasicAuth("admin@example", "пароль")
    expected = base64.b64encode("admin@example:пароль".encode()).decode("ascii")
    assert auth.authorization == f"Basic {expected}"


def test_basic_auth_is_always_a_user() -> None:
    assert BasicAuth("admin@example", "secret").kind is AuthKind.USER


def test_a_colon_in_the_login_is_refused() -> None:
    # Basic auth splits login and password on the first colon.
    with pytest.raises(ValueError, match="colon"):
        BasicAuth("admin:x@example", "secret")


@pytest.mark.parametrize(("login", "password"), [("", "secret"), ("admin@example", "")])
def test_empty_credentials_are_refused(login: str, password: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        BasicAuth(login, password)


def test_a_bearer_token_defaults_to_the_most_limited_kind() -> None:
    token = BearerToken("0cbfc512618efa7d5fa306250bca064c1169b37c")
    assert token.kind is AuthKind.USER
    assert token.authorization == "Bearer 0cbfc512618efa7d5fa306250bca064c1169b37c"


@pytest.mark.parametrize("kind", list(AuthKind))
def test_a_bearer_token_takes_any_kind(kind: AuthKind) -> None:
    assert BearerToken("t", kind=kind).kind is kind


@pytest.mark.parametrize("token", ["", "   ", "a b", "a\nb"])
def test_a_malformed_token_is_refused(token: str) -> None:
    with pytest.raises(ValueError, match="token"):
        BearerToken(token)


def test_secrets_stay_out_of_the_repr() -> None:
    assert "secret" not in repr(BasicAuth("admin@example", "secret"))
    assert "admin@example" in repr(BasicAuth("admin@example", "secret"))
    assert "0cbfc512" not in repr(BearerToken("0cbfc512618efa7d", kind=AuthKind.SOLUTION))
    assert "SOLUTION" in repr(BearerToken("0cbfc512618efa7d", kind=AuthKind.SOLUTION))
