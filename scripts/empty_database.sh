#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DELETE_FILES=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage:
  scripts/empty_database.sh [--yes] [--files]

What it does:
  - Empties app database tables only. It keeps table structure/schema.
  - Does not delete uploaded Excel files unless --files is passed.

Options:
  --yes    Run without interactive confirmation.
  --files  Also delete uploaded files inside ./file.
  --help   Show this help.

Examples:
  scripts/empty_database.sh
  scripts/empty_database.sh --yes
  scripts/empty_database.sh --yes --files
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)
      ASSUME_YES=1
      ;;
    --files)
      DELETE_FILES=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
  shift
done

if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PYTHON="$ROOT_DIR/venv/bin/python"
else
  PYTHON="python3"
fi

echo "This will EMPTY the rconsil database tables."
echo "Database source: app.database.database.DATABASE_URL"
if [[ "$DELETE_FILES" -eq 1 ]]; then
  echo "It will also delete uploaded files from: $ROOT_DIR/file"
else
  echo "Uploaded files in ./file will be kept. Use --files to delete them too."
fi

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -r -p "Type EMPTY to continue: " CONFIRM
  if [[ "$CONFIRM" != "EMPTY" ]]; then
    echo "Cancelled."
    exit 1
  fi
fi

"$PYTHON" - <<'PY'
from sqlalchemy import text

from app.database.database import engine

tables = [
    "reconciliation_remarks",
    "reconciliation_results",
    "staging_records",
    "uploaded_sheets",
    "uploaded_files",
    "upload_batches",
    "organizations",
]

with engine.begin() as conn:
    conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
    try:
        for table in tables:
            conn.execute(text(f"TRUNCATE TABLE `{table}`"))
            print(f"Truncated {table}")
    finally:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

print("Database tables emptied successfully.")
PY

if [[ "$DELETE_FILES" -eq 1 ]]; then
  if [[ -d "$ROOT_DIR/file" ]]; then
    find "$ROOT_DIR/file" -mindepth 1 -maxdepth 1 -type f -delete
    echo "Uploaded files deleted from ./file."
  else
    echo "Upload directory ./file does not exist; skipped."
  fi
fi

echo "Done."
