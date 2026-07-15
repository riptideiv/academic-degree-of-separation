import json

from backend.models import Connection
from backend.neighbor_store import (
    JsonNeighborStore, NeighborCache, SupabaseNeighborStore, decode_ring, merge_rings,
)


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
    assert raw["A1"]["coauthor"][0]["target_author_id"] == "A2"


async def test_close_flushes_pending(tmp_path):
    path = tmp_path / "cache.json"
    store = JsonNeighborStore(path)
    await store.open()
    store.record({"A1": [conn()]})
    await store.close()
    assert json.loads(path.read_text())["A1"]["coauthor"][0]["target_author_id"] == "A2"


async def test_fetch_reads_recorded_before_flush(tmp_path):
    store = JsonNeighborStore(tmp_path / "cache.json")
    store.record({"A1": [conn()]})
    found = await store.fetch(["A1"])
    assert found["A1"]["coauthor"][0].target_author_id == "A2"


async def test_open_loads_previous_flush(tmp_path):
    path = tmp_path / "cache.json"
    first = JsonNeighborStore(path)
    first.record({"A1": [conn()]})
    await first.flush()

    second = JsonNeighborStore(path)
    await second.open()
    found = await second.fetch(["A1"])
    await second.close()
    assert found["A1"]["coauthor"][0].target_author_id == "A2"


def test_legacy_list_decodes_all_author_buckets():
    ring = decode_ring([conn().model_dump()])
    assert set(ring) == {"coauthor", "citation", "institution"}
    assert ring["coauthor"][0].target_author_id == "A2"
    assert ring["citation"] == []


def test_partial_ring_merge_is_monotonic_in_both_orders():
    citation = Connection(
        target_author_id="A3", target_name="Carol", connection_type="citation", label="Cited"
    )
    for first, second in [
        ({"coauthor": [conn()]}, {"citation": [citation]}),
        ({"citation": [citation]}, {"coauthor": [conn()]}),
    ]:
        merged = merge_rings(first, second)
        assert set(merged) == {"coauthor", "citation"}


async def test_cache_requires_every_requested_bucket():
    cache = NeighborCache()
    cache.put({"A1": {"coauthor": [conn()]}})
    assert cache.get_memory("A1", {"coauthor"}) == [conn()]
    assert cache.get_memory("A1", {"coauthor", "citation"}) is None


async def test_open_skips_only_corrupt_entry(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "A1": {"coauthor": [conn().model_dump()]},
        "A2": 42,
    }))
    store = JsonNeighborStore(path)
    await store.open()
    found = await store.fetch(["A1", "A2"])
    await store.close()
    assert "A1" in found
    assert "A2" not in found


def test_supabase_upsert_merges_json_buckets_atomically():
    assert "neighbor_cache.connections || EXCLUDED.connections" in SupabaseNeighborStore._UPSERT
    assert "jsonb_typeof(neighbor_cache.connections) = 'object'" in SupabaseNeighborStore._UPSERT
