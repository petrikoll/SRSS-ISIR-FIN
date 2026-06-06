from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal, InvalidOperation
import json
import re
import sqlite3
import time
from typing import Any

from models import DATABASE_PATH, InsolvencyCase, InsolvencyDocument


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    # SQLite při delších úlohách AI často naráží na souběžné zápisy.
    # Timeout + busy_timeout dají databázi čas počkat místo okamžité chyby „database is locked“.
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # WAL může selhat u některých síťových/uzamčených souborů; aplikace musí pokračovat i bez něj.
        pass
    return conn




def _is_database_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc).casefold()


def _retry_on_database_locked(operation, *, attempts: int = 5, base_delay: float = 0.35):
    """Opakuje krátké DB zápisy, pokud SQLite dočasně zamkne databázi."""
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_database_locked_error(exc) or attempt == attempts - 1:
                raise
            last_exc = exc
            time.sleep(base_delay * (attempt + 1))
    if last_exc:
        raise last_exc
    return None


def _case_table_name() -> str:
    return getattr(InsolvencyCase, "__tablename__", "insolvency_cases")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_structured_data_schema_once() -> None:
    """Vytvoří V1 tabulky pro strukturovaná data z formulářových PDF."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS document_extraction (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                source_document_id INTEGER,
                document_family TEXT,
                document_type TEXT,
                raw_json TEXT NOT NULL,
                summary_text TEXT,
                confidence TEXT,
                status TEXT NOT NULL DEFAULT 'OK',
                extraction_error TEXT,
                model TEXT,
                extracted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_document_extraction_case ON document_extraction(case_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_document_extraction_document ON document_extraction(source_document_id)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS case_claims_review (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                source_document_id INTEGER,
                reviewed_at TEXT,
                reviewed_claims_count INTEGER,
                unsecured_claims_total REAL,
                unsecured_claims_without_subordinated_total REAL,
                subordinated_claims_total REAL,
                secured_claims_total REAL,
                unreviewed_claims_count INTEGER,
                debtor_denied_claims INTEGER,
                trustee_denied_claims INTEGER,
                eu_creditors_known INTEGER,
                summary_json TEXT,
                confidence TEXT,
                status TEXT NOT NULL DEFAULT 'OK',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_case_claims_review_case ON case_claims_review(case_id)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS case_performance_report (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                source_document_id INTEGER,
                reporting_period_start TEXT,
                reporting_period_end TEXT,
                signed_at TEXT,
                debtor_fulfils_obligations INTEGER,
                current_satisfaction_percent REAL,
                expected_satisfaction_3y_percent REAL,
                expected_satisfaction_5y_percent REAL,
                creditors_paid_total REAL,
                trustee_paid_total REAL,
                payment_source_summary TEXT,
                trustee_recommendation TEXT,
                trustee_recommendation_category TEXT,
                trustee_statement TEXT,
                deposit_note TEXT,
                warnings_json TEXT,
                confidence TEXT,
                status TEXT NOT NULL DEFAULT 'OK',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_case_performance_report_case_period ON case_performance_report(case_id, reporting_period_end)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS case_completion_report (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                source_document_id INTEGER,
                signed_at TEXT,
                bankruptcy_decision_date TEXT,
                debt_relief_permitted_date TEXT,
                debt_relief_approved_date TEXT,
                last_payment_date TEXT,
                unsecured_satisfaction_percent REAL,
                unsecured_satisfaction_amount REAL,
                secured_satisfaction_percent REAL,
                secured_satisfaction_amount REAL,
                overpayment_amount REAL,
                debt_relief_interrupted INTEGER,
                debt_relief_extended INTEGER,
                income_payer_stop_deductions_date TEXT,
                all_assets_sold INTEGER,
                debtor_fulfilled_all_obligations INTEGER,
                trustee_recommendation_completion TEXT,
                trustee_recommendation_discharge TEXT,
                course_of_proceeding_summary TEXT,
                warnings_json TEXT,
                confidence TEXT,
                status TEXT NOT NULL DEFAULT 'OK',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_case_completion_report_case ON case_completion_report(case_id)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS case_trustee_accounting (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                source_document_id INTEGER,
                trustee_total_fee_with_vat REAL,
                trustee_total_fee_without_vat REAL,
                review_fee REAL,
                reviewed_claims_count_for_fee INTEGER,
                duration_fee REAL,
                cash_expenses REAL,
                unpaid_amount REAL,
                remaining_to_satisfy REAL,
                months_count INTEGER,
                period_from TEXT,
                period_to TEXT,
                last_income_payer TEXT,
                comment TEXT,
                confidence TEXT,
                status TEXT NOT NULL DEFAULT 'OK',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_case_trustee_accounting_case ON case_trustee_accounting(case_id)")

        table = _case_table_name()
        additions = {
            "current_satisfaction_percent": "REAL",
            "current_expected_satisfaction_3y_percent": "REAL",
            "current_expected_satisfaction_5y_percent": "REAL",
            "latest_performance_period_start": "TEXT",
            "latest_performance_period_end": "TEXT",
            "debtor_fulfils_obligations": "INTEGER",
            "latest_trustee_recommendation": "TEXT",
            "latest_trustee_statement": "TEXT",
            "claims_review_total": "REAL",
            "claims_review_count": "INTEGER",
            "completion_satisfaction_percent": "REAL",
            "completion_report_date": "TEXT",
            "evaluation_status": "TEXT",
            "structured_needs_review": "INTEGER DEFAULT 0",
            "structured_last_updated_at": "TEXT",
        }
        for column, definition in additions.items():
            _add_column_if_missing(conn, table, column, definition)

        conn.commit()



def ensure_structured_data_schema() -> None:
    return _retry_on_database_locked(_ensure_structured_data_schema_once)

def normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def parse_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    text = normalize_text(value).casefold()
    if text in {"ano", "true", "1", "yes", "plni", "plní"}:
        return 1
    if text in {"ne", "false", "0", "no", "neplni", "neplní"}:
        return 0
    return None


def parse_decimal(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = normalize_text(value)
    text = text.replace("\u00a0", " ").replace(" ", "")
    text = text.replace("Kč", "").replace("%", "")
    text = text.replace("'", "").replace("̓", "")
    # český formát 1.234,56 nebo 1234,56; procenta z PDF někdy přijdou jako 22,5138.
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(Decimal(text))
    except (InvalidOperation, ValueError):
        match = re.search(r"-?\d+(?:[\s.̓]\d{3})*(?:,\d+)?|-?\d+(?:\.\d+)?", normalize_text(value))
        if not match:
            return None
        return parse_decimal(match.group(0))


def parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    amount = parse_decimal(value)
    if amount is None:
        return None
    return int(amount)


CZECH_MONTHS = {
    "leden": 1, "ledna": 1,
    "unor": 2, "unora": 2, "únor": 2, "února": 2,
    "brezen": 3, "brezna": 3, "březen": 3, "března": 3,
    "duben": 4, "dubna": 4,
    "kveten": 5, "kvetna": 5, "květen": 5, "května": 5,
    "cerven": 6, "cervna": 6, "červen": 6, "června": 6,
    "cervenec": 7, "cervence": 7, "červenec": 7, "července": 7,
    "srpen": 8, "srpna": 8,
    "zari": 9, "zari": 9, "září": 9,
    "rijen": 10, "rijna": 10, "říjen": 10, "října": 10,
    "listopad": 11, "listopadu": 11,
    "prosinec": 12, "prosince": 12,
}


def parse_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = normalize_text(value)
    if not text:
        return None
    # ISO nebo začátek ISO datetime
    match = re.search(r"(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})", text)
    if match:
        y, m, d = map(int, match.groups())
        return date(y, m, d).isoformat()
    # české datum
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if match:
        d, m, y = map(int, match.groups())
        return date(y, m, d).isoformat()
    # měsíc / rok
    match = re.search(r"([A-Za-zÁ-ž]+)\s*/\s*(\d{4})", text)
    if match:
        month_name = match.group(1).casefold()
        month = CZECH_MONTHS.get(month_name)
        if month:
            return date(int(match.group(2)), month, 1).isoformat()
    return None


def confidence_status(confidence: str | None, warnings: list[Any] | None = None) -> str:
    text = normalize_text(confidence).casefold()
    if text in {"low", "nízká", "nizka"}:
        return "NEEDS_REVIEW"
    return "OK"


DETAIL_CLAIM_KEYS = {
    "claims",
    "claim",
    "claim_items",
    "individual_claims",
    "creditors",
    "creditor_items",
    "distribution_scheme",
    "distribution_table",
    "claim_list",
    "claims_list",
    "seznam_pohledavek_detail",
}


def _prune_claim_detail_lists(value: Any) -> Any:
    """Odstraní detailní seznamy věřitelů/pohledávek z AI JSONu.

    V1 ukládá ze seznamu přihlášených pohledávek pouze souhrny. Jednotlivé
    pohledávky a věřitelé by zbytečně zvětšovali DB a zpomalovali shrnutí.
    """
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = normalize_text(key).casefold()
            if normalized_key in DETAIL_CLAIM_KEYS:
                continue
            cleaned[key] = _prune_claim_detail_lists(item)
        return cleaned
    if isinstance(value, list):
        # Pokud jde o seznam objektů připomínající jednotlivé pohledávky/věřitele,
        # do V1 ho neukládáme. Běžné krátké seznamy warnings/attachments ponecháme.
        if value and all(isinstance(item, dict) for item in value):
            object_keys = {normalize_text(key).casefold() for item in value for key in item.keys()}
            claim_like_keys = {
                "creditor", "creditor_name", "veritel", "věřitel", "claim_number",
                "prihlaska", "přihláška", "amount", "claimed_amount", "satisfied_amount",
                "uspokojeno", "prihlaseno", "přihlášeno",
            }
            if object_keys & claim_like_keys:
                return []
        return [_prune_claim_detail_lists(item) for item in value]
    return value


def document_has_extraction(document_id: int | None) -> bool:
    if not document_id:
        return False
    ensure_structured_data_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM document_extraction WHERE source_document_id = ? LIMIT 1",
            (document_id,),
        ).fetchone()
        return row is not None


def _insert_or_update_by_source(conn: sqlite3.Connection, table: str, source_document_id: int | None, values: dict[str, Any]) -> None:
    now = _now()
    values = dict(values)
    values.setdefault("created_at", now)
    values["updated_at"] = now
    if source_document_id:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE source_document_id = ? ORDER BY id DESC LIMIT 1",
            (source_document_id,),
        ).fetchone()
        if row:
            values.pop("created_at", None)
            assignments = ", ".join(f"{key} = ?" for key in values.keys())
            params = list(values.values()) + [row["id"]]
            conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", params)
            return
    columns = ", ".join(values.keys())
    placeholders = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(values.values()))


def _latest_case_period(conn: sqlite3.Connection, case_id: int) -> str | None:
    table = _case_table_name()
    if not _column_exists(conn, table, "latest_performance_period_end"):
        return None
    row = conn.execute(
        f"SELECT latest_performance_period_end FROM {table} WHERE id = ?",
        (case_id,),
    ).fetchone()
    return row["latest_performance_period_end"] if row else None


def _update_case_current_state(conn: sqlite3.Connection, case_id: int, values: dict[str, Any]) -> None:
    if not values:
        return
    table = _case_table_name()
    values = dict(values)
    values["structured_last_updated_at"] = _now()
    assignments = ", ".join(f"{key} = ?" for key in values.keys())
    conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", list(values.values()) + [case_id])


def _persist_structured_extraction_once(
    case: InsolvencyCase,
    documents: list[InsolvencyDocument],
    payload: dict[str, Any],
    *,
    model: str | None = None,
    summary_text: str | None = None,
) -> dict[str, Any]:
    """Uloží strukturovaný AI výstup z formulářového dokumentu do V1 tabulek.

    Vrací stručný výsledek pro logování/UI.
    """
    ensure_structured_data_schema()
    case_id = int(case.id)
    primary_document = documents[0] if documents else None
    source_document_id = int(primary_document.id) if primary_document and primary_document.id else None
    payload = _prune_claim_detail_lists(payload)
    raw_json = json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    document_type = normalize_text(payload.get("document_type") or "unknown")
    document_family = normalize_text(payload.get("document_family") or "debt_relief_structured_report")
    confidence = normalize_text(payload.get("confidence") or "")
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    status = confidence_status(confidence, warnings)
    now = _now()

    with _connect() as conn:
        _insert_or_update_by_source(
            conn,
            "document_extraction",
            source_document_id,
            {
                "case_id": case_id,
                "source_document_id": source_document_id,
                "document_family": document_family,
                "document_type": document_type,
                "raw_json": raw_json,
                "summary_text": summary_text,
                "confidence": confidence or None,
                "status": status,
                "extraction_error": None,
                "model": model,
                "extracted_at": now,
                "created_at": now,
            },
        )

        claims = payload.get("claims_review") if isinstance(payload.get("claims_review"), dict) else {}
        if any(claims.get(key) is not None for key in claims):
            total_with = parse_decimal(claims.get("reviewed_unsecured_claims_total"))
            total_without = parse_decimal(claims.get("reviewed_unsecured_claims_without_subordinated_total"))
            subordinated = None
            if total_with is not None and total_without is not None:
                subordinated = max(0.0, total_with - total_without)
            _insert_or_update_by_source(
                conn,
                "case_claims_review",
                source_document_id,
                {
                    "case_id": case_id,
                    "source_document_id": source_document_id,
                    "reviewed_at": parse_date(claims.get("review_meeting_date")),
                    "reviewed_claims_count": parse_int(claims.get("reviewed_claim_applications_count")),
                    "unsecured_claims_total": total_with,
                    "unsecured_claims_without_subordinated_total": total_without,
                    "subordinated_claims_total": subordinated,
                    "secured_claims_total": parse_decimal(claims.get("secured_claims_total")),
                    "unreviewed_claims_count": parse_int(claims.get("unreviewed_claims_count")),
                    "debtor_denied_claims": parse_bool(claims.get("claims_denied_by_debtor")),
                    "trustee_denied_claims": parse_bool(claims.get("claims_denied_by_trustee")),
                    "eu_creditors_known": parse_bool(claims.get("eu_creditors_known")),
                    "summary_json": json.dumps(claims, ensure_ascii=False, default=str),
                    "confidence": confidence or None,
                    "status": status,
                    "created_at": now,
                },
            )
            if status == "OK":
                update_values: dict[str, Any] = {}
                count = parse_int(claims.get("reviewed_claim_applications_count"))
                if count is not None:
                    update_values["claims_review_count"] = count
                if total_without is not None:
                    update_values["claims_review_total"] = total_without
                elif total_with is not None:
                    update_values["claims_review_total"] = total_with
                if update_values:
                    _update_case_current_state(conn, case_id, update_values)

        dates = payload.get("document_dates") if isinstance(payload.get("document_dates"), dict) else {}
        perf = payload.get("performance") if isinstance(payload.get("performance"), dict) else {}
        recommendation = payload.get("trustee_recommendation") if isinstance(payload.get("trustee_recommendation"), dict) else {}
        period_start = parse_date(dates.get("report_period_from"))
        period_end = parse_date(dates.get("report_period_to"))
        periods = dates.get("report_periods") if isinstance(dates.get("report_periods"), list) else []
        if not period_start and periods:
            period_start = parse_date(periods[0].get("from") if isinstance(periods[0], dict) else periods[0])
        if not period_end and periods:
            last = periods[-1]
            period_end = parse_date(last.get("to") if isinstance(last, dict) else last)
        if any(perf.get(key) is not None for key in perf):
            _insert_or_update_by_source(
                conn,
                "case_performance_report",
                source_document_id,
                {
                    "case_id": case_id,
                    "source_document_id": source_document_id,
                    "reporting_period_start": period_start,
                    "reporting_period_end": period_end,
                    "signed_at": parse_date(dates.get("signed_at")),
                    "debtor_fulfils_obligations": parse_bool(perf.get("debtor_fulfils_obligations")),
                    "current_satisfaction_percent": parse_decimal(perf.get("current_satisfaction_percent")),
                    "expected_satisfaction_3y_percent": parse_decimal(perf.get("expected_satisfaction_3y_percent")),
                    "expected_satisfaction_5y_percent": parse_decimal(perf.get("expected_satisfaction_5y_percent")),
                    "creditors_paid_total": parse_decimal(perf.get("creditors_paid_total")),
                    "trustee_paid_total": parse_decimal(perf.get("trustee_paid_total")),
                    "payment_source_summary": normalize_text(perf.get("payment_source")) or normalize_text(perf.get("debtor_payment_summary")) or None,
                    "trustee_recommendation": normalize_text(recommendation.get("recommendation_text")) or None,
                    "trustee_recommendation_category": normalize_text(recommendation.get("recommendation_category")) or None,
                    "trustee_statement": normalize_text(perf.get("trustee_statement")) or None,
                    "deposit_note": normalize_text(perf.get("deposit_note")) or None,
                    "warnings_json": json.dumps(warnings, ensure_ascii=False, default=str),
                    "confidence": confidence or None,
                    "status": status,
                    "created_at": now,
                },
            )
            latest_period = _latest_case_period(conn, case_id)
            if status == "OK" and period_end and (not latest_period or period_end > latest_period):
                _update_case_current_state(conn, case_id, {
                    "current_satisfaction_percent": parse_decimal(perf.get("current_satisfaction_percent")),
                    "current_expected_satisfaction_3y_percent": parse_decimal(perf.get("expected_satisfaction_3y_percent")),
                    "current_expected_satisfaction_5y_percent": parse_decimal(perf.get("expected_satisfaction_5y_percent")),
                    "latest_performance_period_start": period_start,
                    "latest_performance_period_end": period_end,
                    "debtor_fulfils_obligations": parse_bool(perf.get("debtor_fulfils_obligations")),
                    "latest_trustee_recommendation": normalize_text(recommendation.get("recommendation_text")) or None,
                    "latest_trustee_statement": normalize_text(perf.get("trustee_statement")) or None,
                    "evaluation_status": "performance_monitoring",
                    "structured_needs_review": 0,
                })
            elif status == "NEEDS_REVIEW":
                _update_case_current_state(conn, case_id, {"structured_needs_review": 1})

        proceeding = payload.get("proceeding_state") if isinstance(payload.get("proceeding_state"), dict) else {}
        completion = payload.get("completion") if isinstance(payload.get("completion"), dict) else {}
        if any(completion.get(key) is not None for key in completion):
            rec_text = normalize_text(recommendation.get("recommendation_text"))
            _insert_or_update_by_source(
                conn,
                "case_completion_report",
                source_document_id,
                {
                    "case_id": case_id,
                    "source_document_id": source_document_id,
                    "signed_at": parse_date(dates.get("signed_at")),
                    "bankruptcy_decision_date": parse_date(proceeding.get("bankruptcy_decision_date")),
                    "debt_relief_permitted_date": parse_date(proceeding.get("debt_relief_permitted_date")),
                    "debt_relief_approved_date": parse_date(proceeding.get("debt_relief_approved_date")),
                    "last_payment_date": parse_date(proceeding.get("last_payment_date")),
                    "unsecured_satisfaction_percent": parse_decimal(completion.get("unsecured_satisfaction_percent")),
                    "unsecured_satisfaction_amount": parse_decimal(completion.get("unsecured_satisfaction_amount")),
                    "secured_satisfaction_percent": parse_decimal(completion.get("secured_satisfaction_percent")),
                    "secured_satisfaction_amount": parse_decimal(completion.get("secured_satisfaction_amount")),
                    "overpayment_amount": parse_decimal(completion.get("overpayment_amount")),
                    "debt_relief_interrupted": parse_bool(completion.get("debt_relief_interrupted")),
                    "debt_relief_extended": parse_bool(completion.get("debt_relief_extended")),
                    "income_payer_stop_deductions_date": parse_date(completion.get("income_payer_stop_deductions_date")),
                    "all_assets_sold": parse_bool(completion.get("all_assets_sold")),
                    "debtor_fulfilled_all_obligations": parse_bool(completion.get("debtor_fulfilled_all_obligations")),
                    "trustee_recommendation_completion": rec_text if "spln" in rec_text.casefold() else None,
                    "trustee_recommendation_discharge": rec_text if "osvobo" in rec_text.casefold() else None,
                    "course_of_proceeding_summary": normalize_text(completion.get("course_of_proceeding_summary")) or None,
                    "warnings_json": json.dumps(warnings, ensure_ascii=False, default=str),
                    "confidence": confidence or None,
                    "status": status,
                    "created_at": now,
                },
            )
            if status == "OK":
                _update_case_current_state(conn, case_id, {
                    "completion_satisfaction_percent": parse_decimal(completion.get("unsecured_satisfaction_percent")),
                    "completion_report_date": parse_date(dates.get("signed_at")),
                    "evaluation_status": "completion_report_received",
                    "structured_needs_review": 0,
                })
            else:
                _update_case_current_state(conn, case_id, {"structured_needs_review": 1})

        accounting = payload.get("trustee_accounting") if isinstance(payload.get("trustee_accounting"), dict) else {}
        if any(accounting.get(key) is not None for key in accounting):
            _insert_or_update_by_source(
                conn,
                "case_trustee_accounting",
                source_document_id,
                {
                    "case_id": case_id,
                    "source_document_id": source_document_id,
                    "trustee_total_fee_with_vat": parse_decimal(accounting.get("trustee_total_fee_with_vat")),
                    "trustee_total_fee_without_vat": parse_decimal(accounting.get("trustee_total_fee_without_vat")),
                    "review_fee": parse_decimal(accounting.get("review_fee")),
                    "reviewed_claims_count_for_fee": parse_int(accounting.get("reviewed_claims_count_for_fee")),
                    "duration_fee": parse_decimal(accounting.get("duration_fee")),
                    "cash_expenses": parse_decimal(accounting.get("cash_expenses")),
                    "unpaid_amount": parse_decimal(accounting.get("unpaid_amount")),
                    "remaining_to_satisfy": parse_decimal(accounting.get("remaining_to_satisfy")),
                    "months_count": parse_int(accounting.get("months_count")),
                    "period_from": parse_date(accounting.get("period_from")),
                    "period_to": parse_date(accounting.get("period_to")),
                    "last_income_payer": normalize_text(accounting.get("last_income_payer")) or None,
                    "comment": normalize_text(accounting.get("comment")) or None,
                    "confidence": confidence or None,
                    "status": status,
                    "created_at": now,
                },
            )

        conn.commit()

    return {
        "status": status,
        "document_type": document_type,
        "source_document_id": source_document_id,
    }



def persist_structured_extraction(
    case: InsolvencyCase,
    documents: list[InsolvencyDocument],
    payload: dict[str, Any],
    *,
    model: str | None = None,
    summary_text: str | None = None,
) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        return _persist_structured_extraction_once(
            case,
            documents,
            payload,
            model=model,
            summary_text=summary_text,
        )

    return _retry_on_database_locked(operation, attempts=6, base_delay=0.5)


def get_document_extraction_payload(document_id: int | None) -> dict[str, Any] | None:
    """Vrátí poslední uložený strukturovaný AI výstup pro dokument, pokud existuje a je použitelný."""
    if not document_id:
        return None
    ensure_structured_data_schema()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT raw_json, status, confidence, document_type, updated_at
            FROM document_extraction
            WHERE source_document_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("_cached_extraction", True)
        payload.setdefault("_cached_status", row["status"])
        payload.setdefault("_cached_confidence", row["confidence"])
        payload.setdefault("_cached_document_type", row["document_type"])
        payload.setdefault("_cached_updated_at", row["updated_at"])
        return payload


def get_document_extraction_payloads(document_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Vrátí mapu document_id -> raw JSON payload pro dokumenty s uloženou extrakcí."""
    result: dict[int, dict[str, Any]] = {}
    for document_id in document_ids:
        payload = get_document_extraction_payload(document_id)
        if payload is not None:
            result[int(document_id)] = payload
    return result


def _first_positive_decimal(*values: Any) -> float | None:
    """Vrátí první nenulovou kladnou částku z předaných hodnot."""
    for value in values:
        amount = parse_decimal(value)
        if amount is not None and amount > 0:
            return amount
    return None


def get_case_claims_review_summary(case_id: int | None) -> dict[str, Any]:
    """Vrátí použitelný souhrn přezkoumaných/přiznaných pohledávek pro UI.

    Dříve se bral jen poslední řádek podle data. U některých spisů ale pozdější
    formulář obsahuje počet přihlášek, zatímco souhrnná částka je v jiném
    formuláři téhož přezkumu. Proto se hodnota pro horní kartu klienta skládá
    z nejnovějších dostupných použitelných údajů napříč řádky případu.
    """
    if not case_id:
        return {}
    ensure_structured_data_schema()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM case_claims_review
            WHERE case_id = ?
            ORDER BY COALESCE(reviewed_at, updated_at) DESC, id DESC
            """,
            (case_id,),
        ).fetchall()
        if not rows:
            return {}

        count_row = next((row for row in rows if row["reviewed_claims_count"] is not None), None)

        total_row = None
        total_value = None
        for row in rows:
            candidate = _first_positive_decimal(
                row["unsecured_claims_without_subordinated_total"],
                row["unsecured_claims_total"],
                row["secured_claims_total"],
            )
            if candidate is not None:
                total_row = row
                total_value = candidate
                break

        source_row = total_row or count_row or rows[0]
        count = count_row["reviewed_claims_count"] if count_row is not None else source_row["reviewed_claims_count"]

        return {
            "reviewed_at": source_row["reviewed_at"],
            "claims_count": count,
            "claims_total": total_value,
            "unsecured_claims_total": source_row["unsecured_claims_total"],
            "unsecured_claims_without_subordinated_total": source_row["unsecured_claims_without_subordinated_total"],
            "subordinated_claims_total": source_row["subordinated_claims_total"],
            "secured_claims_total": source_row["secured_claims_total"],
            "status": source_row["status"],
            "confidence": source_row["confidence"],
            "source_document_id": source_row["source_document_id"],
        }


def get_case_structured_snapshot(case_id: int) -> str:
    """Vrátí krátký textový snapshot uložených strukturovaných dat pro prompt kazuistiky."""
    ensure_structured_data_schema()
    lines: list[str] = []
    with _connect() as conn:
        review = conn.execute(
            "SELECT * FROM case_claims_review WHERE case_id = ? ORDER BY COALESCE(reviewed_at, updated_at) DESC, id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if review:
            lines.append("Přezkum pohledávek ze strukturovaných dat:")
            if review["reviewed_claims_count"] is not None:
                lines.append(f"- Počet přezkoumaných přihlášek: {review['reviewed_claims_count']}")
            if review["unsecured_claims_without_subordinated_total"] is not None:
                lines.append(f"- Nezajištěné nepodřízené pohledávky: {review['unsecured_claims_without_subordinated_total']} Kč")
            elif review["unsecured_claims_total"] is not None:
                lines.append(f"- Nezajištěné pohledávky celkem: {review['unsecured_claims_total']} Kč")
            if review["unreviewed_claims_count"] is not None:
                lines.append(f"- Nepřezkoumané pohledávky: {review['unreviewed_claims_count']}")

        performance = conn.execute(
            "SELECT * FROM case_performance_report WHERE case_id = ? ORDER BY COALESCE(reporting_period_end, signed_at, updated_at) DESC, id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if performance:
            lines.append("Poslední zpráva o plnění ze strukturovaných dat:")
            if performance["reporting_period_start"] or performance["reporting_period_end"]:
                lines.append(f"- Období: {performance['reporting_period_start'] or '?'} až {performance['reporting_period_end'] or '?'}")
            if performance["debtor_fulfils_obligations"] is not None:
                lines.append(f"- Dlužník plní povinnosti: {'ano' if performance['debtor_fulfils_obligations'] else 'ne'}")
            if performance["current_satisfaction_percent"] is not None:
                lines.append(f"- Aktuální míra uspokojení: {performance['current_satisfaction_percent']} %")
            if performance["expected_satisfaction_3y_percent"] is not None:
                lines.append(f"- Očekávaná míra za 3 roky: {performance['expected_satisfaction_3y_percent']} %")
            if performance["expected_satisfaction_5y_percent"] is not None:
                lines.append(f"- Očekávaná míra za 5 let: {performance['expected_satisfaction_5y_percent']} %")
            if performance["trustee_recommendation"]:
                lines.append(f"- Doporučení správce: {performance['trustee_recommendation']}")
            if performance["trustee_statement"]:
                lines.append(f"- Vyjádření správce: {performance['trustee_statement'][:600]}")

        completion = conn.execute(
            "SELECT * FROM case_completion_report WHERE case_id = ? ORDER BY COALESCE(signed_at, updated_at) DESC, id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if completion:
            lines.append("Závěrečná zpráva o splnění ze strukturovaných dat:")
            if completion["signed_at"]:
                lines.append(f"- Datum zprávy/podpisu: {completion['signed_at']}")
            if completion["last_payment_date"]:
                lines.append(f"- Poslední splátka: {completion['last_payment_date']}")
            if completion["unsecured_satisfaction_percent"] is not None:
                lines.append(f"- Skutečná míra uspokojení nezajištěných věřitelů: {completion['unsecured_satisfaction_percent']} %")
            if completion["unsecured_satisfaction_amount"] is not None:
                lines.append(f"- Skutečně uhrazeno nezajištěným věřitelům: {completion['unsecured_satisfaction_amount']} Kč")
            if completion["debtor_fulfilled_all_obligations"] is not None:
                lines.append(f"- Dlužník splnil povinnosti: {'ano' if completion['debtor_fulfilled_all_obligations'] else 'ne'}")
            if completion["trustee_recommendation_completion"] or completion["trustee_recommendation_discharge"]:
                lines.append(f"- Doporučení správce: {completion['trustee_recommendation_completion'] or ''} {completion['trustee_recommendation_discharge'] or ''}".strip())

        accounting = conn.execute(
            "SELECT * FROM case_trustee_accounting WHERE case_id = ? ORDER BY COALESCE(period_to, updated_at) DESC, id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if accounting:
            lines.append("Vyúčtování správce ze strukturovaných dat:")
            if accounting["trustee_total_fee_with_vat"] is not None:
                lines.append(f"- Celková odměna správce s DPH: {accounting['trustee_total_fee_with_vat']} Kč")
            if accounting["cash_expenses"] is not None:
                lines.append(f"- Hotové výdaje správce: {accounting['cash_expenses']} Kč")
            if accounting["remaining_to_satisfy"] is not None:
                lines.append(f"- Správci zbývá uspokojit: {accounting['remaining_to_satisfy']} Kč")

    if not lines:
        return "Strukturovaná data z formulářových PDF zatím nejsou uložena."
    return "\n".join(lines)
