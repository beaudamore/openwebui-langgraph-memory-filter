## Security Finding: Concurrent Request State Can Cross User Boundaries

`Filter._extraction_result` is mutable state stored on the shared `Filter` instance. Concurrent asynchronous requests can overwrite this value between extraction and LangGraph processing. A request could therefore process another user's extraction result, creating a cross-user memory isolation risk.

### Required Fix

Do not store request-specific extraction data on `self`. Carry it through the per-request `MemoryGraphState`, or pass it as a request-local argument to the graph invocation. Add a concurrency test with simultaneous requests from two different Open WebUI user IDs and assert that each stored memory contains only that user's extracted facts.