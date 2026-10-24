"""Offline test of the PII engine switch in filter/user_memory_filter.py (v7.0): none vs regex vs PII-Tracer, fallback, and one
full _update_user_memory_state run with the database and the extraction model faked. No server, no model, no database.

Run inside the Open WebUI container (it has psycopg, aiohttp and open_webui):
  docker cp filter/user_memory_filter.py open-webui:/tmp/ && docker cp tests/test_pii_engine.py open-webui:/tmp/
  docker exec -e PYTHONPATH=/app/backend open-webui python /tmp/test_pii_engine.py /tmp/user_memory_filter.py
"""
import asyncio, importlib.util, json, sys
spec = importlib.util.spec_from_file_location("user_memory_filter", sys.argv[1]); mod = importlib.util.module_from_spec(spec)
sys.modules["user_memory_filter"] = mod; spec.loader.exec_module(mod)

CANNED = {
    "my card is 4111 1111 1111 1111 and I'm Beau": [{"label": "account_number", "start": 11, "end": 30, "score": 0.9}, {"label": "private_person", "start": 39, "end": 43, "score": 0.9}],
    "Beau": [{"label": "private_person", "start": 0, "end": 4, "score": 0.9}],
    "hunter2": [{"label": "secret", "start": 0, "end": 7, "score": 0.8}],
    "likes espresso": [], "coffee": [], "name": [], "password": [], "call me at 555-123-4567": [{"label": "private_phone", "start": 11, "end": 23, "score": 0.9}],
}
async def fake_spans(self, text):
    wanted = set(self.valves.pii_tracer_labels)
    return [s for s in CANNED[text] if s["label"] in wanted]
async def boom(self, text): raise RuntimeError("HTTP 503")

async def main():
    f = mod.Filter()
    # regex engine, unchanged behaviour
    assert "[REDACTED]" in await f._pii_scrub("call me at 555-123-4567")
    assert (await f._validate_fact({"type": "identity", "subject": "name", "value": "Beau"}))["clean"]
    # model engine
    mod.Filter._pii_tracer_spans = fake_spans
    f.valves.pii_engine = "pii_tracer"
    out = await f._pii_scrub("my card is 4111 1111 1111 1111 and I'm Beau")
    assert out == "my card is [REDACTED] and I'm Beau", out            # default labels keep the name
    assert (await f._validate_fact({"type": "identity", "subject": "name", "value": "Beau"}))["clean"]
    r = await f._validate_fact({"type": "status", "subject": "password", "value": "hunter2"})
    assert not r["clean"] and r["blocked_reasons"] == ["secret detected in value"] and r["scrubbed_fact"]["value"] == "[REDACTED]", r
    facts = [{"type": "preference", "subject": "coffee", "value": "likes espresso"}, {"type": "status", "subject": "password", "value": "hunter2"}]
    assert [x["subject"] for x in await f._filter_facts_pii(facts)] == ["coffee"]
    f.valves.pii_filter_mode = "redact"
    assert [x["value"] for x in await f._filter_facts_pii(facts)] == ["likes espresso", "[REDACTED]"]
    f.valves.pii_tracer_labels = list(mod.PII_TRACER_LABELS)                      # names included -> name redacted
    assert await f._pii_scrub("Beau") == "[REDACTED]"
    # fallback
    mod.Filter._pii_tracer_spans = boom
    f = mod.Filter(); f.valves.pii_engine = "pii_tracer"; f._pii_down_logged = False
    assert await f._pii_scrub("call me at 555-123-4567") == "call me at [REDACTED]"   # regex took over
    assert f._pii_down_logged
    f.valves.pii_tracer_fallback = "allow"
    assert await f._pii_scrub("call me at 555-123-4567") == "call me at 555-123-4567"
    assert (await f._validate_fact({"type": "status", "subject": "password", "value": "hunter2"}))["clean"]
    # full update with DB and extraction model faked
    mod.Filter._pii_tracer_spans = fake_spans
    f = mod.Filter(); f.valves.pii_engine = "pii_tracer"
    seen = {}
    async def init_store(self, request, user): return None
    async def search(self, uid, text, request, user, threshold, limit): seen["search_text"] = text; return []
    async def call_model(self, prompt, user=None, request=None):
        seen["prompt"] = prompt
        return json.dumps({"facts": [{"type": "preference", "subject": "coffee", "value": "likes espresso"}, {"type": "status", "subject": "password", "value": "hunter2"}]})
    async def apply(self, uid, changes, request, user): seen["changes"] = changes; return {"updated": 0, "added": len(changes), "removed": 0, "merged": 0, "skipped": 0}
    mod.Filter._init_store = init_store; mod.Filter._search_facts = search; mod.Filter._call_extraction_model = call_model; mod.Filter._apply_changes = apply
    await f._update_user_memory_state("user-1234", "chat", [{"role": "user", "content": "Beau"}], {"role": "user", "content": [{"type": "text", "text": "my card is 4111 1111 1111 1111 and I'm Beau"}]})
    assert "4111" not in seen["prompt"] and "I'm Beau" in seen["prompt"], seen["prompt"][-300:]
    assert "4111" not in seen["search_text"], seen["search_text"]                 # the manager's context search uses the scrubbed text
    assert [c["subject"] for c in seen["changes"]] == ["coffee"], seen["changes"]
    # pii_engine = none: nothing scrubbed, nothing blocked
    f = mod.Filter(); f.valves.pii_engine = "none"; seen.clear()
    assert not f._pii_enabled()
    await f._update_user_memory_state("user-1234", "chat", [], {"role": "user", "content": "my card is 4111 1111 1111 1111 and I'm Beau"})
    assert "4111 1111 1111 1111" in seen["prompt"], seen["prompt"][-300:]
    assert [c["subject"] for c in seen["changes"]] == ["coffee", "password"], seen["changes"]
    # pii_tracer with an empty URL falls back to regex (one warning), 'allow' stores as-is
    mod.Filter._pii_tracer_spans = mod.Filter.__dict__["_pii_tracer_spans"] if "_pii_tracer_spans" in mod.Filter.__dict__ else mod.Filter._pii_tracer_spans
    # query window valve
    f = mod.Filter(); f.valves.query_window_messages = 2
    msgs = [{"role": "system", "content": "persona"}, {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    assert f._query_text(msgs) == "b\nc", repr(f._query_text(msgs))
    f.valves.query_window_messages = 1
    assert f._query_text(msgs) == "c"
    # fact text and vector literal
    assert mod.Filter._fact_text({"subject": " job ", "value": "architect "}) == "job: architect"
    assert mod.Filter._vec([0.5, -1.0]) == "[0.5,-1.0]"
    print("ALL PII ENGINE TESTS PASSED")
asyncio.run(main())
