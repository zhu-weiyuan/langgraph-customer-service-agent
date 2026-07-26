"""Pytest collection policy for this repository.

The root-level ``test_pg*.py`` and ``test_embedding.py`` files are ad-hoc
manual diagnostics. Several execute network/database calls while imported, so
letting pytest collect them makes the normal suite hang or mutate a live
pgvector database. Durable pgvector coverage lives in tests/.
"""

collect_ignore = [
    "test_embedding.py",
    "test_pg.py",
    "test_pg2.py",
    "test_pg3.py",
    "test_pg4.py",
    "test_pg_search.py",
    "test_pgvector.py",
]
