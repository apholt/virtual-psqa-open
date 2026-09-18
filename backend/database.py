from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from config import settings
import os

# Ensure the data directory exists relative to the backend folder
os.makedirs(os.path.dirname(settings.DATABASE_URL.replace("sqlite:///", "")), exist_ok=True)

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={
        "check_same_thread": False,  # required for SQLite
        "timeout": 30,               # WAL_v1: wait up to 30s for a busy DB before erroring
    },
)


# WAL_v1: concurrent log pipelines were dying on sqlite3 "database is locked".
# WAL journal mode lets readers and a writer coexist; busy_timeout makes a
# blocked writer queue (spin/wait) for 30s instead of failing immediately.
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def ensure_schema():
    """Ensure newly added tables and columns exist in SQLite database."""
    try:
        raw_conn = engine.raw_connection()
        cur = raw_conn.cursor()
        
        # Check qa_jobs.fraction_number
        cols = [r[1] for r in cur.execute("PRAGMA table_info(qa_jobs);").fetchall()]
        if cols and "fraction_number" not in cols:
            cur.execute("ALTER TABLE qa_jobs ADD COLUMN fraction_number INTEGER;")
            raw_conn.commit()

        # Check gamma_results.beam_number
        cols_gamma = [r[1] for r in cur.execute("PRAGMA table_info(gamma_results);").fetchall()]
        if cols_gamma and "beam_number" not in cols_gamma:
            cur.execute("ALTER TABLE gamma_results ADD COLUMN beam_number INTEGER;")
            raw_conn.commit()

        # Check fractions columns
        cols_frac = [r[1] for r in cur.execute("PRAGMA table_info(fractions);").fetchall()]
        if cols_frac and "machine" not in cols_frac:
            cur.execute("ALTER TABLE fractions ADD COLUMN machine TEXT;")
            raw_conn.commit()
        if cols_frac and "delivery_date" not in cols_frac:
            cur.execute("ALTER TABLE fractions ADD COLUMN delivery_date TEXT;")
            raw_conn.commit()
        if cols_frac and "delivery_type" not in cols_frac:
            cur.execute("ALTER TABLE fractions ADD COLUMN delivery_type VARCHAR(32) DEFAULT 'curative';")
            raw_conn.commit()
        if cols_frac and "is_interrupted" not in cols_frac:
            cur.execute("ALTER TABLE fractions ADD COLUMN is_interrupted BOOLEAN DEFAULT 0;")
            raw_conn.commit()
        if cols_frac and "interruption_reason" not in cols_frac:
            cur.execute("ALTER TABLE fractions ADD COLUMN interruption_reason TEXT;")
            raw_conn.commit()

        # Ensure beam_deliveries table exists
        cur.execute("""
        CREATE TABLE IF NOT EXISTS beam_deliveries (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id               INTEGER NOT NULL,
            fraction_number       INTEGER NOT NULL,
            fraction_id           INTEGER,
            machine               TEXT,
            treatment_date        TEXT,
            beam_name             TEXT NOT NULL,
            n_spots_prescribed    INTEGER,
            n_spots_delivered     INTEGER,
            n_spots_matched       INTEGER,
            mu_prescribed         REAL,
            mu_delivered          REAL,
            mu_deviation_pct      REAL,
            mu_err_mean_abs_pct   REAL,
            mu_err_max_abs_pct    REAL,
            pos_mean_dx_mm        REAL,
            pos_mean_dy_mm        REAL,
            pos_mean_radial_mm    REAL,
            pos_p95_radial_mm     REAL,
            pos_max_radial_mm     REAL,
            gamma_passing_rate    REAL,
            not_scored            TEXT,
            record_incomplete     INTEGER NOT NULL DEFAULT 0,
            retained_at           TEXT,
            created_at            TEXT NOT NULL,
            UNIQUE (plan_id, fraction_number, beam_name)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_beam_deliveries_plan ON beam_deliveries (plan_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_beam_deliveries_machine ON beam_deliveries (machine);")
        raw_conn.commit()

        # Check synthetic_cts columns
        cols_sct = [r[1] for r in cur.execute("PRAGMA table_info(synthetic_cts);").fetchall()]
        if cols_sct:
            sct_new_cols = [
                ("cbct_dir", "TEXT"),
                ("cbct_series_instance_uid", "TEXT"),
                ("cbct_num_slices", "INTEGER"),
                ("dir_method", "TEXT"),
                ("dir_mean_displacement_mm", "REAL"),
                ("dir_p99_displacement_mm", "REAL"),
                ("mae_hu_before", "REAL"),
                ("mae_hu_after", "REAL"),
            ]
            for col_name, col_type in sct_new_cols:
                if col_name not in cols_sct:
                    cur.execute(f"ALTER TABLE synthetic_cts ADD COLUMN {col_name} {col_type};")
            raw_conn.commit()

        cur.close()
        raw_conn.close()
    except Exception:
        pass


ensure_schema()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()