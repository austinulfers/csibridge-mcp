import pytest

from csibridge_mcp.engine import Engine, MockBackend


@pytest.fixture
def backend():
    return MockBackend()


@pytest.fixture
def engine(backend):
    engine = Engine(backend)
    engine.start()
    yield engine
    engine.stop()


@pytest.fixture
def anyio_backend():
    return "asyncio"
