"""A client's X-Request-Id is echoed back in a response header, so it must be a plain token.

Python's `$` also matches before a trailing newline, so the check must end with `\\Z`.
"""

import pytest

from voiceshop.monitor.server import _REQUEST_ID


@pytest.mark.parametrize("value", ["abcdefgh", "req-corr-000001", "a.b_c-d.e_f-g.h", "x" * 64])
def test_plain_request_ids_are_accepted(value):
    assert _REQUEST_ID.match(value)


@pytest.mark.parametrize(
    "value",
    [
        "abcdefgh\n",  # trailing newline (the case `$` used to accept)
        "abcdefgh\r\nX-Injected: 1",  # CRLF injection
        "short",  # under 8 characters
        "x" * 65,  # over 64 characters
        "bad id here",  # space
        "",
    ],
)
def test_anything_else_is_rejected(value):
    assert not _REQUEST_ID.match(value)
