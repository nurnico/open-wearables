"""make_authenticated_request treats 204 No Content and empty bodies as "no data"."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.providers.api_client import make_authenticated_request


def _request_with_response(status_code: int, content: bytes) -> object:
    connection_repo = MagicMock()
    connection = MagicMock()
    connection.access_token = "tok"
    connection.token_expires_at = None
    connection_repo.get_by_user_and_provider.return_value = connection

    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.raise_for_status.return_value = None
    response.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")

    with patch("httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.request.return_value = response
        client_cls.return_value = client

        return make_authenticated_request(
            db=MagicMock(),
            user_id=uuid4(),
            connection_repo=connection_repo,
            oauth=MagicMock(),
            api_base_url="https://www.polaraccesslink.com",
            provider_name="polar",
            endpoint="/v3/users/nightly-recharge",
        )


@pytest.mark.parametrize(("status_code", "content"), [(204, b""), (200, b"")])
def test_empty_response_returns_none(status_code: int, content: bytes) -> None:
    assert _request_with_response(status_code, content) is None
