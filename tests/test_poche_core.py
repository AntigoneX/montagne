"""Tests sur poche_core.py : solde par dénomination, répartition en billets, et
surtout le pont Suivi financier -> Poche (_cash_confirme_par_mois,
_cash_manquant_par_mois, _projections_restantes) — le point exact où un
même complément cash a déjà affiché des montants incohérents d'une page à
l'autre (voir test_projections.py pour la non-régression du bug d'août
2026 : un mois révolu mais pas clôturé qui disparaissait de la page)."""
from datetime import date

from montagne import db
from montagne import poche_core


# --------------------------------------------------------- Solde / billets --
def test_solde_par_denomination_ajout_et_depense():
    poche_core.add_mouvement(50, 3, "ajout")
    poche_core.add_mouvement(10, 2, "depense")
    solde = poche_core.get_solde_par_denomination()
    assert solde[50] == 3
    assert solde[10] == -2
    assert solde[20] == 0  # dénomination jamais touchée -> 0, pas absente


def test_solde_retour_compte_comme_un_ajout_physique():
    poche_core.add_mouvement(100, 1, "depense")
    poche_core.add_mouvement(100, 1, "retour")
    assert poche_core.get_solde_par_denomination()[100] == 0


def test_repartition_billets_gloutonne_plus_grosses_coupures_d_abord():
    poche_core.set_active_denominations([5, 10, 20, 50, 100])
    repartition, reste = poche_core.repartition_billets(365.0)
    assert repartition == [(100, 3), (50, 1), (10, 1), (5, 1)]
    assert reste == 0.0


def test_repartition_billets_montant_pas_multiple_de_5():
    poche_core.set_active_denominations([5, 10, 20, 50, 100])
    repartition, reste = poche_core.repartition_billets(297.0)
    couvert = sum(d * q for d, q in repartition)
    assert couvert + reste == 297.0
    assert round(reste, 2) == 2.0  # 297 = 2*100+1*50+2*20+1*5 + 2 restants


def test_repartition_billets_respecte_les_denominations_actives():
    poche_core.set_active_denominations([10, 50])  # pas de 5, 20, 100
    repartition, reste = poche_core.repartition_billets(65.0)
    assert repartition == [(50, 1), (10, 1)]
    assert reste == 5.0  # pas de coupure de 5€ active pour couvrir le solde


def test_repartition_billets_montant_negatif_ne_produit_pas_de_quantite_negative():
    # divmod() sur un nombre négatif fait une division plancher en Python
    # (divmod(-5, 20) == (-1, 15)), pas une troncature — sans clamp, un
    # montant négatif produirait une quantité de billets négative, silencieuse.
    poche_core.set_active_denominations([5, 10, 20, 50, 100])
    repartition, reste = poche_core.repartition_billets(-40.0)
    assert repartition == []
    assert all(q >= 0 for _, q in repartition)
    assert reste == -40.0  # l'anomalie ressort dans le reste, pas dans les quantités


def test_repartir_versement_groupe_remplit_le_mois_le_plus_ancien_d_abord():
    # 450€ (100x4+50x1) -> juillet (300€) puis août (150€), jamais l'inverse
    # même si août est passé en premier dans le dict.
    qtys = {100: 4, 50: 1}
    result = poche_core.repartir_versement_groupe(qtys, {"2026-08": 150.0, "2026-07": 300.0})
    assert set(result) == {"2026-07", "2026-08"}
    juillet_total = sum(d * q for d, q in result["2026-07"].items())
    aout_total = sum(d * q for d, q in result["2026-08"].items())
    assert juillet_total == 300.0
    assert aout_total == 150.0


def test_repartir_versement_groupe_ne_double_compte_pas_les_billets():
    qtys = {100: 8, 50: 1, 20: 1, 10: 1}
    result = poche_core.repartir_versement_groupe(qtys, {"2026-07": 614.0, "2026-08": 270.0})
    total_distribue = sum(d * q for mois in result.values() for d, q in mois.items())
    assert total_distribue <= sum(d * q for d, q in qtys.items())


def test_repartir_versement_groupe_billets_insuffisants_laisse_le_dernier_mois_partiel():
    qtys = {100: 6}  # 600€ pour 614€ (juillet) + 270€ (août) = 884€ attendus
    result = poche_core.repartir_versement_groupe(qtys, {"2026-07": 614.0, "2026-08": 270.0})
    total_distribue = sum(d * q for mois in result.values() for d, q in mois.items())
    assert total_distribue == 600.0
    assert "2026-08" not in result  # rien reste pour août, aucune coupure de 100 en dessous de 14


def test_repartir_versement_groupe_tolere_un_montant_non_representable():
    # 614€ (juillet) n'est pas un multiple de 5 : le glouton fait de son
    # mieux (610€, comme repartition_billets tolère déjà un "reste" sur un
    # seul mois) plutôt que d'échouer ou de sur-attribuer sur août.
    qtys = {100: 8, 50: 1, 20: 1, 10: 1}
    result = poche_core.repartir_versement_groupe(qtys, {"2026-07": 614.0, "2026-08": 270.0})
    juillet_total = sum(d * q for d, q in result["2026-07"].items())
    assert juillet_total == 610.0


def test_get_montants_verses_agrege_par_mois_source():
    poche_core.add_mouvement(100, 2, "ajout", source_month="2026-07")
    poche_core.add_mouvement(50, 1, "ajout", source_month="2026-07")
    poche_core.add_mouvement(20, 1, "ajout")  # saisie manuelle, pas de source_month
    verses = poche_core.get_montants_verses()
    assert verses == {"2026-07": 250.0}


def test_ignore_versement_puis_unignore():
    assert poche_core.get_ignored_versements() == set()
    poche_core.ignore_versement("2026-07")
    assert poche_core.get_ignored_versements() == {"2026-07"}
    poche_core.unignore_versement("2026-07")
    assert poche_core.get_ignored_versements() == set()


# --------------------------------------------------- Pont Suivi financier --
def _entry(month, **overrides):
    base = {
        "month": month, "jours_travailles": 20.0,
        "complement_cash_attendu": 0, "complement_cash_confirme": 0,
        "complement_cash_montant": None, "complement_cash_manquant": None,
        "complement_crypto_attendu": 0, "complement_crypto_confirme": 0,
        "complement_crypto_devise": None, "complement_crypto_conversion_euros": None,
        "mois_cloture": 0, "perdiem_auto": 1, "frais_confirmes": 0,
    }
    base.update(overrides)
    return base


def test_cash_confirme_par_mois_filtre_sur_confirmation_cash():
    db.upsert_entry("2026-07", _entry("2026-07", complement_cash_confirme=1, complement_cash_montant=500.0))
    db.upsert_entry("2026-08", _entry("2026-08", complement_cash_confirme=0, complement_cash_montant=999.0))  # pas confirmé
    db.upsert_entry("2026-09", _entry("2026-09", complement_crypto_confirme=1, complement_crypto_conversion_euros=999.0))  # crypto, pas cash
    assert poche_core._cash_confirme_par_mois() == {"2026-07": 500.0}


def test_cash_confirme_par_mois_ignore_le_volet_crypto_du_meme_mois():
    # Un mois peut avoir cash ET crypto confirmés à la fois — Poche ne doit
    # remonter que la part cash, le crypto n'a rien à voir avec l'argent
    # liquide physique.
    db.upsert_entry("2026-07", _entry(
        "2026-07", complement_cash_confirme=1, complement_cash_montant=500.0,
        complement_crypto_confirme=1, complement_crypto_conversion_euros=250.0,
    ))
    assert poche_core._cash_confirme_par_mois() == {"2026-07": 500.0}


def test_cash_manquant_par_mois():
    db.upsert_entry("2026-07", _entry(
        "2026-07", complement_cash_confirme=1, complement_cash_manquant=615.0,
    ))
    db.upsert_entry("2026-08", _entry(
        "2026-08", complement_cash_confirme=1, complement_cash_manquant=None,
    ))  # rien manquant
    assert poche_core._cash_manquant_par_mois() == {"2026-07": 615.0}


def test_projections_restantes_exclut_un_mois_deja_confirme_ou_crypto():
    today = date.today()
    year = today.year
    mk_confirme = f"{year}-{today.month:02d}"
    db.upsert_entry(mk_confirme, _entry(mk_confirme, complement_cash_confirme=1))
    projections = poche_core._projections_restantes(year)
    assert mk_confirme not in projections


# ------------------------------------------------------ pending_reminders --
# Extrait de app.py::_pending_months() (refactor de session : chaque module
# porte désormais ses propres rappels) — utilise date.today() directement
# (comme test_projections_restantes_exclut_un_mois_deja_confirme_ou_crypto
# ci-dessus) plutôt qu'un mock, aucune garde type MIN_ANNEE ici contrairement
# à temps.py.
def test_pending_reminders_versement_manquant_pour_le_mois_en_cours():
    today = date.today()
    mk = f"{today.year}-{today.month:02d}"
    db.upsert_entry(mk, _entry(mk, complement_cash_confirme=1, complement_cash_montant=500.0))
    assert poche_core.pending_reminders() == [{
        "kind": "poche_versement", "text": "🏦 Confirmer versement poche",
        "year": today.year, "month_num": today.month,
    }]


def test_pending_reminders_vide_si_deja_verse():
    today = date.today()
    mk = f"{today.year}-{today.month:02d}"
    db.upsert_entry(mk, _entry(mk, complement_cash_confirme=1, complement_cash_montant=500.0))
    poche_core.add_mouvement(100, 5, "ajout", source_month=mk)
    assert poche_core.pending_reminders() == []


def test_pending_reminders_vide_si_cash_pas_confirme():
    today = date.today()
    mk = f"{today.year}-{today.month:02d}"
    db.upsert_entry(mk, _entry(mk, complement_cash_confirme=0))
    assert poche_core.pending_reminders() == []


def test_pending_reminders_vide_si_aucune_entree_pour_le_mois_en_cours():
    assert poche_core.pending_reminders() == []


def test_projections_restantes_regression_mois_revolu_non_cloture_reste_visible():
    """Non-régression du bug réel d'août 2026 : un mois révolu (calendrier
    passé) mais PAS clôturé ne doit pas disparaître des projections tant
    qu'il n'est pas explicitement clos — sa confirmation peut légitimement
    arriver après la fin du mois calendaire (voir la relance À faire, mi-mois
    suivant)."""
    today = date.today()
    if today.month == 1:
        return  # test non pertinent en janvier (pas de mois précédent cette année)
    year = today.year
    mois_revolu = today.month - 1
    mk = f"{year}-{mois_revolu:02d}"
    # Dict brut (pas _entry(), qui force complement_cash_attendu=0) : ici on
    # veut justement le laisser NULL ("jamais réglé"), pas explicitement
    # décoché — voir test_projections_restantes_inclut_cash_par_defaut_si_jamais_regle.
    db.upsert_entry(mk, {"jours_travailles": 20.0, "mois_cloture": 0, "perdiem_auto": 1})
    projections = poche_core._projections_restantes(year)
    assert mk in projections
    assert projections[mk] > 0


def test_projections_restantes_exclut_un_mois_revolu_et_cloture():
    today = date.today()
    if today.month == 1:
        return
    year = today.year
    mois_revolu = today.month - 1
    mk = f"{year}-{mois_revolu:02d}"
    db.upsert_entry(mk, _entry(mk, jours_travailles=20.0, mois_cloture=1))
    projections = poche_core._projections_restantes(year)
    assert mk not in projections


def test_projections_restantes_exclut_cash_explicitement_pas_attendu():
    # complement_cash_attendu=0 explicite (pas juste absent/NULL) : le
    # cash n'est pas attendu ce mois-ci, rien à anticiper pour Poche.
    today = date.today()
    year = today.year
    mk = f"{year}-{today.month:02d}"
    db.upsert_entry(mk, _entry(mk, complement_cash_attendu=0))
    projections = poche_core._projections_restantes(year)
    assert mk not in projections


def test_projections_restantes_inclut_cash_par_defaut_si_jamais_regle():
    # complement_cash_attendu jamais réglé (absent du dict -> NULL en base,
    # pas _entry() qui le force à 0) : traité comme attendu par défaut, même
    # logique que la relance À faire et la Saisie mensuelle.
    today = date.today()
    year = today.year
    mk = f"{year}-{today.month:02d}"
    db.upsert_entry(mk, {"jours_travailles": 20.0, "perdiem_auto": 1})
    projections = poche_core._projections_restantes(year)
    assert mk in projections


# --- Régression : la crypto déjà confirmée doit réduire la projection cash --
# (vécu en pratique : juillet 2027, 800 € de crypto confirmés sur un
# complément total de 4 914 € — la projection cash affichait encore les
# 4 914 € pleins au lieu des 4 114 € réellement encore possibles en cash.)
def test_projections_restantes_reduite_par_la_crypto_deja_confirmee():
    today = date.today()
    year = today.year
    mk = f"{year}-{today.month:02d}"
    # jours_travailles=20 + perdiem_auto=1 + refs par défaut -> complement_attendu = 1260€
    # (260€/j * 20j = 5200, plafonné à 3800, reste 1400, -10% commission = 1260€)
    db.upsert_entry(mk, {
        "jours_travailles": 20.0, "perdiem_auto": 1,
        "complement_crypto_confirme": 1, "complement_crypto_conversion_euros": 500.0,
    })
    projections = poche_core._projections_restantes(year)
    assert projections[mk] == 1260.0 - 500.0


def test_projections_restantes_exclue_si_crypto_couvre_deja_tout():
    today = date.today()
    year = today.year
    mk = f"{year}-{today.month:02d}"
    # complement_attendu = 1260€, crypto confirmée = 1500€ (couvre et dépasse) -> plus rien à projeter en cash.
    db.upsert_entry(mk, {
        "jours_travailles": 20.0, "perdiem_auto": 1,
        "complement_crypto_confirme": 1, "complement_crypto_conversion_euros": 1500.0,
    })
    projections = poche_core._projections_restantes(year)
    assert mk not in projections


def test_projections_restantes_ignore_la_crypto_pas_confirmee():
    # Un montant de conversion crypto renseigné mais PAS confirmé (juste
    # attendu) ne doit rien retrancher — sinon une saisie en cours,
    # pas encore validée, fausserait déjà la projection cash.
    today = date.today()
    year = today.year
    mk = f"{year}-{today.month:02d}"
    db.upsert_entry(mk, {
        "jours_travailles": 20.0, "perdiem_auto": 1,
        "complement_crypto_attendu": 1, "complement_crypto_confirme": 0, "complement_crypto_conversion_euros": 500.0,
    })
    projections = poche_core._projections_restantes(year)
    assert projections[mk] == 1260.0
