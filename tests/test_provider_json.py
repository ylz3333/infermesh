import asyncio
import json

import httpx
import pytest

from infermesh.providers import UpstreamError, events, read_json


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
@pytest.mark.parametrize("stream", [False, True])
def test_nonstandard_numeric_constants_are_rejected_and_response_closed(constant, stream):
    async def scenario():
        payload = ('{"choices":[{"message":{"content":' + constant +
                   '},"delta":{"content":' + constant + '}}]}').encode()
        content = b"data: " + payload + b"\n\ndata: [DONE]\n\n" if stream else payload
        response = httpx.Response(200, content=content)
        with pytest.raises(UpstreamError) as error:
            if stream:
                await anext(events(response))
            else:
                await read_json(response)
        assert error.value.code == ("invalid_stream" if stream else "invalid_response")
        assert response.is_closed
    asyncio.run(scenario())


def test_constant_names_inside_strings_remain_valid():
    async def scenario():
        payload = {"choices": [{"message": {"content": "NaN Infinity -Infinity"}}]}
        assert await read_json(httpx.Response(200, json=payload)) == payload
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0.0, -0.5, 1e300, -1e300])
def test_finite_json_numbers_remain_valid(value):
    async def scenario():
        payload = {"choices": [{"message": {"content": "ok"}, "delta": {}}],
                   "score": value}
        assert await read_json(httpx.Response(200, json=payload)) == payload
        content = ("data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n").encode()
        assert [event async for event in events(httpx.Response(200, content=content))] == [payload]
    asyncio.run(scenario())
