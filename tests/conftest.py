from pathlib import Path

import pytest

from montagne import db

# --------------------------------------------------------------------------
# Isolation totale vis-à-vis de Turso/production — même pattern que V2
# (tests/conftest.py) : neutralisé avant que quoi que ce soit d'autre importe
# `db`, pour ne jamais toucher une vraie base Turso pendant les tests.
# --------------------------------------------------------------------------
_TEST_DB_DIR = Path(__file__).resolve().parent / "_tmp_test_db"
_TEST_DB_DIR.mkdir(exist_ok=True)

db.TURSO_DATABASE_URL = None
db.TURSO_AUTH_TOKEN = None
db.DB_PATH = _TEST_DB_DIR / "test.db"
db.init_db()

from montagne import poche_core  # noqa: E402  (après le patch ci-dessus, volontairement)

poche_core.init_db()

_ALL_TABLES = [
    "monthly_entries", "settings", "rate_overrides", "delta_status",
    "poche_mouvements", "poche_versement_ignore",
]

_CACHED_READS = [
    db.get_all_entries, db.get_setting, db.get_rate_override,
    db.get_rate_overrides_for_year, db.get_delta_statuses,
    poche_core.get_montants_verses,
]


@pytest.fixture(autouse=True)
def clean_db():
    """Base vidée (pas juste "cache invalidé") avant CHAQUE test — même
    fichier SQLite réutilisé pour toute la session par rapidité, sans ça les
    écritures d'un test resteraient visibles dans le suivant."""
    with db.get_connection() as conn:
        for table in _ALL_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    for cached in _CACHED_READS:
        cached.clear()
    yield
    for cached in _CACHED_READS:
        cached.clear()
