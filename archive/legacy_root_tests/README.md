# Archived root-level test and diagnostic scripts

These scripts were historically kept in the repository root during early
customer-service, PostgreSQL/pgvector, RAG, and streaming debugging. They are
kept here for code study and historical troubleshooting only.

They are **not** the current test entry point. The maintained automated tests
are under `tests/` and are collected according to `pytest.ini`. Some archived
scripts target old modules or assume a live local database, so review them
before running and never use them as production startup commands.
