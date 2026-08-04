"""
FileMapping Repository
----------------------
All database interactions for the ``file_mappings`` table.
Business logic belongs in the service layer, not here.
"""

import logging
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.file_mapping import FileMapping, MappingDataType

logger = logging.getLogger(__name__)


class FileMappingRepository:
    """Data-access layer for :class:`~app.models.file_mapping.FileMapping`."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    def create(
        self,
        organization_id: int,
        sheet_name: str,
        excel_column: str,
        system_field: str,
        data_type: MappingDataType = MappingDataType.STRING,
        is_required: bool = False,
    ) -> FileMapping:
        """
        Persist a new mapping record and return it.

        Raises
        ------
        IntegrityError
            If a mapping already exists for
            ``(organization_id, sheet_name, excel_column)``.
            The caller is responsible for catching this and deciding whether
            to call :meth:`update` instead.
        """
        record = FileMapping(
            organization_id=organization_id,
            sheet_name=sheet_name,
            excel_column=excel_column,
            system_field=system_field,
            data_type=data_type,
            is_required=is_required,
        )
        self._db.add(record)
        self._db.flush()

        logger.info(
            "FileMapping queued (org=%s, sheet='%s', '%s' -> '%s').",
            organization_id,
            sheet_name,
            excel_column,
            system_field,
        )
        return record

    def update(
        self,
        organization_id: int,
        sheet_name: str,
        excel_column: str,
        system_field: str,
        data_type: MappingDataType = MappingDataType.STRING,
        is_required: bool = False,
    ) -> Optional[FileMapping]:
        """
        Update an existing mapping identified by its natural business key
        ``(organization_id, sheet_name, excel_column)``.

        Returns the updated record, or ``None`` if no matching record exists.
        """
        record = self.get_mapping(organization_id, sheet_name, excel_column)
        if record is None:
            return None

        record.system_field = system_field
        record.data_type = data_type
        record.is_required = is_required
        self._db.flush()

        logger.info(
            "FileMapping updated (id=%s, org=%s, sheet='%s', '%s' -> '%s').",
            record.id,
            organization_id,
            sheet_name,
            excel_column,
            system_field,
        )
        return record

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    def get_by_organization(self, organization_id: int) -> list[FileMapping]:
        """
        Return all mapping records for *organization_id*, ordered by
        sheet name then excel column name.
        """
        return (
            self._db.query(FileMapping)
            .filter(FileMapping.organization_id == organization_id)
            .order_by(FileMapping.sheet_name.asc(), FileMapping.excel_column.asc())
            .all()
        )

    def get_by_sheet(
        self,
        organization_id: int,
        sheet_name: str,
    ) -> list[FileMapping]:
        """
        Return all mapping records for a specific org + sheet combination,
        ordered by excel column name.
        """
        return (
            self._db.query(FileMapping)
            .filter(
                FileMapping.organization_id == organization_id,
                FileMapping.sheet_name == sheet_name,
            )
            .order_by(FileMapping.excel_column.asc())
            .all()
        )

    def get_mapping(
        self,
        organization_id: int,
        sheet_name: str,
        excel_column: str,
    ) -> Optional[FileMapping]:
        """
        Look up a single mapping by its natural business key.
        Returns ``None`` if no matching record exists.
        """
        return (
            self._db.query(FileMapping)
            .filter(
                FileMapping.organization_id == organization_id,
                FileMapping.sheet_name == sheet_name,
                FileMapping.excel_column == excel_column,
            )
            .first()
        )
