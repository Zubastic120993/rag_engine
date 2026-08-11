"""Collision / invariant fail-safes for stable-id-v1 (fail closed)."""

from __future__ import annotations

from typing import Any, Mapping

from rag_engine.stable_identity.constants import IDENTITY_SCHEME_VERSION
from rag_engine.stable_identity.ids import chunk_id, chunk_id_preimage
from rag_engine.stable_identity.validation import (
    IdentityValidationError,
    validate_chunk_id,
    validate_chunking_fingerprint,
    validate_document_id,
)


class IdentityCollisionError(RuntimeError):
    """Raised when a stable ID conflicts with a different identity preimage.

    Fail-closed: callers must STOP / REVIEW. Never silent overwrite.
    """


def verify_chunk_id_matches_preimage(
    value: str,
    *,
    document_id: str,
    chunking_fingerprint: str,
    ordinal: int,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
) -> None:
    """Ensure ``value`` equals the chunk_id derived from the stated constituents."""
    validate_chunk_id(value)
    expected = chunk_id(
        document_id,
        chunking_fingerprint,
        ordinal,
        identity_scheme_version=identity_scheme_version,
    )
    if value != expected:
        raise IdentityCollisionError(
            "chunk_id does not match identity preimage "
            f"(got {value!r}, expected {expected!r} for "
            f"{chunk_id_preimage(document_id, chunking_fingerprint, ordinal, identity_scheme_version=identity_scheme_version)!r})"
        )


def assert_chunk_record_consistent(record: Mapping[str, Any]) -> None:
    """Validate a persisted chunk row's ID against its stored constituents."""
    try:
        cid = record["chunk_id"]
        document_id = record["document_id"]
        fingerprint = record["chunking_fingerprint"]
    except KeyError as exc:
        raise IdentityValidationError(
            f"chunk record missing required field: {exc}"
        ) from exc
    if "ordinal" in record:
        ordinal = record["ordinal"]
    elif "chunk_ordinal" in record:
        ordinal = record["chunk_ordinal"]
    else:
        raise IdentityValidationError(
            "chunk record missing ordinal / chunk_ordinal"
        )
    scheme = record.get("identity_scheme_version", IDENTITY_SCHEME_VERSION)
    validate_document_id(str(document_id))
    validate_chunking_fingerprint(str(fingerprint))
    verify_chunk_id_matches_preimage(
        str(cid),
        document_id=str(document_id),
        chunking_fingerprint=str(fingerprint),
        ordinal=ordinal,  # type: ignore[arg-type]
        identity_scheme_version=str(scheme),
    )


def reject_conflicting_chunk_reuse(
    existing: Mapping[str, Any] | None,
    *,
    chunk_id: str,
    document_id: str,
    chunking_fingerprint: str,
    ordinal: int,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
) -> None:
    """Identical reuse is idempotent; divergent constituents fail hard."""
    validate_chunk_id(chunk_id)
    verify_chunk_id_matches_preimage(
        chunk_id,
        document_id=document_id,
        chunking_fingerprint=chunking_fingerprint,
        ordinal=ordinal,
        identity_scheme_version=identity_scheme_version,
    )
    if existing is None:
        return
    existing_id = str(existing.get("chunk_id", chunk_id))
    if existing_id != chunk_id:
        raise IdentityCollisionError(
            f"existing record chunk_id {existing_id!r} != {chunk_id!r}"
        )
    fields = {
        "document_id": document_id,
        "chunking_fingerprint": chunking_fingerprint,
        "ordinal": ordinal,
        "identity_scheme_version": identity_scheme_version,
    }
    for key, expected in fields.items():
        if key == "ordinal":
            got = existing.get("ordinal", existing.get("chunk_ordinal"))
        else:
            got = existing.get(key)
        if got is None:
            raise IdentityCollisionError(
                f"existing chunk row missing {key} while inserting {chunk_id!r}"
            )
        if key == "ordinal":
            if int(got) != int(expected):  # type: ignore[arg-type]
                raise IdentityCollisionError(
                    f"chunk_id {chunk_id!r} ordinal conflict: {got!r} vs {expected!r}"
                )
        elif str(got) != str(expected):
            raise IdentityCollisionError(
                f"chunk_id {chunk_id!r} {key} conflict: {got!r} vs {expected!r}"
            )
