"""Phase 3 metadata registry — stable-id-v1 aligned SQLite foundation.

Target authoritative architecture for identity + lifecycle. Production
authority switch is NOT activated; APIs require an explicit DB path.

Does not open production DBs or create ``.rag_state`` on import.
"""

from __future__ import annotations

from rag_engine.metadata_registry.connection import (
    connect_registry,
    normalize_db_path,
    open_registry,
)
from rag_engine.metadata_registry.exceptions import (
    DowngradeNotAllowedError,
    ExistingDatabaseError,
    ExplicitPathRequiredError,
    InvalidDatabasePathError,
    MissingDatabaseError,
    MigrationError,
    ParentDirectoryMissingError,
    RegistryConflictError,
    RegistryError,
    RegistryIntegrityError,
    RegistryValidationError,
    UnknownSchemaVersionError,
)
from rag_engine.metadata_registry.migrations import (
    current_schema_version,
    foreign_keys_enabled,
    get_schema_version,
    initialize_registry,
    migrate_connection,
)
from rag_engine.metadata_registry.paths import (
    APPROVED_PRODUCTION_FILENAME,
    production_registry_dir,
    production_registry_path,
)
from rag_engine.metadata_registry.repository import (
    RegistryRepository,
    make_source_file_id,
    register_chunk,
    register_document_lifecycle,
    register_document_version,
    register_source_file,
    register_subject,
    register_vector_mapping,
    registry_transaction,
)
from rag_engine.metadata_registry.schema import (
    CURRENT_SCHEMA_VERSION,
    REQUIRED_TABLES,
    SCHEMA_SQL,
)

__all__ = [
    "APPROVED_PRODUCTION_FILENAME",
    "CURRENT_SCHEMA_VERSION",
    "DowngradeNotAllowedError",
    "ExistingDatabaseError",
    "ExplicitPathRequiredError",
    "InvalidDatabasePathError",
    "MissingDatabaseError",
    "MigrationError",
    "ParentDirectoryMissingError",
    "REQUIRED_TABLES",
    "RegistryConflictError",
    "RegistryError",
    "RegistryIntegrityError",
    "RegistryRepository",
    "RegistryValidationError",
    "SCHEMA_SQL",
    "UnknownSchemaVersionError",
    "connect_registry",
    "current_schema_version",
    "foreign_keys_enabled",
    "get_schema_version",
    "initialize_registry",
    "make_source_file_id",
    "migrate_connection",
    "normalize_db_path",
    "open_registry",
    "production_registry_dir",
    "production_registry_path",
    "register_chunk",
    "register_document_lifecycle",
    "register_document_version",
    "register_source_file",
    "register_subject",
    "register_vector_mapping",
    "registry_transaction",
]
