"""Typed exceptions for embedding-fp-v1 compatibility enforcement."""

from __future__ import annotations

from typing import Any


class FingerprintError(Exception):
    """Base class for fingerprint / index-compatibility failures."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class FingerprintConfigurationError(FingerprintError):
    """Runtime fingerprint cannot be formed from current configuration."""


class FingerprintCorruptError(FingerprintError):
    """Stored fingerprint payload is malformed."""


class FingerprintUnsupportedVersionError(FingerprintError):
    """Stored fingerprint_schema_version is not supported."""


class FingerprintConflictError(FingerprintError):
    """Authoritative copies of fingerprint state disagree."""


class FingerprintMissingError(FingerprintError):
    """Authority missing while vectors exist / mutation requested."""


class FingerprintMismatchError(FingerprintError):
    """Runtime index fingerprint does not match stored authority."""


class FingerprintLegacyBlockedError(FingerprintError):
    """Mutation attempted against UNKNOWN_LEGACY index state."""


class FingerprintIncompatibleRetrievalError(FingerprintError):
    """Retrieval blocked for incompatible / corrupt / conflict state."""


class IndexCompatibilityError(FingerprintError):
    """Generic index compatibility gate failure (ingest/append)."""


class CertificationError(FingerprintError):
    """Base class for legacy-index certification failures."""


class CertificationEvidenceError(CertificationError):
    """Evidence is missing, insufficient, or malformed for certification."""


class CertificationTargetChangedError(CertificationError):
    """Target index no longer matches the evidence manifest binding."""


class CertificationConflictError(CertificationError):
    """Certification would overwrite a conflicting authority/audit record."""


class LegacyIndexNotCertifiableError(CertificationError):
    """Evidence evaluation concluded the index is not certifiable."""


class CertificationRequiresOperatorApprovalError(CertificationError):
    """Mutation requested without explicit operator apply intent."""
