"""Moteur de projection / estimation pour le suivi financier freelance.

Principe général : chaque montant a une valeur "effective" — la valeur réelle
si elle est confirmée (flag correspondant à 1), sinon une estimation dérivée
des jours travaillés et de ratios €/jour calculés sur les mois déjà clôturés
de l'année (voir `build_projection_refs`). Cette estimation se remplace
automatiquement par la vraie valeur dès qu'elle est confirmée — pas besoin de
distinguer "mois réel" / "mois projeté" pour calculer un total plausible.

Règles métier (déduites du fonctionnement réel avec l'agence) :
- Le complément cash/crypto correspond au perdiem attendu qui dépasse le
  plafond de frais versés en banque, moins 10% de commission d'agence.
- Ce mécanisme n'existe qu'à partir de juin 2026.
- Le daily allowance suit une règle fixe : 50€/jour travaillé.
"""
from __future__ import annotations

from dataclasses import dataclass

AGENCY_COMMISSION_RATE = 0.10
CRYPTO_CASH_START_MONTH = "2026-06"
DAILY_ALLOWANCE_PAR_JOUR = 50.0

MILEAGE_PAR_JOUR_FALLBACK = 150.0
PERDIEM_PAR_JOUR_FALLBACK = 260.0
TAUX_JOURNALIER_FALLBACK = 600.0
PLAFOND_FRAIS_BANQUE_FALLBACK = 3800.0
SALAIRE_NET_MOYEN_FALLBACK = 2200.0


@dataclass(frozen=True)
class ProjectionRefs:
    mileage_par_jour: float = MILEAGE_PAR_JOUR_FALLBACK
    perdiem_par_jour: float = PERDIEM_PAR_JOUR_FALLBACK
    taux_journalier: float = TAUX_JOURNALIER_FALLBACK
    plafond_frais_banque: float = PLAFOND_FRAIS_BANQUE_FALLBACK
    salaire_net_moyen: float = SALAIRE_NET_MOYEN_FALLBACK


def _num(v) -> float:
    return v or 0.0


def is_salaire_confirme(entry: dict) -> bool:
    return bool(entry.get("salaire_confirme"))


def is_frais_confirmes(entry: dict) -> bool:
    return bool(entry.get("frais_confirmes"))


def is_complement_confirme(entry: dict) -> bool:
    """Un mois peut avoir un complément cash, crypto, ou les deux à la fois
    (voir complement_reel) — "confirmé" veut dire que chaque volet
    effectivement attendu (case cochée en Saisie mensuelle) l'est. Un volet
    jamais attendu (case décochée) ne bloque pas la confirmation globale."""
    cash_ok = not entry.get("complement_cash_attendu") or bool(entry.get("complement_cash_confirme"))
    crypto_ok = not entry.get("complement_crypto_attendu") or bool(entry.get("complement_crypto_confirme"))
    return cash_ok and crypto_ok


def is_mois_clos(entry: dict) -> bool:
    return bool(entry.get("mois_cloture"))


def crypto_cash_applicable(entry: dict) -> bool:
    month = entry.get("month")
    return bool(month) and month >= CRYPTO_CASH_START_MONTH


def _complement_flag(entry: dict, field: str) -> bool:
    """NULL (jamais réglé, mois futur pas encore saisi) -> considéré attendu
    par défaut, cohérent avec le champ complement_cash_attendu lui-même (voir
    db._NO_DEFAULT_FLAGS) ; 0 explicite (case décochée à la main en Saisie
    mensuelle) -> vraiment pas attendu."""
    v = entry.get(field)
    return True if v is None else bool(v)


def complement_possible(entry: dict) -> bool:
    """Un complément cash/crypto n'a de sens que si le mécanisme existe pour
    ce mois (crypto_cash_applicable) ET qu'au moins un des deux volets est
    effectivement attendu — sinon (les deux explicitement décochés, ex. tout
    le perdiem routé en banque ce mois-là) reste_avant_commission/
    commission_agence/complement_attendu ne doivent rien renvoyer, même avant
    confirmation des frais banque : sinon un perdiem au-dessus du plafond
    fait ressortir un complément "attendu" non nul (carte Projection, page
    Complément cash/crypto attendu, suggestion de montant en Saisie
    mensuelle) alors que l'utilisateur a explicitement dit ne rien attendre
    ce mois-là — retour direct de l'utilisateur, suite du bug de la carte
    "Frais banque" corrigé plus tôt dans la session."""
    return crypto_cash_applicable(entry) and (
        _complement_flag(entry, "complement_cash_attendu") or _complement_flag(entry, "complement_crypto_attendu")
    )


def _weighted_ratio(entries: list[dict], field: str, fallback: float, require_flag: str | None = None) -> float:
    total_val, total_jours = 0.0, 0.0
    for e in entries:
        if not is_mois_clos(e):
            continue
        if require_flag and not e.get(require_flag):
            continue
        if e.get(field) is None or not e.get("jours_travailles"):
            continue
        total_val += e[field]
        total_jours += e["jours_travailles"]
    return total_val / total_jours if total_jours > 0 else fallback


def _average(entries: list[dict], field: str, fallback: float) -> float:
    vals = [e[field] for e in entries if is_mois_clos(e) and e.get(field) is not None]
    return sum(vals) / len(vals) if vals else fallback


def build_projection_refs(
    entries: list[dict],
    taux_journalier: float | None = None,
    perdiem_par_jour: float | None = None,
    plafond_frais_banque: float | None = None,
) -> ProjectionRefs:
    """Ratios de référence dérivés des mois clôturés de `entries`, ou des
    réglages utilisateur (`taux_journalier`, `perdiem_par_jour`,
    `plafond_frais_banque`) quand ils sont fournis."""
    salaire_net_moyen = _average(entries, "salaire_net_attendu", None) or _average(
        entries, "salaire_net_recu", SALAIRE_NET_MOYEN_FALLBACK
    )
    return ProjectionRefs(
        mileage_par_jour=_weighted_ratio(entries, "frais_mileage", MILEAGE_PAR_JOUR_FALLBACK, "frais_confirmes"),
        perdiem_par_jour=(
            perdiem_par_jour if perdiem_par_jour is not None
            else _weighted_ratio(entries, "perdiem_attendu", PERDIEM_PAR_JOUR_FALLBACK)
        ),
        taux_journalier=(
            taux_journalier if taux_journalier is not None
            else _weighted_ratio(entries, "facture", TAUX_JOURNALIER_FALLBACK)
        ),
        plafond_frais_banque=(
            plafond_frais_banque if plafond_frais_banque is not None else PLAFOND_FRAIS_BANQUE_FALLBACK
        ),
        salaire_net_moyen=salaire_net_moyen,
    )


def facture_effective(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    if entry.get("facture_auto"):
        return refs.taux_journalier * _num(entry.get("jours_travailles"))
    return _num(entry.get("facture"))


def perdiem_effective(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    if entry.get("perdiem_auto"):
        return refs.perdiem_par_jour * _num(entry.get("jours_travailles"))
    return _num(entry.get("perdiem_attendu"))


def salaire_attendu_effective(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    if entry.get("salaire_attendu_auto"):
        return refs.salaire_net_moyen
    return _num(entry.get("salaire_net_attendu"))


def frais_allowance_effective(entry: dict) -> float:
    if is_frais_confirmes(entry):
        return _num(entry.get("frais_allowance"))
    return DAILY_ALLOWANCE_PAR_JOUR * _num(entry.get("jours_travailles"))


def frais_mileage_effective(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    if is_frais_confirmes(entry):
        return _num(entry.get("frais_mileage"))
    return refs.mileage_par_jour * _num(entry.get("jours_travailles"))


def total_frais_banque_reel(entry: dict) -> float:
    """Frais banque réellement enregistrés, sans estimation (0 si non confirmés)."""
    if not is_frais_confirmes(entry):
        return 0.0
    return _num(entry.get("frais_allowance")) + _num(entry.get("frais_expense_batch")) + _num(entry.get("frais_mileage"))


def total_frais_banque_effective(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    """Frais banque à utiliser dans les calculs de "meilleur total actuel"
    (net_percu_total, suggestion de complément restant, versement Poche) :
    réels si confirmés, sinon le perdiem attendu plafonné (le reste part en
    cash/crypto). Ne PAS utiliser comme référence "attendu" dans une
    comparaison réel vs attendu (net_attendu_total, écarts) : bascule sur le
    réel dès frais_confirmes, donc une fois confirmé un mois où l'agence a
    envoyé plus que prévu en banque, cette valeur EST déjà le réel — la
    comparer au réel donnerait toujours un écart nul. Voir frais_banque_projete."""
    if is_frais_confirmes(entry):
        return total_frais_banque_reel(entry)
    return min(perdiem_effective(entry, refs), refs.plafond_frais_banque)


def frais_banque_projete(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    """Projection frais banque (perdiem attendu plafonné) — TOUJOURS, même une
    fois frais_confirmes, contrairement à total_frais_banque_effective qui
    bascule sur le réel dès confirmation. Sert UNIQUEMENT d'affichage de
    référence isolé (carte "Frais banque (plafonnés)" du panneau Projection,
    ligne "Frais banque" de la page Delta) — jamais dans un total qui doit
    rester cohérent avec le reste (net_attendu_total, écart mensuel, Poche,
    etc. utilisent tous total_frais_banque_effective, en phase avec le réel
    dès qu'il est connu : sinon un virement bancaire plus gros que prévu ferait
    ressortir un complément "manquant" qui en réalité ne l'est pas — retour
    direct de l'utilisateur)."""
    return min(perdiem_effective(entry, refs), refs.plafond_frais_banque)


def reste_avant_commission(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    if not complement_possible(entry):
        return 0.0
    return max(perdiem_effective(entry, refs) - total_frais_banque_effective(entry, refs), 0.0)


def commission_agence(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    return reste_avant_commission(entry, refs) * AGENCY_COMMISSION_RATE


def complement_attendu(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    if not complement_possible(entry):
        return 0.0
    return reste_avant_commission(entry, refs) - commission_agence(entry, refs)


def complement_reel(entry: dict) -> float:
    """Somme des volets cash et crypto confirmés — les deux peuvent
    coexister le même mois (ex. une partie versée en cash, une autre en
    USDT). Pour la crypto, seule la conversion en € compte (pas le montant
    natif reçu, purement informatif)."""
    total = 0.0
    if entry.get("complement_cash_confirme"):
        total += _num(entry.get("complement_cash_montant"))
    if entry.get("complement_crypto_confirme"):
        total += _num(entry.get("complement_crypto_conversion_euros"))
    return total


def total_avantages(entry: dict) -> float:
    return _num(entry.get("sodexo")) + _num(entry.get("pecule_vacances")) + _num(entry.get("treizieme_mois"))


def total_frais_effectif(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    """Frais banque + complément, réels si confirmés sinon estimés — pour le
    net perçu "meilleure estimation actuelle". Tout-ou-rien plutôt qu'un
    mélange réel/projeté par volet : si cash ET crypto sont attendus mais un
    seul est confirmé, complement_attendu (projection globale, jamais
    décomposée par volet) sert de repli pour le mois entier — pas de façon
    fiable de soustraire "juste la part cash" d'une projection qui ne
    distingue pas les deux."""
    frais_banque = total_frais_banque_effective(entry, refs)
    complement = complement_reel(entry) if is_complement_confirme(entry) else complement_attendu(entry, refs)
    return frais_banque + complement


def net_percu_total(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float | None:
    if not is_salaire_confirme(entry):
        return None
    return _num(entry.get("salaire_net_recu")) + total_frais_effectif(entry, refs) + total_avantages(entry)


def net_attendu_total(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float:
    """Ce qui était/est attendu pour ce mois — toujours calculable, sert de
    référence de comparaison une fois le réel connu. Volontairement "vivant"
    (effectif, pas figé) pour frais banque/complément quand un complément est
    réellement possible ce mois-ci (voir complement_possible) : une fois les
    frais banque confirmés, le complément restant se réévalue à partir du
    réel déjà reçu (retour direct de l'utilisateur : sinon un complément déjà
    partiellement couvert par un virement bancaire plus élevé que prévu
    ressort comme "manquant" alors qu'il ne l'est pas — incohérent avec
    complement_cash_manquant, qui utilise déjà cette même logique vivante).
    Mais si aucun complément n'est possible ce mois, il ne faut PAS passer
    par total_frais_banque_effective : celui-ci bascule sur le réel dès
    frais_confirmes, donc un virement bancaire imprévu au-delà du plafond
    habituel s'y substituerait silencieusement à l'attendu au lieu d'être
    visible comme un écart (retour direct de l'utilisateur). complement_attendu
    est déjà gaté par complement_possible (renvoie 0), mais ce terme-ci ne
    suffit pas seul : c'est bien total_frais_banque_effective, pas
    complement_attendu, qui absorbe silencieusement le réel dans ce
    scénario."""
    if not complement_possible(entry):
        return salaire_attendu_effective(entry, refs) + perdiem_effective(entry, refs) + total_avantages(entry)
    return (
        salaire_attendu_effective(entry, refs)
        + total_frais_banque_effective(entry, refs)
        + complement_attendu(entry, refs)
        + total_avantages(entry)
    )


def taux_conversion(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> float | None:
    facture = facture_effective(entry, refs)
    net = net_percu_total(entry, refs)
    if not facture or net is None:
        return None
    return net / facture * 100


def jours_conges_total(entry: dict) -> float:
    return _num(entry.get("jours_conges_payes")) + _num(entry.get("jours_feries")) + _num(entry.get("jours_sans_solde"))


def enrich(entry: dict, refs: ProjectionRefs = ProjectionRefs()) -> dict:
    """Retourne l'entrée augmentée de tous les champs calculés."""
    e = dict(entry)
    e["salaire_confirme_"] = is_salaire_confirme(entry)
    e["mois_clos_"] = is_mois_clos(entry)
    e["statut"] = "réel" if is_mois_clos(entry) else "projeté"
    e["facture_eff"] = facture_effective(entry, refs)
    e["perdiem_eff"] = perdiem_effective(entry, refs)
    e["salaire_attendu_eff"] = salaire_attendu_effective(entry, refs)
    e["frais_allowance_eff"] = frais_allowance_effective(entry)
    e["frais_mileage_eff"] = frais_mileage_effective(entry, refs)
    e["frais_banque_reel"] = total_frais_banque_reel(entry)
    e["frais_banque_eff"] = total_frais_banque_effective(entry, refs)
    e["frais_banque_projete"] = frais_banque_projete(entry, refs)
    e["reste_avant_commission"] = reste_avant_commission(entry, refs)
    e["commission_agence"] = commission_agence(entry, refs)
    e["complement_attendu"] = complement_attendu(entry, refs)
    # Toujours la somme réelle des volets confirmés (jamais masquée à None
    # si un seul des deux est fait) — sinon un complément crypto déjà reçu
    # disparaît de l'affichage tant que le volet cash, coché "attendu" par
    # défaut, n'est pas lui aussi confirmé. `is_complement_confirme` reste
    # utilisé ailleurs (ex. total_frais_effectif) où le tout-ou-rien a un
    # sens différent : ne pas mélanger réel partiel et projection globale.
    e["complement_reel"] = complement_reel(entry)
    e["complement_confirme_"] = is_complement_confirme(entry)
    e["total_avantages"] = total_avantages(entry)
    e["net_percu_total"] = net_percu_total(entry, refs)
    e["net_attendu_total"] = net_attendu_total(entry, refs)
    e["taux_conversion_pct"] = taux_conversion(entry, refs)
    e["crypto_cash_applicable"] = crypto_cash_applicable(entry)
    e["jours_conges_total"] = jours_conges_total(entry)
    return e
