from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from apscheduler.schedulers.background import BackgroundScheduler
from lxml import html
from requests import Session
from sqlalchemy import case, exc as sqlalchemy_exc
from zeep import Client as SoapClient, client
from zeep.helpers import serialize_object
from zeep.transports import Transport

from models import Client, InsolvencyCase, InsolvencyChange, InsolvencyDocument, SessionLocal
from ai_analysis import (
    extract_claim_amount_from_pdf_ai,
    analyze_case_study,
    extract_structured_report_from_pdf_ai,
    persist_structured_report_payload_for_case,
)
from structured_data import document_has_extraction
from storage_paths import DOCUMENTS_DIR
from runtime_guard import CoordinatedExecutor
from pdf_io import download_pdf_content, write_pdf_atomic


ISIR_WSDL = "https://isir.justice.cz:8443/isir_cuzk_ws/IsirWsCuzkService?wsdl"
ISIR_PUBLIC_WSDL = "https://isir.justice.cz:8443/isir_public_ws/IsirWsPublicService?wsdl"
PUBLIC_EVENT_LOOKBACK = 500
USE_PUBLIC_EVENT_WS = False
DOCUMENT_STORAGE = DOCUMENTS_DIR

logger = logging.getLogger(__name__)


def make_soap_client() -> SoapClient:
    session = Session()
    session.trust_env = False
    transport = Transport(session=session, timeout=20, operation_timeout=30)
    try:
        return SoapClient(wsdl=ISIR_WSDL, transport=transport)
    except BaseException:
        session.close()
        raise


def make_public_soap_client() -> SoapClient:
    session = Session()
    session.trust_env = False
    transport = Transport(session=session, timeout=10, operation_timeout=12)
    try:
        return SoapClient(wsdl=ISIR_PUBLIC_WSDL, transport=transport)
    except BaseException:
        session.close()
        raise


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _find_result_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for value in data for item in _find_result_rows(value)]
    if not isinstance(data, dict):
        return []

    likely_row_keys = {"druhStavKonkursu", "urlDetailRizeni", "relevanceVysledku", "nazevOsoby"}
    if likely_row_keys.intersection(data.keys()):
        return [data]

    rows: list[dict[str, Any]] = []
    for value in data.values():
        rows.extend(_find_result_rows(value))
    return rows


def _stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _result_hash(rows: list[dict[str, Any]]) -> str:
    important = [
        {
            "cisloSenatu": row.get("cisloSenatu"),
            "druhVec": row.get("druhVec"),
            "bcVec": row.get("bcVec"),
            "rocnik": row.get("rocnik"),
            "stav": row.get("druhStavKonkursu"),
            "url": row.get("urlDetailRizeni"),
            "zahajeni": row.get("datumPmZahajeniUpadku"),
            "ukonceni": row.get("datumPmUkonceniUpadku"),
        }
        for row in rows
    ]
    payload = _stable_json(important)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _case_label(row: dict[str, Any]) -> str:
    senat = row.get("cisloSenatu")
    druh_vec = row.get("druhVec") or "INS"
    bc_vec = row.get("bcVec")
    rocnik = row.get("rocnik")
    if senat and bc_vec and rocnik:
        return f"{senat} {druh_vec} {bc_vec}/{rocnik}"
    if bc_vec and rocnik:
        return f"{druh_vec} {bc_vec}/{rocnik}"
    return "bez spisové značky"


def _address(row: dict[str, Any]) -> str:
    parts = [
        row.get("ulice"),
        row.get("cisloPopisne"),
        row.get("mesto"),
        row.get("psc"),
        row.get("okres"),
        row.get("zeme"),
    ]
    return ", ".join(str(part) for part in parts if part)


def _debtor_name(row: dict[str, Any]) -> str:
    if row.get("nazevOrganizace"):
        return str(row["nazevOrganizace"])

    parts = [
        row.get("titulPred"),
        row.get("jmeno"),
        row.get("nazevOsoby"),
        row.get("titulZa"),
    ]
    return " ".join(str(part) for part in parts if part)


def _upsert_case(client: Client, row: dict[str, Any]) -> None:
    spisova_znacka = _case_label(row)
    if spisova_znacka == "bez spisové značky":
        return

    case = next((item for item in client.cases if item.spisova_znacka == spisova_znacka), None)
    if case is None:
        case = InsolvencyCase(spisova_znacka=spisova_znacka)
        client.cases.append(case)

    case.debtor_name = _debtor_name(row)
    case.address = _address(row)
    case.state = row.get("druhStavKonkursu")
    case.detail_url = row.get("urlDetailRizeni")
    case.started_at = row.get("datumPmZahajeniUpadku")
    case.ended_at = row.get("datumPmUkonceniUpadku")
    case.raw_result = _stable_json(row)


def _parse_czech_datetime(value: str) -> datetime | None:
    value = " ".join(value.split())
    for date_format in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            pass
    return None


def _add_months(value: datetime, months: int) -> datetime:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    days_in_month = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(value.day, days_in_month[month - 1])
    return value.replace(year=year, month=month, day=day)


def _safe_filename(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_")
    return value[:90] or "dokument"


def _document_folder_name(document: InsolvencyDocument) -> str:
    case = document.case
    client = case.client if case is not None else None
    if client is None:
        case_id = document.case_id or (case.id if case else None)
        return f"nezarazeno_{case_id or 'bez_cisla'}"

    birth_date = client.birth_date.isoformat() if client.birth_date else "bez_data"
    return _safe_filename(f"{client.last_name}_{client.first_name}_{birth_date}")


def _download_document(session: Session, document: InsolvencyDocument) -> None:
    if document.deleted_at:
        return
    case_dir = DOCUMENT_STORAGE / _document_folder_name(document)
    case_dir.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.sha1(document.source_url.encode("utf-8")).hexdigest()[:10]
    filename = f"{url_hash}_{_safe_filename(document.title)}.pdf"
    target = case_dir / filename

    if document.local_path:
        current = Path(document.local_path)
        if current.exists():
            if current.resolve() != target.resolve():
                write_pdf_atomic(target, current.read_bytes())
                document.local_path = str(target)
            return

    content = download_pdf_content(session, document.source_url)
    write_pdf_atomic(target, content)

    document.local_path = str(target)
    document.file_size = len(content)


def _upsert_document(
    case: InsolvencyCase,
    event_at: datetime | None,
    title: str,
    document_type: str,
    source_url: str,
) -> InsolvencyDocument:
    document = next((item for item in case.documents if item.source_url == source_url), None)
    if document is None:
        document = InsolvencyDocument(source_url=source_url)
        case.documents.append(document)

    document.event_at = event_at
    document.title = title or "Dokument"
    document.document_type = document_type
    return document


from typing import Union
from pathlib import Path

def _read_pdf_text(path: Union[str, Path]) -> str:
    pdf_path = Path(path)

    if not pdf_path.exists():
        return ""

    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("Chybí balíček pypdf")
        return ""

    try:
        with open(pdf_path, "rb") as f:
            reader = PdfReader(f)
            pages_text = []

            for page in reader.pages:
                try:
                    text = page.extract_text()
                    if text:
                        pages_text.append(text)
                except Exception:
                    continue

            return "\n".join(pages_text)

    except Exception as e:
        logger.warning("Nepodařilo se přečíst PDF: %s (%s)", pdf_path, e)
        return ""




def _parse_czech_money(value: str) -> Decimal | None:
    value = value.replace("\xa0", " ")
    value = re.sub(r"[^\d,.\s]", "", value)
    value = value.strip()

    if not value:
        return None

    value = value.replace(" ", "")

    if "," in value and "." in value:
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", ".")
    elif value.count(".") > 1:
        value = value.replace(".", "")

    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _format_czech_money(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"))
    number = f"{value:,.2f}"
    number = number.replace(",", " ").replace(".", ",")
    return f"{number} Kč"


def _extract_first_money_after_label(text: str, label: str) -> Decimal | None:
    compact_text = " ".join(text.replace("\xa0", " ").split())

    pattern = (
        re.escape(label)
        + r"\s*:?\s*"
        + r"([0-9][0-9\s.,]*)(?:\s*Kč|\s*CZK)?"
    )

    match = re.search(pattern, compact_text, flags=re.IGNORECASE)
    if not match:
        return None

    return _parse_czech_money(match.group(1))


def _extract_claim_amount_from_pdf_text(text: str, title: str = "") -> Decimal | None:
    if not text:
        return None

    text = " ".join(text.split())
    title_cf = (title or "").casefold()

    # ✅ 1) klasická přihláška
    match = re.search(
        r"Celková výše přihlášených pohledávek.*?([0-9][^\d]*[0-9])\s*K",
        text,
        re.IGNORECASE,
    )

    if match:
        raw = match.group(1)
        cislo = re.sub(r"[^\d]", "", raw)
        if cislo:
            return Decimal(cislo)

    # ✅ 2) pohledávka za podstatou
    match = re.search(
        r"Zbývá k uspokojení.*?([0-9][^\d]*[0-9])\s*K",
        text,
        re.IGNORECASE,
    )

    if match:
        raw = match.group(1)
        cislo = re.sub(r"[^\d]", "", raw)
        if cislo:
            return Decimal(cislo)

    return None



def _is_claim_amount_document(document: InsolvencyDocument) -> bool:
    title = (document.title or "").casefold()
    document_type = (document.document_type or "").casefold()

    if "vedlejší dokument" in document_type:
        return False

    return (
        "přihláška pohledávky" in title
        or "návrh na uspokojení pohledávky za podstatou" in title
    )


def _parse_claim_deadline_date(value: str | None) -> datetime | None:
    if not value:
        return None
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", str(value))
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _claim_collection_is_running(case: InsolvencyCase) -> bool:
    """Vrací True jen tehdy, když bezpečně běží lhůta pro přihlášky.

    Jednotlivé přihlášky čteme AI jen během této lhůty. Po jejím skončení
    se částky berou ze souhrnných formulářů přezkumu/seznamu pohledávek.
    Pokud lhůtu neumíme určit, raději jednotlivé přihlášky nečteme.
    """
    deadline = _parse_claim_deadline_date(case.claims_deadline)
    if deadline is None:
        return False
    return datetime.now().date() <= deadline.date()


def _update_claims_amounts_from_pdfs(
    case: InsolvencyCase,
    new_document_urls: set[str] | None = None,
) -> None:
    """
    AI extrakce částek z PDF.

    Pravidlo:
    - pokud už je u case uložená celková částka, AI čte jen nově přidané dokumenty,
    - pokud celková částka zatím není uložená, AI může zpracovat všechny relevantní dokumenty
      jako první naplnění dat.
    """

    if not _claim_collection_is_running(case):
        print("AI částky: lhůta pro přihlášky neběží nebo není bezpečně určena, přeskakuji čtení jednotlivých přihlášek.")
        return

    already_has_total = bool(case.claims_total_amount)

    if already_has_total and not new_document_urls:
        print("AI částky: žádné nové dokumenty, přeskakuji.")
        return

    existing_total = Decimal("0")

    if case.claims_total_amount:
        parsed_existing = _parse_czech_money(case.claims_total_amount)
        if parsed_existing is not None:
            existing_total = parsed_existing

    added_total = Decimal("0")
    found_any_amount = False

    print("=== START AI PARSOVANI CASTEK Z NOVYCH PDF ===")
    print("CASE:", case.spisova_znacka)
    print("STAVAJICI CASTKA:", case.claims_total_amount or "není")
    print("POCET DOKUMENTU:", len(case.documents))

    for document in case.documents:
        print("\n------")
        print("DOKUMENT:", document.title)

        if not _is_claim_amount_document(document):
            print("PRESKOCENO (neni relevantni typ)")
            continue

        if not document.local_path:
            print("PRESKOCENO (chybi cesta k souboru)")
            continue

        # Pokud už existuje celková částka, zpracujeme jen nové dokumenty.
        if already_has_total:
            if not document.source_url or document.source_url not in new_document_urls:
                print("PRESKOCENO (dokument neni novy)")
                continue

        print("CESTA:", document.local_path)

        try:
            amount = extract_claim_amount_from_pdf_ai(
                pdf_path=document.local_path,
                document_title=document.title or "",
            )
        except Exception as exc:
            logger.warning(
                "AI extrakce castky selhala pro dokument %s: %s",
                document.title,
                exc,
            )
            print("AI CHYBA:", exc)
            continue

        print("AI CASTKA:", amount)

        if amount is None:
            logger.info("AI nenasla castku v PDF: %s", document.title)
            continue

        added_total += Decimal(str(amount))
        found_any_amount = True

    if found_any_amount:
        new_total = existing_total + added_total
        case.claims_total_amount = _format_czech_money(new_total)

        print("PRIDANA CASTKA:", _format_czech_money(added_total))
        print("NOVA CELKOVA CASTKA:", case.claims_total_amount)
    else:
        print("NIC NOVEHO SE NENASLO")

def _is_case_study_relevant_document(document: InsolvencyDocument) -> bool:
    title = (document.title or "").casefold()
    document_type = (document.document_type or "").casefold()

    if "vedlejší dokument" in document_type or "vedlejší dokument" in title:
        return False

    important_words = (
        "insolvenční návrh",
        "návrh na povolení oddlužení",
        "usnesení",
        "sdělení insolvenčního správce",
        "insolvenční správce",
        "zpráva",
        "přezkumn",
        "seznam",
        "oddlužení",
        "opatření",
        "soupis",
        "neschválení",
        "neschválen",
        "zrušení oddlužení",
        "zrušuje oddlužení",
        "ukončení",
        "odškrtnutí",
        "odškrtnuta",
    )

    return any(word in title for word in important_words)




def _normalize_document_text(value: str | None) -> str:
    try:
        import unicodedata
        text = unicodedata.normalize("NFKD", str(value or "").casefold())
        text = "".join(char for char in text if not unicodedata.combining(char))
    except Exception:
        text = str(value or "").casefold()
    return re.sub(r"\s+", " ", text).strip()


def _is_structured_form_document(document: InsolvencyDocument) -> bool:
    """Vrací True jen pro skutečné formulářové PDF, ze kterých chceme ukládat strukturovaná data.

    Důležité: toto není filtr pro stahování PDF ani pro kazuistiku. Je to jen úzký filtr pro
    strukturovanou extrakci. Proto výslovně vylučujeme vyhlášky/usnesení/výzvy, i když v názvu
    obsahují slova jako „zpráva o přezkumu“.
    """
    title = _normalize_document_text(document.title)
    document_type = _normalize_document_text(document.document_type)
    text = f"{title} {document_type}"

    # Nevyžadujeme local_path už ve filtru. Při čistém importu nového klienta
    # může být dokument evidovaný z ISIR, ale PDF ještě nemusí být fyzicky stažené.
    # Pokud jde o skutečný formulářový dokument, stáhneme ho až těsně před extrakcí.

    if "vedlejsi dokument" in text:
        return False

    excluded_phrases = (
        "vyhlaska",
        "usneseni",
        "vyzva",
        "pripis",
        "oznameni",
        "uredni zaznam",
        "opatreni",
        "vypis z katastru",
        "navrh - prilohy",
        "priloha",
        "prilohy",
        "prihlaska pohledavky",
        "doplneni/oprava/zmena prihlasene pohledavky",
        "navrh na zmenu v osobe veritele",
        "oznameni o zmene v osobe veritele",
        "zmena v osobe veritele",
    )
    if any(phrase in text for phrase in excluded_phrases):
        return False

    included_phrases = (
        "zprava pro oddluzeni",
        "zprava o prezkumu",
        "seznam prihlasenych pohledavek",
        "seznam/upraveny seznam prihlasenych pohledavek",
        "upraveny seznam prihlasenych pohledavek",
        "sdeleni spravce o plneni oddluzeni",
        "zprava o plneni oddluzeni",
        "sdeleni spravce o splneni oddluzeni",
        "zprava o splneni oddluzeni",
        "vyuctovani odmeny",
        "vyuctovani hotovych vydaju",
        "navrh na osvobozeni",
        "soupis majetkove podstaty",
    )
    return any(phrase in text for phrase in included_phrases)


def _structured_form_documents_without_extraction(case: InsolvencyCase) -> list[InsolvencyDocument]:
    documents = []
    for document in sorted(case.documents, key=lambda item: (item.event_at or datetime.min, item.id or 0)):
        if document.deleted_at:
            continue
        if not _is_structured_form_document(document):
            continue
        if document_has_extraction(document.id):
            continue
        documents.append(document)
    return documents


def _extract_structured_form_data_for_case(
    case: InsolvencyCase,
    progress_callback: ProgressCallback | None = None,
    client: Client | None = None,
) -> dict[str, int]:
    """Vytěží strukturovaná data jen z úzkého seznamu formulářových dokumentů.

    Funkce předpokládá, že hlavní SQLAlchemy session už byla commitnutá, aby SQLite
    nedržela zámek při ukládání výsledků přes structured_data.
    """
    candidates = _structured_form_documents_without_extraction(case)
    total = len(candidates)
    done = 0
    errors = 0
    skipped = 0

    _notify_progress(
        progress_callback,
        "client_step",
        client=client,
        step="AI formuláře",
        detail=f"Nalezeno {total} formulářových PDF k vytěžení",
    )
    print(f"AI formuláře – Nalezeno {total} formulářových PDF k vytěžení")

    for index, document in enumerate(candidates, start=1):
        title = document.title or document.document_type or f"dokument #{document.id}"
        _notify_progress(
            progress_callback,
            "client_step",
            client=client,
            step="AI formuláře",
            detail=f"Vytěžuji formulář {index}/{total}: {title}",
        )
        print(f"AI formuláře – Vytěžuji formulář {index}/{total}: {title}")

        try:
            if not getattr(document, "local_path", None) or not Path(str(document.local_path)).exists():
                _notify_progress(
                    progress_callback,
                    "client_step",
                    client=client,
                    step="AI formuláře",
                    detail=f"Stahuji PDF pro formulář {index}/{total}: {title}",
                )
                print(f"AI formuláře – Stahuji PDF pro formulář {index}/{total}: {title}")
                with Session() as http_session:
                    _download_document(http_session, document)

            if not getattr(document, "local_path", None) or not Path(str(document.local_path)).exists():
                raise RuntimeError("PDF formuláře není dostupné po stažení")

            payload = extract_structured_report_from_pdf_ai(
                pdf_path=document.local_path,
                document_title=title,
            )
            _notify_progress(
                progress_callback,
                "client_step",
                client=client,
                step="AI formuláře",
                detail=f"Ukládám strukturovaná data {index}/{total}: {title}",
            )
            print(f"AI formuláře – Ukládám strukturovaná data {index}/{total}: {title}")
            persist_structured_report_payload_for_case(case, [document], payload)
            done += 1
        except Exception as exc:
            errors += 1
            logger.warning("Strukturovana extrakce selhala pro dokument %s: %s", title, exc)
            print(f"AI formuláře – CHYBA {index}/{total}: {title}: {exc}")

    summary = f"Strukturovaná extrakce: {done} hotovo, {errors} chyba, {skipped} přeskočeno"
    _notify_progress(progress_callback, "client_step", client=client, step="AI formuláře", detail=summary)
    print(f"AI formuláře – {summary}")
    return {"total": total, "done": done, "errors": errors, "skipped": skipped}

def _auto_generate_case_study_if_needed(
    case: InsolvencyCase,
    new_document_urls: set[str] | None = None,
) -> dict[str, str]:
    has_case_study = bool(case.ai_case_study and case.ai_case_study.strip())
    new_document_urls = new_document_urls or set()

    new_relevant_documents = [
        document
        for document in case.documents
        if document.source_url
        and document.source_url in new_document_urls
        and _is_case_study_relevant_document(document)
    ]

    # 1) Kazuistika existuje a nepřibyl žádný nový dokument
    if has_case_study and not new_document_urls:
        return {
            "status": "Kazuistika – není potřebná",
            "reason": "Kazuistika už existuje a od poslední kontroly nepřibyl žádný nový dokument.",
        }

    # 2) Přibyly nové dokumenty, ale nejsou podstatné pro kazuistiku
    if has_case_study and new_document_urls and not new_relevant_documents:
        return {
            "status": "Kazuistika – není potřebná",
            "reason": "Přibyly nové dokumenty, ale žádný z nich není podstatný pro aktualizaci kazuistiky.",
        }

    try:
        analyze_case_study(case)
        case.ai_last_error = None

        # 3) Kazuistika už existovala a teď se aktualizovala
        if has_case_study:
            relevant_titles = ", ".join(
                document.title or "dokument bez názvu"
                for document in new_relevant_documents
            )

            return {
                "status": "Kazuistika – aktualizována",
                "reason": f"Kazuistika byla aktualizována, protože přibyl podstatný dokument: {relevant_titles}.",
            }

        # 4) Kazuistika dosud nebyla
        return {
            "status": "Kazuistika – vytvořena",
            "reason": "Kazuistika byla vytvořena, protože dosud neexistovala.",
        }

    except Exception as exc:
        if isinstance(exc, sqlalchemy_exc.SQLAlchemyError):
            raise
        case.ai_last_error = f"Automatická kazuistika selhala ({exc.__class__.__name__})."
        logger.warning(
            "Automatické vytvoření kazuistiky selhalo pro spis %s: %s",
            case.spisova_znacka,
            exc,
        )

        return {
            "status": "Kazuistika – ERROR při tvorbě",
            "reason": f"Kazuistiku se nepodařilo vytvořit: {exc}",
        }



def _update_claims_summary(case: InsolvencyCase) -> None:
    claim_documents = [
        document
        for document in case.documents
        if _is_claim_amount_document(document)
    ]

    if claim_documents:
        case.claims_count = len(claim_documents)

    bankruptcy_decision = next(
        (
            document
            for document in sorted(case.documents, key=lambda item: item.event_at or datetime.max)
            if document.event_at
            and "usnesení o úpadku" in (document.title or "").casefold()
            and "povolen" in (document.title or "").casefold()
        ),
        None,
    )
    if bankruptcy_decision is None:
        bankruptcy_decision = next(
            (
                document
                for document in sorted(case.documents, key=lambda item: item.event_at or datetime.max)
                if document.event_at and "usnesení o úpadku" in (document.title or "").casefold()
            ),
            None,
        )

    if bankruptcy_decision:
        # Lhůta pro přihlášky je v naší aplikaci vždy počítána jako 2 měsíce
        # od zveřejnění usnesení o úpadku. Tímto přepíšeme případný starší
        # nebo chybně vyčtený tříměsíční údaj z textu/detailu ISIR.
        case.claims_deadline = _add_months(bankruptcy_decision.event_at, 2).strftime("%d.%m.%Y")



def _extract_claims_info_from_text(case: InsolvencyCase, text: str) -> None:
    compact_text = " ".join(text.split())

    deadline_patterns = [
        r"přihláš(?:ek|ky)\s+pohledávek.{0,120}?(?:do|ve lhůtě do)\s+(\d{1,2}\.\d{1,2}\.\d{4})",
        r"lhůt[ay]\s+pro\s+podávání\s+přihlášek\s+pohledávek.{0,120}?(\d{1,2}\.\d{1,2}\.\d{4})",
        r"věřitelé.{0,80}?přihlásit.{0,80}?do\s+(\d{1,2}\.\d{1,2}\.\d{4})",
    ]
    for pattern in deadline_patterns:
        match = re.search(pattern, compact_text, flags=re.IGNORECASE)
        if match:
            case.claims_deadline = match.group(1)
            break

    total_patterns = [
        r"celkov[áa]\s+výše\s+přihlášených\s+pohledávek.{0,80}?([0-9\s.,]+(?:Kč|CZK))",
        r"přihlášen[ée]\s+pohledávky\s+celkem.{0,80}?([0-9\s.,]+(?:Kč|CZK))",
    ]
    for pattern in total_patterns:
        match = re.search(pattern, compact_text, flags=re.IGNORECASE)
        if match:
            case.claims_total_amount = " ".join(match.group(1).split())
            break


def enrich_case_from_detail_page(case: InsolvencyCase) -> None:
    if not case.detail_url:
        return

    with Session() as session:
        session.trust_env = False
        response = session.get(case.detail_url, timeout=15)
        response.raise_for_status()

        tree = html.fromstring(response.content)
        _extract_claims_info_from_text(case, tree.text_content())
        document_rows = []
        all_pdf_links = []
        for row in tree.xpath("//tr"):
            cells = [" ".join(cell.text_content().split()) for cell in row.xpath("./td|./TD")]
            pdf_anchors = [a for a in row.xpath(".//a") if "/isir/doc/dokument.PDF" in (a.get("href") or "")]
            pdf_links = [urljoin(case.detail_url, anchor.get("href")) for anchor in pdf_anchors]
            all_pdf_links.extend(pdf_links)

            if len(cells) < 4 or not pdf_links:
                continue

            event_at = None
            if len(cells) > 2:
                event_at = _parse_czech_datetime(f"{cells[1]} {cells[2]}") or _parse_czech_datetime(cells[1])

            is_main_case_event = not cells[0].startswith("P")
            for index, pdf_url in enumerate(pdf_links):
                document_type = "hlavní dokument" if index == 0 else "vedlejší dokument"
                document = _upsert_document(case, event_at, cells[3], document_type, pdf_url)
                document_rows.append(
                    {
                        "event_at": event_at,
                        "description": cells[3],
                        "url": pdf_url,
                        "is_main_case_event": is_main_case_event,
                        "document": document,
                    }
                )

        case.document_count = len(all_pdf_links)
        _update_claims_summary(case)
        for document in case.documents:
            _download_document(session, document)

        main_document_rows = [row for row in document_rows if row["is_main_case_event"]]
        latest_document = max(
            main_document_rows or document_rows,
            key=lambda row: row["event_at"] or datetime.min,
            default=None,
        )
        if latest_document:
            case.document_url = latest_document["url"]
            case.last_event_at = latest_document["event_at"]
            case.last_event_description = latest_document["description"]

        for row in tree.xpath("//tr"):
            cells = [" ".join(cell.text_content().split()) for cell in row.xpath("./td|./TD")]
            row_datetime = None
            for index, cell in enumerate(cells):
                row_datetime = _parse_czech_datetime(cell)
                if row_datetime:
                    if index + 1 < len(cells) and ":" in cells[index + 1]:
                        row_datetime = _parse_czech_datetime(f"{cell} {cells[index + 1]}") or row_datetime
                    break

            if row_datetime and case.last_event_at is None:
                case.last_event_at = row_datetime
                descriptions = [
                    cell
                    for cell in cells
                    if cell
                    and not _parse_czech_datetime(cell)
                    and ":" not in cell
                    and "plný text" not in cell
                    and "kB" not in cell
                ]
                if descriptions:
                    case.last_event_description = descriptions[0]

            row_text = " ".join(row.text_content().split())
            if "Vyhláška o zahájení insolvenčního řízení" not in row_text:
                continue

            for index, cell in enumerate(cells):
                if _parse_czech_datetime(cell):
                    combined = cell
                    if index + 1 < len(cells) and ":" in cells[index + 1]:
                        combined = f"{cell} {cells[index + 1]}"
                    case.proceeding_started_at = _parse_czech_datetime(combined) or _parse_czech_datetime(cell)
                    return


def _status_from_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Bez nalezeného řízení"
    states = sorted({str(row.get("druhStavKonkursu") or "stav neuveden") for row in rows})
    return f"Nalezeno řízení: {', '.join(states)}"


def _change_description(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Nově nebylo nalezeno žádné insolvenční řízení."

    parts = []
    for row in rows[:5]:
        state = row.get("druhStavKonkursu") or "stav neuveden"
        detail_url = row.get("urlDetailRizeni")
        label = f"{_case_label(row)} - {state}"
        if detail_url:
            label = f"{label} ({detail_url})"
        parts.append(label)
    return "Nový stav v ISIR: " + "; ".join(parts)


def check_client(client: Client, soap_client: SoapClient | None = None) -> None:
    soap_client = soap_client or make_soap_client()

    request = {
        "nazevOsoby": client.last_name,
        "jmeno": client.first_name,
        "datumNarozeni": client.birth_date.isoformat(),
        "maxPocetVysledku": 20,
        "filtrAktualniRizeni": "F",
        "vyhledatPresnouShoduJmen": "T",
        "maxRelevanceVysledku": 4,
    }

    response = soap_client.service.getIsirWsCuzkData(**request)
    serialized = serialize_object(response)
    rows = _find_result_rows(serialized)
    new_hash = _result_hash(rows)

    client.last_checked_at = datetime.utcnow()
    client.last_check_error = None
    client.insolvency_status = _status_from_rows(rows)

    for row in rows:
        _upsert_case(client, row)

    if client.last_result_hash != new_hash:
        description = _change_description(rows)
        client.last_result_hash = new_hash
        client.last_found_change = description
        client.changes.append(
            InsolvencyChange(
                description=description,
                raw_result=_stable_json(serialized),
            )
        )


def _public_events_from_response(response: Any) -> list[dict[str, Any]]:
    serialized = serialize_object(response)
    data = serialized.get("data") if isinstance(serialized, dict) else None
    return [event for event in _as_list(data) if isinstance(event, dict)]


def enrich_cases_with_public_events(
    cases: list[InsolvencyCase],
    public_client: SoapClient | None = None,
) -> None:
    if not cases:
        return

    public_client = public_client or make_public_soap_client()
    posledni = serialize_object(public_client.service.getIsirWsPublicPodnetPosledniId())
    latest_values = posledni.get("cisloPosledniId") if isinstance(posledni, dict) else None
    latest_ids = [int(value) for value in _as_list(latest_values) if value is not None]
    if not latest_ids:
        return

    start_id = max(max(latest_ids) - PUBLIC_EVENT_LOOKBACK, 0)
    events = _public_events_from_response(public_client.service.getIsirWsPublicPodnetId(start_id))

    for event in events:
        event_spis = str(event.get("spisovaZnacka") or "")
        case = next((item for item in cases if item.spisova_znacka in event_spis), None)
        if case is None:
            continue

        event_id = event.get("id")
        if case.last_event_id is not None and event_id is not None and int(event_id) <= case.last_event_id:
            continue

        case.last_event_id = int(event_id) if event_id is not None else None
        case.last_event_at = event.get("datumZverejneniUdalosti") or event.get("datumZalozeniUdalosti")
        case.last_event_type = event.get("typUdalosti")
        case.last_event_description = event.get("popisUdalosti") or event.get("poznamka")
        case.document_url = event.get("dokumentUrl")


ProgressCallback = Callable[[str, dict[str, Any]], None]
CancelCallback = Callable[[], bool]


def _notify_progress(progress_callback: ProgressCallback | None, event: str, **payload: Any) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(event, payload)
    except Exception:
        logger.warning("Aktualizace prubehu kontroly selhala")


def check_client_with_retry(client: Client, soap_client: SoapClient | None) -> None:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            check_client(client, soap_client)
            return
        except sqlalchemy_exc.SQLAlchemyError:
            raise
        except Exception as error:
            last_error = error
            if attempt == 0:
                time.sleep(2)
    if last_error is not None:
        raise last_error



def check_all_clients(
    progress_callback: ProgressCallback | None = None,
    client_ids: list[int] | None = None,
    cancel_callback: CancelCallback | None = None,
) -> None:
    session = SessionLocal()
    soap_client: SoapClient | None = None

    try:
        query = session.query(Client).order_by(Client.id)

        if client_ids is not None:
            query = query.filter(Client.id.in_(client_ids))

        clients = query.all()

        _notify_progress(progress_callback, "started", total=len(clients))
        if cancel_callback is not None and cancel_callback():
            _notify_progress(progress_callback, "cancelled")
            return

        if clients:
            try:
                soap_client = make_soap_client()
            except Exception as exc:
                logger.exception("Nepodarilo se pripojit k ISIR SOAP sluzbe")

                now = datetime.utcnow()
                message = f"Kontrola selhala: nepodařilo se připojit k ISIR ({exc.__class__.__name__})"

                for client in clients:
                    client.last_checked_at = now
                    client.last_check_error = message
                    _notify_progress(progress_callback, "client_error", client=client, error=message)

                session.commit()
                _notify_progress(progress_callback, "finished")
                return

        for client in clients:
            client_id = client.id
            if cancel_callback is not None and cancel_callback():
                _notify_progress(progress_callback, "cancelled")
                return

            _notify_progress(progress_callback, "client_started", client=client)

            try:
                warnings = []
                before_document_urls = {
                    document.source_url
                    for case in client.cases
                    for document in case.documents
                    if document.source_url
                }

                check_client_with_retry(client, soap_client)

                for case in client.cases:
                    try:
                        enrich_case_from_detail_page(case)

                        case_new_document_urls = {
                            document.source_url
                            for document in case.documents
                            if document.source_url
                            and document.source_url not in before_document_urls
                        }

                        print(f"👉 Kontrola nových PDF pro case: {case.spisova_znacka}")
                        print(f"👉 Počet nových dokumentů v case: {len(case_new_document_urls)}")

                        _notify_progress(
                            progress_callback,
                            "client_step",
                            client=client,
                            step="AI přihlášky",
                            detail="Kontroluji, zda běží lhůta pro přihlášky",
                        )
                        _update_claims_amounts_from_pdfs(
                            case,
                            new_document_urls=case_new_document_urls,
                        )

                        # Důležité pořadí: nejdřív uložit stažené dokumenty, potom vytěžit
                        # strukturovaná data z formulářů, a teprve pak tvořit kazuistiku.
                        _notify_progress(
                            progress_callback,
                            "client_step",
                            client=client,
                            step="Ukládání",
                            detail="Ukládám stažené dokumenty před formulářovou extrakcí",
                        )
                        session.commit()

                        structured_result = _extract_structured_form_data_for_case(
                            case,
                            progress_callback=progress_callback,
                            client=client,
                        )
                        if structured_result.get("errors"):
                            warnings.append("Část formulářových dokumentů se nepodařilo vytěžit.")

                        _notify_progress(
                            progress_callback,
                            "client_step",
                            client=client,
                            step="Kazuistika",
                            detail="Vytvářím / aktualizuji kazuistiku z uložených strukturovaných dat",
                        )
                        case_study_result = _auto_generate_case_study_if_needed(
                            case,
                            new_document_urls=case_new_document_urls,
                        )
                        if case.ai_last_error:
                            warnings.append(case.ai_last_error)

                        print(case_study_result.get("status", "Kazuistika – bez výsledku"))
                        print("Důvod:", case_study_result.get("reason", "Funkce nevrátila důvod."))


                    
                    except sqlalchemy_exc.SQLAlchemyError:
                        raise
                    except Exception as error:
                        logger.warning(
                            "Nacteni detailu ISIR selhalo pro spis %s: %s",
                            case.spisova_znacka,
                            error.__class__.__name__,
                        )
                        warnings.append("Detail řízení nebo jeho dokumenty se nepodařilo úplně načíst.")


                if USE_PUBLIC_EVENT_WS:
                    try:
                        enrich_cases_with_public_events(client.cases)
                    except sqlalchemy_exc.SQLAlchemyError:
                        raise
                    except Exception:
                        warnings.append("Veřejné události ISIR se nepodařilo načíst.")
                        logger.warning(
                            "Dohledani detailu ISIR_PUBLIC_WS selhalo pro klienta id=%s",
                            client.id,
                        )

                session.commit()

                new_documents = [
                    document
                    for case in client.cases
                    for document in case.documents
                    if document.source_url and document.source_url not in before_document_urls
                ]

                current_documents = [
                    document
                    for case in client.cases
                    for document in case.documents
                    if document.source_url
                ]

                if warnings:
                    client.last_check_error = " ".join(sorted(set(warnings)))
                    session.commit()
                _notify_progress(
                    progress_callback,
                    "client_error" if warnings else "client_success",
                    client=client,
                    error=client.last_check_error,
                    new_document_count=len(new_documents),
                    document_count=len(current_documents),
                    new_document_titles=[document.title for document in new_documents[:5]],
                )

            except Exception as error:
                logger.exception("ISIR kontrola selhala pro klienta id=%s", client_id)
                session.rollback()
                error_message = f"Kontrola selhala: {error.__class__.__name__}. Poslední známý stav zůstal zachován."
                try:
                    client = session.get(Client, client_id)
                    if client is not None:
                        client.last_checked_at = datetime.utcnow()
                        client.last_check_error = error_message
                        session.commit()
                except Exception:
                    session.rollback()
                    logger.exception("Nepodařilo se uložit chybu kontroly klienta id=%s", client_id)
                    client = None
                _notify_progress(progress_callback, "client_error", client=client, error=error_message)

            time.sleep(1.5)

            if cancel_callback is not None and cancel_callback():
                _notify_progress(progress_callback, "cancelled")
                return

        _notify_progress(progress_callback, "finished")

    finally:
        session.close()
        if soap_client is not None:
            soap_client.transport.session.close()



def start_scheduler(check_job=None) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone="Europe/Prague", executors={"default": CoordinatedExecutor()},
    )
    scheduler.add_job(
        check_job or check_all_clients,
        "cron",
        day_of_week="mon-fri",
        hour=10,
        minute=0,
        id="weekday_isir_check_10",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        check_job or check_all_clients,
        "cron",
        day_of_week="mon-fri",
        hour=14,
        minute=0,
        id="weekday_isir_check_14",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    check_all_clients()
