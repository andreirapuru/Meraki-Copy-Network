import httpx
import pytest

from app.meraki import MerakiClient


@pytest.mark.asyncio
async def test_get_all_follows_meraki_next_link() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "startingAfter" not in str(request.url):
            return httpx.Response(200, json=[{"id": "one"}], headers={"Link": '<https://api.meraki.com/api/v1/organizations?startingAfter=one>; rel="next"'})
        return httpx.Response(200, json=[{"id": "two"}])

    client = MerakiClient("x" * 24, transport=httpx.MockTransport(handler))
    try:
        assert await client.get_all("/organizations") == [{"id": "one"}, {"id": "two"}]
        assert len(seen) == 2
    finally:
        await client.close()
