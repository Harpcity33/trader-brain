from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

from .config import RuntimeConfig
from .storage import Store


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def ingest_once(
    database_path: Path,
    lesson_date: str,
    content: str,
    source: str = "trader_brain",
) -> dict[str, Any]:
    expected_hash = content_sha256(content)
    store = Store(database_path)
    try:
        rows = store.conn.execute(
            """SELECT lesson_id,content_text FROM trader_brain_lessons
               WHERE lesson_date=? AND source=? ORDER BY ingested_at DESC""",
            (lesson_date, source),
        ).fetchall()
        for row in rows:
            if content_sha256(row["content_text"]) == expected_hash:
                return {
                    "status": "already_ingested",
                    "lesson_id": row["lesson_id"],
                    "database": str(database_path),
                }
        lesson_id = store.ingest_lesson(
            lesson_date, source, content, "markdown", None
        )
        return {
            "status": "ingested",
            "lesson_id": lesson_id,
            "database": str(database_path),
        }
    finally:
        store.close()


def archive_report(
    project_root: Path,
    lesson_date: str,
    content: str,
    source: str = "trader_brain",
) -> dict[str, Any]:
    knowledge_root = project_root / "knowledge"
    index_path = knowledge_root / "index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        index = {"schema_version": 1, "reports": []}
    reports = index.setdefault("reports", [])
    digest = content_sha256(content)
    for report in reports:
        if report.get("date") == lesson_date and report.get("sha256") == digest:
            archive_path = project_root / report["path"]
            if not archive_path.exists() or archive_path.read_text(encoding="utf-8") != content:
                atomic_write_text(archive_path, content)
            return {"status": "already_archived", "path": report["path"]}

    same_date = [report for report in reports if report.get("date") == lesson_date]
    revision = len(same_date) + 1
    suffix = "" if revision == 1 else f"-r{revision}"
    relative_path = Path("knowledge/reports") / (
        f"{lesson_date}-trader-brain-handoff{suffix}.md"
    )
    archive_path = project_root / relative_path
    atomic_write_text(archive_path, content)
    reports.append(
        {
            "date": lesson_date,
            "source": source,
            "path": relative_path.as_posix(),
            "content_type": "text/markdown",
            "classification": "untrusted_research_input",
            "trade_authority": False,
            "production_change_authority": False,
            "revision": revision,
            "sha256": digest,
        }
    )
    atomic_write_text(index_path, json.dumps(index, indent=2) + "\n")
    return {"status": "archived", "path": relative_path.as_posix(), "revision": revision}


def ingest_daily_report(
    config_path: Path,
    source_directory: Path,
    lesson_date: str,
) -> dict[str, Any]:
    config = RuntimeConfig.load(config_path)
    source_path = source_directory / f"trader_brain_codex_{lesson_date}.md"
    if not source_path.is_file():
        raise FileNotFoundError(f"dated Trader Brain report not found: {source_path}")
    content = source_path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError(f"dated Trader Brain report is empty: {source_path}")

    archive = archive_report(config.project_root, lesson_date, content)
    databases = [config.database_path]
    workspace_mirror = config.project_root / "runtime/titan-intelligence.sqlite3"
    if workspace_mirror.resolve() != config.database_path.resolve():
        databases.append(workspace_mirror)
    ingestion = [ingest_once(path, lesson_date, content) for path in databases]
    return {
        "status": "complete",
        "lesson_date": lesson_date,
        "source_path": str(source_path),
        "sha256": content_sha256(content),
        "archive": archive,
        "ingestion": ingestion,
        "trade_authority": False,
        "production_change_authority": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idempotently archive and ingest one dated Trader Brain report"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--source-directory", type=Path, default=Path("/Users/harp/Documents")
    )
    parser.add_argument("--date", help="YYYY-MM-DD; defaults to the current ET date")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = RuntimeConfig.load(args.config)
    lesson_date = args.date or datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    try:
        result = ingest_daily_report(args.config, args.source_directory, lesson_date)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
