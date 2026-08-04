"""
Organization Service
--------------------
Responsible for extracting an organization name from an uploaded Excel filename
and ensuring it is persisted in the database (get-or-create pattern).
"""

import logging
import re
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.organization import Organization

logger = logging.getLogger(__name__)

# Keywords that signal the end of the organization name inside a filename.
# Everything before the first matching keyword is treated as the org name.
_STOP_WORDS = re.compile(
    r"\b(reconciliation|recon|report|statement|data|file|export|sheet)\b",
    re.IGNORECASE,
)


def _extract_org_name(file_path: str) -> str:
    """
    Derive an organization name from the given file path.

    Strategy:
      1. Take only the filename (strip directory).
      2. Drop the file extension.
      3. Split on the first recognized stop-word (e.g. "Reconciliation").
         Everything to the left is the organization name.
      4. If no stop-word is found, use the full stem as the name.
      5. Normalize whitespace and title-case the result.

    Examples:
      "Indigo Reconciliation 2025-26.xlsx"  -> "Indigo"
      "Air India Reconciliation.xlsx"       -> "Air India"
      "SpiceJet_Recon_Q1.xlsx"              -> "Spicejet"
    """
    stem = Path(file_path).stem  # filename without extension

    # Replace underscores/hyphens with spaces for uniform processing
    stem = stem.replace("_", " ").replace("-", " ")

    match = _STOP_WORDS.search(stem)
    if match:
        raw_name = stem[: match.start()]
    else:
        raw_name = stem

    # Collapse multiple spaces, strip edges, apply title-case
    org_name = " ".join(raw_name.split()).strip()

    if not org_name:
        raise ValueError(
            f"Could not extract an organization name from filename: '{Path(file_path).name}'. "
            "Ensure the filename follows the pattern '<OrgName> Reconciliation ...xlsx'."
        )

    return org_name.title()


def _generate_code(name: str) -> str:
    """
    Generate a short, uppercase code from the organization name.

    Rules:
      - Single-word name  -> first 6 characters, uppercased  (e.g. "Indigo" -> "INDIGO")
      - Multi-word name   -> initials of each word, uppercased (e.g. "Air India" -> "AI")
    """
    words = name.split()
    if len(words) == 1:
        return words[0][:6].upper()
    return "".join(w[0] for w in words).upper()


def get_or_create_organization(file_path: str, db: Session) -> Organization:
    """
    Extract the organization name from *file_path* and return the matching
    database record, creating it first if it does not yet exist.

    Parameters
    ----------
    file_path : str
        Path (or filename) of the uploaded Excel file.
    db : Session
        Active SQLAlchemy database session (injected by FastAPI dependency).

    Returns
    -------
    Organization
        The existing or newly-created ``Organization`` ORM object.

    Raises
    ------
    ValueError
        If an organization name cannot be extracted from the filename.
    """
    org_name = _extract_org_name(file_path)
    logger.info("Extracted organization name '%s' from file '%s'", org_name, file_path)

    # Case-insensitive lookup
    existing: Organization | None = (
        db.query(Organization)
        .filter(func.lower(Organization.name) == func.lower(org_name))
        .first()
    )

    if existing:
        logger.info(
            "Organization '%s' already exists (id=%s). Reusing.", org_name, existing.id
        )
        return existing

    # Generate a unique code; append a numeric suffix if there is a collision
    base_code = _generate_code(org_name)
    code = base_code
    suffix = 1
    while db.query(Organization).filter(Organization.code == code).first():
        code = f"{base_code}{suffix}"
        suffix += 1

    new_org = Organization(
        name=org_name,
        code=code,
        is_active=True,
    )
    db.add(new_org)
    db.commit()
    db.refresh(new_org)

    logger.info(
        "Created new organization '%s' with code '%s' (id=%s).",
        new_org.name,
        new_org.code,
        new_org.id,
    )
    return new_org
