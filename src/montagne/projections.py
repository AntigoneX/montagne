"""Résolution des taux de projection et enrichissement annuel — SOURCE UNIQUE
pour Suivi financier (app.py) et Poche — Versement (poche.py).

Avant l'extraction dans ce module, poche.py maintenait sa propre copie de
cette logique (`_projection_refs_pour_mois`, avec ce commentaire : "Dupliqué
(pas importé) pour éviter un import circulaire — app.py importe déjà poche —
à garder en synchro si cette logique change côté Suivi financier."). Deux
implémentations séparées du même calcul, à synchroniser manuellement à
chaque changement, est exactement le genre de risque qui fait qu'un même
mois affiche un chiffre différent selon la page consultée. Ce module casse
le cycle d'import (app.py -> poche.py) en n'important ni l'un ni l'autre :
app.py et poche.py importent tous les deux CE module, jamais l'inverse.

Déplacé tel quel dans ce package séparé (2026-09) : seuls les imports
`import db` / `import estimation as est` sont devenus des imports relatifs
de package (`from . import ...`) — aucune autre ligne changée.
"""
from __future__ import annotations

from . import db
from . import estimation as est


def year_entries(year: int) -> dict[str, dict]:
    """Les 12 mois de `year`, chacun garanti présent (mois sans donnée saisie
    = un dict "vide" avec TOUS les champs habituels à None, pas juste
    {"month": ...}) — évite aux appelants de gérer des trous dans la
    séquence. Champs complets même pour un mois vide : sinon, une année sans
    aucune entrée réelle produit une liste de dicts qui ne partagent que la
    clé "month", et pd.DataFrame(...) ne crée alors AUCUNE des autres
    colonnes (ex. "frais_mileage") — KeyError en aval dès qu'une page y
    accède, vécu en prod sur Totaux annuels pour une année sans données."""
    by_month = {e["month"]: e for e in db.get_all_entries() if e["month"].startswith(str(year))}
    vide = dict.fromkeys(db.ALL_FIELDS)
    return {
        f"{year}-{m:02d}": by_month.get(f"{year}-{m:02d}") or {"month": f"{year}-{m:02d}", **vide}
        for m in range(1, 13)
    }


def resolve_rates(month_key: str) -> tuple[float | None, float | None, float | None]:
    """Exception mensuelle si réglée, sinon réglage annuel, sinon None (charge
    à `build_projection_refs` de retomber sur la moyenne des mois clôturés ou
    un fallback fixe — voir estimation.py)."""
    year = month_key.split("-")[0]
    override = db.get_rate_override(month_key) or {}

    def _resolve(field: str, default_key: str):
        val = override.get(field)
        if val is None:
            default_val = db.get_setting(default_key)
            val = float(default_val) if default_val is not None else None
        return val

    return (
        _resolve("taux_journalier", f"taux_journalier_{year}"),
        _resolve("perdiem_par_jour", f"perdiem_par_jour_{year}"),
        _resolve("plafond_frais_banque", f"plafond_frais_banque_{year}"),
    )


def projection_refs(entries: list[dict], month_key: str) -> est.ProjectionRefs:
    taux, perdiem, plafond = resolve_rates(month_key)
    return est.build_projection_refs(entries, taux_journalier=taux, perdiem_par_jour=perdiem, plafond_frais_banque=plafond)


def enrich_year(year: int) -> list[dict]:
    entries = list(year_entries(year).values())
    return [est.enrich(e, projection_refs(entries, e["month"])) for e in entries]
