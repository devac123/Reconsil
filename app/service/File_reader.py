from pathlib import Path

import pandas as pd
from openpyxl import load_workbook


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

            headers = []

            # Read first row as column names
            for row in sheet.iter_rows(min_row=1, max_row=1, values_only=True):
                headers = [str(cell) if cell is not None else "" for cell in row]

            response["sheets"].append({
                "name": sheet.title,
                "rows": sheet.max_row,
                "columns": headers
            })

        workbook.close()

        return response

    @staticmethod
    def read_sheet_as_dataframe(file_path: Path, sheet_name: str) -> pd.DataFrame:
        """
        Read a single worksheet from an Excel file into a pandas DataFrame.

        - The first row is used as column headers.
        - All values are kept exactly as they appear in the workbook; no type
          coercion or normalisation is applied.
        - Rows where every cell is NaN (completely empty rows) are dropped
          because they carry no information and would pollute the staging table.
          Rows that have at least one non-empty cell are preserved in full,
          including any individual empty cells within them.

        Parameters
        ----------
        file_path : Path
            Path to the Excel file on disk.
        sheet_name : str
            Name of the worksheet tab to read.

        Returns
        -------
        pd.DataFrame
            DataFrame with the sheet contents. Column names are strings;
            the index is the default RangeIndex (not written to the DB).
        """
        df: pd.DataFrame = pd.read_excel(
            file_path,
            sheet_name=sheet_name,
            engine="openpyxl",
            header=0,           # first row = column names
            dtype=object,       # keep every value as Python object; no auto-cast
        )

        # Drop rows where every column is NaN — truly empty rows
        df = df.dropna(how="all")

        # Ensure all column names are plain strings (openpyxl can return ints)
        df.columns = [str(col) for col in df.columns]

        return df


if __name__ == "__main__":
    file_path = Path("../../file/file_example_XLSX_5000.xlsx")

    response = FileReaderService.read_excel(file_path)

    print(response)
