"""Metadata registry exceptions (Phase 3)."""

from __future__ import annotations


class RegistryError(Exception):
    """Base exception for metadata registry errors."""


class ExplicitPathRequiredError(RegistryError):
    """Raised when a DB path was omitted."""


class InvalidDatabasePathError(RegistryError):
    """Raised when a DB path is not usable."""


class ExistingDatabaseError(RegistryError):
    """Raised when init targets an existing database unexpectedly."""


class MissingDatabaseError(RegistryError):
    """Raised when a required database file does not exist."""


class ParentDirectoryMissingError(RegistryError):
    """Raised when the parent directory does not exist."""


class UnknownSchemaVersionError(RegistryError):
    """Raised when the registry version is newer than supported."""


class DowngradeNotAllowedError(RegistryError):
    """Raised when a downgrade is requested."""


class MigrationError(RegistryError):
    """Raised for migration failures."""


class RegistryValidationError(RegistryError):
    """Raised when registry validation or invariant checks fail."""


class RegistryConflictError(RegistryError):
    """Raised when an identity conflict would overwrite authoritative data."""


class RegistryIntegrityError(RegistryError):
    """Raised when SQLite integrity/FK checks fail."""
