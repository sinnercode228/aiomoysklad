"""Credentials, and the kind of integration they belong to.

``_general.md § Аутентификация``: Basic auth with ``login:password`` in RFC 2045 Base64, or
``Authorization: Bearer <Access-Token>``. ``_restrictions.md § Лимиты и ограничения в JSON
API`` names three ways in: login and password, a user token, a solution token. The rate limit
weight depends on which one is used, and a Bearer token does not tell, so the caller declares
it with :class:`AuthKind`.
"""

from __future__ import annotations

import base64
import enum

__all__ = ["Auth", "AuthKind", "BasicAuth", "BearerToken"]


class AuthKind(enum.Enum):
    """Who the credentials belong to; this sets the weight of each request.

    Attributes:
        USER: Login and password, or a user's token; the default of
            :class:`BearerToken`. It is not the most expensive kind on every date: until
            2026-12-01 a draft solution costs more, so declare the kind that matches the
            token.
        SOLUTION: The token of a public or private solution.
        DRAFT_SOLUTION: The token of a solution in status ``Черновик`` or ``На модерации``.
    """

    USER = "user"
    SOLUTION = "solution"
    DRAFT_SOLUTION = "draft_solution"


def _header_safe(value: str) -> bool:
    return bool(value) and all(c.isascii() and c.isprintable() and not c.isspace() for c in value)


class BasicAuth:
    """A login and password, sent with Basic auth. Always :attr:`AuthKind.USER`."""

    __slots__ = ("_password", "login")

    def __init__(self, login: str, password: str) -> None:
        if not login or not password:
            msg = "login and password must not be empty"
            raise ValueError(msg)
        if ":" in login:
            msg = "a login with a colon cannot be sent with Basic auth"
            raise ValueError(msg)
        self.login = login
        self._password = password

    @property
    def kind(self) -> AuthKind:
        """Always :attr:`AuthKind.USER`."""
        return AuthKind.USER

    @property
    def authorization(self) -> str:
        """The ``Authorization`` header value."""
        raw = f"{self.login}:{self._password}".encode()
        return f"Basic {base64.b64encode(raw).decode('ascii')}"

    def __repr__(self) -> str:
        return f"BasicAuth(login={self.login!r})"


class BearerToken:
    """An access token, sent as ``Authorization: Bearer <token>``.

    Args:
        token: The token of a user or a solution.
        kind: Whose token it is; it sets the weight of each request. Defaults to
            :attr:`AuthKind.USER`.
    """

    __slots__ = ("_token", "kind")

    def __init__(self, token: str, *, kind: AuthKind = AuthKind.USER) -> None:
        if not _header_safe(token):
            msg = "the token must be printable ASCII without spaces"
            raise ValueError(msg)
        self._token = token
        self.kind = kind

    @property
    def authorization(self) -> str:
        """The ``Authorization`` header value."""
        return f"Bearer {self._token}"

    def __repr__(self) -> str:
        return f"BearerToken(kind=AuthKind.{self.kind.name})"


Auth = BasicAuth | BearerToken
