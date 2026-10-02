"""Configuration contains endpoint metadata, never provider credentials."""
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    kind: Literal["openai", "mock"] = "openai"
    base_url: str = "http://localhost:8001/v1"
    model: str
    api_key_env: str | None = None
    input_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    output_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    quality: int = Field(default=1, ge=1, le=3)
    initial_latency_ms: float = Field(default=1000, gt=0, allow_inf_nan=False)
    concurrency: int = Field(default=16, ge=1, le=1000)
    supports_tools: bool = False
    supports_stream: bool = True
    mock_latency_ms: int = Field(default=10, ge=0, le=10000)
    mock_fail_every: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_url(self):
        if self.kind == "openai":
            url = urlparse(self.base_url)
            if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("Use an http(s) API base URL without credentials/query/fragment")
            if self.api_key_env and not os.getenv(self.api_key_env):
                raise ValueError(f"Missing provider key environment variable: {self.api_key_env}")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: list[Provider] = Field(min_length=1)
    pools: dict[str, list[str]]
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    failure_threshold: int = Field(default=3, ge=1)
    cooldown_seconds: float = Field(default=15, gt=0)
    requests_per_minute: int = Field(default=600, ge=1)
    max_inflight: int = Field(default=100, ge=1)
    capture_content: bool = False

    @model_validator(mode="after")
    def valid_pools(self):
        names = [p.name for p in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("Provider names must be unique")
        if not self.pools or any(not members or len(set(members)) != len(members) or not set(members) <= set(names) for members in self.pools.values()):
            raise ValueError("Every pool must contain unique, configured provider names")
        return self

    @classmethod
    def load(cls):
        path = Path(os.getenv("INFERMESH_CONFIG", "config/demo.json"))
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

