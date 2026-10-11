import pytest
from pydantic import ValidationError

from infermesh.config import Settings


@pytest.mark.parametrize("value", ["1e400", "Infinity", '"Infinity"', "NaN", "-Infinity"])
def test_loading_nonfinite_cooldown_is_rejected(tmp_path, monkeypatch, value):
    config = tmp_path / "config.json"
    config.write_text(
        '{"providers": [{"name": "demo", "kind": "mock", "model": "demo"}],'
        '"pools": {"auto": ["demo"]}, "cooldown_seconds": ' + value + '}',
        encoding="utf-8",
    )
    monkeypatch.setenv("INFERMESH_CONFIG", str(config))
    with pytest.raises(ValidationError, match="cooldown_seconds"):
        Settings.load()


@pytest.mark.parametrize("cooldown", [0.01, 15, 3600])
def test_finite_cooldown_permits_recovery_probe(monkeypatch, cooldown):
    from infermesh.routing import Router

    cfg = Settings.model_validate({
        "providers": [{"name": "demo", "kind": "mock", "model": "demo"}],
        "pools": {"auto": ["demo"]}, "cooldown_seconds": cooldown,
        "failure_threshold": 1,
    })
    monkeypatch.setattr("infermesh.routing.time.monotonic", lambda: 100.0)
    router = Router(cfg)
    assert router.acquire("demo")
    router.release("demo", False, 10)
    assert not router.available("demo", now=100.0)
    assert router.available("demo", now=100.0 + cooldown)
