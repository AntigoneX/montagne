# montagne

Logique métier partagée entre deux applications consommatrices (une app
Streamlit, et une app Next.js + FastAPI) : estimation/projection financière,
accès aux données (SQLite local ou Turso), et la logique de suivi de cash
physique.

Zéro dépendance UI — ni Streamlit, ni FastAPI ne doivent être importés dans
ce package. Les deux applications le consomment via
`pip install git+https://github.com/AntigoneX/montagne.git`.

## Contenu

| Module | Rôle |
|---|---|
| `estimation.py` | Moteur de calcul (commission agence, plafond frais banque, net attendu/perçu) |
| `projections.py` | Résolution des taux par mois, enrichissement annuel — source unique |
| `db.py` | Accès données (SQLite local ou réplique Turso) |
| `poche_core.py` | Logique de suivi de cash physique (répartition billets, projections, rappels) |

## Configuration (variables d'environnement)

- `MONTAGNE_DB_PATH` / `MONTAGNE_REPLICA_PATH` — chemins SQLite locaux (défaut : `./local.db` / `./local_replica.db`)
- `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` — si définis, bascule sur la réplique Turso au lieu du SQLite local

## Développement

```bash
pip install -e ".[dev]"
pytest
```
