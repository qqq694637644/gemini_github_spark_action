import httpx
import pytest

from app.main import app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_privacy_page_returns_deployment_privacy_content() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/privacy")

    assert response.status_code == 200
    assert response.headers["X-MCP-Surface-Version"] == "2"
    assert "text/html" in response.headers["content-type"]
    assert "隐私说明" in response.text
    assert "访问令牌不会写入" in response.text
