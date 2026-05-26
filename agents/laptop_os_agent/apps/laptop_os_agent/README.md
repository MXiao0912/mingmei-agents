# Laptop Audit Tool

A command-line, read-only audit tool that generates Markdown and CSV reports about laptop organization and potential clutter. It has no dashboard and does not require Streamlit.

## Safety Guarantee

The audit reads file metadata and, for potential exact duplicates smaller than the configured limit, file contents for SHA-256 hashing. It only writes report files inside `apps/laptop_os_agent/reports/`.
The report output directory is automatically excluded from later scans so generated CSV and Markdown files do not distort subsequent results.

It never:

- deletes, moves, renames, or modifies scanned files
- uninstalls applications
- changes browser or Chrome data
- performs any cleanup action

Every cleanup idea in the reports is a manual recommendation that must be reviewed first.

## Run

From `/Users/mingmeixiao/Documents/MINGMEI_AGENTS/agents/laptop_os_agent`:

```bash
python apps/laptop_os_agent/audit.py
```

The script uses the Python standard library. If `pyyaml` is installed it is used to load configuration; otherwise the bundled simple YAML reader handles this configuration format.

## Configure

Edit `apps/laptop_os_agent/config.yaml`:

```yaml
scan_roots:
  - /Users/mingmeixiao

ignored_paths:
  - /System
  - /Library
  - /private
  - /dev
  - /Applications
  - .Trash
  - .git
  - __pycache__
  - node_modules
  - Library/Caches
  - Library/Developer
  - Library/Containers
  - Library/Group Containers
  - Library/Application Support/Google/Chrome
  - Library/Application Support/Chromium
  - Library/Application Support/Firefox
  - Library/Application Support/Microsoft Edge
  - Library/Application Support/BraveSoftware
  - Library/Application Support/Arc
  - Library/Safari
  - .venv/lib
  - .cache
  - conda/pkgs

max_scan_depth: 8
max_hash_file_size_mb: 500
stale_months:
  - 6
  - 12
  - 24
```

`/Users/mingmeixiao` is used because it is the home directory present on this Mac; `/Users/mingmei` was previously attempted and does not exist here.

Browser profile folders are excluded in addition to the requested system/cache exclusions so the audit provides browser-organization advice without inspecting browser internals. `Library/Group Containers` is excluded because it can contain application-managed cloud-sync mirrors that should not be treated as ordinary manual cleanup targets.

## Generated Reports

Running the command creates or refreshes:

- `reports/storage_summary.md`: largest files and folders, extension totals, and storage offenders
- `reports/stale_files.csv`: files meeting configured 6, 12, or 24 month stale thresholds
- `reports/duplicate_candidates.csv`: exact hash-matched duplicates under the 500 MB hashing limit
- `reports/dev_environment_audit.md`: manual-review signals for development environments and caches
- `reports/project_health.md`: likely projects, documentation gaps, and output review suggestions
- `reports/browser_organization.md`: browser tab, bookmark, and weekly review guidance
- `reports/cloud_storage_audit.md`: locally represented OneDrive and iCloud Drive footprint, stale-file signals, and manual efficiency recommendations
- `reports/action_plan.md`: prioritized manual review plan

## Safe Manual Review

Start with the Markdown summaries, then use CSV reports as checklists. Open files and confirm backups or reproducibility before manually archiving or removing anything. Duplicate hashes show matching contents, but not which copy is important in its location or project context.

The cloud storage report measures only locally represented synchronized content. To evaluate your actual OneDrive or iCloud quota and online-only content, compare it manually with each provider's signed-in storage usage screen.
