import pytest

import readeckbot.readeck_client as rc


@pytest.mark.asyncio
async def test_get_readeck_version_success(mocker):
    class DummyCompletedProcess:
        def __init__(self):
            self.stdout = "readeck version: 0.19.2"

    mocker.patch("subprocess.run", return_value=DummyCompletedProcess())
    version = rc.get_readeck_version()
    assert version == "readeck version: 0.19.2"


@pytest.mark.asyncio
async def test_get_readeck_version_failure(mocker):
    mocker.patch("subprocess.run", side_effect=FileNotFoundError("not found"))
    version = rc.get_readeck_version()
    assert "Could not determine readeck version" in version


@pytest.mark.asyncio
async def test_is_admin_user_true(mocker):
    # Simulate a response with admin role
    mock_response = mocker.AsyncMock()
    mock_response.json = mocker.AsyncMock(return_value={"provider": {"roles": ["admin", "user"]}})
    mocker.patch.object(rc.requests, "get", return_value=mock_response)
    result = await rc.is_admin_user("dummy_token")
    assert result is True


@pytest.mark.asyncio
async def test_is_admin_user_false(mocker):
    # Simulate a response with no admin role
    mock_response = mocker.AsyncMock()
    mock_response.json = mocker.AsyncMock(return_value={"provider": {"roles": ["user"]}})
    mocker.patch.object(rc.requests, "get", return_value=mock_response)
    result = await rc.is_admin_user("dummy_token")
    assert result is False


@pytest.mark.asyncio
async def test_is_admin_user_exception(mocker):
    # Simulate an exception in the request
    async def raise_exc(*args, **kwargs):
        raise Exception("fail")

    mocker.patch.object(rc.requests, "get", side_effect=raise_exc)
    result = await rc.is_admin_user("dummy_token")
    assert result is False


@pytest.mark.asyncio
async def test_save_bookmark_url_only(mocker):
    mock_response = mocker.MagicMock()
    mock_response.headers = {"Bookmark-Id": "abc123"}
    mock_post = mocker.patch.object(rc.requests, "post", return_value=mock_response)

    bookmark_id = await rc.save_bookmark("https://example.com", "dummy_token")

    assert bookmark_id == "abc123"
    _, kwargs = mock_post.call_args
    assert kwargs["json"] == {"url": "https://example.com"}


@pytest.mark.asyncio
async def test_save_bookmark_with_title_and_labels(mocker):
    mock_response = mocker.MagicMock()
    mock_response.headers = {"Bookmark-Id": "abc123"}
    mock_post = mocker.patch.object(rc.requests, "post", return_value=mock_response)

    bookmark_id = await rc.save_bookmark(
        "https://example.com", "dummy_token", title="My Title", labels=["python", "ml"]
    )

    assert bookmark_id == "abc123"
    _, kwargs = mock_post.call_args
    assert kwargs["json"] == {"url": "https://example.com", "title": "My Title", "labels": ["python", "ml"]}


@pytest.mark.asyncio
async def test_get_bookmark(mocker):
    mock_response = mocker.MagicMock()
    mock_response.json.return_value = {"id": "abc123", "title": "Example", "labels": ["a", "b"]}
    mocker.patch.object(rc.requests, "get", return_value=mock_response)

    result = await rc.get_bookmark("abc123", "dummy_token")

    assert result == {"id": "abc123", "title": "Example", "labels": ["a", "b"]}


@pytest.mark.asyncio
async def test_update_bookmark_labels(mocker):
    mock_response = mocker.MagicMock()
    mock_patch = mocker.patch.object(rc.requests, "patch", return_value=mock_response)

    result = await rc.update_bookmark_labels("abc123", ["python", "ml"], "dummy_token")

    assert result is True
    _, kwargs = mock_patch.call_args
    assert kwargs["json"] == {"labels": ["python", "ml"]}
