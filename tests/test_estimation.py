"""Tests sur le moteur de calcul (estimation.py) — le cœur métier de l'app :
commission agence, plafond frais banque, seuil cash/crypto, flags de
confirmation. Sans ça, chaque changement doit être revérifié à la main dans
le navigateur, ce qui ne passe pas à l'échelle sur cette base de code."""
from montagne import estimation as est

REFS = est.ProjectionRefs(
    mileage_par_jour=150.0, perdiem_par_jour=260.0, taux_journalier=600.0,
    plafond_frais_banque=3800.0, salaire_net_moyen=2200.0,
)


def entry(**overrides):
    base = {
        "month": "2026-07", "jours_travailles": 20.0,
        "jours_conges_payes": 0.0, "jours_feries": 0.0, "jours_sans_solde": 0.0,
        "facture": None, "facture_auto": 1,
        "salaire_net_recu": None, "salaire_confirme": 0,
        "salaire_net_attendu": None, "salaire_attendu_auto": 1,
        "frais_allowance": None, "frais_expense_batch": None, "frais_mileage": None,
        "frais_confirmes": 0,
        "perdiem_attendu": None, "perdiem_auto": 1,
        "complement_cash_attendu": 0, "complement_cash_confirme": 0, "complement_cash_montant": None,
        "complement_crypto_attendu": 0, "complement_crypto_confirme": 0,
        "complement_crypto_conversion_euros": None, "complement_crypto_montant_natif": None,
        "sodexo": None, "pecule_vacances": None, "treizieme_mois": None,
        "mois_cloture": 0,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------- Flags & seuils --
def test_crypto_cash_seuil_juin_2026():
    assert est.crypto_cash_applicable(entry(month="2026-05")) is False
    assert est.crypto_cash_applicable(entry(month="2026-06")) is True
    assert est.crypto_cash_applicable(entry(month="2026-07")) is True


def test_flags_confirmation():
    e = entry(salaire_confirme=1, frais_confirmes=0, mois_cloture=0)
    assert est.is_salaire_confirme(e) is True
    assert est.is_frais_confirmes(e) is False
    assert est.is_mois_clos(e) is False


# ------------------------------------------------- Complément cash + crypto
def test_is_complement_confirme_rien_attendu_est_confirme_par_defaut():
    # Aucun volet coché "attendu" -> rien à confirmer, considéré fait.
    assert est.is_complement_confirme(entry()) is True


def test_is_complement_confirme_cash_seul():
    e = entry(complement_cash_attendu=1)
    assert est.is_complement_confirme(e) is False
    e["complement_cash_confirme"] = 1
    assert est.is_complement_confirme(e) is True


def test_is_complement_confirme_cash_et_crypto_les_deux_requis():
    e = entry(complement_cash_attendu=1, complement_cash_confirme=1, complement_crypto_attendu=1)
    # Cash confirmé mais crypto encore attendu et pas confirmé -> pas fini.
    assert est.is_complement_confirme(e) is False
    e["complement_crypto_confirme"] = 1
    assert est.is_complement_confirme(e) is True


def test_complement_reel_additionne_cash_et_crypto_confirmes():
    e = entry(
        complement_cash_confirme=1, complement_cash_montant=500.0,
        complement_crypto_confirme=1, complement_crypto_conversion_euros=420.0,
    )
    assert est.complement_reel(e) == 920.0


def test_complement_reel_ignore_un_volet_non_confirme():
    e = entry(
        complement_cash_confirme=1, complement_cash_montant=500.0,
        complement_crypto_attendu=1, complement_crypto_conversion_euros=420.0,  # pas confirmé
    )
    assert est.complement_reel(e) == 500.0


def test_complement_reel_zero_si_rien_confirme():
    assert est.complement_reel(entry()) == 0.0


def test_enrich_complement_reel_visible_meme_si_un_seul_volet_confirme():
    # Régression : un complément crypto déjà reçu (ex. BTC en août) ne doit
    # jamais disparaître de l'affichage juste parce que le volet cash, coché
    # "attendu" par défaut, n'a pas encore été confirmé lui — sinon l'argent
    # réellement reçu semble ne jamais être arrivé.
    e = entry(
        complement_cash_attendu=1, complement_cash_confirme=0,
        complement_crypto_attendu=1, complement_crypto_confirme=1,
        complement_crypto_devise="BTC", complement_crypto_conversion_euros=30000.0,
    )
    assert est.is_complement_confirme(e) is False
    enriched = est.enrich(e, REFS)
    assert enriched["complement_reel"] == 30000.0


# ------------------------------------------------------- Valeurs "auto" ----
def test_facture_auto_vs_manuelle():
    e_auto = entry(jours_travailles=20.0, facture_auto=1)
    assert est.facture_effective(e_auto, REFS) == 20.0 * 600.0

    e_manuelle = entry(facture_auto=0, facture=11000.0)
    assert est.facture_effective(e_manuelle, REFS) == 11000.0


def test_perdiem_auto_vs_manuel():
    e_auto = entry(jours_travailles=15.0, perdiem_auto=1)
    assert est.perdiem_effective(e_auto, REFS) == 15.0 * 260.0

    e_manuel = entry(perdiem_auto=0, perdiem_attendu=3000.0)
    assert est.perdiem_effective(e_manuel, REFS) == 3000.0


def test_salaire_attendu_auto_utilise_la_moyenne():
    e = entry(salaire_attendu_auto=1)
    assert est.salaire_attendu_effective(e, REFS) == REFS.salaire_net_moyen


# ------------------------------------------------------------- Frais banque
def test_frais_allowance_effective_fixe_50_par_jour_si_non_confirme():
    e = entry(jours_travailles=10.0, frais_confirmes=0)
    assert est.frais_allowance_effective(e) == 10.0 * est.DAILY_ALLOWANCE_PAR_JOUR


def test_total_frais_banque_reel_zero_si_non_confirme():
    e = entry(frais_confirmes=0, frais_allowance=1000, frais_expense_batch=500, frais_mileage=300)
    assert est.total_frais_banque_reel(e) == 0.0


def test_total_frais_banque_reel_somme_si_confirme():
    e = entry(frais_confirmes=1, frais_allowance=1000, frais_expense_batch=500, frais_mileage=300)
    assert est.total_frais_banque_reel(e) == 1800.0


def test_frais_banque_effective_plafonnee_si_non_confirme():
    # perdiem effectif (20j * 260 = 5200) dépasse le plafond (3800) : les
    # frais "effectifs" (utilisés dans les projections) sont plafonnés.
    e = entry(jours_travailles=20.0, perdiem_auto=1, frais_confirmes=0)
    assert est.total_frais_banque_effective(e, REFS) == REFS.plafond_frais_banque


def test_frais_banque_effective_sous_le_plafond():
    e = entry(jours_travailles=10.0, perdiem_auto=1, frais_confirmes=0)  # 10*260=2600 < 3800
    assert est.total_frais_banque_effective(e, REFS) == 2600.0


# --------------------------------------------------- Commission agence 10% -
def test_commission_et_complement_avant_juin_2026_toujours_zero():
    e = entry(month="2026-05", jours_travailles=25.0, perdiem_auto=1, frais_confirmes=0)
    assert est.reste_avant_commission(e, REFS) == 0.0
    assert est.commission_agence(e, REFS) == 0.0
    assert est.complement_attendu(e, REFS) == 0.0


def test_commission_10_pourcent_sur_le_reste_apres_juin_2026():
    # 25j * 260€/j = 6500€ de perdiem, plafond frais banque 3800€
    # -> reste = 2700€, commission = 270€, complément net = 2430€
    e = entry(month="2026-07", jours_travailles=25.0, perdiem_auto=1, frais_confirmes=0, complement_cash_attendu=1)
    assert est.reste_avant_commission(e, REFS) == 2700.0
    assert est.commission_agence(e, REFS) == 270.0
    assert est.complement_attendu(e, REFS) == 2430.0


def test_pas_de_reste_negatif_si_perdiem_sous_le_plafond():
    e = entry(  # 5*260=1300 < 3800
        month="2026-07", jours_travailles=5.0, perdiem_auto=1, frais_confirmes=0, complement_cash_attendu=1,
    )
    assert est.reste_avant_commission(e, REFS) == 0.0
    assert est.complement_attendu(e, REFS) == 0.0


def test_complement_attendu_zero_si_aucun_volet_attendu():
    # Perdiem largement au-dessus du plafond, mais cash ET crypto explicitement
    # décochés (comportement par défaut de entry()) -> pas de complément
    # "attendu" même si les frais banque ne sont pas encore confirmés (retour
    # direct de l'utilisateur : la carte "Complément cash/crypto attendu" ne
    # doit pas afficher un montant qu'il a explicitement dit ne pas attendre).
    e = entry(month="2026-07", jours_travailles=25.0, perdiem_auto=1, frais_confirmes=0)
    assert est.complement_possible(e) is False
    assert est.reste_avant_commission(e, REFS) == 0.0
    assert est.commission_agence(e, REFS) == 0.0
    assert est.complement_attendu(e, REFS) == 0.0


# ------------------------------------------------------------- Net perçu --
def test_net_percu_none_si_salaire_pas_confirme():
    e = entry(salaire_confirme=0)
    assert est.net_percu_total(e, REFS) is None


def test_net_percu_calcule_si_salaire_confirme():
    e = entry(
        month="2026-07", salaire_confirme=1, salaire_net_recu=2200.0,
        frais_confirmes=1, frais_allowance=1000.0, frais_expense_batch=0.0, frais_mileage=0.0,
        complement_cash_attendu=1, complement_cash_confirme=1, complement_cash_montant=200.0,
        sodexo=50.0, pecule_vacances=0.0, treizieme_mois=0.0,
    )
    # 2200 (salaire) + 1000 (frais banque réels) + 200 (complément réel) + 50 (avantages)
    assert est.net_percu_total(e, REFS) == 3450.0


def test_taux_conversion_none_si_facture_nulle():
    e = entry(facture_auto=0, facture=0, salaire_confirme=1, salaire_net_recu=1000.0)
    assert est.taux_conversion(e, REFS) is None


def test_taux_conversion_pourcentage_net_sur_facture():
    e = entry(
        month="2026-05", jours_travailles=20.0, facture_auto=1,
        salaire_confirme=1, salaire_net_recu=6000.0,
        frais_confirmes=1, frais_allowance=0.0, frais_expense_batch=0.0, frais_mileage=0.0,
    )
    # facture = 20*600 = 12000, net = 6000 -> 50%
    assert est.taux_conversion(e, REFS) == 50.0


# --------------------------- frais_banque_projete (référence isolée fixe) --
# Cas vécu (retour direct de l'utilisateur, août) : perdiem attendu 4030€,
# plafond frais banque 3800€. L'agence a finalement versé 4345€ de frais
# banque (donc plus que le perdiem attendu lui-même). Une fois
# frais_confirmes, total_frais_banque_effective bascule sur le réel — c'est
# volontaire (net_percu_total, complément restant, Poche en dépendent) mais
# frais_banque_projete, lui, reste sur le plafond : il ne sert QUE d'affichage
# de référence isolé (carte "Frais banque (plafonnés)", ligne "Frais banque"
# de la page Delta), jamais dans un total.
def test_frais_banque_projete_reste_plafonnee_meme_confirmee():
    e = entry(
        month="2026-08", perdiem_auto=0, perdiem_attendu=4030.0,
        frais_confirmes=1, frais_allowance=4345.0, frais_expense_batch=0.0, frais_mileage=0.0,
    )
    assert est.total_frais_banque_effective(e, REFS) == 4345.0  # réel une fois confirmé
    assert est.frais_banque_projete(e, REFS) == 3800.0  # reste la projection plafonnée


# net_attendu_total : "vivant" (total_frais_banque_effective + complement_attendu)
# UNIQUEMENT si un complément cash/crypto est effectivement attendu ce mois-là
# (voir complement_possible) — sinon un virement bancaire imprévu au-delà du
# plafond s'y substituerait silencieusement à l'attendu au lieu de ressortir
# comme écart (retour direct de l'utilisateur, cas vécu d'août : les deux
# volets cash/crypto explicitement décochés ce mois-là, tout le perdiem étant
# passé par la banque).
def test_net_attendu_total_simple_si_aucun_complement_attendu():
    e = entry(
        month="2026-08", perdiem_auto=0, perdiem_attendu=4030.0,
        salaire_attendu_auto=0, salaire_net_attendu=2200.0,
        complement_cash_attendu=0, complement_crypto_attendu=0,
        frais_confirmes=1, frais_allowance=4345.0, frais_expense_batch=0.0, frais_mileage=0.0,
    )
    # 2200 (salaire attendu) + 4030 (perdiem attendu, somme simple) — pas de
    # passage par le plafond/la commission puisqu'aucun complément n'était
    # attendu ce mois-ci.
    assert est.net_attendu_total(e, REFS) == 6230.0


def test_net_attendu_total_vivant_si_complement_attendu():
    e = entry(
        month="2026-08", perdiem_auto=0, perdiem_attendu=4030.0,
        salaire_attendu_auto=0, salaire_net_attendu=2200.0,
        complement_cash_attendu=1,
        frais_confirmes=1, frais_allowance=4345.0, frais_expense_batch=0.0, frais_mileage=0.0,
    )
    # 2200 (salaire attendu) + 4345 (frais banque réels, banque > perdiem
    # attendu -> rien de plus attendu en complément) + 0 (complément)
    assert est.net_attendu_total(e, REFS) == 6545.0


def test_net_attendu_total_traite_flag_cash_null_comme_attendu():
    # complement_cash_attendu jamais réglé (None, mois futur) -> considéré
    # attendu par défaut, donc toujours "vivant" (voir _complement_flag).
    e = entry(month="2026-08", jours_travailles=20.0, perdiem_auto=1)
    e["complement_cash_attendu"] = None
    e["complement_crypto_attendu"] = None
    assert est.complement_possible(e) is True


# ------------------------------------------------------------------ enrich -
def test_enrich_ajoute_bien_tous_les_champs_calcules():
    e = entry(month="2026-07", mois_cloture=1)
    enriched = est.enrich(e, REFS)
    for champ in (
        "facture_eff", "perdiem_eff", "salaire_attendu_eff", "frais_banque_eff",
        "net_percu_total", "net_attendu_total", "taux_conversion_pct", "statut",
    ):
        assert champ in enriched
    assert enriched["statut"] == "réel"  # mois_cloture=1
