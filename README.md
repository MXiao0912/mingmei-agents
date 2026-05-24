# Research Reader

## Local Test Before Deploy

Run the pipeline from the project root:

```bash
python agents/research_reader/collect.py
python agents/research_reader/rank.py
python agents/research_reader/learn.py
streamlit run web/app.py
```

Tune keyword weights, source boosts, score thresholds, and learning shrinkage in:

```bash
config/research_preferences.yaml
```

## Monthly Archive Workflow

The active SQLite database can stay lightweight while older records are exported to local archive files. Archive files are written to:

```text
archives/research_reader/
```

Export records older than 12 months without deleting anything:

```bash
python agents/research_reader/archive.py --months 12
```

Export records older than 12 months and then clean them from the active database:

```bash
python agents/research_reader/archive.py --months 12 --delete-after-export
```

When deleting, the script prints the number of records and archive file paths, then asks for confirmation. To skip the prompt in an automated local workflow:

```bash
python agents/research_reader/archive.py --months 12 --delete-after-export --yes
```

The archive workflow does not delete preference configuration files or learned preference settings. It only deletes exported paper rows and their feedback rows from the active database when explicitly requested.

## Streamlit Community Cloud Deployment

1. Push this repo to GitHub.
2. Go to Streamlit Community Cloud.
3. Create a new app from the GitHub repo.
4. Set the main file path to:

```text
web/app.py
```

5. Deploy.

If the deployed app shows no data, run collection and ranking locally, then commit `data/reader.db` and redeploy:

```bash
python agents/research_reader/collect.py
python agents/research_reader/rank.py
python agents/research_reader/learn.py
```

Committing SQLite makes deployment easy because Streamlit Cloud can read `data/reader.db` directly. It is not ideal long-term because feedback written in the deployed app may not persist across app restarts and does not sync back to GitHub. Later, move the archive and feedback tables to Postgres or another hosted database.
