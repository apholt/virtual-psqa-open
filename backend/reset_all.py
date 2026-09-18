"""
Full reset for Virtual PSQA — wipes ALL plans, results, and stored DICOM.

Clears:
  - Every row in all DB tables (drop + recreate → IDs reset to 1)
  - data/results/*        (MCsquare output, log output, gamma maps)
  - data/dicom_store/*    (archived plan/dose/record DICOM)
  - P:\PSQA               (any files loitering in the watch folder) [optional]

STOP THE APP before running this so no files are locked and nothing is
mid-write. Requires typing WIPE to confirm.

Run from the backend dir:
  ..\python\python.exe reset_all.py
"""
import sys
import shutil
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from database import Base, engine  # noqa: E402
import models  # noqa: F401,E402  (register all tables)
from config import settings  # noqa: E402

RESULTS = BACKEND / "data" / "results"
STORE = BACKEND / "data" / "dicom_store"
WATCH = Path(settings.DICOM_WATCH_FOLDER) if settings.DICOM_WATCH_FOLDER else None


def _clear_dir(d: Path) -> int:
    """Delete everything inside d (but keep d itself). Returns items removed."""
    if not d.exists():
        return 0
    n = 0
    for child in d.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            n += 1
        except Exception as exc:
            print(f"  ! could not remove {child}: {exc}")
    return n


def main():
    print("=" * 60)
    print("FULL RESET — this will PERMANENTLY delete:")
    print(f"  - ALL database rows ({', '.join(sorted(Base.metadata.tables))})")
    print(f"  - {RESULTS}")
    print(f"  - {STORE}")
    if WATCH:
        print(f"  - files in {WATCH}  (watch folder)")
    print("=" * 60)
    confirm = input("Type WIPE to confirm (anything else aborts): ").strip()
    if confirm != "WIPE":
        print("Aborted — nothing was changed.")
        return

    # 1. Database: drop all tables and recreate empty (IDs reset to 1).
    print("\nDropping all tables...")
    Base.metadata.drop_all(bind=engine)
    print("Recreating empty tables...")
    Base.metadata.create_all(bind=engine)
    print("  DB reset complete.")

    # 2. Results
    n = _clear_dir(RESULTS)
    print(f"Cleared {n} item(s) from {RESULTS}")

    # 3. DICOM store
    n = _clear_dir(STORE)
    print(f"Cleared {n} item(s) from {STORE}")

    # 4. Watch folder (optional — clears leftover records)
    if WATCH and WATCH.exists():
        n = _clear_dir(WATCH)
        print(f"Cleared {n} item(s) from {WATCH}")

    print("\nReset complete. Restart the app for a fresh start.")


if __name__ == "__main__":
    main()
