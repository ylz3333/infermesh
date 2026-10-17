import pytest
from pydantic import ValidationError

from infermesh.config import Provider


@pytest.mark.parametrize("port", ["not-a-port", "-1", "65536", "999999"])
def test_provider_rejects_invalid_url_ports(port):
    with pytest.raises(ValidationError, match="port"):
        Provider(name="real", model="demo", base_url=f"https://example.invalid:{port}/v1")


@pytest.mark.parametrize("url", [
    "https://example.invalid/v1",
    "http://localhost:8001/v1",
    "http://[::1]:8001/v1",
    "https://example.invalid:65535/v1",
])
def test_provider_accepts_valid_url_ports(url):
    assert Provider(name="real", model="demo", base_url=url).base_url == url
