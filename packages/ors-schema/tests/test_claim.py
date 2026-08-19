"""The claim handover's one shared constant, and where it has to be reachable.

Nothing here proves the format is right -- a constant compared to itself
agrees with any edit, and this file could not tell a rename from a fix. What
proves it is the pair of frozen wire vectors, one at each end:
`server/tests/test_api_claims.py::test_the_sealed_blob_matches_a_frozen_wire_vector`
and `daemon/tests/test_join.py::test_the_daemon_opens_a_frozen_wire_vector`,
each carrying a ciphertext produced before this tag could be edited.

What is here is the two things those vectors cannot say: that the name is
reachable where both production ends import it from, and that its value is
spelled out once in a file whose diff says "you are changing a wire protocol"
rather than only appearing inside an `HKDF(...)` call.
"""

from __future__ import annotations

import ors_schema
from ors_schema.claim import CLAIM_HKDF_INFO


def test_the_claim_tag_is_reachable_from_the_package_root():
    """`ors_server.api.claims` and `ors_daemon.join` both import it from
    `ors_schema`, not from `ors_schema.claim`, and `__all__` is
    hand-maintained -- so a name added to the module and forgotten there is a
    constant neither end can reach."""
    assert "CLAIM_HKDF_INFO" in ors_schema.__all__
    assert ors_schema.CLAIM_HKDF_INFO is CLAIM_HKDF_INFO


def test_the_claim_tag_is_bytes_and_is_the_v1_tag():
    """A tripwire, not a proof (see the module docstring): every rack in the
    field holds the peer half of this string in a release that shipped months
    ago, so it may not move without a `v2` and a decision about all of them.

    `bytes` and not `str` is worth pinning separately: `HKDF(info=...)` takes
    bytes and a `str` there is a `TypeError` raised inside an admin's approve
    click, on the server, with the write lock held.
    """
    assert isinstance(CLAIM_HKDF_INFO, bytes)
    assert CLAIM_HKDF_INFO == b"ors-claim-v1"
