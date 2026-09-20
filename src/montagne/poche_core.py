"""Logique métier de "Ma Poche" (cash physique) — isolée du rendu Streamlit
de V2 (qui vit dans poche.py, lequel importe ce module) pour rester
réutilisable telle quelle par l'API FastAPI de V3. Zéro dépendance à
streamlit/pandas ici : tout ce qui manipule st.* ou construit un DataFrame
pour l'affichage reste côté appelant.

Deux ponts délibérés vers le Suivi financier, documentés là où ils lisent des
données : le complément cash confirmé (_cash_confirme_par_mois,
_cash_manquant_par_mois) et sa projection avant confirmation
(_projections_restantes) — pas une dépendance générale, seulement ces
lectures ciblées.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from . import db
from . import estimation as est
from .db import _ttl_cache
from .projections import projection_refs

# 200€ et 500€ : listées ici mais désactivées par défaut (DEFAULT_DENOMINATIONS)
# tant que l'utilisateur n'a pas ces coupures — activables côté UI (get/set_active_denominations).
ALL_DENOMINATIONS = [5, 10, 20, 50, 100, 200, 500]
DEFAULT_DENOMINATIONS = [5, 10, 20, 50, 100]

_initialized = False


def get_active_denominations() -> list[int]:
    """Dénominations activées par l'utilisateur — stocké via les settings
    génériques de db.py (clé/valeur, pas une table dédiée à Poche), en CSV
    d'entiers. Retombe sur DEFAULT_DENOMINATIONS si jamais réglé ou si la
    valeur stockée est invalide/vide."""
    raw = db.get_setting("poche_denominations_actives")
    if not raw:
        return DEFAULT_DENOMINATIONS
    try:
        actifs = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        return DEFAULT_DENOMINATIONS
    actifs = [d for d in actifs if d in ALL_DENOMINATIONS]
    return actifs or DEFAULT_DENOMINATIONS


def set_active_denominations(denoms: list[int]) -> None:
    db.set_setting("poche_denominations_actives", ",".join(str(d) for d in sorted(denoms)))


def init_db() -> None:
    global _initialized
    if _initialized:
        return
    with db.get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS poche_mouvements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                denomination INTEGER NOT NULL,
                quantite INTEGER NOT NULL,
                type TEXT NOT NULL,
                montant REAL NOT NULL,
                source_month TEXT
            )
            """
        )
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(poche_mouvements)").fetchall()}
        if "source_month" not in existing_cols:
            conn.execute("ALTER TABLE poche_mouvements ADD COLUMN source_month TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS poche_versement_ignore (
                month TEXT PRIMARY KEY,
                ignored_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        db._sync(conn)
    _initialized = True


def add_mouvement(denomination: int, quantite: int, type_: str, source_month: str | None = None) -> None:
    # "retour" : partie non dépensée d'une sortie d'argent (ex. tu sors 100€
    # pour dépenser, tu ne dépenses que 10€, tu rentres les 90€ restants).
    # Compte comme "ajout" pour le solde physique, mais PAS comme un ajout
    # historique (ce n'est pas de l'argent nouveau) — il vient réduire la
    # dépense nette à la place. Sans ce 3ème type, un aller-retour de 100€
    # gonflait à la fois "Total ajouté" et "Total dépensé" de façon fictive.
    #
    # source_month : renseigné uniquement quand le mouvement vient d'un
    # versement de complément cash confirmé côté Suivi financier — reste
    # `None` pour toute saisie manuelle normale. C'est le seul lien entre les
    # deux bases, volontairement minimal : un mois "en attente de versement"
    # = complément cash confirmé côté Suivi sans aucun mouvement Poche
    # portant ce source_month.
    assert type_ in ("ajout", "depense", "retour")
    montant = denomination * quantite * (-1 if type_ == "depense" else 1)
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO poche_mouvements (timestamp, denomination, quantite, type, montant, source_month) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), denomination, quantite, type_, montant, source_month),
        )
        conn.commit()
        db._sync(conn)
    get_montants_verses.clear()


def get_ignored_versements() -> set[str]:
    """Mois écartés de la proposition de versement sans qu'aucun mouvement
    Poche n'ait été créé — pour un mois traité autrement (ex. saisi à la
    main) que ce rapprochement automatique n'a pas besoin de continuer à
    signaler."""
    init_db()
    with db.get_connection() as conn:
        cur = conn.execute("SELECT month FROM poche_versement_ignore")
        return {r["month"] for r in db._rows_to_dicts(cur)}


def ignore_versement(month_key: str) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO poche_versement_ignore (month, ignored_at) VALUES (?, ?) "
            "ON CONFLICT(month) DO NOTHING",
            (month_key, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        db._sync(conn)


def unignore_versement(month_key: str) -> None:
    with db.get_connection() as conn:
        conn.execute("DELETE FROM poche_versement_ignore WHERE month = ?", (month_key,))
        conn.commit()
        db._sync(conn)


@_ttl_cache(30)
def get_montants_verses() -> dict[str, float]:
    """Total déjà versé en Poche par mois source (somme des mouvements liés à
    ce mois via source_month) — permet de savoir quels mois sont "en attente"
    (absents de ce dict) sans dupliquer aucune donnée du Suivi financier.
    init_db() explicite ici (pas seulement dans les pages Poche) : appelée
    depuis pending_reminders() côté Suivi financier, potentiellement avant
    qu'aucune page Poche n'ait tourné dans ce process.

    Mis en cache (ttl=30, même pattern que db.py::_bust_read_cache) : c'est
    le SEUL appel qui tournait sur CHAQUE rerun de l'app entière côté V2 (via
    pending_reminders() appelé au niveau module dans app.py, pas dans une
    fonction de page) — identifié comme le point de plus fort levier lors
    d'un audit performance."""
    init_db()
    with db.get_connection() as conn:
        cur = conn.execute(
            "SELECT source_month, SUM(montant) AS total FROM poche_mouvements "
            "WHERE source_month IS NOT NULL GROUP BY source_month"
        )
        return {r["source_month"]: r["total"] for r in db._rows_to_dicts(cur)}


def pending_reminders() -> list[dict]:
    """Rappel "confirmer versement poche" pour À faire — chaque item porte
    son year/month_num pour que l'appelant puisse le rattacher à la bonne
    carte mensuelle sans connaître la logique de Poche.

    Uniquement le mois en cours (comportement historique) : un complément
    cash confirmé un mois passé et jamais versé physiquement n'est
    volontairement pas couvert ici."""
    today = date.today()
    month_key = f"{today.year}-{today.month:02d}"
    entry = next((e for e in db.get_all_entries() if e["month"] == month_key), None)
    if not entry or not entry.get("complement_cash_confirme"):
        return []
    if month_key in get_montants_verses():
        return []
    return [{
        "kind": "poche_versement", "text": "🏦 Confirmer versement poche",
        "year": today.year, "month_num": today.month,
    }]


def repartition_billets(montant: float) -> tuple[list[tuple[int, int]], float]:
    """Répartition gloutonne (plus grosse coupure active d'abord) d'un
    montant en billets. Retourne (liste de (coupure, quantité) non nulles,
    reste non représentable en billets — ex. montant pas multiple de 5€).
    Un montant négatif n'a pas de sens ici (pas de "billets négatifs") — sans
    ce clamp, divmod() sur un nombre négatif fait une division plancher en
    Python (pas une troncature), donc des quantités négatives silencieuses
    plutôt qu'une erreur explicite."""
    reste = int(max(montant, 0.0))
    repartition = []
    for d in sorted(get_active_denominations(), reverse=True):
        qty, reste = divmod(reste, d)
        if qty:
            repartition.append((d, qty))
    couvert = sum(d * q for d, q in repartition)
    return repartition, round(montant - couvert, 2)


def repartir_versement_groupe(qtys: dict[int, int], montants_par_mois: dict[str, float]) -> dict[str, dict[int, int]]:
    """Répartit un même tas de billets (qtys : coupure -> quantité) sur
    plusieurs mois à la fois (versement groupé, ex. juillet + août reçus
    ensemble en une fois) — mois les plus anciens remplis en premier,
    glouton (plus grosses coupures d'abord d'un billet donné), même principe
    que repartition_billets. Peut légèrement dépasser ou manquer le montant
    exact d'un mois si aucune coupure ne tombe pile — même tolérance que le
    formulaire mono-mois, qui affiche déjà un écart sans bloquer."""
    restant = dict(qtys)
    result: dict[str, dict[int, int]] = {}
    for month_key in sorted(montants_par_mois):
        a_couvrir = montants_par_mois[month_key]
        attribution: dict[int, int] = {}
        for d in sorted(restant, reverse=True):
            while a_couvrir >= d and restant.get(d, 0) > 0:
                attribution[d] = attribution.get(d, 0) + 1
                restant[d] -= 1
                a_couvrir -= d
        if attribution:
            result[month_key] = attribution
    return result


def delete_mouvement(mouvement_id: int) -> None:
    with db.get_connection() as conn:
        conn.execute("DELETE FROM poche_mouvements WHERE id = ?", (mouvement_id,))
        conn.commit()
        db._sync(conn)
    get_montants_verses.clear()


def vider_poche() -> None:
    """Retrait total en un coup : une dépense par coupure pour tout le solde
    actuel, pour le cas où on sort tout son cash physique d'un coup plutôt
    que de le saisir coupure par coupure. Contrairement à
    delete_all_mouvements(), l'historique est conservé — ce retrait apparaît
    comme des mouvements "Dépense" normaux, chacun supprimable
    individuellement en cas d'erreur."""
    for denom, qty in get_solde_par_denomination().items():
        if qty > 0:
            add_mouvement(denom, qty, "depense")


def delete_all_mouvements() -> None:
    """Vide tout l'historique — utilisé pour repartir d'une valeur initiale
    saisie manuellement (ex. après des mouvements de test à corriger)."""
    with db.get_connection() as conn:
        conn.execute("DELETE FROM poche_mouvements")
        conn.commit()
        db._sync(conn)
    get_montants_verses.clear()


def get_all_mouvements() -> list[dict]:
    with db.get_connection() as conn:
        cur = conn.execute("SELECT * FROM poche_mouvements ORDER BY timestamp DESC")
        return db._rows_to_dicts(cur)


def get_solde_par_denomination() -> dict[int, int]:
    # Toutes les dénominations connues, pas seulement les actives : si une
    # coupure est désactivée après avoir déjà des mouvements enregistrés, son
    # solde doit rester visible et compté, pas disparaître silencieusement.
    solde = {d: 0 for d in ALL_DENOMINATIONS}
    for r in get_all_mouvements():
        signe = -1 if r["type"] == "depense" else 1
        solde[r["denomination"]] += signe * r["quantite"]
    return solde


def _cash_confirme_par_mois() -> dict[str, float]:
    """Mois (toutes années) où le volet CASH du complément est confirmé côté
    Suivi financier, avec son montant. Le volet crypto (indépendant depuis la
    scission cash/crypto) n'entre jamais ici : Poche ne suit que l'argent
    liquide physique. Seul point de lecture des données de Suivi financier
    dans ce module — délibéré, pas une dépendance générale."""
    return {
        e["month"]: e.get("complement_cash_montant") or 0.0
        for e in db.get_all_entries()
        if e.get("complement_cash_confirme")
    }


def _cash_manquant_par_mois() -> dict[str, float]:
    """Montant encore dû (reçu partiellement) par mois — pas encore reçu,
    donc jamais versable, juste une prévisualisation de répartition pour se
    préparer."""
    return {
        e["month"]: e["complement_cash_manquant"]
        for e in db.get_all_entries()
        if e.get("complement_cash_confirme") and e.get("complement_cash_manquant")
    }


def _projections_restantes(year: int) -> dict[str, float]:
    """Complément cash PROJETÉ (pas encore confirmé) pour les mois restants
    de l'année, du mois en cours à décembre — pour anticiper les prochains
    versements Poche avant même la confirmation côté Suivi financier. Exclut
    les mois déjà confirmés côté cash et les mois où le cash n'est
    explicitement pas attendu. Le montant projeté est net de toute crypto
    déjà confirmée ce mois-là (complement_attendu est une projection
    globale, pas décomposée par volet — une crypto confirmée en couvre une
    part réelle)."""
    all_entries = db.get_all_entries()
    by_month = {e["month"]: e for e in all_entries}
    today = date.today()
    resultats = {}
    for m in range(1, 13):
        month_key = f"{year}-{m:02d}"
        entry = by_month.get(month_key, {"month": month_key})
        # Un mois révolu ne doit sortir de la projection que s'il est
        # réellement clos (mois_cloture) — pas seulement parce que le
        # calendrier a tourné. Sinon un mois fini mais dont le complément
        # n'est confirmé que mi-mois suivant disparaît de la vue Versement
        # pendant tout cet intervalle : ni confirmé, ni manquant, ni "à
        # venir". Vécu en pratique avec août 2026, dont le complément (297€)
        # n'était censé être confirmé qu'à partir du 15/09 mais s'est
        # volatilisé dès le 1er septembre.
        if (year, m) < (today.year, today.month) and entry.get("mois_cloture"):
            continue
        if entry.get("complement_cash_confirme"):
            continue
        if not est.crypto_cash_applicable(entry):
            continue
        # NULL (jamais réglé) -> considéré attendu par défaut, comme en
        # Saisie mensuelle et dans la relance À faire (voir db._NO_DEFAULT_FLAGS).
        cash_attendu_raw = entry.get("complement_cash_attendu")
        cash_attendu = True if cash_attendu_raw is None else bool(cash_attendu_raw)
        if not cash_attendu:
            continue
        year_entries_list = [e for e in all_entries if e["month"].startswith(str(year))]
        refs = projection_refs(year_entries_list, month_key)
        # complement_attendu est UNE projection globale, jamais décomposée
        # par volet (voir estimation.py) — si de la crypto a déjà été
        # confirmée ce mois-là, elle en couvre une part réelle : il faut la
        # soustraire, sinon le cash encore attendu reste affiché à son
        # montant plein alors qu'une partie (ou la totalité) est déjà
        # arrivée par un autre canal. Vécu en pratique : juillet 2027, 800 €
        # de crypto confirmés sur un complément total de 4 914 € — la
        # projection cash affichait encore 4 914 € au lieu de 4 114 €.
        montant_total = est.complement_attendu(entry, refs)
        deja_recu_crypto = (entry.get("complement_crypto_conversion_euros") or 0.0) if entry.get("complement_crypto_confirme") else 0.0
        montant = max(0.0, montant_total - deja_recu_crypto)
        if montant > 0.01:
            resultats[month_key] = montant
    return resultats
