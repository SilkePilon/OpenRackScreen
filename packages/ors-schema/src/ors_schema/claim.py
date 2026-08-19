"""The claim handover's wire constants, shared by the two ends that ship.

One module for one byte string, because of what that byte string is. The
claim protocol (design spec S6.3 step 5) hands a newly minted daemon key over
encrypted to an ephemeral X25519 public key: X25519, then HKDF-SHA256 with the
tag below, then AES-256-GCM. A peer that derives with a different tag derives
a different key and cannot open anything the other sealed -- whatever else the
two ends agree on, and with no error either end can attribute. It is protocol,
exactly like `PROTOCOL_VERSION` and `ors_daemon.discovery.SERVICE_TYPE`, and
this package is where this repository keeps protocol.

**It lives here now, and it deliberately did not before.** Until the daemon
had a claim client there was one production speller of these twelve bytes
(`ors_server.api.claims`) and one independent literal in the server's own test
(`test_api_claims.unseal`), and that independence was the only thing standing
between a one-sided edit and a silent protocol rename. Sharing the constant
then would have deleted the check and put nothing in its place. What replaced
it is the frozen wire vector, and the pin is the test at each end that feeds
production: `test_api_claims.test_the_frozen_vector_is_the_format_the_live_seal_still_produces`
(which seals with the live `_seal` and opens with the test's own literal) and
`daemon/tests/test_join.test_the_daemon_opens_a_frozen_wire_vector` (whose
reader *is* production). Each carries a ciphertext produced before this tag
could be edited, so no edit here -- one-sided or coordinated, in this file or
in either end's -- can leave the suite green.
`test_api_claims.test_the_sealed_blob_matches_a_frozen_wire_vector` is not one
of the two, verified by mutation: it opens the frozen blob with the test's own
`unseal`, and no production code runs in it at all.

With the format pinned from outside, one definition for the two
production ends is the safer arrangement: a daemon and a server that disagree
about this string do not fail, they simply never pair.

The server's test keeps its own literal copy. That is not an oversight either:
it is the peer implementation a daemon's author would copy, and it means the
round-trip tests still fail if this module and `api.claims` drift apart.
"""

from __future__ import annotations

CLAIM_HKDF_INFO = b"ors-claim-v1"
"""The HKDF `info` tag for the claim key handover.

Versioned in its own text rather than by `PROTOCOL_VERSION`, because it is not
carried in any message: there is nowhere on this wire to negotiate it, so a
change to it is a new tag (`ors-claim-v2`) and an explicit decision about every
rack already in the field, not a number a server can compare and complain
about.
"""
