from uuid import UUID


def normalize_uuid(value: str | UUID) -> str:
    """Return a canonical UUID string or raise ValueError."""
    return str(UUID(str(value)))
