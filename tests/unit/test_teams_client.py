# CMN-C2-234 - Unit tests: TeamsClient service (Microsoft Graph API v1.0 shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.teams_client import TeamsApiError, TeamsClient

_TEAM_ID = "a1b2c3d4-e5f6-4711-8abc-9def01234567"
_CHANNEL_ID = "19:abcdef1234567890@thread.tacv2"
_CHAT_ID = "19:aa11bb22cc33@unq.gbl.spaces"
_PAYLOAD = {"body": {"contentType": "text", "content": "hello from the ops channel"}}


def test_post_channel_message_with_injected_transport():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 201, {"id": "1738437", "webUrl": f"https://teams.microsoft.com/l/message/{_CHANNEL_ID}/1738437"}

    client = TeamsClient("https://graph.example.test/v1.0/", post=post)
    resp = client.post_channel_message(_TEAM_ID, _CHANNEL_ID, _PAYLOAD, "tok123")
    assert resp["id"] == "1738437"
    # Trailing slash on base_url is stripped - no double slash in the path.
    assert captured["url"] == f"https://graph.example.test/v1.0/teams/{_TEAM_ID}/channels/{_CHANNEL_ID}/messages"
    # Microsoft Graph auth: the per-call token travels as a Bearer header.
    assert captured["headers"]["Authorization"] == "Bearer " + "tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"] == _PAYLOAD


def test_post_chat_message_with_injected_transport():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 201, {"id": "42", "webUrl": f"https://teams.microsoft.com/l/message/{_CHAT_ID}/42"}

    client = TeamsClient("https://graph.example.test/v1.0", post=post)
    resp = client.post_chat_message(_CHAT_ID, _PAYLOAD, "tok")
    assert resp["id"] == "42"
    assert captured["url"] == f"https://graph.example.test/v1.0/chats/{_CHAT_ID}/messages"
    assert captured["body"] == _PAYLOAD


def test_non_2xx_raises_teams_api_error():
    def post(url, headers, body):
        return 403, {"error": {"code": "Forbidden", "message": "forbidden by Graph permissions"}}

    client = TeamsClient("https://graph.example.test/v1.0", post=post)
    with pytest.raises(TeamsApiError) as exc:
        client.post_channel_message(_TEAM_ID, _CHANNEL_ID, _PAYLOAD, "tok")
    assert exc.value.status_code == 403
    assert "forbidden by Graph permissions" in str(exc.value)


def test_error_message_extracted_from_flat_body():
    def post(url, headers, body):
        return 400, {"message": "bad payload"}

    client = TeamsClient(post=post)
    with pytest.raises(TeamsApiError) as exc:
        client.post_chat_message(_CHAT_ID, _PAYLOAD, "tok")
    assert "bad payload" in str(exc.value)


def test_default_stub_transport_channel_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = TeamsClient()
    assert client.uses_stub_transport is True
    resp = client.post_channel_message(_TEAM_ID, _CHANNEL_ID, _PAYLOAD, "tok")
    assert resp.get("_stub") is True
    assert resp["id"].isdigit()
    # webUrl thread segment = the path element before /messages (the channel id).
    assert resp["webUrl"] == f"https://teams.microsoft.com/l/message/{_CHANNEL_ID}/{resp['id']}"


def test_default_stub_transport_is_deterministic():
    client = TeamsClient()
    first = client.post_channel_message(_TEAM_ID, _CHANNEL_ID, _PAYLOAD, "tok")
    second = client.post_channel_message(_TEAM_ID, _CHANNEL_ID, _PAYLOAD, "tok")
    assert first["id"] == second["id"]
    assert first["webUrl"] == second["webUrl"]


def test_default_stub_transport_chat_shape():
    client = TeamsClient()
    resp = client.post_chat_message(_CHAT_ID, _PAYLOAD, "tok")
    assert resp.get("_stub") is True
    assert _CHAT_ID in resp["webUrl"]


def test_injected_transport_disables_stub_flag():
    client = TeamsClient(post=lambda url, headers, body: (201, {"id": "1"}))
    assert client.uses_stub_transport is False
