"""Tests sur projections.py — résolution des taux (exception mensuelle ->
réglage annuel -> None) et enrichissement annuel. C'est la SOURCE UNIQUE de
ce calcul pour tous les consommateurs (V2 Streamlit, V3 FastAPI) — les tests
de non-régression structurels vérifiant qu'aucun consommateur ne réintroduit
une copie locale vivent dans le repo de chaque appli consommatrice, pas ici
(ce module n'a pas la visibilité sur leur code source)."""
from montagne import db, poche_core
from montagne import estimation as est
from montagne import projections


def test_resolve_rates_priorite_exception_mensuelle_sur_reglage_annuel():
    db.set_setting("taux_journalier_2026", "600")
    db.set_rate_override("2026-07", taux_journalier=650.0, perdiem_par_jour=None, plafond_frais_banque=None)
    taux, perdiem, plafond = projections.resolve_rates("2026-07")
    assert taux == 650.0  # exception mensuelle gagne
    assert perdiem is None  # pas d'exception ni de réglage annuel pour perdiem
    assert plafond is None


def test_resolve_rates_repli_sur_reglage_annuel_si_pas_d_exception():
    db.set_setting("taux_journalier_2026", "620")
    taux, _, _ = projections.resolve_rates("2026-07")
    assert taux == 620.0


def test_resolve_rates_none_si_ni_exception_ni_reglage():
    taux, perdiem, plafond = projections.resolve_rates("2026-07")
    assert (taux, perdiem, plafond) == (None, None, None)


def test_year_entries_remplit_les_12_mois_meme_sans_donnees():
    entries = projections.year_entries(2026)
    assert set(entries) == {f"2026-{m:02d}" for m in range(1, 13)}
    assert entries["2026-01"]["month"] == "2026-01"
    assert entries["2026-01"]["jours_travailles"] is None


def test_year_entries_mois_vide_a_toujours_les_memes_champs_qu_un_mois_reel():
    # Régression : un mois vide qui n'a QUE la clé "month" fait que
    # pd.DataFrame(list(year_entries(...).values())) ne crée aucune colonne
    # pour les autres champs quand toute l'année est vide — KeyError en aval
    # dès qu'un consommateur y accède (vécu côté V2 sur Totaux annuels). Un
    # mois vide doit avoir exactement les mêmes clés que db.ALL_FIELDS
    # (+ "month"), toutes à None.
    entries = projections.year_entries(2030)
    assert set(entries["2030-06"]) == {"month", *db.ALL_FIELDS}
    assert all(v is None for k, v in entries["2030-06"].items() if k != "month")


def test_year_entries_reprend_les_donnees_existantes():
    db.upsert_entry("2026-03", {"jours_travailles": 18.0})
    entries = projections.year_entries(2026)
    assert entries["2026-03"]["jours_travailles"] == 18.0


def test_projection_refs_utilise_les_taux_fournis_sans_calculer_de_moyenne():
    db.set_rate_override("2026-07", taux_journalier=700.0, perdiem_par_jour=300.0, plafond_frais_banque=4000.0)
    refs = projections.projection_refs([], "2026-07")
    assert refs.taux_journalier == 700.0
    assert refs.perdiem_par_jour == 300.0
    assert refs.plafond_frais_banque == 4000.0


def test_enrich_year_calcule_un_champ_par_mois_pour_toute_l_annee():
    db.upsert_entry("2026-01", {"jours_travailles": 20.0, "facture_auto": 1})
    rows = projections.enrich_year(2026)
    assert len(rows) == 12
    row_janvier = next(r for r in rows if r["month"] == "2026-01")
    assert "net_attendu_total" in row_janvier


def test_enrich_year_annee_sans_aucune_donnee_garde_toutes_les_colonnes():
    # Régression concrète (Totaux annuels côté V2, KeyError: 'frais_mileage') :
    # une année totalement vide (aucune entrée en base) doit quand même
    # produire des lignes avec toutes les colonnes attendues, pas seulement
    # "month" + les champs calculés par enrich().
    rows = projections.enrich_year(2031)
    assert all("frais_mileage" in r for r in rows)
    assert all(r["frais_mileage"] is None for r in rows)


# -------------------------------------- Cohérence Suivi financier <-> Poche --
def test_cash_confirme_par_mois_coherent_avec_est_complement_reel_cash_seul():
    """Quand seul le volet cash est confirmé (pas de crypto ce mois-là),
    poche_core._cash_confirme_par_mois() doit produire EXACTEMENT ce que
    donne estimation.complement_reel() — les deux lisent le même champ
    complement_cash_montant, pas de logique dupliquée qui pourrait diverger."""
    entry = {
        "month": "2026-07", "complement_cash_confirme": 1, "complement_cash_montant": 500.0,
        "complement_crypto_confirme": 0, "complement_crypto_conversion_euros": None,
    }
    db.upsert_entry("2026-07", entry)
    assert poche_core._cash_confirme_par_mois()["2026-07"] == est.complement_reel(entry)


def test_poche_core_projection_refs_est_bien_la_fonction_partagee_pas_une_copie():
    """Garde-fou direct : poche_core.py doit référencer le MÊME objet
    fonction que projections.projection_refs — si une fonction locale du
    même nom est réintroduite dans poche_core.py sans mettre à jour l'import,
    ce test échoue immédiatement."""
    assert poche_core.projection_refs is projections.projection_refs
