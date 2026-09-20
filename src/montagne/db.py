"""Persistance SQLite/Turso — sans dépendance UI (ni Streamlit, ni FastAPI) :
ce module doit rester importable aussi bien par l'app Streamlit que par
l'API FastAPI qui la remplace progressivement.

Différence clé avec la génération précédente : les catégories sujettes à
ambiguïté (frais, complément cash/crypto, facture/perdiem "auto") ont chacune
un flag de confirmation explicite en plus de leur valeur. Un `number_input`
Streamlit ne peut pas représenter "vide" — il écrit toujours un vrai nombre —
donc sans flag explicite, "0€ pas encore su" et "0€ confirmé" sont
indiscernables. C'est ce qui a causé la plupart des bugs de cette génération
précédente (mois marqués "clos" à tort, projections qui retombent à 0...).
Ici, chaque flag dit *si* la valeur doit être prise au sérieux ; la valeur
elle-même est presque accessoire tant que le flag est faux.

Extrait dans ce package séparé, deux changements par rapport à la version
Streamlit-only d'origine :
- `_config()` ne lit plus que des variables d'environnement (plus de
  `st.secrets`) — MONTAGNE_DB_PATH / MONTAGNE_REPLICA_PATH / TURSO_DATABASE_URL
  / TURSO_AUTH_TOKEN se règlent côté appelant (l'app Streamlit les pose depuis
  .streamlit/secrets.toml au démarrage, l'API FastAPI depuis les env vars
  Vercel).
- `@st.cache_data(ttl=30)` remplacé par `_ttl_cache`, un cache TTL maison sans
  dépendance : même comportement (élide les lectures redondantes dans une
  même fenêtre de 30s, `.clear()` toujours disponible pour _bust_read_cache),
  mais fonctionne identiquement sous Streamlit ou sous FastAPI.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path


def _ttl_cache(ttl_seconds: float):
    """Cache TTL minimal, sans dépendance de framework — remplace
    @st.cache_data(ttl=...) pour que ce module reste importable hors
    Streamlit. Même contrat que st.cache_data côté appelant : la fonction
    décorée expose .clear()."""
    def decorator(fn):
        cache: dict = {}

        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            cached = cache.get(key)
            if cached is not None:
                value, ts = cached
                if now - ts < ttl_seconds:
                    return value
            value = fn(*args, **kwargs)
            cache[key] = (value, now)
            return value

        wrapper.clear = cache.clear
        return wrapper
    return decorator


def _config(key: str) -> str | None:
    return os.environ.get(key)


DB_PATH = Path(os.environ.get("MONTAGNE_DB_PATH", "local.db"))
REPLICA_PATH = Path(os.environ.get("MONTAGNE_REPLICA_PATH", "local_replica.db"))

TURSO_DATABASE_URL = _config("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = _config("TURSO_AUTH_TOKEN")

# (nom_colonne, type_sql) — FIELDS_NUM sont des nombres, FIELDS_TEXT du texte,
# FIELDS_FLAG des booléens stockés en INTEGER (0/1).
#
# complement_montant / conversion_crypto_euros / complement_manquant /
# complement_confirme / complement_type / complement_type_attendu sont
# legacy : un même mois pouvait avoir soit du cash, soit de la crypto, jamais
# les deux. Remplacés par des champs dédiés complement_cash_*/complement_
# crypto_* (voir migrate_complement_split ci-dessous) — gardés en base
# (jamais de colonne supprimée dans ce projet) mais plus lus ni écrits par le
# code courant.
FIELDS_NUM = [
    "jours_travailles", "jours_conges_payes", "jours_feries", "jours_sans_solde",
    "facture", "salaire_brut", "salaire_net_recu",
    "frais_allowance", "frais_expense_batch", "frais_mileage",
    "complement_montant", "conversion_crypto_euros", "complement_manquant",  # legacy
    "complement_cash_montant", "complement_cash_manquant",
    "complement_crypto_montant_natif", "complement_crypto_conversion_euros", "complement_crypto_manquant",
    "salaire_net_attendu", "perdiem_attendu",
    "sodexo", "pecule_vacances", "treizieme_mois",
]
FIELDS_TEXT = [
    "complement_type", "complement_type_attendu",  # legacy
    "complement_crypto_devise", "commentaire",
]
FIELDS_FLAG = [
    "salaire_confirme",   # salaire_net_recu fiable (sinon: pas encore versé)
    "frais_confirmes",    # frais_allowance/expense_batch/mileage fiables
    "complement_confirme",  # legacy
    "complement_cash_attendu", "complement_cash_confirme",
    "complement_crypto_attendu", "complement_crypto_confirme",
    "mois_cloture",       # tout est confirmé pour ce mois (statistiques annuelles)
    "facture_auto",       # facture = jours × taux journalier (calculée)
    "perdiem_auto",       # perdiem_attendu = jours × perdiem journalier (calculé)
    "salaire_attendu_auto",  # salaire_net_attendu = moyenne des mois clos (calculé)
]
ALL_FIELDS = FIELDS_NUM + FIELDS_TEXT + FIELDS_FLAG

# complement_cash_attendu est le seul flag sans DEFAULT 0 : NULL doit rester
# distinct de 0 pour que "jamais réglé" (nouveau mois) se comporte comme
# "cash attendu" par défaut côté UI (Saisie mensuelle), alors qu'un 0
# explicite (case décochée à la main) le désactive vraiment. Les autres
# flags n'ont pas ce besoin (leur défaut "non" est correct dès la création).
_NO_DEFAULT_FLAGS = {"complement_cash_attendu"}


def _col_type(f: str) -> str:
    if f in FIELDS_TEXT:
        return "TEXT"
    if f in FIELDS_FLAG:
        return "INTEGER" if f in _NO_DEFAULT_FLAGS else "INTEGER DEFAULT 0"
    return "REAL"


_turso_conn = None  # connexion Turso réutilisée pour tout le process (une seule réplique embarquée)


def _turso_connection():
    global _turso_conn
    import libsql
    if _turso_conn is None:
        _turso_conn = libsql.connect(str(REPLICA_PATH), sync_url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN or "")
        _turso_conn.sync()
    return _turso_conn


@contextlib.contextmanager
def get_connection():
    """Connexion Turso (réplique embarquée, réutilisée) si configuré via
    variables d'environnement, sinon SQLite local (comportement de dev
    inchangé).

    Contrairement à un `.sync()` par requête (des dizaines d'allers-retours
    réseau par page — beaucoup trop lent), la connexion Turso n'est
    synchronisée qu'une fois à sa création et après chaque écriture ; voir
    `refresh()` pour forcer une resynchro (à appeler au plus une fois par
    rerun Streamlit / requête FastAPI, jamais par requête SQL individuelle).

    Vrai context manager (pas juste le protocole `with` natif de
    sqlite3.Connection, qui ne gère que commit/rollback de la transaction,
    JAMAIS la fermeture) : la connexion SQLite locale est explicitement
    fermée en sortie de `with`. Sans ça, chaque `with db.get_connection() as
    conn:` fuyait une connexion — cause probable de la flakiness récurrente
    des tests (tous en SQLite local, un seul fichier, des centaines d'appels
    par run) : sqlite3.OperationalError aléatoires en suite complète, jamais
    reproductibles en isolation. La connexion Turso singleton n'est elle
    jamais fermée ici (fermer un process-wide reusable serait pire que le
    bug) — voir `_turso_connection`."""
    if TURSO_DATABASE_URL:
        yield _turso_connection()
        return
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # PAS de PRAGMA journal_mode=WAL ici, volontairement : essayé puis
    # retiré côté V2 — WAL crée des fichiers annexes (-wal/-shm) à côté du
    # .db, et le dossier V2 est synchronisé par Google Drive Desktop, qui
    # verrouille/surveille ces fichiers assez agressivement pour ralentir la
    # suite de tests de ~22s à 200-270s (mesuré). `timeout` ci-dessous suffit
    # comme filet de sécurité contre un verrou transitoire (attend jusqu'à 5s
    # avant d'échouer, au lieu d'un "database is locked" immédiat) sans les
    # fichiers annexes qui posent problème sur ce dossier précis. Sans objet
    # ici (ce package vit hors Google Drive), gardé pour l'app Streamlit qui,
    # elle, y est toujours exposée tant qu'elle tourne depuis son propre dossier.
    try:
        yield conn
    finally:
        conn.close()


def refresh() -> None:
    """Récupère les derniers changements distants (autre instance/déploiement).
    À appeler au plus une fois par rerun Streamlit / requête FastAPI — jamais
    dans une fonction de lecture individuelle, ça retransformerait chaque
    requête en aller-retour réseau complet."""
    if TURSO_DATABASE_URL:
        _turso_connection().sync()


def _sync(conn) -> None:
    """Pousse une écriture vers Turso immédiatement (no-op en SQLite local)."""
    if hasattr(conn, "sync"):
        conn.sync()


def _rows_to_dicts(cursor) -> list[dict]:
    """Convertit les lignes du curseur en dicts, quel que soit le backend :
    sqlite3.Row supporte déjà dict(row), mais les curseurs libsql renvoient
    de simples tuples — on reconstruit les clés depuis cursor.description
    (identique dans les deux cas) pour garder un seul chemin de code."""
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def init_db() -> None:
    with get_connection() as conn:
        cols_sql = ", ".join(f"{f} {_col_type(f)}" for f in ALL_FIELDS)
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS monthly_entries (
                month TEXT PRIMARY KEY,
                {cols_sql},
                source TEXT DEFAULT 'manuel',
                updated_at TEXT
            )
            """
        )
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(monthly_entries)").fetchall()}
        for f in ALL_FIELDS:
            if f not in existing_cols:
                conn.execute(f"ALTER TABLE monthly_entries ADD COLUMN {f} {_col_type(f)}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_overrides (
                month TEXT PRIMARY KEY,
                taux_journalier REAL,
                perdiem_par_jour REAL,
                plafond_frais_banque REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS delta_status (
                month TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (month, category)
            )
            """
        )
        conn.commit()
        _sync(conn)


def migrate_complement_split() -> int:
    """Migration ponctuelle : un mois ne pouvait avoir qu'UN complément
    (cash OU crypto, jamais les deux) via les champs complement_type/
    complement_montant/conversion_crypto_euros/complement_manquant/
    complement_confirme. Bascule chaque mois déjà confirmé vers le champ
    dédié complement_cash_*/complement_crypto_* correspondant, pour que les
    deux puissent désormais coexister le même mois.

    Idempotent sans flag de migration séparé au niveau des LIGNES : une
    entrée migrée a toujours complement_cash_montant OU
    complement_crypto_conversion_euros renseigné (même à 0.0, jamais NULL) —
    le WHERE ci-dessous ne resélectionne donc plus une entrée déjà traitée.
    Colonnes REAL sans DEFAULT (contrairement aux FIELDS_FLAG qui ont
    DEFAULT 0) : une entrée jamais migrée a bien ces deux champs à NULL
    après l'ALTER TABLE de init_db().

    Un flag `migration_complement_split_done` (settings, lu via le cache
    existant de get_setting) évite en plus de refaire cette requête sur
    monthly_entries à CHAQUE rerun de toute l'app pour toujours — appelé au
    démarrage (bootstrap), pas dans une fonction de page. Sûr à poser dès le
    premier passage : aucun autre code de l'app n'écrit plus les anciens
    champs complement_type/complement_montant/conversion_crypto_euros que
    migrate_v1.migrate() (import ponctuel, V2 uniquement), donc rien ne peut
    plus jamais retomber dans le cas que cette migration traite."""
    if get_setting("migration_complement_split_done") == "1":
        return 0
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT month, complement_type, complement_montant, conversion_crypto_euros, complement_manquant "
            "FROM monthly_entries WHERE complement_confirme = 1 "
            "AND complement_cash_montant IS NULL AND complement_crypto_conversion_euros IS NULL"
        )
        rows = _rows_to_dicts(cur)
        for row in rows:
            if (row.get("complement_type") or "").upper() == "CASH":
                conn.execute(
                    "UPDATE monthly_entries SET complement_cash_attendu=1, complement_cash_confirme=1, "
                    "complement_cash_montant=?, complement_cash_manquant=? WHERE month=?",
                    (row.get("complement_montant") or 0.0, row.get("complement_manquant"), row["month"]),
                )
            else:
                conn.execute(
                    "UPDATE monthly_entries SET complement_crypto_attendu=1, complement_crypto_confirme=1, "
                    "complement_crypto_devise=?, complement_crypto_conversion_euros=?, complement_crypto_manquant=? "
                    "WHERE month=?",
                    (
                        row.get("complement_type") or "Autre",
                        row.get("conversion_crypto_euros") or row.get("complement_montant") or 0.0,
                        row.get("complement_manquant"),
                        row["month"],
                    ),
                )
        if rows:
            conn.commit()
            _sync(conn)
    if rows:
        _bust_read_cache()
    set_setting("migration_complement_split_done", "1")
    return len(rows)


def _bust_read_cache() -> None:
    """Invalide les lectures mises en cache après une écriture. Une seule
    connexion Turso synchronisée une fois par rerun (voir `refresh()`) avait
    déjà supprimé le coût réseau par requête, mais chaque page relisait quand
    même la même table plusieurs fois (`year_entries` + `enrich_year`, un
    `get_rate_override`/`get_setting` par mois dans la boucle de projection…)
    — jusqu'à des dizaines de requêtes locales redondantes par clic. Mis en
    cache ici une bonne fois, invalidé explicitement à chaque écriture.

    Ce `.clear()` n'invalide que le cache du PROCESSUS qui écrit — un poste
    local et l'app déployée (Streamlit Cloud ou Vercel) tournent dans des
    processus Python séparés, donc une écriture locale ne peut pas vider le
    cache de l'autre. D'où le `ttl` sur chaque cache ci-dessous : sans lui,
    un changement fait sur une instance ne serait jamais vu par l'autre tant
    que son processus ne redémarre pas. Avec le ttl, l'écart se résorbe tout
    seul en quelques dizaines de secondes.

    Conflit d'écriture (non testé, comportement de Turso lui-même — pas de
    logique applicative ici à couvrir par un test) : si le même mois est
    modifié sur deux instances DANS la même fenêtre de ~30s (ex. un onglet
    oublié ouvert pendant une correction ailleurs), c'est un "dernier
    écrivain gagne" au niveau de la synchro embedded replica de Turso —
    aucune détection de conflit, aucune fusion. Risque faible en usage normal
    (un seul utilisateur, généralement une seule instance active à la fois)
    mais à garder en tête avant de modifier le même mois de deux côtés à
    quelques secondes d'intervalle."""
    get_all_entries.clear()
    get_setting.clear()
    get_rate_override.clear()
    get_rate_overrides_for_year.clear()
    get_delta_statuses.clear()


def is_empty() -> bool:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM monthly_entries").fetchone()[0] == 0


def upsert_entry(month: str, data: dict, source: str = "manuel") -> None:
    columns = ["month"] + ALL_FIELDS + ["source", "updated_at"]
    values = [month] + [data.get(f) for f in ALL_FIELDS] + [source, datetime.now(timezone.utc).isoformat()]
    placeholders = ", ".join("?" for _ in columns)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "month")
    with get_connection() as conn:
        conn.execute(
            f"INSERT INTO monthly_entries ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(month) DO UPDATE SET {update_clause}",
            values,
        )
        conn.commit()
        _sync(conn)
    _bust_read_cache()


def get_entry(month: str) -> dict | None:
    # Non caché : lu une seule fois par page (Saisie), contrairement aux
    # helpers ci-dessous relus plusieurs fois par rerun.
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM monthly_entries WHERE month = ?", (month,))
        rows = _rows_to_dicts(cur)
        return rows[0] if rows else None


@_ttl_cache(30)
def get_all_entries() -> list[dict]:
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM monthly_entries ORDER BY month")
        return _rows_to_dicts(cur)


def delete_entry(month: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM monthly_entries WHERE month = ?", (month,))
        conn.commit()
        _sync(conn)
    _bust_read_cache()


@_ttl_cache(30)
def get_setting(key: str, default: str | None = None) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
        _sync(conn)
    _bust_read_cache()


@_ttl_cache(30)
def get_delta_statuses() -> dict[tuple[str, str], str]:
    """Statut ('traite' / 'a_traiter') par (mois, catégorie) pour la page
    Delta — purement du suivi/acquittement, ne modifie aucun calcul financier."""
    with get_connection() as conn:
        cur = conn.execute("SELECT month, category, status FROM delta_status")
        return {(r["month"], r["category"]): r["status"] for r in _rows_to_dicts(cur)}


def set_delta_status(month: str, category: str, status: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO delta_status (month, category, status, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(month, category) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
            (month, category, status, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        _sync(conn)
    _bust_read_cache()


@_ttl_cache(30)
def get_rate_override(month: str) -> dict | None:
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM rate_overrides WHERE month = ?", (month,))
        rows = _rows_to_dicts(cur)
        return rows[0] if rows else None


@_ttl_cache(30)
def get_rate_overrides_for_year(year: int) -> dict[str, dict]:
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM rate_overrides WHERE month LIKE ?", (f"{year}%",))
        return {r["month"]: r for r in _rows_to_dicts(cur)}


def set_rate_override(month: str, taux_journalier, perdiem_par_jour, plafond_frais_banque) -> None:
    with get_connection() as conn:
        if taux_journalier is None and perdiem_par_jour is None and plafond_frais_banque is None:
            conn.execute("DELETE FROM rate_overrides WHERE month = ?", (month,))
        else:
            conn.execute(
                """
                INSERT INTO rate_overrides (month, taux_journalier, perdiem_par_jour, plafond_frais_banque)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(month) DO UPDATE SET
                    taux_journalier = excluded.taux_journalier,
                    perdiem_par_jour = excluded.perdiem_par_jour,
                    plafond_frais_banque = excluded.plafond_frais_banque
                """,
                (month, taux_journalier, perdiem_par_jour, plafond_frais_banque),
            )
        conn.commit()
        _sync(conn)
    _bust_read_cache()
