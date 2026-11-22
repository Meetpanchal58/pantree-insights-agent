# Compiler baseline

294/294 results matched gold SQL across 98 cases and 3 passes. Snapshot unchanged: True.

Setup: 6.652s. Query p50/p95/max: 0.0076/0.0702/0.1182s (n=294).

No model calls. These figures measure compiler and local DuckDB execution only; they do not measure retrieval, question understanding, full answers, Cloud Run cold starts or concurrent users. Passes share a connection and OS caches; the first pass is not a controlled cold-start benchmark.
