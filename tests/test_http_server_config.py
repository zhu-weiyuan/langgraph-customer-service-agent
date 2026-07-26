"""Server-level safety settings that protect the app before handlers run."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_threaded_server_has_burst_tolerant_accept_backlog():
    from app import CustomerServiceHTTPServer

    assert CustomerServiceHTTPServer.request_queue_size >= 200
    assert CustomerServiceHTTPServer.daemon_threads is True
