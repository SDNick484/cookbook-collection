"""The suite never reaches the real network, and its fakes patch the modules that define the calls."""
import socket
import urllib.request

import pytest

import ask
import capture
import dinnerlog
import mcp
import scan


def test_public_host_connection_is_refused():
    with pytest.raises(AssertionError, match="real network connection"):
        socket.create_connection(("203.0.113.7", 443), timeout=1)


def test_public_hostname_lookup_is_refused():
    with pytest.raises(AssertionError, match="real network connection"):
        socket.getaddrinfo("api.anthropic.com", 443)


def test_urlopen_to_the_claude_api_is_refused():
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=b"{}", method="POST")
    with pytest.raises(AssertionError, match="real network connection"):
        urllib.request.urlopen(req, timeout=1)


def test_loopback_is_still_reachable():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        socket.create_connection(srv.getsockname(), timeout=2).close()
    finally:
        srv.close()


@pytest.mark.parametrize("module, name", [
    (scan, "messages_api"), (scan, "call_claude"), (scan, "prepare_photo"), (scan, "model_call"),
    (capture, "call_claude"), (capture, "run_capture_job"),
    (dinnerlog, "dl_open"), (dinnerlog, "fetch_dl_photo"), (dinnerlog, "pull_history"),
    (dinnerlog, "start_background"), (mcp, "run_tool"),
])
def test_patch_targets_are_defined_where_they_are_patched(module, name):
    """A name re-exported by another module is a copy: patching it leaves the real function in place."""
    assert getattr(module, name).__module__ == module.__name__


def test_callers_reach_the_defining_module():
    for mod in (ask, mcp):
        assert not hasattr(mod, "call_claude") and not hasattr(mod, "dl_open")
    with pytest.raises(ImportError):
        __import__("extras")
