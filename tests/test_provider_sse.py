import asyncio
import json

import httpx
import pytest

from infermesh.providers import UpstreamError, events


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("split", [False, True])
def test_multiple_valid_frames_can_exceed_limit_in_one_network_chunk(split):
    async def scenario():
        payload = {"choices": [{"delta": {"content": "x" * 600000}}]}
        frame = b"data: " + json.dumps(payload).encode() + b"\n\n"
        wire = frame * 2 + b"data: [DONE]\n\n"
        # Same event stream, independent of transport chunk boundaries.
        chunks = [wire[:700000], wire[700000:]] if split else [wire]
        body = Chunks(chunks)
        response = httpx.Response(200, stream=body)
        assert [event async for event in events(response)] == [payload, payload]
        assert body.closed and response.is_closed
    asyncio.run(scenario())


@pytest.mark.parametrize("terminated", [False, True])
def test_single_oversized_frame_is_rejected_even_when_split(terminated):
    async def scenario():
        wire = b"data: " + b"x" * (1024 * 1024) + (b"\n\n" if terminated else b"")
        body = Chunks([wire[:600000], wire[600000:]])
        response = httpx.Response(200, stream=body)
        with pytest.raises(UpstreamError) as error:
            await anext(events(response))
        assert error.value.code == "stream_frame_too_large"
        assert not error.value.retryable
        assert body.closed and response.is_closed
    asyncio.run(scenario())
