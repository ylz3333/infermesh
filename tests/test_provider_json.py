import asyncio
import json

import httpx
import pytest

from infermesh.providers import UpstreamError, events, read_json


@pytest.mark.parametrize("stream", [False, True])
def test_excessive_json_nesting_is_an_upstream_error_and_closes_stream(stream):
    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            # Exceeds decoder stack limits while staying below both byte caps.
            depth = 100000
            payload = (b'{"choices":[{"message":{},"delta":{}}],"extra":'
                       + b'[' * depth + b'0' + b']' * depth + b'}')
            yield b'data: ' + payload + b'\n\ndata: [DONE]\n\n' if stream else payload

        async def aclose(self):
            self.closed = True

    async def scenario():
        body = Body()
        response = httpx.Response(200, stream=body)
        assert not response.is_closed
        with pytest.raises(UpstreamError) as error:
            if stream:
                await anext(events(response))
            else:
                await read_json(response)
        assert error.value.code == ("invalid_stream" if stream else "invalid_response")
        assert response.is_closed
        assert body.closed

    asyncio.run(scenario())


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
