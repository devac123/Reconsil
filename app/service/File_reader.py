from pathlib import Path

import pandas as pd
from openpyxl import load_workbook


# ---------------------------------------------------------------------------
# Per-sheet header row configuration
# ---------------------------------------------------------------------------
# Maps a normalised sheet name (lower-case, stripped) → zero-based row index
# that pandas should use as the column header.
#
# Why different rows?
#   - Most sheets have a title / subtitle row(s) above the real header.
#   - AIR COST TRN, CASH x SAle, CASH X Re: one title row → real header at index 1
#   - SPYJ SALE, SPJY Refund: two rows before the real header → header at index 2
#   - All other sheets default to index 0 (first row is the header).
#
# The key is the sheet name lowercased + stripped so matching is
# case-insensitive and whitespace-tolerant.

_SHEET_HEADER_ROW: dict[str, int] = {
    "air cost trn":  1,
    "cash x sale":   1,
    "cash x re":     1,
    "spyj sale":     2,
    "spjy refund":   2,
}


def _header_row_for(sheet_name: str) -> int:
    """Return the zero-based pandas ``header`` row index for *sheet_name*."""
    return _SHEET_HEADER_ROW.get(sheet_name.strip().lower(), 0)


class FileReaderService:

    @staticmethod
    def read_excel(file_path: Path):
        workbook = load_workbook(file_path, read_only=True)

        response = {
            "file_name": file_path.name,
            "total_sheets": len(workbook.sheetnames),
            "sheets": []
        }

        for sheet in workbook.worksheets:
            header_row = _header_row_for(sheet.title)

            headers = []
            # Skip `header_row` rows, then read the next row as column names
            for i, row in enumerate(
                sheet.iter_rows(
                    min_row=1,
                    max_row=header_row + 2,   # read up to header row + 1 data row
                    values_only=True,
                )
            ):
                if i == header_row:
                    headers = [str(cell) if cell is not None else "" for cell in row]
                    break

            response["sheets"].append({
                "name": sheet.title,
                "rows": sheet.max_row,
                "columns": headers,
                "header_row": header_row,
            })

        workbook.close()

        return response

    @staticmethod
    def read_sheet_as_dataframe(
        file_path: Path,
        sheet_name: str,
        header_row: int | None = None,
    ) -> pd.DataFrame:
        """
        Read a single worksheet from an Excel file into a pandas DataFrame.

        Parameters
        ----------
        file_path : Path
            Path to the Excel file on disk.
        sheet_name : str
            Name of the worksheet tab to read.
        header_row : int | None
            Zero-based row index to use as column headers.  If *None* (default)
            the value is looked up from ``_SHEET_HEADER_ROW``; sheets not
            listed there default to row 0.

        Returns
        -------
        pd.DataFrame
            DataFrame with the sheet contents.  Column names are plain strings.
        """
        if header_row is None:
            header_row = _header_row_for(sheet_name)

        df: pd.DataFrame = pd.read_excel(
            file_path,
            sheet_name=sheet_name,
            engine="openpyxl",
            header=header_row,  # correct header row per sheet
            dtype=object,       # keep every value as Python object; no auto-cast
        )

        # Drop rows where every column is NaN — truly empty rows
        df = df.dropna(how="all")

        # Ensure all column names are plain strings (openpyxl can return ints)
        df.columns = [str(col) for col in df.columns]

        return df


if __name__ == "__main__":
    file_path = Path("../../file/Development Indigo Reconciliation 2024-25.xlsx")
    response = FileReaderService.read_excel(file_path)
    for s in response["sheets"]:
        print(s["name"], "| header_row:", s["header_row"], "| cols:", s["columns"][:5])
