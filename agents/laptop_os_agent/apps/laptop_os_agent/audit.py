"""Generate read-only laptop organization reports.

This program reads filesystem metadata and file content only when hashing
possible duplicates. It writes reports beside itself and never changes scanned
files or applications.
"""

from __future__ import annotations

import csv
import hashlib
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    import yaml
except ImportError:
    yaml = None


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.yaml"
REPORTS_DIR = APP_DIR / "reports"
PROJECT_MARKERS = {
    ".git",
    "README.md",
    ".Rproj",
    "pyproject.toml",
    "requirements.txt",
    "environment.yml",
    "renv.lock",
    "scripts",
    "notebooks",
    "data",
    "outputs",
}
ENVIRONMENT_MARKERS = {"pyproject.toml", "requirements.txt", "environment.yml", "renv.lock"}
DEV_NAMES = {".venv", "node_modules", "__pycache__", ".ipynb_checkpoints", ".Rproj.user"}
OUTPUT_REVIEW_BYTES = 1024**3
CLOUD_LOCATIONS = (
    ("OneDrive", Path.home() / "Library" / "CloudStorage" / "OneDrive-UniversityofCambridge"),
    ("iCloud Drive", Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"),
)


@dataclass(frozen=True)
class AuditConfig:
    scan_roots: tuple[Path, ...]
    ignored_paths: tuple[str, ...]
    max_scan_depth: int
    max_hash_file_size_mb: int
    stale_months: tuple[int, ...]

    @property
    def max_hash_bytes(self) -> int:
        return self.max_hash_file_size_mb * 1024 * 1024


@dataclass(frozen=True)
class FileRecord:
    path: Path
    size_bytes: int
    modified_time: float
    extension: str


def load_config(path: Path = CONFIG_PATH) -> AuditConfig:
    if yaml:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    else:
        raw = _read_simple_yaml(path)
    return AuditConfig(
        scan_roots=tuple(Path(value).expanduser() for value in raw.get("scan_roots", [])),
        ignored_paths=tuple(str(value) for value in raw.get("ignored_paths", [])),
        max_scan_depth=int(raw.get("max_scan_depth", 8)),
        max_hash_file_size_mb=int(raw.get("max_hash_file_size_mb", 500)),
        stale_months=tuple(sorted(int(value) for value in raw.get("stale_months", [6, 12, 24]))),
    )


def _read_simple_yaml(path: Path) -> dict:
    """Parse the small list-and-scalar configuration if PyYAML is unavailable."""
    values: dict[str, object] = {}
    current_key = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(":"):
            current_key = line[:-1]
            values[current_key] = []
        elif line.startswith("- ") and current_key:
            cast_value = line[2:].strip()
            if cast_value.isdigit():
                cast_value = int(cast_value)
            values[current_key].append(cast_value)
        elif ":" in line:
            key, value = (part.strip() for part in line.split(":", 1))
            values[key] = int(value) if value.isdigit() else value
    return values


def human_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} TB"


def date_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def age_days(timestamp: float, now: float) -> int:
    return max(0, int((now - timestamp) / 86400))


def is_ignored(path: Path, config: AuditConfig) -> bool:
    if path == REPORTS_DIR or REPORTS_DIR in path.parents:
        return True
    text = str(path)
    parts = path.parts
    for ignored in config.ignored_paths:
        ignored_path = Path(ignored).expanduser()
        if ignored.startswith("/"):
            ignored_text = str(ignored_path)
            if text == ignored_text or text.startswith(ignored_text + os.sep):
                return True
            continue
        ignored_parts = ignored_path.parts
        if len(ignored_parts) == 1 and ignored in parts:
            return True
        for index in range(len(parts) - len(ignored_parts) + 1):
            if tuple(parts[index : index + len(ignored_parts)]) == ignored_parts:
                return True
    return False


def add_to_folder_totals(path: Path, size_bytes: int, root: Path, totals: Counter) -> None:
    current = path.parent
    while True:
        totals[str(current)] += size_bytes
        if current == root:
            return
        current = current.parent


def dev_kind(path: Path) -> str | None:
    if path.name in DEV_NAMES:
        return path.name
    if path.name == "library" and path.parent.name == "renv":
        return "renv/library"
    return None


def directory_measurement(path: Path, max_depth: int) -> tuple[int, int, float]:
    size_bytes = 0
    file_count = 0
    modified_time = 0.0
    stack = [(path, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    size_bytes += stat.st_size
                    file_count += 1
                    modified_time = max(modified_time, stat.st_mtime)
                elif entry.is_dir(follow_symlinks=False) and depth < max_depth:
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    return size_bytes, file_count, modified_time


def inventory(config: AuditConfig) -> dict:
    records: list[FileRecord] = []
    folder_totals: Counter = Counter()
    extensions: Counter = Counter()
    projects: set[Path] = set()
    dev_paths: dict[Path, str] = {}
    notices: list[str] = []
    for root in config.scan_roots:
        if not root.exists():
            notices.append(f"Scan root not found: {root}")
            continue
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            folder = Path(current)
            try:
                relative_depth = len(folder.relative_to(root).parts)
            except ValueError:
                continue
            if relative_depth > config.max_scan_depth or is_ignored(folder, config):
                directories[:] = []
                continue
            original_directories = list(directories)
            marker_names = set(original_directories) | set(files)
            if marker_names & PROJECT_MARKERS or any(name.endswith(".Rproj") for name in files):
                projects.add(folder)
            kept_directories = []
            for name in original_directories:
                candidate = folder / name
                kind = dev_kind(candidate)
                if kind and not candidate.is_symlink():
                    dev_paths[candidate] = kind
                if relative_depth < config.max_scan_depth and not is_ignored(candidate, config):
                    kept_directories.append(name)
            directories[:] = kept_directories
            for filename in files:
                path = folder / filename
                if is_ignored(path, config):
                    continue
                try:
                    if path.is_symlink():
                        continue
                    stat = path.stat()
                except OSError as error:
                    notices.append(f"Unable to read {path}: {error}")
                    continue
                if not path.is_file():
                    continue
                extension = path.suffix.lower() or "[no extension]"
                record = FileRecord(path, stat.st_size, stat.st_mtime, extension)
                records.append(record)
                extensions[extension] += stat.st_size
                add_to_folder_totals(path, stat.st_size, root, folder_totals)
    return {
        "records": records,
        "folder_totals": folder_totals,
        "extensions": extensions,
        "projects": projects,
        "dev_paths": dev_paths,
        "notices": notices,
    }


def hash_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        if path.is_symlink():
            return None
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def find_duplicates(records: list[FileRecord], config: AuditConfig) -> list[dict]:
    by_size: defaultdict[int, list[FileRecord]] = defaultdict(list)
    for record in records:
        if record.size_bytes <= config.max_hash_bytes:
            by_size[record.size_bytes].append(record)
    by_hash: defaultdict[str, list[FileRecord]] = defaultdict(list)
    for candidates in by_size.values():
        if len(candidates) < 2:
            continue
        for record in candidates:
            file_hash = hash_file(record.path)
            if file_hash:
                by_hash[file_hash].append(record)
    rows = []
    for file_hash, candidates in by_hash.items():
        if len(candidates) < 2:
            continue
        wasted = candidates[0].size_bytes * (len(candidates) - 1)
        for record in candidates:
            rows.append(
                {
                    "hash": file_hash,
                    "number_of_copies": len(candidates),
                    "total_wasted_space_estimate_bytes": wasted,
                    "size_bytes": record.size_bytes,
                    "modified_date": date_text(record.modified_time),
                    "path": str(record.path),
                    "manual_recommendation": "Review copies manually; confirm which version is needed before any action.",
                }
            )
    return sorted(rows, key=lambda row: (row["total_wasted_space_estimate_bytes"], row["hash"]), reverse=True)


def stale_rows(records: list[FileRecord], config: AuditConfig, now: float) -> list[dict]:
    rows = []
    for record in records:
        months_old = [months for months in config.stale_months if age_days(record.modified_time, now) >= months * 30.4375]
        if not months_old:
            continue
        rows.append(
            {
                "path": str(record.path),
                "size_bytes": record.size_bytes,
                "modified_date": date_text(record.modified_time),
                "age_days": age_days(record.modified_time, now),
                "stale_at_months": ",".join(str(month) for month in months_old),
                "manual_recommendation": "Review whether this file remains useful; archive or remove only after checking.",
            }
        )
    return sorted(rows, key=lambda row: row["size_bytes"], reverse=True)


def dev_audit(dev_paths: dict[Path, str], config: AuditConfig, now: float) -> list[dict]:
    rows = []
    for path, kind in sorted(dev_paths.items()):
        size_bytes, file_count, modified_time = directory_measurement(path, config.max_scan_depth)
        days = age_days(modified_time, now) if modified_time else 0
        suggestion = "keep"
        if days >= 365:
            suggestion = "likely cleanup manually after confirming the project is inactive"
        elif days >= 180:
            suggestion = "review manually"
        rows.append(
            {
                "type": kind,
                "path": str(path),
                "estimated_size_bytes": size_bytes,
                "files_counted": file_count,
                "last_modified": date_text(modified_time) if modified_time else "unknown",
                "age_days": days,
                "manual_recommendation": suggestion,
            }
        )
    return sorted(rows, key=lambda row: row["estimated_size_bytes"], reverse=True)


def project_audit(project_paths: set[Path], records: list[FileRecord], folder_totals: Counter) -> list[dict]:
    records_by_project: defaultdict[Path, list[FileRecord]] = defaultdict(list)
    for record in records:
        current = record.path.parent
        while current != current.parent:
            if current in project_paths:
                records_by_project[current].append(record)
            current = current.parent
    rows = []
    for project in sorted(project_paths):
        try:
            names = {entry.name for entry in os.scandir(project)}
        except OSError:
            names = set()
        project_files = records_by_project[project]
        outputs = [record for record in project_files if "outputs" in record.path.relative_to(project).parts]
        output_size = sum(record.size_bytes for record in outputs)
        immediate_subfolders = [
            (path, size)
            for path, size in folder_totals.items()
            if Path(path).parent == project
        ]
        largest_subfolder = max(immediate_subfolders, key=lambda item: item[1])[0] if immediate_subfolders else ""
        has_readme = "README.md" in names
        has_environment = bool(names & ENVIRONMENT_MARKERS)
        improvements = []
        if not has_readme:
            improvements.append("Add a README describing purpose and how to reproduce results.")
        if not has_environment:
            improvements.append("Add an environment file or document dependencies.")
        if output_size > OUTPUT_REVIEW_BYTES:
            improvements.append("Review large outputs manually; archive reproducible artifacts if appropriate.")
        if not improvements:
            improvements.append("Keep documentation current and review outputs periodically.")
        rows.append(
            {
                "path": str(project),
                "scanned_size_bytes": folder_totals.get(str(project), 0),
                "last_modified": max((record.modified_time for record in project_files), default=0),
                "has_readme": has_readme,
                "has_environment": has_environment,
                "outputs_size_bytes": output_size,
                "outputs_over_1gb": output_size > OUTPUT_REVIEW_BYTES,
                "largest_subfolder": largest_subfolder,
                "suggested_improvements": " ".join(improvements),
            }
        )
    return sorted(rows, key=lambda row: row["scanned_size_bytes"], reverse=True)


def markdown_table(headers: list[str], rows: Iterable[list[str]]) -> str:
    content = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    content.extend("| " + " | ".join(str(value).replace("|", "\\|") for value in row) + " |" for row in rows)
    return "\n".join(content)


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def storage_report(data: dict, timestamp: str) -> str:
    records = data["records"]
    folder_totals = data["folder_totals"]
    extensions = data["extensions"]
    largest_folders = folder_totals.most_common(20)
    largest_files = sorted(records, key=lambda item: item.size_bytes, reverse=True)[:20]
    extension_totals = extensions.most_common(20)
    offender_threshold = 1024**3
    offenders = [(path, size) for path, size in largest_folders if size >= offender_threshold][:15]
    if not offenders:
        offenders = largest_folders[:5]
    return f"""# Storage Summary

Generated: {timestamp}

This is a read-only report. Recommendations are for manual review only.

## Inventory Summary

- Scanned files: {len(records):,}
- Scanned file storage represented: {human_size(sum(record.size_bytes for record in records))}

## Largest Folders

{markdown_table(["Folder", "Scanned Size"], ([path, human_size(size)] for path, size in largest_folders))}

## Largest Files

{markdown_table(["File", "Size", "Modified"], ([str(item.path), human_size(item.size_bytes), date_text(item.modified_time)] for item in largest_files))}

## Storage By Extension

{markdown_table(["Extension", "Size"], ([extension, human_size(size)] for extension, size in extension_totals))}

## Likely Storage Offenders

{markdown_table(["Folder", "Scanned Size", "Manual Recommendation"], ([path, human_size(size), "Review contents and decide manually what remains needed."] for path, size in offenders))}
"""


def developer_report(rows: list[dict], timestamp: str) -> str:
    table_rows = [
        [
            row["type"],
            row["path"],
            human_size(row["estimated_size_bytes"]),
            row["last_modified"],
            row["manual_recommendation"],
        ]
        for row in rows
    ]
    return f"""# Developer Environment Audit

Generated: {timestamp}

Detected development folders are candidates for review only. Nothing was removed or changed.

{markdown_table(["Type", "Path", "Estimated Size", "Last Modified", "Manual Recommendation"], table_rows) if rows else "No listed development environment folders were found within the configured scan depth."}
"""


def project_report(rows: list[dict], timestamp: str) -> str:
    table_rows = [
        [
            row["path"],
            human_size(row["scanned_size_bytes"]),
            "yes" if row["has_readme"] else "no",
            "yes" if row["has_environment"] else "no",
            human_size(row["outputs_size_bytes"]),
            row["suggested_improvements"],
        ]
        for row in rows
    ]
    return f"""# Project Health

Generated: {timestamp}

Likely projects contain at least one project marker such as `.git`, `README.md`, an environment file, `scripts`, `notebooks`, `data`, or `outputs`.

{markdown_table(["Project", "Scanned Size", "README", "Environment File", "Outputs Size", "Suggested Improvements"], table_rows) if rows else "No likely project folders were found within the configured scan depth."}
"""


def browser_report(timestamp: str) -> str:
    return f"""# Browser Organization

Generated: {timestamp}

This recommendation does not inspect or modify Chrome or any browser data.

## Suggested Chrome Tab Groups

| Group | Purpose |
| --- | --- |
| Today | Tabs tied to tasks you intend to finish today |
| Deep Work | Current project and the few references required for it |
| Research | Temporary sources to compare or synthesize |
| Admin | Accounts, scheduling, forms, and short operational tasks |
| Waiting | Pages needed after a reply or future event |

## Bookmark Folder Structure

| Folder | Contents |
| --- | --- |
| 01 Daily | Mail, calendar, collaboration, frequently used tools |
| 02 Projects | One subfolder per active project |
| 03 Research | Sources, papers, and reference collections |
| 04 Coding | Documentation, repositories, issue trackers |
| 05 AI | Model tools and AI documentation |
| 06 Admin | Forms, accounts, appointments |
| 07 Personal | Home, travel, interests |
| 08 Read Later | Items to process during weekly review |
| 99 Archive | Inactive bookmarks retained for reference |

## Weekly Cleanup Checklist

- Close tabs no longer connected to an active task.
- Bookmark reusable sources into the appropriate folder manually.
- Process Read Later links: keep, archive, or close them manually.
- Consolidate active work into a small number of named tab groups.
- Export bookmarks before any large manual reorganization.
"""


def cloud_storage_report(
    records: list[FileRecord], duplicates: list[dict], timestamp: str, now: float
) -> str:
    sections = []
    summary_rows = []
    duplicate_paths = defaultdict(set)
    for row in duplicates:
        duplicate_paths[row["hash"]].add(Path(row["path"]))
    for provider, root in CLOUD_LOCATIONS:
        provider_records = []
        for record in records:
            try:
                relative_path = record.path.relative_to(root)
            except ValueError:
                continue
            provider_records.append((record, relative_path))
        represented_size = sum(record.size_bytes for record, _ in provider_records)
        stale_24 = [
            record
            for record, _ in provider_records
            if age_days(record.modified_time, now) >= 24 * 30.4375
        ]
        provider_duplicate_hashes = {
            file_hash
            for file_hash, paths in duplicate_paths.items()
            if any(root == path or root in path.parents for path in paths)
        }
        summary_rows.append(
            [
                provider,
                str(root),
                f"{len(provider_records):,}",
                human_size(represented_size),
                human_size(sum(record.size_bytes for record in stale_24)),
                f"{len(provider_duplicate_hashes):,}",
            ]
        )
        child_totals: Counter = Counter()
        for record, relative_path in provider_records:
            label = relative_path.parts[0] if relative_path.parts else "[root files]"
            child_totals[label] += record.size_bytes
        top_children = child_totals.most_common(10)
        recommendations = []
        if stale_24:
            recommendations.append(
                f"Review {human_size(sum(record.size_bytes for record in stale_24))} represented by files not modified in at least 24 months."
            )
        if provider_duplicate_hashes:
            recommendations.append(
                f"Review {len(provider_duplicate_hashes):,} exact-duplicate groups that include at least one locally represented {provider} file."
            )
        if represented_size >= 20 * 1024**3:
            recommendations.append("Review the largest folders first and decide manually whether older outputs still belong in cloud storage.")
        if not provider_records:
            recommendations.append("No files were represented in the local synced location; this does not prove the cloud account is empty.")
        sections.append(
            f"""## {provider}

- Local synced location: `{root}`
- Locally represented files: {len(provider_records):,}
- Locally represented file size: {human_size(represented_size)}

### Largest Top-Level Folders

{markdown_table(["Folder", "Locally Represented Size"], ([name, human_size(size)] for name, size in top_children)) if top_children else "No locally represented folders were found."}

### Manual Review Suggestions

{chr(10).join("- " + recommendation for recommendation in recommendations) if recommendations else "- No obvious local-footprint concern was flagged; check provider-side quota for a complete view."}
"""
        )
    return f"""# Cloud Storage Audit

Generated: {timestamp}

## Scope And Limitation

This read-only report measures files represented in locally synced OneDrive and iCloud Drive locations. It **does not show total provider-side storage, account quota, online-only files that are not represented locally, or billing-plan utilization**. A locally represented file size is a review signal, not necessarily bytes currently occupying this Mac's disk.

For a full quota assessment, manually compare this report with the storage usage shown in your signed-in OneDrive storage management page and Apple iCloud storage settings.

## Local Footprint Summary

{markdown_table(["Provider", "Local Synced Location", "Files", "Locally Represented Size", "24+ Month Size", "Duplicate Groups Including Provider"], summary_rows)}

{"".join(sections)}
"""


def action_plan(
    folder_totals: Counter,
    stale: list[dict],
    duplicates: list[dict],
    development: list[dict],
    projects: list[dict],
    timestamp: str,
) -> str:
    largest = folder_totals.most_common(8)
    duplicate_groups = len({row["hash"] for row in duplicates})
    older = [row for row in stale if "24" in row["stale_at_months"]][:10]
    dev_review = [row for row in development if row["manual_recommendation"] != "keep"][:10]
    project_gaps = [row for row in projects if not row["has_readme"] or not row["has_environment"]][:10]
    return f"""# Manual Action Plan

Generated: {timestamp}

No action in this plan is automatic. Confirm importance, backups, and rebuildability before manually changing anything.

## A. Safe To Review

- Review the largest scanned folders first:

{markdown_table(["Folder", "Scanned Size"], ([path, human_size(size)] for path, size in largest))}

- Review project documentation gaps:

{markdown_table(["Project", "Suggested Improvement"], ([row["path"], row["suggested_improvements"]] for row in project_gaps)) if project_gaps else "No README or environment-file gaps were detected in likely projects."}

## B. Likely Archive

Files unmodified for at least 24 months may be candidates for manual archival after opening and checking them:

{markdown_table(["Path", "Size", "Modified"], ([row["path"], human_size(row["size_bytes"]), row["modified_date"]] for row in older)) if older else "No files older than 24 months were identified."}

Development directories worth manual review:

{markdown_table(["Type", "Path", "Estimated Size", "Recommendation"], ([row["type"], row["path"], human_size(row["estimated_size_bytes"]), row["manual_recommendation"]] for row in dev_review)) if dev_review else "No aged development folders were flagged for manual review."}

## C. Possible Duplicates

- Exact hash-matched duplicate groups found: {duplicate_groups:,}
- Review `duplicate_candidates.csv` and compare context before manually deciding whether any copy is unnecessary.

## D. Do Not Touch Without Checking

- Application bundles and operating-system folders.
- Application-managed sync folders such as `~/Library/Group Containers`; manage cloud files through their user-facing synced folder or provider after confirming synchronization.
- Active project environments or dependency folders required to reproduce work.
- Files in duplicate groups until you know which location is authoritative.
- Browser bookmarks or open tabs without first exporting or recording anything important.
"""


def write_reports(config: AuditConfig, data: dict) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat(timespec="seconds")
    records: list[FileRecord] = data["records"]
    stale = stale_rows(records, config, now.timestamp())
    duplicates = find_duplicates(records, config)
    development = dev_audit(data["dev_paths"], config, now.timestamp())
    projects = project_audit(data["projects"], records, data["folder_totals"])
    (REPORTS_DIR / "storage_summary.md").write_text(storage_report(data, timestamp), encoding="utf-8")
    write_csv(
        REPORTS_DIR / "stale_files.csv",
        stale,
        ["path", "size_bytes", "modified_date", "age_days", "stale_at_months", "manual_recommendation"],
    )
    write_csv(
        REPORTS_DIR / "duplicate_candidates.csv",
        duplicates,
        [
            "hash",
            "number_of_copies",
            "total_wasted_space_estimate_bytes",
            "size_bytes",
            "modified_date",
            "path",
            "manual_recommendation",
        ],
    )
    (REPORTS_DIR / "dev_environment_audit.md").write_text(developer_report(development, timestamp), encoding="utf-8")
    (REPORTS_DIR / "project_health.md").write_text(project_report(projects, timestamp), encoding="utf-8")
    (REPORTS_DIR / "browser_organization.md").write_text(browser_report(timestamp), encoding="utf-8")
    (REPORTS_DIR / "cloud_storage_audit.md").write_text(
        cloud_storage_report(records, duplicates, timestamp, now.timestamp()),
        encoding="utf-8",
    )
    (REPORTS_DIR / "action_plan.md").write_text(
        action_plan(data["folder_totals"], stale, duplicates, development, projects, timestamp),
        encoding="utf-8",
    )
    print(f"Read-only audit completed: {len(records):,} files inventoried.")
    print(f"Reports written to: {REPORTS_DIR}")
    if data["notices"]:
        print(f"Notices while scanning: {len(data['notices']):,}")
        for notice in data["notices"][:10]:
            print(f"- {notice}")


def main() -> None:
    config = load_config()
    data = inventory(config)
    write_reports(config, data)


if __name__ == "__main__":
    main()
