# Forward Erratum — LC6 Operational Controls Earlier Partial Report

This is an additive correction. Historical claims are not silently rewritten.

Earlier partial report used a legacy inventory checksum representation without the `type` field and reported production inventory SHA-256:
`0728d4f6a7d4a7a996ee66a0a1ef225afa42c7fd9428835a5a21131b187b27b9`

Remediation changes the canonical contract to accepted `inventory-row-v1` rows with fields:
`path`, `type`, `bytes`, `sha256`.

Read-only discovery confirmed the unchanged 19-file production snapshot produces:
`b189d622935faf6a0aa14830a80fa73af85c49f2d965bb83cdfb23acdb4f992a`

The earlier value is retained as a legacy representation note only and is not accepted for candidate readiness.
