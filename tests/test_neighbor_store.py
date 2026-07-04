import json

from backend.models import Connection
from backend.neighbor_store import JsonNeighborStore


def conn(target="A2"):
    return Connection(
        target_author_id=target, target_name="Bob",
        connection_type="coauthor", label="Paper",
    )


async def test_record_does_not_write_synchronously(tmp_path):
    store = JsonNeighborStore(tmp_path / "cache.json")
    store.record({"A1": [conn()]})
    assert not (tmp_path / "cache.json").exists()


async def test_flush_persists_recorded_entries(tmp_path):
    path = tmp_path / "cache.json"
    store = JsonNeighborStore(path)
    store.record({"A1": [conn()]})
    await store.flush()
    raw = json.loads(path.read_text())
    assert raw["A1"][0]["target_author_id"] == "A2"


async def test_close_flushes_pending(tmp_path):
    path = tmp_path / "cache.json"
    store = JsonNeighborStore(path)
    await store.open()
    store.record({"A1": [conn()]})
    await store.close()
    assert json.loads(path.read_text())["A1"][0]["target_author_id"] == "A2"


async def test_fetch_reads_recorded_before_flush(tmp_path):
    store = JsonNeighborStore(tmp_path / "cache.json")
    store.record({"A1": [conn()]})
    found = await store.fetch(["A1"])
    assert found["A1"][0].target_author_id == "A2"


async def test_open_loads_previous_flush(tmp_path):
    path = tmp_path / "cache.json"
    first = JsonNeighborStore(path)
    first.record({"A1": [conn()]})
    await first.flush()

    second = JsonNeighborStore(path)
    await second.open()
    found = await second.fetch(["A1"])
    await second.close()
    assert found["A1"][0].target_author_id == "A2"
