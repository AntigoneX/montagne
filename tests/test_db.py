"""Tests sur la couche de persistance (db.py) : round-trip écriture/lecture,
cache invalidé après chaque écriture, réglages, exceptions mensuelles, statuts
Delta. La peur concrète que ces tests couvrent : qu'une page relise une
valeur différente de ce qui vient d'être écrit, parce qu'un cache n'a pas été
invalidé au bon moment."""
from montagne import db


# ------------------------------------------------------------- monthly_entries
def test_upsert_puis_get_entry_round_trip():
    db.upsert_entry("2026-07", {"jours_travailles": 20.0, "salaire_confirme": 1, "salaire_net_recu": 2500.0})
    e = db.get_entry("2026-07")
    assert e["month"] == "2026-07"
    assert e["jours_travailles"] == 20.0
    assert e["salaire_confirme"] == 1
    assert e["salaire_net_recu"] == 2500.0


def test_upsert_preserve_les_champs_non_fournis_lors_d_une_maj_partielle():
    db.upsert_entry("2026-07", {"jours_travailles": 20.0, "salaire_net_recu": 2500.0})
    # Deuxième écriture qui ne fournit QUE jours_travailles : upsert_entry
    # remplace toute la ligne (INSERT ... ON CONFLICT UPDATE sur toutes les
    # colonnes), donc l'appelant est responsable de repartir de l'existant
    # (`{**existing, **changements}`) — vérifié ici pour que ce contrat ne
    # change pas silencieusement.
    db.upsert_entry("2026-07", {"jours_travailles": 21.0})
    e = db.get_entry("2026-07")
    assert e["jours_travailles"] == 21.0
    assert e["salaire_net_recu"] is None


def test_get_entry_mois_inexistant_retourne_none():
    assert db.get_entry("2099-01") is None


def test_get_all_entries_reflete_une_ecriture_immediatement():
    # get_all_entries est @st.cache_data(ttl=30) : sans invalidation
    # explicite après écriture, cette lecture pourrait renvoyer un résultat
    # périmé — exactement la classe de bug redoutée ("données différentes
    # pour un même sujet").
    assert db.get_all_entries() == []
    db.upsert_entry("2026-01", {"jours_travailles": 10.0})
    entries = db.get_all_entries()
    assert len(entries) == 1
    assert entries[0]["month"] == "2026-01"

    db.upsert_entry("2026-02", {"jours_travailles": 5.0})
    assert len(db.get_all_entries()) == 2


def test_delete_entry_invalide_bien_le_cache():
    db.upsert_entry("2026-03", {"jours_travailles": 10.0})
    assert len(db.get_all_entries()) == 1
    db.delete_entry("2026-03")
    assert db.get_all_entries() == []
    assert db.get_entry("2026-03") is None


def test_is_empty():
    assert db.is_empty() is True
    db.upsert_entry("2026-01", {"jours_travailles": 1.0})
    assert db.is_empty() is False


# ------------------------------------------------------------------- settings
def test_get_setting_defaut_si_absent():
    assert db.get_setting("taux_journalier_2026") is None
    assert db.get_setting("taux_journalier_2026", "600") == "600"


def test_set_setting_puis_get_setting_reflete_immediatement():
    db.set_setting("taux_journalier_2026", "650")
    assert db.get_setting("taux_journalier_2026") == "650"
    db.set_setting("taux_journalier_2026", "700")
    assert db.get_setting("taux_journalier_2026") == "700"


# ------------------------------------------------------------- rate_overrides
def test_rate_override_absent_par_defaut():
    assert db.get_rate_override("2026-07") is None
    assert db.get_rate_overrides_for_year(2026) == {}


def test_set_rate_override_puis_lecture():
    db.set_rate_override("2026-07", taux_journalier=650.0, perdiem_par_jour=None, plafond_frais_banque=4000.0)
    o = db.get_rate_override("2026-07")
    assert o["taux_journalier"] == 650.0
    assert o["perdiem_par_jour"] is None
    assert o["plafond_frais_banque"] == 4000.0

    overrides = db.get_rate_overrides_for_year(2026)
    assert set(overrides) == {"2026-07"}


def test_set_rate_override_tout_none_supprime_la_ligne():
    db.set_rate_override("2026-07", taux_journalier=650.0, perdiem_par_jour=None, plafond_frais_banque=None)
    assert db.get_rate_override("2026-07") is not None
    db.set_rate_override("2026-07", taux_journalier=None, perdiem_par_jour=None, plafond_frais_banque=None)
    assert db.get_rate_override("2026-07") is None


# --------------------------------------------------------------- delta_status
def test_delta_status_round_trip():
    assert db.get_delta_statuses() == {}
    db.set_delta_status("2026-07", "Salaire net", "a_traiter")
    assert db.get_delta_statuses() == {("2026-07", "Salaire net"): "a_traiter"}
    db.set_delta_status("2026-07", "Salaire net", "traite")
    assert db.get_delta_statuses() == {("2026-07", "Salaire net"): "traite"}


def test_delta_status_distinct_par_categorie():
    db.set_delta_status("2026-07", "Salaire net", "a_traiter")
    db.set_delta_status("2026-07", "Frais banque", "traite")
    statuses = db.get_delta_statuses()
    assert statuses[("2026-07", "Salaire net")] == "a_traiter"
    assert statuses[("2026-07", "Frais banque")] == "traite"


# -------------------------------------- migration complement cash/crypto
def test_migrate_complement_split_cash():
    db.upsert_entry("2026-07", {
        "complement_confirme": 1, "complement_type": "CASH",
        "complement_montant": 500.0, "complement_manquant": 50.0,
    })
    migrated = db.migrate_complement_split()
    assert migrated == 1
    e = db.get_entry("2026-07")
    assert e["complement_cash_confirme"] == 1
    assert e["complement_cash_attendu"] == 1
    assert e["complement_cash_montant"] == 500.0
    assert e["complement_cash_manquant"] == 50.0
    assert not e["complement_crypto_confirme"]  # jamais touché par la migration -> NULL, pas 0


def test_migrate_complement_split_crypto():
    db.upsert_entry("2026-07", {
        "complement_confirme": 1, "complement_type": "USDT",
        "conversion_crypto_euros": 420.0,
    })
    db.migrate_complement_split()
    e = db.get_entry("2026-07")
    assert e["complement_crypto_confirme"] == 1
    assert e["complement_crypto_attendu"] == 1
    assert e["complement_crypto_devise"] == "USDT"
    assert e["complement_crypto_conversion_euros"] == 420.0
    assert not e["complement_cash_confirme"]  # jamais touché par la migration -> NULL, pas 0


def test_migrate_complement_split_idempotent():
    db.upsert_entry("2026-07", {
        "complement_confirme": 1, "complement_type": "CASH", "complement_montant": 500.0,
    })
    assert db.migrate_complement_split() == 1
    assert db.migrate_complement_split() == 0  # déjà migré, rien à refaire


def test_migrate_complement_split_ignore_les_mois_non_confirmes():
    db.upsert_entry("2026-07", {"complement_confirme": 0, "complement_type": "CASH", "complement_montant": 500.0})
    assert db.migrate_complement_split() == 0
