"""Validate a data archive entirely before it can replace the live data."""
from contextlib import closing
from datetime import date
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import zipfile


MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_EXPANDED_BYTES = 10 * 1024**3
MAX_ARCHIVE_FILES = 100_000
STRUCTURED_TABLES = (
    "document_extraction", "case_claims_review", "case_performance_report",
    "case_completion_report", "case_trustee_accounting",
)


def safe_member_name(name):
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in ("..", ".") or ":" in part for part in path.parts):
        raise ValueError("Záloha obsahuje neplatnou cestu.")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
    if any(part.endswith((" ", ".")) or any(char in part for char in '<>"|?*') or part.split(".")[0].upper() in reserved for part in path.parts):
        raise ValueError("Záloha obsahuje název souboru nepodporovaný ve Windows.")
    return path.as_posix()


def relative_document_path(value):
    normalized = str(value).replace("\\", "/")
    marker = "/downloaded_documents/"
    if normalized.startswith("downloaded_documents/"):
        relative = normalized[len("downloaded_documents/"):]
    elif marker in normalized:
        relative = normalized.split(marker, 1)[1]
    else:
        raise ValueError("Záloha obsahuje cestu dokumentu mimo úložiště aplikace.")
    relative = safe_member_name(relative)
    if relative in ("", "."):
        raise ValueError("Záloha obsahuje neplatnou cestu dokumentu.")
    return Path(relative)


def validate_database(path):
    required = {
        "clients": {"id", "first_name", "last_name", "birth_date"},
        "insolvency_cases": {"id", "client_id", "spisova_znacka"},
        "insolvency_documents": {"id", "case_id", "local_path", "source_url", "title"},
        "insolvency_changes": {"id", "client_id"},
    }
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Databáze v záloze neprošla kontrolou integrity.")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise ValueError("Databáze obsahuje nepodporované triggery.")
        for table, columns in required.items():
            if table not in tables:
                raise ValueError("ZIP neobsahuje databázi ISIR Kontrola.")
            actual = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if not columns <= actual:
                raise ValueError("Databáze má neplatnou strukturu.")
        for row in conn.execute("SELECT birth_date FROM clients"):
            date.fromisoformat(str(row[0]))
        for row in conn.execute("SELECT local_path FROM insolvency_documents WHERE local_path IS NOT NULL AND local_path != ''"):
            relative_document_path(row[0])
        for child, parent, column in (("insolvency_cases", "clients", "client_id"), ("insolvency_documents", "insolvency_cases", "case_id"), ("insolvency_changes", "clients", "client_id")):
            if conn.execute(f'SELECT 1 FROM "{child}" c LEFT JOIN "{parent}" p ON p.id=c."{column}" WHERE p.id IS NULL LIMIT 1').fetchone():
                raise ValueError("Databáze obsahuje záznamy bez příslušného klienta nebo řízení.")
        return {table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in required}


def stage_archive(uploaded_file, staging):
    archive_path = staging / "restore.zip"
    uploaded_file.save(archive_path)
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("ZIP přesahuje maximální velikost 2 GB.")
    database = staging / "app.db"
    documents = staging / "downloaded_documents"
    documents.mkdir()
    rules = staging / "manual_download_rules.json"
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_FILES or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
            raise ValueError("Rozbalená záloha přesahuje podporovaný rozsah.")
        seen = set()
        for member in members:
            name = safe_member_name(member.filename)
            if name.casefold() in seen or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("Záloha obsahuje duplicitní nebo neplatné soubory.")
            seen.add(name.casefold())
            if member.is_dir():
                continue
            if name == "data/app.db":
                target = database
            elif name == "data/manual_download_rules.json":
                target = rules
            elif name.startswith("downloaded_documents/"):
                relative = Path(name).relative_to("downloaded_documents")
                target = (documents / relative).resolve()
                if documents.resolve() not in target.parents:
                    raise ValueError("Neplatná cesta dokumentu v záloze.")
            elif name == "README.txt":
                continue
            else:
                raise ValueError("Záloha obsahuje nepodporovaný soubor.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    if not database.exists():
        raise ValueError("Archiv neobsahuje databázi data/app.db.")
    counts = validate_database(database)
    with closing(sqlite3.connect(database)) as conn:
        for row in conn.execute("SELECT local_path FROM insolvency_documents WHERE local_path IS NOT NULL AND local_path != ''"):
            if not (documents / relative_document_path(row[0])).is_file():
                raise ValueError("V záloze chybí dokument uvedený v databázi. Aktuální data nebyla změněna.")
    if rules.exists():
        payload = json.loads(rules.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Vlastní pravidla v záloze nemají správný formát.")
    return database, documents, rules if rules.exists() else None, counts


def delete_structured_for_cases(connection, case_ids):
    if not case_ids:
        return
    existing = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    parameters = tuple(case_ids)
    placeholders = ",".join("?" for _ in parameters)
    for table in STRUCTURED_TABLES:
        if table in existing:
            connection.exec_driver_sql(f'DELETE FROM "{table}" WHERE case_id IN ({placeholders})', parameters)
