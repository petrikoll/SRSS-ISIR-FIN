from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import calendar
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, date
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from sqlalchemy import case

from app_settings import get_gemini_api_key
from google import genai
from google.genai import types
from requests import Session

from models import InsolvencyCase, InsolvencyDocument, SessionLocal
from structured_data import persist_structured_extraction, get_case_structured_snapshot, get_document_extraction_payloads


GEMINI_MODEL = "gemini-2.5-flash"
MAX_CASE_STUDY_PDFS = 14
MAX_DATA_VERIFICATION_NON_CLAIM_PDFS = 10
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
MAX_DOCUMENT_SUMMARY_CHARS = 3500
MAX_CASE_STUDY_CHARS = 6000
MAX_DOCUMENT_WORKING_ANALYSIS_CHARS = 8000
MAX_CASE_STUDY_WORKING_ANALYSIS_CHARS = 15000


FULFILLMENT_REPORT_ANALYSIS_PROMPT = """
Přečti PDF dokument typu „Sdělení správce o plnění oddlužení“ / „Zpráva o plnění oddlužení“.

Tento dokument má pevnou formulářovou strukturu. Nejde o běžné shrnutí PDF.
Úkolem je vytěžit konkrétní údaje pro dluhového poradce co nejpřesněji.

Důležité:
- Čti vizuální obsah PDF formuláře, tabulky i zaškrtnutá pole.
- Nepoužívej obecné právní úvahy.
- Neopisuj celý dokument.
- Pokud údaj není bezpečně čitelný, vrať null a uveď poznámku do warnings.
- Částky opisuj přesně tak, jak jsou v dokumentu, včetně Kč, pokud je měna uvedena.
- Procenta opisuj přesně tak, jak jsou v dokumentu.
- Měsíční tabulku vytěž jen v rozsahu potřebném pro praktický přehled; pokud je velmi rozsáhlá, uveď první a poslední měsíc a pravidelné opakující se hodnoty.

Vrať pouze validní JSON v tomto formátu:

{
  "document_type": "sdeleni_spravce_o_plneni_oddluzeni",
  "category": "Sdělení správce o plnění oddlužení",
  "court": "",
  "case_number": "",
  "debtor": {
    "name": "",
    "address": "",
    "birth_date": "",
    "ico": ""
  },
  "bankruptcy_decision_date": "",
  "report_periods": [
    {
      "from": "měsíc/rok nebo text",
      "to": "měsíc/rok nebo text"
    }
  ],
  "debtor_fulfils_obligations": "ano | ne | nezjištěno",
  "trustee_statement": "stručné věcné shrnutí vyjádření správce",
  "current_satisfaction_rate_percent": "",
  "expected_satisfaction_rate_3y_percent": "",
  "expected_satisfaction_rate_5y_percent": "",
  "trustee_recommendation": "",
  "payment_overview": {
    "regular_debtor_payment": "",
    "regular_contribution_or_gift": "",
    "other_income": "",
    "trustee_fee_monthly": "",
    "paid_to_creditors_monthly": "",
    "period_covered_by_monthly_table": ""
  },
  "totals": {
    "trustee_total": "",
    "creditors_total_from_start": "",
    "deposit_for_trustee_fee": ""
  },
  "distribution_scheme": [
    {
      "claim_number": "",
      "creditor": "",
      "claimed_amount": "",
      "satisfied_amount": "",
      "satisfied_percent": ""
    }
  ],
  "attachments": [
    {
      "name": "",
      "file_name": ""
    }
  ],
  "signed": {
    "place": "",
    "date": "",
    "person": ""
  },
  "advisor_takeaways": [
    "nejdůležitější praktické body pro dluhového poradce"
  ],
  "confidence": "nízká | střední | vysoká",
  "warnings": []
}
"""

FULFILLMENT_REPORT_FINAL_PROMPT = """
Převeď strukturované vytěžení dokumentu „Sdělení správce o plnění oddlužení“ do krátkého shrnutí pro webovou aplikaci.

Vrať pouze text ve formátu sekcí. Nepoužívej JSON.
Použij přesně tyto tři sekce:

[[SECTION:summary:Shrnutí]]
Maximálně 700 znaků. Uveď období zprávy, zda dlužník plní povinnosti, hlavní způsob plnění oddlužení a doporučení správce.

[[SECTION:deadlines:Lhůty a povinnosti]]
Maximálně 1 000 znaků. Uveď jen konkrétní aktuální povinnosti, termíny nebo údaje k plnění povinností, které ze zprávy vyplývají. Pokud žádné nové lhůty nejsou, napiš to stručně.

[[SECTION:other:Ostatní informace a doporučení]]
Maximálně 1 200 znaků. Uveď aktuální míru uspokojení, očekávanou míru uspokojení, významné částky, distribuční schéma jen velmi stručně a nejvýše 5 doporučení pro poradce. Na konec uveď jistotu shrnutí.

Pravidla:
- Celý výstup max. 3 500 znaků.
- Nepiš právní rady.
- Nepřidávej nové skutečnosti.
- Neopisuj celou měsíční tabulku.
- Neuváděj historii řízení, pokud není nutná pro pochopení zprávy.
- Pokud správce doporučuje ponechat dlužníka v oddlužení, napiš to výslovně.
- Pokud dlužník neplní povinnosti nebo je doporučení negativní, zvýrazni to věcně v první sekci.
"""


DEBT_RELIEF_STRUCTURED_REPORT_EXTRACTION_PROMPT = """
Přečti PDF z insolvenčního rejstříku jako specializovaný extractor formulářových dokumentů správce.

Dokument může obsahovat jeden nebo více těchto formulářů:
- Zpráva pro oddlužení a o přezkumu
- Seznam přihlášených pohledávek
- Soupis majetkové podstaty
- Vyrozumění o popření přihlášky
- Zpráva / Sdělení správce o plnění oddlužení
- Zpráva / Sdělení správce o splnění oddlužení
- Vyúčtování odměny a hotových výdajů insolvenčního správce
- Návrh na osvobození od placení zbývajících pohledávek

Úkol:
Nevytvářej běžné shrnutí. Vytěž strukturovaná data do JSON.
Pokud dokument obsahuje více formulářů, vyplň všechny relevantní bloky.
Pokud údaj nenajdeš, vrať null nebo prázdný seznam. Nehádej.
Datumy vracej přednostně ve formátu YYYY-MM-DD. Období typu „leden / 2025“ vracej jako první den měsíce, např. 2025-01-01. Částky a procenta můžeš vrátit jako číslo nebo jako přesný text z formuláře.

Důležité:
- Čti vizuální obsah formuláře, tabulky i zaškrtnutá pole.
- Textová extrakce z těchto PDF může být rozbitá; rozhodující je obsah viditelný ve formuláři.
- Částky a procenta opisuj přesně podle dokumentu.
- Dlouhá právní poučení neopisuj; vytěž jen rozhodná fakta.
- U dokumentů typu „Zpráva pro oddlužení“, „Zpráva o přezkumu“ a „Seznam přihlášených pohledávek“ vytěžuj jen souhrny.
- Nevytěžuj jednotlivé věřitele, jednotlivé pohledávky, distribuční schéma ani seznam přihlášek jako pole JSONu.
- Z pohledávkových tabulek ber pouze souhrnné údaje: počet přezkoumaných přihlášek, celkové částky zajištěných/nezajištěných/podřízených pohledávek, počet nepřezkoumaných pohledávek a informace o popření, pokud jsou uvedeny.

Vrať pouze validní JSON podle tohoto schématu:
{
  "document_family": "debt_relief_structured_report",
  "document_type": "review_report | performance_report | completion_report | trustee_fee_accounting | mixed | unknown",
  "case_identification": {
    "court": null,
    "case_number": null,
    "debtor_name": null,
    "debtor_birth_date": null,
    "debtor_address": null,
    "trustee_name": null,
    "trustee_is_vat_payer": null
  },
  "document_dates": {
    "signed_at": null,
    "submitted_at": null,
    "report_period_from": null,
    "report_period_to": null,
    "report_periods": []
  },
  "proceeding_state": {
    "bankruptcy_decision_date": null,
    "debt_relief_permitted_date": null,
    "debt_relief_approved_date": null,
    "last_payment_date": null,
    "stage": "review_completed | debt_relief_running | debt_relief_completed_by_trustee | awaiting_court_discharge_decision | unknown",
    "main_conclusion": null
  },
  "claims_review": {
    "review_meeting_date": null,
    "review_meeting_time_from": null,
    "review_meeting_time_to": null,
    "debtor_present": null,
    "reviewed_claim_applications_count": null,
    "reviewed_unsecured_claims_total": null,
    "reviewed_unsecured_claims_without_subordinated_total": null,
    "secured_claims_total": null,
    "unreviewed_claims_count": null,
    "creditors_under_177_count": null,
    "creditors_without_voting_rights_count": null,
    "creditor_voting_result": null,
    "claims_denied_by_debtor": null,
    "claims_denied_by_trustee": null,
    "denial_notice_present": null,
    "debtor_proposed_debt_relief_form": null,
    "debtor_requests_different_payment_amount": null,
    "eu_creditors_known": null
  },
  "performance": {
    "debtor_fulfils_obligations": null,
    "trustee_statement": null,
    "payment_source": null,
    "regular_contribution_amount": null,
    "debtor_payment_summary": null,
    "extra_income_summary": null,
    "creditors_paid_total": null,
    "trustee_paid_total": null,
    "current_satisfaction_percent": null,
    "expected_satisfaction_3y_percent": null,
    "expected_satisfaction_5y_percent": null,
    "deposit_note": null
  },
  "completion": {
    "debt_relief_completed_under": null,
    "unsecured_satisfaction_percent": null,
    "unsecured_satisfaction_amount": null,
    "secured_satisfaction_percent": null,
    "secured_satisfaction_amount": null,
    "overpayment_amount": null,
    "debt_relief_interrupted": null,
    "debt_relief_extended": null,
    "income_payer_stop_deductions_date": null,
    "all_assets_sold": null,
    "debtor_fulfilled_all_obligations": null,
    "course_of_proceeding_summary": null
  },
  "trustee_accounting": {
    "trustee_total_fee_with_vat": null,
    "trustee_total_fee_without_vat": null,
    "review_fee": null,
    "reviewed_claims_count_for_fee": null,
    "duration_fee": null,
    "secured_asset_proceeds_fee": null,
    "unsecured_distribution_proceeds_fee": null,
    "cash_expenses": null,
    "unpaid_amount": null,
    "remaining_to_satisfy": null,
    "months_count": null,
    "period_from": null,
    "period_to": null,
    "last_income_payer": null,
    "comment": null
  },
  "trustee_recommendation": {
    "recommendation_text": null,
    "recommendation_category": "continue_debt_relief | approve_debt_relief | decide_completed | grant_discharge | cancel_or_problem | other | unknown"
  },
  "assets": {
    "asset_inventory_present": null,
    "assets_summary": null,
    "all_assets_sold": null
  },
  "attachments": [],
  "advisor_summary": {
    "short_conclusion": null,
    "what_to_check": [],
    "important_warnings": []
  },
  "confidence": "low | medium | high",
  "warnings": []
}

Pravidla:
- Piš česky tam, kde jde o textová pole.
- Nevyvozuj právní závěry.
- Do advisor_summary dej jen praktický závěr pro dluhového poradce.
- Pokud si nejsi jistý důležitým údajem, přidej vysvětlení do warnings a nastav nižší confidence.
"""

DEBT_RELIEF_STRUCTURED_REPORT_FINAL_PROMPT = """
Převeď strukturované vytěžení formulářového dokumentu insolvenčního správce do krátkého shrnutí pro webovou aplikaci.

Vrať pouze text ve formátu sekcí. Nepoužívej JSON.
Použij přesně tyto tři sekce:

[[SECTION:summary:Shrnutí]]
Maximálně 700 znaků. Uveď typ dokumentu, fázi řízení a hlavní závěr. Pokud jde o plnění/splnění oddlužení, uveď výslovně doporučení správce a zda dlužník plní povinnosti.

[[SECTION:deadlines:Lhůty a povinnosti]]
Maximálně 1 000 znaků. Uveď jen konkrétní lhůty, termíny, povinnosti nebo úkony, které z dokumentu prakticky plynou. Pokud žádné aktuální lhůty nejsou, napiš to jednou větou.

[[SECTION:other:Ostatní informace a doporučení]]
Maximálně 1 200 znaků. Uveď nejdůležitější čísla a závěry podle typu dokumentu: přezkoumané pohledávky, míru uspokojení, částky věřitelům/správci, deponaci, popření, splnění oddlužení, vyúčtování správce. Přidej nejvýše 5 doporučení pro poradce a jistotu vytěžení.

Pravidla:
- Celý výstup max. 3 500 znaků.
- Nepiš právní rady.
- Nepřidávej nové skutečnosti mimo strukturované vytěžení.
- Neopisuj dlouhá právní poučení ani celé tabulky.
- U přezkumu/seznamu pohledávek uváděj jen souhrny, nikdy jednotlivé věřitele ani jednotlivé pohledávky.
- Pokud je údaj nejistý, označ ho jako nejistý.
- Pokud je doporučení správce negativní nebo problémové, uveď to už ve Shrnutí.
"""


def _build_structured_report_final_prompt(payload: dict) -> str:
    """Prompt pro převod uloženého/vytěženého strukturovaného JSONu do stručného sekčního shrnutí."""
    return (
        DEBT_RELIEF_STRUCTURED_REPORT_FINAL_PROMPT
        + "\n\nSTRUKTUROVANÁ DATA:\n"
        + _format_json_for_prompt(payload)
    )


def _combine_cached_structured_payloads(documents: list[Any], payloads_by_document_id: dict[int, dict[str, Any]]) -> dict:
    items = []
    for document in documents:
        document_id = int(getattr(document, "id", 0) or 0)
        payload = payloads_by_document_id.get(document_id)
        if payload is None:
            continue
        items.append({
            "source_document": {
                "id": document_id,
                "title": getattr(document, "title", None) or getattr(document, "document_type", None),
                "event_at": getattr(document, "event_at", None).isoformat() if getattr(document, "event_at", None) else None,
            },
            "extracted_data": payload,
        })
    if len(items) == 1:
        payload = dict(items[0]["extracted_data"])
        payload.setdefault("source_document", items[0]["source_document"])
        payload.setdefault("_used_cached_structured_data", True)
        return payload
    return {
        "document_family": "debt_relief_structured_report",
        "document_type": "mixed",
        "_used_cached_structured_data": True,
        "documents": items,
        "confidence": "medium",
        "warnings": ["Shrnutí bylo vytvořeno z už uložených strukturovaných dat, nikoli novým čtením PDF."],
    }


DOCUMENT_ANALYSIS_PROMPT = """
Přečti vybraný dokument nebo sadu vybraných dokumentů z insolvenčního rejstříku jako odborný asistent pro dluhového poradce.

Toto je 1. krok zpracování: pracovní odborný rozbor pro interní použití.
Cílem není hezký výstup pro web, ale přesné zachycení obsahu dokumentů.

Shrnuj pouze dodané dokumenty. Nejde o celkovou kazuistiku insolvenčního případu.
Nepropojuj výstup s jinými dokumenty, které nejsou součástí vstupu.

Vrať pouze validní JSON v tomto formátu:

{
  "category": "typ dokumentu nebo stručné označení sady dokumentů",
  "document_scope": "jeden dokument | více dokumentů",
  "working_analysis": {
    "what_document_says": ["věcná zjištění výslovně uvedená v dokumentech"],
    "practical_meaning_for_debt_advisor": ["praktický význam pro dluhové poradenství"],
    "explicit_deadlines": [
      {
        "date_or_period": "datum nebo lhůta výslovně uvedená v dokumentech",
        "description": "čeho se lhůta týká",
        "recipient": "komu nebo kam, pokud je uvedeno, jinak null",
        "source_document": "název nebo typ dokumentu"
      }
    ],
    "explicit_debtor_obligations": [
      {
        "obligation": "co má dlužník podle dokumentů výslovně udělat",
        "recipient": "komu nebo kam, pokud je uvedeno, jinak null",
        "deadline": "lhůta nebo datum, pokud je výslovně uvedeno, jinak null",
        "source_document": "název nebo typ dokumentu"
      }
    ],
    "advisor_recommendations": ["co má poradce s klientem ověřit, vysvětlit nebo připravit"],
    "unclear_or_incomplete_information": ["nejasnosti, rozpory nebo špatně čitelné údaje přímo patrné z dokumentů"]
  },
  "confidence": "nízká | střední | vysoká"
}

Pravidla:
- Vrať pouze validní JSON.
- Piš česky, jednoduše a věcně.
- Piš pro dluhového poradce.
- Zachyť relevantní fakta přesně, ale nepřepisuj celý dokument.
- Nepiš právní rady.
- Nevyvozuj právní závěry.
- Nepřidávej povinnosti, které v dokumentech nejsou výslovně uvedené.
- Lhůty uváděj jen tehdy, pokud jsou v dokumentech výslovně napsané.
- Pokud dokumenty neobsahují konkrétní lhůty, vrať prázdný seznam explicit_deadlines.
- Pokud dokumenty neobsahují konkrétní povinnosti dlužníka, vrať prázdný seznam explicit_debtor_obligations.
- U lhůt a povinností vždy uveď zdrojový dokument.
- Pokud si dokumenty odporují, nevybírej jednu verzi jako jistou; uveď rozpor mezi nejasnosti.
- Neuváděj obecná rizika ani domnělé následky.
- Nepiš o exekuci, zrušení oddlužení ani jiných následcích, pokud to není v dokumentech výslovně uvedeno.
- Pracovní rozbor nesmí přesáhnout 8 000 znaků.
"""

DOCUMENT_FINAL_PROMPT = """
Převeď pracovní odborný rozbor dokumentu nebo dokumentů do krátkého finálního shrnutí pro webovou aplikaci.

Toto je 2. krok zpracování: kontrola správnosti, zkrácení a rozdělení do sekcí.
Nevracej pracovní poznámky. Nepřidávej nové skutečnosti.
Zachovej jen informace důležité pro dluhového poradce.

Vrať pouze text ve formátu sekcí. Nepoužívej JSON.
Nepřidávej žádný úvod ani závěr mimo sekce.

Použij přesně tyto tři sekce:

[[SECTION:summary:Shrnutí]]
Maximálně 700 znaků. Shrň, o jaký dokument nebo dokumenty jde a jaký mají praktický význam. Neopisuj dokument.

[[SECTION:deadlines:Lhůty a povinnosti]]
Maximálně 1 200 znaků. Uveď pouze konkrétní lhůty, data a výslovné povinnosti dlužníka. Pokud nejsou, napiš jednou větou, že dokumenty žádné konkrétní lhůty ani výslovné povinnosti dlužníka neobsahují.

[[SECTION:other:Ostatní informace a doporučení]]
Maximálně 1 200 znaků. Uveď další důležité informace a nejvýše 5 praktických doporučení pro dluhového poradce. Na konec uveď: Jistota shrnutí: nízká / střední / vysoká.

Limity:
- Celý výstup nesmí přesáhnout 3 500 znaků včetně mezer.
- Lhůty: maximálně 5 položek.
- Povinnosti: maximálně 6 položek.
- Doporučení poradci: maximálně 5 položek.
- Nejasnosti: maximálně 3 položky.

Pravidla:
- Piš česky, stručně a věcně.
- Shrnutí dokumentu není kazuistika.
- Neuváděj úplnou historii řízení.
- Nepiš právní rady.
- Nevyvozuj právní závěry.
- Nepřidávej nové skutečnosti mimo pracovní rozbor.
- Neopakuj stejné informace.
- Pokud je údaj nejistý, označ ho jako nejistý.
- Nepiš obecná rizika.
"""

CASE_STUDY_ANALYSIS_PROMPT = """
Vytvoř pracovní odborný rozbor insolvenčního řízení pro dluhového poradce.

Toto je 1. krok zpracování kazuistiky: interní procesní analýza.
Cílem není finální text pro web, ale přesné zachycení toho, co se v řízení děje právě teď, co je potřeba řešit, a jaký je dosavadní vývoj.

Dostaneš:
1. ověřená systémová data z aplikace,
2. STRUKTUROVANÁ DATA Z FORMULÁŘOVÝCH PDF uložená v databázi,
3. seznam dokumentů z insolvenčního rejstříku,
4. vybraná PDF pro kontext řízení.

Priorita zdrojů:
- Pro finanční a procentuální údaje používej přednostně STRUKTUROVANÁ DATA Z FORMULÁŘOVÝCH PDF.
- PDF používej hlavně pro kontext řízení a vysvětlení vývoje.
- Lhůtu přihlášek, počet přihlášek a celkovou výši pohledávek přebírej výhradně ze systémových/strukturovaných dat dodaných aplikací.
- Pokud strukturovaná data obsahují konkrétní částku, procento, počet, datum nebo doporučení správce, nesmíš je nahrazovat odhadem z textu PDF.

Důležité pravidlo času:
- Vždy pracuj s aktuálním datem zpracování uvedeným v systémových datech.
- Termíny, které jsou před aktuálním datem zpracování, nejsou nejbližší budoucí termíny.
- Minulé termíny nedávej do „nearest_deadlines_and_events“ jako něco, co se teprve očekává. Patří pouze do historie nebo mezi nejasnosti, pokud ovlivňují aktuální práci.
- Pokud dokument z roku 2018 uvádí očekávaný budoucí krok, ale aktuální datum zpracování je pozdější, neformuluj ho jako aktuálně očekávaný krok.

Vrať pouze validní JSON v tomto formátu:

{
  "working_case_analysis": {
    "current_state_now": ["co se v případu děje právě teď a v jaké fázi řízení je"],
    "latest_important_change": ["poslední významná změna nebo dokument a jeho praktický význam"],
    "nearest_deadlines_and_events": ["datum – událost – co hlídat"],
    "advisor_tasks_now": ["co má poradce s klientem ověřit, vysvětlit nebo připravit nyní"],
    "client_tasks_now": ["co má klient udělat nyní nebo v nejbližší době"],
    "finance_and_claims_now": ["finanční a pohledávkové údaje významné pro aktuální postup; používat přesné údaje ze strukturovaných dat; systémové částky nepřepočítávat ani neodhadovat"],
    "debt_relief_evaluation": ["pokud existují strukturovaná data o plnění nebo splnění oddlužení, uveď přehled očekávání/průběh/skutečnost: přezkoumané pohledávky, průběžné uspokojení, konečné uspokojení, doporučení správce, osvobození nebo čekání na rozhodnutí soudu"],
    "uncertainties_affecting_current_work": ["neověřené, rozporné nebo průběžné údaje, které ovlivňují aktuální práci poradce"],
    "case_history_summary": ["stručný dosavadní vývoj případu"],
    "timeline": ["datum – dokument nebo událost – praktický význam"],
    "confidence": "nízká | střední | vysoká"
  }
}

Pravidla:
- Vrať pouze validní JSON.
- Piš česky, jednoduše a věcně.
- Piš pro dluhového poradce.
- Hlavní důraz dej na aktuální stav a aktuální práci poradce, ne na historický popis.
- Nepiš právní rady.
- Nevyvozuj právní závěry nad rámec dokumentů.
- Nevypočítávej lhůtu přihlášek.
- Nevypočítávej počet přihlášek.
- Nevypočítávej celkovou výši pohledávek.
- Nepoužívej formulace „cca“, „odhadem“, „pravděpodobně“ u částek a procent, pokud jsou ve strukturovaných datech konkrétní hodnoty.
- Pokud existují strukturovaná data o přezkumu, plnění nebo splnění oddlužení, musí se objevit v části finance_and_claims_now a případně debt_relief_evaluation.
- Pokud systémový údaj chybí, napiš „není bezpečně ověřeno“.
- Pokud údaj není jistý, označ ho jako neověřený, průběžný nebo rozporný.
- Neopakuj stejné informace vícekrát.
- Neuváděj obecné právní poučky.
- U každého termínu posuzuj, zda je vzhledem k aktuálnímu datu zpracování budoucí, dnešní, nebo minulý.
- Do aktuálních úkolů dávej pouze věci, které jsou stále relevantní k aktuálnímu datu zpracování.
- Pracovní rozbor nesmí přesáhnout 15 000 znaků.
"""

CASE_STUDY_FINAL_PROMPT = """
Převeď pracovní odborný rozbor do finální kazuistiky pro webovou aplikaci.

Toto je 2. krok zpracování: kontrola správnosti, zkrácení, odstranění duplicit a rozdělení do sekcí.
Kazuistika se pravidelně aktualizuje s novými dokumenty. Její hlavní funkce je rychle ukázat, co se v případu děje právě teď a co je potřeba řešit.

Při finálním zpracování vždy respektuj aktuální datum zpracování uvedené v systémových datech. Minulé termíny nesmí být popsány jako nejbližší očekávané kroky. Pokud je termín starý, patří do historie, případně do nejistot pouze tehdy, pokud z podkladů není jasné, jak byl vyřešen.

Vrať pouze text ve formátu sekcí. Nepoužívej JSON.
Nepřidávej žádný úvod ani závěr mimo sekce.

Použij přesně tyto 2 sekce:

Pokud ověřená systémová data obsahují STRUKTUROVANÁ DATA Z FORMULÁŘOVÝCH PDF, musí se jejich klíčové finanční a procentuální údaje promítnout do první sekce. Neignoruj je.

[[SECTION:current:Aktuální stav a co řešit]]
Max. 3 500 znaků. Tato sekce je hlavní pracovní část pro poradce.
Uveď:
- aktuální stav řízení,
- poslední významnou změnu,
- nejbližší termíny,
- co má poradce ověřit,
- co má klient udělat,
- finanční nebo pohledávkové údaje ze strukturovaných dat, pokud existují,
- u ukončeného oddlužení krátké vyhodnocení očekávání / průběh / skutečnost,
- neověřené nebo průběžné údaje, pokud ovlivňují aktuální práci,
- jistotu výstupu.

Doporučená vnitřní struktura:
Stav nyní:
...

Nejbližší termíny:
- ...

Co ověřit / řešit s klientem:
- ...

Co má udělat klient:
- ...

Finance a pohledávky:
- Přezkoumané pohledávky: ...
- Poslední průběžné uspokojení: ...
- Konečné uspokojení / splnění oddlužení: ...
- Doporučení správce: ...

Vyhodnocení oddlužení, pokud je případ ukončený nebo je k dispozici zpráva o splnění:
- Očekávání / průběh / skutečnost: ...

Nejistoty pro aktuální práci:
- ...

Jistota výstupu: nízká / střední / vysoká

[[SECTION:history:Vývoj řízení]]
Max. 2 500 znaků. Tato sekce je stručná historie případu.
Uveď:
- stručný vývoj případu ve 3–5 větách,
- časovou osu nejdůležitějších událostí,
- maximálně 12 položek časové osy,
- starší méně významné události slučuj.

Doporučená vnitřní struktura:
Stručný vývoj:
...

Časová osa:
- datum – dokument/událost – praktický význam

Limity:
- Celý výstup nesmí přesáhnout 6 000 znaků včetně mezer.
- Nevytvářej jiné hlavní sekce.
- Neopakuj informace mezi oběma sekcemi.
- Aktuální úkoly, termíny, finance a nejistoty patří do první sekce, pokud ovlivňují aktuální práci.
- Historické procesní informace patří do druhé sekce.

Pravidla:
- Piš česky, stručně a věcně.
- Nepřidávej nové informace mimo pracovní rozbor a systémová data.
- Nepiš právní rady.
- Nevyvozuj právní závěry.
- Nepiš obecné právní poučky.
- Pokud údaj není jistý, označ ho jako neověřený, průběžný nebo rozporný.
- Lhůtu přihlášek, počet přihlášek a celkovou výši pohledávek přebírej pouze ze systémových/strukturovaných dat.
- Pro přezkoumané pohledávky, procenta uspokojení, částky vyplacené věřitelům/správci, splnění oddlužení a doporučení správce používej přednostně STRUKTUROVANÁ DATA Z FORMULÁŘOVÝCH PDF.
- Pokud jsou strukturovaná data dostupná, nepiš místo nich obecné odhady ani „cca“.
- Nepoužívej markdownové zvýraznění pomocí **hvězdiček**. Piš čistý text, odrážky a krátké popisky.
- Termíny starší než aktuální datum zpracování neuváděj v první sekci jako „nejbližší termíny“ ani jako budoucí očekávané kroky.
- Jestliže pracovní rozbor obsahuje starý termín jako aktuální, ve finálním výstupu ho oprav: přesuň ho do historie nebo napiš, že z dostupných dokumentů není zřejmé, jak byl po tomto termínu vyřešen.
"""


# Zpětně kompatibilní názvy pro části kódu, které mohou ještě používat původní konstanty.
PROMPT = DOCUMENT_ANALYSIS_PROMPT
CASE_STUDY_PROMPT = CASE_STUDY_ANALYSIS_PROMPT

DATA_VERIFICATION_PROMPT = """
Zkontroluj návrh kazuistiky nebo AI textu z hlediska opatrnosti, srozumitelnosti a souladu s podklady.

Nejde o kontrolu databáze.
Nejde o výpočet lhůt, počtu přihlášek ani výše pohledávek.
Nesmíš navrhovat přímé opravy autoritativních polí aplikace.

Cíl:
Najít věty, které mohou být:
- nepodložené,
- příliš právně kategorické,
- zavádějící,
- nevhodné pro dluhové poradenství,
- v rozporu se systémovými daty.

Schéma:
{
  "overall_result": "v pořádku | nalezeny problémy | nelze ověřit",
  "issues": [
    {
      "sentence": "problematická věta nebo část textu",
      "problem": "proč je problém",
      "suggested_fix": "bezpečnější formulace",
      "severity": "nízká | střední | vysoká"
    }
  ],
  "safe_summary": "krátké shrnutí, zda je text použitelný pro dluhového poradce",
  "confidence": "nízká | střední | vysoká"
}

Pravidla:
- Vrať pouze validní JSON.
- Piš česky, stručně a věcně.
- Nekontroluj databázi.
- Nevypočítávej žádné částky.
- Nevypočítávej žádné lhůty.
- Neurčuj počet přihlášek.
- Nevracej doporučení typu „zapiš do systému“.
- Pokud text obsahuje částku, lhůtu nebo počet, pouze upozorni, zda je v rozporu se systémovým snapshotem, pokud byl dodán.
- Neposkytuj právní rady.
- Zaměř se na bezpečnou formulaci pro dluhového poradce.
"""


def _download_pdf(url: str) -> str:
    session = Session()
    session.trust_env = False
    response = session.get(url, timeout=30)
    response.raise_for_status()

    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        handle.write(response.content)
        return handle.name
    finally:
        handle.close()


def _as_text_list(value) -> str:
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value)
    if value is None:
        return ""
    return str(value)


def _format_obligations(value) -> str:
    if not isinstance(value, list):
        return _as_text_list(value)

    rows = []
    for item in value:
        if not isinstance(item, dict):
            rows.append(f"- {item}")
            continue

        obligation = item.get("obligation") or "Povinnost není popsána"
        recipient = item.get("recipient")
        deadline = item.get("deadline")
        certainty = item.get("source_certainty")
        details = []
        if recipient:
            details.append(f"komu/kam: {recipient}")
        if deadline:
            details.append(f"lhůta: {deadline}")
        if certainty:
            details.append(f"jistota: {certainty}")
        suffix = f" ({'; '.join(details)})" if details else ""
        rows.append(f"- {obligation}{suffix}")
    return "\n".join(rows)


def _format_value_for_display(value, indent: int = 0) -> str:
    """Převede JSON hodnotu na čitelný text pro sekční zobrazení."""
    prefix = "  " * indent

    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, (int, float, Decimal)):
        return str(value)

    if isinstance(value, list):
        rows = []
        for item in value:
            formatted = _format_value_for_display(item, indent + 1)
            if formatted:
                rows.append(f"{prefix}- {formatted}" if "\n" not in formatted else f"{prefix}- {formatted}")
        return "\n".join(rows)

    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            if key in {"id", "title"}:
                continue
            formatted = _format_value_for_display(item, indent + 1)
            if formatted:
                label = str(key).replace("_", " ")
                rows.append(f"{prefix}{label}:\n{formatted}")
        return "\n\n".join(rows)

    return str(value)


def _format_deadline_items(items) -> str:
    if not isinstance(items, list) or not items:
        return ""

    rows = []
    for item in items:
        if not isinstance(item, dict):
            rows.append(f"- {item}")
            continue

        date_or_period = item.get("date_or_period") or item.get("date") or item.get("deadline") or "neuvedeno"
        description = item.get("description") or item.get("text") or "bez popisu"
        recipient = item.get("recipient")
        source = item.get("source_document") or item.get("source")

        lines = [f"- Lhůta / datum: {date_or_period}", f"  Týká se: {description}"]
        if recipient:
            lines.append(f"  Komu / kam: {recipient}")
        if source:
            lines.append(f"  Zdroj: {source}")
        rows.append("\n".join(lines))

    return "\n".join(rows)


def _format_obligation_items(items) -> str:
    if not isinstance(items, list) or not items:
        return ""

    rows = []
    for item in items:
        if not isinstance(item, dict):
            rows.append(f"- {item}")
            continue

        obligation = item.get("obligation") or item.get("text") or "Povinnost není popsána"
        recipient = item.get("recipient")
        deadline = item.get("deadline")
        source = item.get("source_document") or item.get("source")

        lines = [f"- Povinnost: {obligation}"]
        if recipient:
            lines.append(f"  Komu / kam: {recipient}")
        if deadline:
            lines.append(f"  Lhůta: {deadline}")
        if source:
            lines.append(f"  Zdroj: {source}")
        rows.append("\n".join(lines))

    return "\n".join(rows)


def _section_text_from_payload_section(section: dict) -> str:
    section_id = str(section.get("id") or "").strip()

    if section_id == "summary":
        return _format_value_for_display(
            section.get("text")
            or section.get("summary")
            or section.get("content")
            or section.get("body")
        )

    if section_id == "deadlines_and_obligations":
        parts = []
        deadlines = _format_deadline_items(section.get("deadlines"))
        obligations = _format_obligation_items(section.get("obligations"))
        note = section.get("note")

        if deadlines:
            parts.append("Lhůty:\n" + deadlines)
        if obligations:
            parts.append("Povinnosti:\n" + obligations)
        if note:
            parts.append(str(note).strip())
        if not parts:
            parts.append("Dokumenty neobsahují konkrétní lhůty ani výslovné povinnosti dlužníka.")
        return "\n\n".join(parts)

    if section_id == "other_information_and_recommendations":
        parts = []
        important = _as_text_list(section.get("important_information"))
        recommendations = _as_text_list(section.get("debt_advisor_recommendations"))
        unclear = _as_text_list(section.get("unclear_or_incomplete_information"))

        if important:
            parts.append("Důležité informace:\n" + important)
        if recommendations:
            parts.append("Doporučení pro poradce:\n" + recommendations)
        if unclear:
            parts.append("Nejasné nebo neúplné údaje:\n" + unclear)
        return "\n\n".join(parts)

    value = section.get("content") if "content" in section else section.get("body")
    if value is None and "text" in section:
        value = section.get("text")
    if value is None:
        value = {key: item for key, item in section.items() if key not in {"id", "title"}}
    return _format_value_for_display(value)


def _format_document_summary(payload: dict) -> str:
    """Uloží shrnutí dokumentů ve stejném SECTION formátu, jaký používá frontend."""
    sections_payload = payload.get("sections") if isinstance(payload, dict) else None
    sections = []

    if isinstance(sections_payload, list) and sections_payload:
        for section in sections_payload:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("id") or "section").strip() or "section"
            title = str(section.get("title") or section_id).strip()
            body = _section_text_from_payload_section(section).strip()
            sections.append(f"[[SECTION:{section_id}:{title}]]\n{body or 'Bez obsahu.'}")
    else:
        summary = payload.get("summary") or payload.get("document_summary") or payload.get("debtor_obligations_summary") or "Shrnutí dokumentu se nepodařilo z AI odpovědi načíst."
        deadlines_and_obligations = {
            "id": "deadlines_and_obligations",
            "deadlines": payload.get("deadlines") or (payload.get("deadlines_and_obligations") or {}).get("deadlines") or [],
            "obligations": payload.get("obligations") or payload.get("explicit_obligations") or (payload.get("deadlines_and_obligations") or {}).get("obligations") or [],
            "note": payload.get("note") or (payload.get("deadlines_and_obligations") or {}).get("note") or "",
        }
        other = payload.get("other_information_and_recommendations") or {}
        sections = [
            "[[SECTION:summary:Shrnutí]]\n" + _format_value_for_display(summary),
            "[[SECTION:deadlines_and_obligations:Lhůty a povinnosti]]\n" + _section_text_from_payload_section(deadlines_and_obligations),
            "[[SECTION:other_information_and_recommendations:Ostatní informace a doporučení]]\n" + _section_text_from_payload_section({
                "id": "other_information_and_recommendations",
                "important_information": payload.get("important_information") or other.get("important_information") or [],
                "debt_advisor_recommendations": payload.get("debt_advisor_recommendations") or payload.get("practical_points_for_debt_advisor") or other.get("debt_advisor_recommendations") or [],
                "unclear_or_incomplete_information": payload.get("unclear_or_incomplete_information") or other.get("unclear_or_incomplete_information") or [],
            }),
        ]

    confidence = payload.get("confidence") if isinstance(payload, dict) else None
    if confidence and sections:
        sections[-1] = sections[-1].rstrip() + f"\n\nJistota shrnutí: {confidence}"


    return "\n\n".join(sections)


def _strip_code_fence(text: str | None) -> str:
    """Odstraní případné Markdown code fence kolem AI odpovědi."""
    value = str(text or "").strip()
    match = re.match(r"^```(?:json|text|markdown)?\s*([\s\S]*?)\s*```$", value, flags=re.IGNORECASE)
    return match.group(1).strip() if match else value


def _generate_json_with_retry(client: genai.Client, contents: list[Any], *, max_attempts: int = 2) -> dict:
    """Zavolá Gemini a vrátí JSON. Při obalení do code fence se pokusí JSON očistit."""
    last_error: Exception | None = None
    for _ in range(max_attempts):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            return json.loads(_strip_code_fence(response.text))
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    if last_error is not None:
        raise last_error
    raise RuntimeError("AI nevrátila validní JSON.")


def _generate_text_with_retry(client: genai.Client, contents: list[Any], *, max_attempts: int = 2) -> str:
    """Zavolá Gemini a vrátí text. Používá se pro finální SECTION výstupy."""
    last_error: Exception | None = None
    for _ in range(max_attempts):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
            )
            text = _strip_code_fence(response.text)
            if text:
                return text
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    if last_error is not None:
        raise last_error
    raise RuntimeError("AI nevrátila textový výstup.")


def _ensure_section_output_length(
    client: genai.Client,
    text: str,
    *,
    max_chars: int,
    output_type: str,
) -> str:
    """Pokud finální výstup překročí limit, požádá AI o zkrácení se zachováním SECTION značek."""
    cleaned = _strip_code_fence(text)
    if len(cleaned) <= max_chars:
        return cleaned

    shortening_prompt = f"""
Následující finální výstup je příliš dlouhý.
Zkrať ho na maximálně {max_chars} znaků včetně mezer.
Zachovej stejný formát značek [[SECTION:key:Název]].
Neměň význam, nepřidávej nové informace a nevynechávej konkrétní lhůty nebo povinnosti.
Typ výstupu: {output_type}.

PŮVODNÍ VÝSTUP:
{cleaned}
"""
    shortened = _generate_text_with_retry(client, [shortening_prompt], max_attempts=2)
    shortened = _strip_code_fence(shortened)

    # Poslední bezpečnostní brzda: raději neuseknout uprostřed section značky.
    if len(shortened) <= max_chars:
        return shortened
    return shortened[:max_chars].rstrip() + "\n\n[Výstup byl zkrácen kvůli délkovému limitu.]"


def _format_json_for_prompt(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)



def _normalize_czech_text(value: str | None) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip()


def _is_fulfillment_report_document(document: Any) -> bool:
    # Zpětná kompatibilita pro starší část kódu.
    return _is_debt_relief_structured_report_document(document)


def _is_debt_relief_structured_report_document(document: Any) -> bool:
    text = _normalize_czech_text(" ".join(
        part for part in [
            getattr(document, "title", None),
            getattr(document, "document_type", None),
        ] if part
    ))
    return any(phrase in text for phrase in (
        "zprava pro oddluzeni",
        "zprava o prezkumu",
        "zprave o prezkumu",
        "seznam prihlasenych pohledavek",
        "soupis majetkove podstaty",
        "vyrozumeni o popreni",
        "sdeleni spravce o plneni oddluzeni",
        "zdeleni spravce o plneni oddluzeni",
        "zprava o plneni oddluzeni",
        "zprava spravce o plneni oddluzeni",
        "sdeleni spravce o splneni oddluzeni",
        "zprava o splneni oddluzeni",
        "vyuctovani odmeny",
        "vyuctovani hotovych vydaju",
        "navrh na osvobozeni",
        "zopo",
    ))


def _structured_report_category(payload: dict) -> str:
    document_type = str(payload.get("document_type") or "").strip()
    labels = {
        "review_report": "Zpráva pro oddlužení / zpráva o přezkumu",
        "performance_report": "Sdělení správce o plnění oddlužení",
        "completion_report": "Sdělení správce o splnění oddlužení",
        "trustee_fee_accounting": "Vyúčtování odměny a výdajů správce",
        "mixed": "Formulářový dokument správce k oddlužení",
    }
    return labels.get(document_type, "Formulářový dokument správce k oddlužení")

def extract_structured_report_from_pdf_ai(
    pdf_path: str,
    document_title: str = "",
    api_key: str | None = None,
) -> dict[str, Any]:
    """Vytěží strukturovaná data z jednoho formulářového PDF správce.

    Funkce pouze vrací JSON payload. Ukládání do databáze řeší structured_data.persist_structured_extraction.
    """
    api_key = api_key or get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Chybí proměnná prostředí GEMINI_API_KEY.")

    uploaded_file = None
    temp_path = None
    client = None
    try:
        with _without_proxy_env():
            client = _make_gemini_client(api_key)
            temp_path = _make_ascii_pdf_copy(pdf_path)
            uploaded_file = _upload_with_retry(client, temp_path)
            return _generate_json_with_retry(
                client,
                [
                    uploaded_file,
                    f"{DEBT_RELIEF_STRUCTURED_REPORT_EXTRACTION_PROMPT}\n\nNázev dokumentu v ISIR: {document_title}",
                ],
            )
    finally:
        if uploaded_file is not None and client is not None:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def persist_structured_report_payload_for_case(
    case: InsolvencyCase,
    documents: list[InsolvencyDocument],
    payload: dict[str, Any],
    summary_text: str | None = None,
) -> dict[str, Any]:
    return persist_structured_extraction(
        case,
        documents,
        payload,
        model=GEMINI_MODEL,
        summary_text=summary_text,
    )






def _find_document_by_source_url(case: InsolvencyCase, source_url: str | None):
    if not source_url:
        return None
    return next((document for document in case.documents if document.source_url == source_url), None)


def _selected_documents_for_analysis(case: InsolvencyCase, document_ids: list[int] | None) -> list:
    ids = {int(value) for value in (document_ids or []) if str(value).strip().isdigit()}
    documents = [
        document
        for document in case.documents
        if document.local_path and Path(document.local_path).exists()
    ]
    if ids:
        documents = [document for document in documents if document.id in ids]
    return sorted(documents, key=lambda item: (item.event_at or datetime.min, item.id or 0))


def _build_fulfillment_report_final_prompt(payload: dict) -> str:
    return (
        f"{FULFILLMENT_REPORT_FINAL_PROMPT}\n\n"
        f"STRUKTUROVANÉ VYTĚŽENÍ ZPRÁVY:\n"
        f"{_format_json_for_prompt(payload)}"
    )

def _build_document_final_prompt(analysis_payload: dict) -> str:
    return (
        f"{DOCUMENT_FINAL_PROMPT}\n\n"
        f"PRACOVNÍ ODBORNÝ ROZBOR:\n"
        f"{_format_json_for_prompt(analysis_payload)}"
    )


def _build_case_study_final_prompt(
    analysis_payload: dict,
    *,
    verified_system_snapshot: str,
    claim_collection_running: bool,
) -> str:
    claim_note = (
        "Podle systémových údajů může stále běžet lhůta pro podávání přihlášek. "
        "Počet přihlášek a výši pohledávek formuluj jako průběžné údaje, pokud to odpovídá systémovým datům.\n\n"
        if claim_collection_running
        else ""
    )
    structured_priority_note = (
        "POVINNÉ PRAVIDLO PRO FINÁLNÍ KAZUISTIKU:\n"
        "- Jestliže ověřená systémová data obsahují část STRUKTUROVANÁ DATA Z FORMULÁŘOVÝCH PDF, použij ji jako hlavní zdroj pro finance, pohledávky, procenta uspokojení, plnění, splnění oddlužení a doporučení správce.\n"
        "- V první sekci musí být samostatný blok Finance a pohledávky. Pokud existuje zpráva o splnění oddlužení, přidej také blok Vyhodnocení oddlužení.\n"
        "- Neuváděj místo strukturovaných údajů odhady, cca hodnoty ani obecné formulace.\n"
        "- Nepoužívej markdownové hvězdičky pro zvýraznění.\n\n"
    )
    return (
        f"{CASE_STUDY_FINAL_PROMPT}\n\n"
        f"OVĚŘENÁ SYSTÉMOVÁ DATA Z APLIKACE:\n{verified_system_snapshot}\n\n"
        f"{structured_priority_note}"
        f"{claim_note}"
        f"PRACOVNÍ ODBORNÝ ROZBOR KAZUISTIKY:\n"
        f"{_format_json_for_prompt(analysis_payload)}"
    )


def _upload_with_retry(client: genai.Client, path: str | Path):
    last_error = None
    for _ in range(3):
        try:
            return client.files.upload(file=path)
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise last_error


@contextmanager
def _without_proxy_env():
    saved = {key: os.environ.get(key) for key in PROXY_ENV_KEYS}
    try:
        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _make_gemini_client(api_key: str) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            client_args={"trust_env": False, "timeout": 180},
            async_client_args={"trust_env": False, "timeout": 180},
        ),
    )


def _make_ascii_pdf_copy(path: str | Path) -> str:
    source = Path(path)
    handle = tempfile.NamedTemporaryFile(
        delete=False,
        prefix="gemini_pdf_",
        suffix=".pdf",
    )
    try:
        handle.close()
        shutil.copyfile(source, handle.name)
        return handle.name
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
CLAIM_AMOUNT_EXTRACTION_PROMPT = """
Přečti jedno PDF z insolvenčního rejstříku a vytěž pouze částku pohledávky.

Dostaneš vždy právě jeden dokument.

Rozliš tyto typy:

1) Přihláška pohledávky
- Použij část V. Pohledávky celkem.
- Vezmi hodnotu z pole „Celková výše přihlášených pohledávek“.
- Nepoužívej jistinu jednotlivé pohledávky, příslušenství ani jiné dílčí částky.

2) Návrh na uspokojení pohledávky za podstatou
- Použij hodnotu u textu „Zbývá k uspokojení“.
- Nepoužívej jiné částky, pokud je uvedeno více částek.

Vrať pouze validní JSON ve tvaru:
{
  "document_type": "prihlaska_pohledavky | pohledavka_za_podstatou | unknown",
  "amount": 11674,
  "currency": "CZK",
  "evidence": "krátký opis místa v dokumentu, ze kterého částka vyplývá",
  "confidence": "high | medium | low"
}

Pravidla:
- Nehádej.
- Pokud částku nenajdeš, vrať "amount": null.
- Pokud si nejsi jistý, nastav confidence na "low".
- Částku vrať jako číslo bez mezer a bez měny.
- Nepiš žádný komentář mimo JSON.
"""


def _parse_ai_amount(value) -> Decimal | None:
    if value is None:
        return None

    text = str(value).strip()

    if not text or text.lower() in {"null", "none", "neuvedeno", "není uvedeno"}:
        return None

    text = text.replace("\xa0", " ")
    text = re.sub(r"[^\d,.\s]", "", text).strip()

    if not text:
        return None

    text = text.replace(" ", "")

    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")

    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def extract_claim_amount_from_pdf_ai(
    pdf_path: str | Path,
    document_title: str = "",
    api_key: str | None = None,
) -> Decimal | None:
    api_key = api_key or get_gemini_api_key()

    if not api_key:
        raise RuntimeError("Chybí proměnná prostředí GEMINI_API_KEY.")

    source_path = Path(pdf_path)

    if not source_path.exists():
        raise RuntimeError(f"PDF soubor neexistuje: {source_path}")

    uploaded_file = None
    temp_path = None
    client = None

    try:
        with _without_proxy_env():
            client = _make_gemini_client(api_key)

            temp_path = _make_ascii_pdf_copy(source_path)
            uploaded_file = _upload_with_retry(client, temp_path)

            prompt = (
                f"{CLAIM_AMOUNT_EXTRACTION_PROMPT}\n\n"
                f"Název dokumentu podle ISIR: {document_title or 'neuvedeno'}"
            )

            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )

        payload = json.loads(response.text)

        confidence = str(payload.get("confidence") or "").strip().lower()
        amount = _parse_ai_amount(payload.get("amount"))

        if amount is None:
            return None

        # Nízkou jistotu raději nepoužíváme jako částku.
        if confidence in {"low", "nízká", "nizka"}:
            return None

        return amount

    finally:
        if uploaded_file is not None and client is not None:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass

        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass



def analyze_case_documents(
    case: InsolvencyCase,
    document_ids: list[int] | None = None,
    api_key: str | None = None,
) -> None:
    """Vytvoří shrnutí vybraných dokumentů. Speciální formuláře ZOPO zpracuje šablonově."""
    api_key = api_key or get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Chybí proměnná prostředí GEMINI_API_KEY.")

    documents = _selected_documents_for_analysis(case, document_ids)
    if not documents:
        raise RuntimeError("Nejsou vybrané žádné stažené PDF dokumenty pro AI shrnutí.")

    # Formulářové/tabulkové dokumenty nesmí tlačítko „Vytvořit AI shrnutí“ znovu
    # dlouze vytěžovat z PDF. Shrnutí se vytváří z uložených strukturovaných dat.
    # Pokud data chybí, vrátí se krátká informace místo spuštění dalšího dlouhého sběru.
    if all(_is_debt_relief_structured_report_document(document) for document in documents):
        document_ids_for_cache = [int(document.id) for document in documents if getattr(document, "id", None)]
        cached_payloads = get_document_extraction_payloads(document_ids_for_cache)
        missing_documents = [
            document for document in documents
            if getattr(document, "id", None) and int(document.id) not in cached_payloads
        ]
        with _without_proxy_env():
            client = _make_gemini_client(api_key)
            if cached_payloads:
                analysis_payload = _combine_cached_structured_payloads(documents, cached_payloads)
                if missing_documents:
                    analysis_payload.setdefault("warnings", [])
                    analysis_payload["warnings"].append(
                        "U části vybraných formulářových dokumentů zatím nejsou uložená strukturovaná data; shrnutí pracuje jen s již vytěženými dokumenty."
                    )
                    analysis_payload["missing_structured_documents"] = [
                        {"id": d.id, "title": d.title or d.document_type} for d in missing_documents
                    ]
                final_prompt = _build_structured_report_final_prompt(analysis_payload)
                final_summary = _generate_text_with_retry(client, [final_prompt])
                final_summary = _ensure_section_output_length(
                    client,
                    final_summary,
                    max_chars=MAX_DOCUMENT_SUMMARY_CHARS,
                    output_type="shrnutí dokumentu",
                )
                category = _structured_report_category(analysis_payload)
            else:
                analysis_payload = {
                    "document_family": "debt_relief_structured_report",
                    "document_type": "not_extracted_yet",
                    "missing_structured_documents": [
                        {"id": d.id, "title": d.title or d.document_type} for d in documents
                    ],
                    "confidence": "low",
                    "warnings": [
                        "Vybrané formulářové/tabulkové dokumenty zatím nemají uložená strukturovaná data. Shrnutí se proto nespustilo nad PDF, aby znovu neblokovalo aplikaci dlouhým sběrem dat."
                    ],
                }
                final_summary = (
                    "[[SECTION:summary:Shrnutí]]\n"
                    "Vybrané formulářové/tabulkové dokumenty zatím nemají uložená strukturovaná data. "
                    "Shrnutí nebylo spuštěno z PDF, aby se neopakoval dlouhý sběr dat.\n\n"
                    "[[SECTION:deadlines:Lhůty a povinnosti]]\n"
                    "Z těchto dokumentů zatím nejsou bezpečně vytěžena data pro lhůty nebo povinnosti.\n\n"
                    "[[SECTION:other:Ostatní informace a doporučení]]\n"
                    "Nechte doběhnout automatickou strukturovanou extrakci, případně zkontrolujte chyby u formulářových dokumentů. "
                    "Poté lze shrnutí vytvořit z uložených dat bez opětovného čtení PDF."
                )
                category = "Formulářová data zatím nejsou vytěžena"

        case.ai_checked_at = datetime.utcnow()
        case.ai_model = GEMINI_MODEL
        case.ai_category = category
        case.ai_summary = final_summary
        case.ai_key_points = ""
        case.ai_deadlines = ""
        case.ai_recommended_action = ""
        case.ai_raw_result = json.dumps(
            {
                "selected_documents": [
                    {
                        "id": document.id,
                        "title": document.title,
                        "event_at": document.event_at.isoformat() if document.event_at else None,
                        "special_structured_report": True,
                    }
                    for document in documents
                ],
                "used_cached_structured_data": bool(cached_payloads),
                "missing_structured_document_ids": [int(d.id) for d in missing_documents if getattr(d, "id", None)],
                "cached_structured_payload": analysis_payload,
                "step_2_final_summary": final_summary,
            },
            ensure_ascii=False,
            indent=2,
        )
        return

    uploaded_files = []
    temp_paths = []
    client = None

    try:
        with _without_proxy_env():
            client = _make_gemini_client(api_key)

            for document in documents:
                temp_path = _make_ascii_pdf_copy(document.local_path)
                temp_paths.append(temp_path)
                uploaded_files.append(_upload_with_retry(client, temp_path))

            document_list = "\n".join(
                f"- {document.event_at.strftime('%d.%m.%Y') if document.event_at else 'bez data'}: {document.title or document.document_type or 'dokument'}"
                for document in documents
            )

            # Pokud jsou všechny vybrané dokumenty formulářové dokumenty správce
            # k přezkumu / plnění / splnění oddlužení, použijeme společný specializovaný extractor.
            if all(_is_debt_relief_structured_report_document(document) for document in documents):
                analysis_payload = _generate_json_with_retry(
                    client,
                    [
                        *uploaded_files,
                        f"{DEBT_RELIEF_STRUCTURED_REPORT_EXTRACTION_PROMPT}\n\nVybrané dokumenty:\n{document_list}",
                    ],
                )
                final_prompt = _build_structured_report_final_prompt(analysis_payload)
                final_summary = _generate_text_with_retry(client, [final_prompt])
                category = _structured_report_category(analysis_payload)
            else:
                analysis_payload = _generate_json_with_retry(
                    client,
                    [
                        *uploaded_files,
                        f"{DOCUMENT_ANALYSIS_PROMPT}\n\nVybrané dokumenty:\n{document_list}",
                    ],
                )
                final_prompt = _build_document_final_prompt(analysis_payload)
                final_summary = _generate_text_with_retry(client, [final_prompt])
                category = analysis_payload.get("category") or "AI shrnutí vybraných dokumentů"

            final_summary = _ensure_section_output_length(
                client,
                final_summary,
                max_chars=MAX_DOCUMENT_SUMMARY_CHARS,
                output_type="shrnutí dokumentu",
            )

        case.ai_checked_at = datetime.utcnow()
        case.ai_model = GEMINI_MODEL
        case.ai_category = category
        case.ai_summary = final_summary
        case.ai_key_points = ""
        case.ai_deadlines = ""
        case.ai_recommended_action = ""
        case.ai_raw_result = json.dumps(
            {
                "selected_documents": [
                    {
                        "id": document.id,
                        "title": document.title,
                        "event_at": document.event_at.isoformat() if document.event_at else None,
                        "special_structured_report": _is_debt_relief_structured_report_document(document),
                    }
                    for document in documents
                ],
                "step_1_working_analysis": analysis_payload,
                "step_2_final_summary": final_summary,
            },
            ensure_ascii=False,
            indent=2,
        )

        # V1: pokud šlo o formulářový dokument správce/přezkumu/plnění/splnění,
        # ulož strukturovaná data do samostatných DB tabulek.
        if all(_is_debt_relief_structured_report_document(document) for document in documents):
            try:
                persist_structured_report_payload_for_case(
                    case,
                    documents,
                    analysis_payload,
                    summary_text=final_summary,
                )
            except Exception as exc:
                # Shrnutí dokumentu nesmí spadnout jen proto, že se nepovedlo uložit
                # strukturovaná data. Chyba zůstane v surovém AI výstupu.
                existing_payload = json.loads(case.ai_raw_result or "{}")
                existing_payload["structured_persist_error"] = str(exc)
                case.ai_raw_result = json.dumps(existing_payload, ensure_ascii=False, indent=2)

    finally:
        if uploaded_files and client is not None:
            for uploaded_file in uploaded_files:
                try:
                    client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass

        for temp_path in temp_paths:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def analyze_case_latest_document(case, api_key=None):
    api_key = api_key or get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Chybí proměnná prostředí GEMINI_API_KEY.")
    if not case.document_url:
        raise RuntimeError("U řízení není uložený odkaz na PDF dokument.")

    matching_document = _find_document_by_source_url(case, case.document_url)
    if matching_document is not None and matching_document.local_path and Path(matching_document.local_path).exists():
        analyze_case_documents(case, [matching_document.id], api_key=api_key)
        return

    pdf_path = _download_pdf(case.document_url)
    uploaded_file = None
    client = None

    try:
        with _without_proxy_env():
            client = _make_gemini_client(api_key)
            uploaded_file = _upload_with_retry(client, pdf_path)

            # 1) Pracovní odborný rozbor z PDF.
            analysis_payload = _generate_json_with_retry(
                client,
                [uploaded_file, DOCUMENT_ANALYSIS_PROMPT],
            )

            # 2) Finální krátké shrnutí do tří sekcí pro web.
            final_prompt = _build_document_final_prompt(analysis_payload)
            final_summary = _generate_text_with_retry(client, [final_prompt])
            final_summary = _ensure_section_output_length(
                client,
                final_summary,
                max_chars=MAX_DOCUMENT_SUMMARY_CHARS,
                output_type="shrnutí dokumentu",
            )

        case.ai_checked_at = datetime.utcnow()
        case.ai_model = GEMINI_MODEL
        case.ai_category = analysis_payload.get("category") or "AI shrnutí dokumentu"
        case.ai_summary = final_summary

        # Nové zobrazení používá case.ai_summary ve formátu [[SECTION:...]].
        # Stará pole čistíme, aby se informace ve starších šablonách nedublovaly.
        case.ai_key_points = ""
        case.ai_deadlines = ""
        case.ai_recommended_action = ""
        case.ai_raw_result = json.dumps(
            {
                "step_1_working_analysis": analysis_payload,
                "step_2_final_summary": final_summary,
            },
            ensure_ascii=False,
            indent=2,
        )

    finally:
        if uploaded_file is not None and client is not None:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass

        try:
            os.unlink(pdf_path)
        except OSError:
            pass


def _format_case_study(payload, claim_collection_running=False):
    """Převede JSON kazuistiku do pěti sekcí pro levé menu / pravý obsah."""

    def to_list_text(value):
        if isinstance(value, list):
            rows = []
            for item in value:
                if isinstance(item, dict):
                    formatted = _format_value_for_display(item)
                    if formatted:
                        rows.append("- " + formatted)
                elif item:
                    rows.append("- " + str(item))
            return "\n".join(rows)
        if value:
            return str(value)
        return ""

    def section(key: str, title: str, body: str) -> str:
        return f"[[SECTION:{key}:{title}]]\n{body.strip() or 'Není bezpečně ověřeno.'}"

    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    finance = payload.get("finance") if isinstance(payload.get("finance"), dict) else {}
    history = payload.get("history") if isinstance(payload.get("history"), dict) else {}
    uncertainties = payload.get("uncertainties") if isinstance(payload.get("uncertainties"), dict) else {}

    # Záloha pro starší JSON z předchozího promptu.
    old_overview = payload.get("overview") if isinstance(payload.get("overview"), dict) else {}
    old_current_state = payload.get("current_state") if isinstance(payload.get("current_state"), dict) else {}
    old_development = payload.get("development") if isinstance(payload.get("development"), dict) else {}
    old_claims = payload.get("claims") if isinstance(payload.get("claims"), dict) else {}
    old_recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), dict) else {}

    claims_deadline = payload.get("claims_deadline") or finance.get("claims_deadline") or old_claims.get("deadline") or "není bezpečně ověřeno"
    claims_total_amount = payload.get("claims_total_amount") or finance.get("claims_total_amount") or old_claims.get("total_amount") or "není bezpečně ověřeno"
    claims_count = payload.get("claims_count") or finance.get("claims_count") or old_claims.get("count") or "není bezpečně ověřeno"

    current_summary = (
        current.get("working_summary")
        or old_current_state.get("summary")
        or old_overview.get("short_status")
        or "Není bezpečně ověřeno."
    )
    nearest_terms = current.get("nearest_terms") or payload.get("key_dates") or []
    advisor_checklist = current.get("advisor_checklist") or payload.get("debt_advisor_next_steps") or payload.get("social_worker_next_steps") or old_recommendations.get("debt_advisor_next_steps") or []
    client_checklist = current.get("client_checklist") or old_current_state.get("current_obligations") or payload.get("debtor_obligations") or []
    changes = current.get("changes_since_last_update") or ["Nelze určit bez předchozí verze kazuistiky."]

    current_parts = ["Pracovní souhrn:\n" + str(current_summary)]
    if nearest_terms:
        current_parts.append("Nejbližší termíny:\n" + to_list_text(nearest_terms))
    if advisor_checklist:
        current_parts.append("Co má poradce ověřit:\n" + to_list_text(advisor_checklist))
    if client_checklist:
        current_parts.append("Co má klient udělat:\n" + to_list_text(client_checklist))
    if changes:
        current_parts.append("Změny od poslední aktualizace:\n" + to_list_text(changes))

    status_lines = []
    status_fields = [
        ("Soud", status.get("court")),
        ("Spisová značka", status.get("case_number")),
        ("Dlužník", status.get("debtor") or payload.get("case_title") or old_overview.get("title")),
        ("Aktuální fáze", status.get("phase")),
        ("Forma řešení úpadku", status.get("insolvency_solution")),
        ("Insolvenční správce", status.get("insolvency_administrator")),
    ]
    for label, value in status_fields:
        if value:
            status_lines.append(f"- {label}: {value}")
    status_summary = status.get("summary") or old_current_state.get("summary") or old_development.get("summary")
    if status_summary:
        status_lines.append("\nShrnutí:\n" + str(status_summary))

    finance_lines = [
        f"- Lhůta pro přihlášky: {claims_deadline}",
        f"- Celková výše přihlášených pohledávek: {claims_total_amount}",
        f"- Počet přihlášek: {claims_count}",
    ]
    data_status = finance.get("data_status")
    if data_status:
        finance_lines.append(f"- Stav údajů: {data_status}")
    if claim_collection_running:
        finance_lines.append("- Poznámka: Sběr přihlášek může stále probíhat; počet přihlášek a výše pohledávek formuluj jako průběžné údaje.")
    for label, value in [
        ("Výživné", finance.get("maintenance")),
        ("Zálohy insolvenčnímu správci", finance.get("administrator_deposit")),
        ("Srážky ze mzdy", finance.get("wage_deductions")),
    ]:
        if value:
            finance_lines.append(f"- {label}: {value}")
    other_finance = finance.get("other_financial_information")
    if other_finance:
        finance_lines.append("\nDalší finanční informace:\n" + to_list_text(other_finance))

    timeline = history.get("timeline") or old_development.get("timeline") or payload.get("timeline") or []

    unclear = uncertainties.get("unclear_or_unverified") or payload.get("not_safely_verified") or old_recommendations.get("not_safely_verified") or []
    risks = uncertainties.get("practical_risks") or payload.get("risks") or old_recommendations.get("risks") or []
    confidence = uncertainties.get("confidence") or payload.get("confidence") or "není uvedena"
    uncertainty_parts = []
    if unclear:
        uncertainty_parts.append("Neověřené / rozporné / průběžné údaje:\n" + to_list_text(unclear))
    if risks:
        uncertainty_parts.append("Praktická rizika:\n" + to_list_text(risks))
    uncertainty_parts.append("Jistota výstupu: " + str(confidence))

    sections = [
        section("current", "Aktuálně řešit", "\n\n".join(current_parts)),
        section("status", "Stav případu", "\n".join(status_lines)),
        section("finance", "Pohledávky a finance", "\n".join(finance_lines)),
        section("history", "Vývoj řízení", to_list_text(timeline) or "Není bezpečně ověřeno."),
        section("uncertainties", "Nejistoty a rizika", "\n\n".join(uncertainty_parts)),
    ]

    return "\n\n".join(sections)


def _normalized_text(value: str | None) -> str:
    return str(value or "").casefold()

def _case_is_closed(case: InsolvencyCase) -> bool:
    status = _normalized_text(case.state)
    return "odškrtnuta" in status or "odskrtnuta" in status or "od krtnuta" in status or "zruš" in status or "zrus" in status


def _closure_paragraph(case: InsolvencyCase) -> str:
    documents = sorted(
        [
            document
            for document in case.documents
            if document.title or document.event_at
        ],
        key=lambda item: (item.event_at or datetime.min, item.id or 0),
        reverse=True,
    )[:5]
    document_lines = []
    for document in documents:
        date_text = document.event_at.strftime("%d.%m.%Y") if document.event_at else "bez data"
        document_lines.append(f"{date_text}: {document.title or 'dokument bez názvu'}")

    parts = []
    if case.state:
        parts.append(f"Řízení je v ISIR vedené jako {case.state}.")
    if case.last_event_at or case.last_event_description:
        date_text = case.last_event_at.strftime("%d.%m.%Y") if case.last_event_at else "bez data"
        description = case.last_event_description or case.last_event_type or "poslední událost bez popisu"
        parts.append(f"Poslední zaznamenaná událost je {date_text}: {description}.")
    if document_lines:
        parts.append("Poslední dostupné dokumenty: " + "; ".join(document_lines) + ".")
    if not parts:
        parts.append("Řízení je označené jako ukončené, ale v uložených dokumentech není k dispozici bližší popis posledních kroků.")

    return "Ukončení insolvence:\n" + " ".join(parts)


def _clean_claim_value(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"neuvedeno", "není uvedeno", "nezjištěno"}:
        return None
    return text[:2000]


def _parse_claim_deadline(value: str | None) -> date | None:
    if not value:
        return None
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", str(value))
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _effective_claim_deadline(case: InsolvencyCase) -> date | None:
    explicit_deadline = _parse_claim_deadline(case.claims_deadline)
    if explicit_deadline:
        return explicit_deadline
    if case.proceeding_started_at:
        return _add_months(case.proceeding_started_at.date(), 2)
    if case.started_at:
        return _add_months(case.started_at, 2)
    return None


def _claim_collection_running(case: InsolvencyCase) -> bool:
    deadline = _effective_claim_deadline(case)
    return bool(deadline and datetime.now().date() <= deadline)


def _claim_document_count(case: InsolvencyCase) -> int:
    return sum(
        1
        for document in case.documents
        if _is_claim_document(document)
    )


def _is_claim_document(document) -> bool:
    text = " ".join(
        part
        for part in [getattr(document, "title", None), getattr(document, "document_type", None)]
        if part
    ).casefold()
    document_type = (getattr(document, "document_type", None) or "").casefold()
    title = (getattr(document, "title", None) or "").casefold()
    if "přihláška pohledávky" not in text:
        return False
    if "vedlejší dokument" in document_type or "vedlejší dokument" in title:
        return False
    return True


def _important_non_claim_documents(available_documents: list) -> list:
    important_words = (
        "usnesení",
        "vyhláška",
        "sdělení insolvenčního správce",
        "zpráva",
        "přezkumn",
        "seznam",
        "oddlužení",
        "opatření",
        "soupis",
    )
    return [
        document
        for document in available_documents
        if not _is_claim_document(document)
        and any(word in (document.title or "").casefold() for word in important_words)
    ]


def _case_study_documents(case: InsolvencyCase) -> list:
    available_documents = [
        document
        for document in sorted(case.documents, key=lambda item: item.event_at or datetime.min)
        if document.local_path and Path(document.local_path).exists()
    ]
    if len(available_documents) <= MAX_CASE_STUDY_PDFS:
        return available_documents

    claim_documents = [document for document in available_documents if _is_claim_document(document)]
    important_documents = _important_non_claim_documents(available_documents)

    selected = []
    for document in claim_documents:
        if document not in selected:
            selected.append(document)

    for document in important_documents:
        if document not in selected:
            selected.append(document)

    for document in available_documents:
        if len(selected) >= max(MAX_CASE_STUDY_PDFS, len(claim_documents)):
            break
        if document not in selected:
            selected.append(document)

    return sorted(selected[: max(MAX_CASE_STUDY_PDFS, len(claim_documents))], key=lambda item: item.event_at or datetime.min)


def _case_data_verification_documents(case: InsolvencyCase) -> list:
    available_documents = [
        document
        for document in sorted(case.documents, key=lambda item: item.event_at or datetime.min)
        if document.local_path and Path(document.local_path).exists()
    ]
    claim_documents = [document for document in available_documents if _is_claim_document(document)]
    selected = list(claim_documents)

    for document in _important_non_claim_documents(available_documents):
        if len(selected) >= len(claim_documents) + MAX_DATA_VERIFICATION_NON_CLAIM_PDFS:
            break
        if document not in selected:
            selected.append(document)

    for document in available_documents:
        if len(selected) >= len(claim_documents) + MAX_DATA_VERIFICATION_NON_CLAIM_PDFS:
            break
        if document not in selected:
            selected.append(document)

    return sorted(selected, key=lambda item: item.event_at or datetime.min)



def analyze_case_study(case: InsolvencyCase, api_key: str | None = None) -> None:
    api_key = api_key or get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Chybí proměnná prostředí GEMINI_API_KEY.")

    all_documents = [
        document
        for document in sorted(case.documents, key=lambda item: item.event_at or datetime.min)
        if document.local_path and Path(document.local_path).exists()
    ]

    documents = _case_study_documents(case)
    if not documents:
        raise RuntimeError("Nejsou uložené žádné PDF dokumenty pro vytvoření kazuistiky.")

    claim_collection_running = _claim_collection_running(case)

    # Ověřené údaje ze systému/Pythonu.
    # AI je smí použít v textu, ale nesmí je sama počítat ani přepisovat.
    verified_claims_deadline = case.claims_deadline or "není bezpečně ověřeno"
    verified_claims_total_amount = case.claims_total_amount or "není bezpečně ověřeno"
    verified_claims_count = (
        str(case.claims_count)
        if case.claims_count is not None
        else "není bezpečně ověřeno"
    )

    current_processing_date = datetime.now().strftime("%d.%m.%Y")

    structured_snapshot = get_case_structured_snapshot(case.id) if case.id else "Strukturovaná data zatím nejsou dostupná."

    verified_system_snapshot = "\n".join(
        [
            f"- Aktuální datum zpracování: {current_processing_date}",
            f"- Spisová značka: {case.spisova_znacka or 'neuvedeno'}",
            f"- Stav řízení: {case.state or 'neuvedeno'}",
            f"- Dlužník: {case.debtor_name or 'neuvedeno'}",
            f"- Lhůta přihlášek podle systému/Pythonu: {verified_claims_deadline}",
            f"- Počet přihlášek / pohledávkových dokumentů podle systému/Pythonu: {verified_claims_count}",
            f"- Celková výše přihlášených pohledávek podle systému/Pythonu: {verified_claims_total_amount}",
            f"- Poslední událost: {case.last_event_description or case.last_event_type or 'neuvedeno'}",
            "",
            "STRUKTUROVANÁ DATA Z FORMULÁŘOVÝCH PDF:",
            structured_snapshot,
        ]
    )

    uploaded_files = []
    temp_paths = []
    client = None

    try:
        with _without_proxy_env():
            client = _make_gemini_client(api_key)

            for document in documents:
                temp_path = _make_ascii_pdf_copy(document.local_path)
                temp_paths.append(temp_path)
                uploaded_files.append(_upload_with_retry(client, temp_path))

            document_list = "\n".join(
                f"- {document.event_at.strftime('%d.%m.%Y') if document.event_at else 'bez data'}: {document.title}"
                for document in all_documents
            )

            uploaded_document_list = "\n".join(
                f"- {document.event_at.strftime('%d.%m.%Y') if document.event_at else 'bez data'}: {document.title}"
                for document in documents
            )

            claim_status_note = (
                "Pozor: podle systémových údajů může stále běžet lhůta pro podávání přihlášek. "
                "Počet přihlášek a celkovou výši pohledávek proto formuluj jako průběžné údaje, pokud to odpovídá systémovým datům. "
                "Tyto hodnoty ale nepřepočítávej z PDF.\n\n"
                if claim_collection_running
                else ""
            )

            analysis_prompt = (
                f"{CASE_STUDY_ANALYSIS_PROMPT}\n\n"
                f"OVĚŘENÁ SYSTÉMOVÁ DATA Z APLIKACE:\n"
                f"{verified_system_snapshot}\n\n"
                f"Pravidlo pro číselné a finanční údaje:\n"
                f"- Lhůtu přihlášek, počet přihlášek a celkovou výši pohledávek přebírej pouze z ověřených systémových/strukturovaných dat výše.\n"
                f"- Přezkoumané pohledávky, procenta uspokojení, částky vyplacené věřitelům/správci, splnění oddlužení a doporučení správce přebírej přednostně ze STRUKTUROVANÝCH DAT Z FORMULÁŘOVÝCH PDF.\n"
                f"- Tyto údaje nevypočítávej ani neodhaduj z PDF, pokud jsou ve strukturovaných datech.\n"
                f"- Pokud je systémový údaj označen jako „není bezpečně ověřeno“, napiš to stejně.\n"
                f"- Nepoužívej slova cca/odhadem u částek a procent, pokud jsou ve strukturovaných datech konkrétní hodnoty.\n"
                f"- Aktuální datum zpracování je {current_processing_date}. Termíny před tímto datem nepovažuj za budoucí ani nejbližší.\n\n"
                f"V řízení je celkem {len(all_documents)} PDF dokumentů.\n\n"
                f"Kompletní seznam dokumentů podle ISIR:\n{document_list}\n\n"
                f"Obsahově přiložené PDF dokumenty pro kontext kazuistiky:\n{uploaded_document_list}"
            )

            # 1) Pracovní odborný rozbor z PDF a systémových dat.
            analysis_payload = _generate_json_with_retry(
                client,
                [*uploaded_files, f"{claim_status_note}{analysis_prompt}"],
            )

            # Systémová data připojujeme znovu i do finálního kroku, aby druhý krok
            # nepřepsal ani nepřepočítal lhůtu, počet přihlášek nebo částky.
            final_prompt = _build_case_study_final_prompt(
                analysis_payload,
                verified_system_snapshot=verified_system_snapshot,
                claim_collection_running=claim_collection_running,
            )

            # 2) Finální kazuistika do pěti sekcí pro web.
            formatted_case_study = _generate_text_with_retry(client, [final_prompt])
            formatted_case_study = _ensure_section_output_length(
                client,
                formatted_case_study,
                max_chars=MAX_CASE_STUDY_CHARS,
                output_type="kazuistika",
            )

        if _case_is_closed(case):
            formatted_case_study = "\n\n".join(
                part for part in [formatted_case_study, _closure_paragraph(case)] if part
            )

        case.ai_case_study_at = datetime.utcnow()
        case.ai_case_study = formatted_case_study

        # Uložíme oba kroky pro ladění a kontrolu, ale do UI jde pouze finální SECTION text.
        try:
            case.ai_raw_result = json.dumps(
                {
                    "step_1_working_case_analysis": analysis_payload,
                    "step_2_final_case_study": formatted_case_study,
                    "verified_system_snapshot": verified_system_snapshot,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except Exception:
            pass

    finally:
        if uploaded_files and client is not None:
            for uploaded_file in uploaded_files:
                try:
                    client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass

        for temp_path in temp_paths:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _format_stored_datetime(value) -> str:
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value)


def _case_data_snapshot(case: InsolvencyCase) -> dict[str, str]:
    client = case.client
    return {
        "Klient": f"{client.last_name} {client.first_name}" if client else "-",
        "Dlužník": case.debtor_name or "-",
        "Poslední událost přihlášky": case.last_event_description or case.last_event_type or "-",
        "Datum narození": _format_stored_datetime(client.birth_date) if client else "-",
        "Poslední kontrola": _format_stored_datetime(client.last_checked_at) if client else "-",
        "Spisová značka": case.spisova_znacka or "-",
        "Počet dokumentů": str(case.document_count if case.document_count is not None else "-"),
        "Stav řízení": case.state or "-",
        "Datum zahájení": _format_stored_datetime(case.proceeding_started_at or case.started_at),
        "Lhůta přihlášek": case.claims_deadline or _format_stored_datetime(_effective_claim_deadline(case)) or "-",
        "Počet přihlášek": str(case.claims_count if case.claims_count is not None else "-"),
        "Výše přihlášených pohledávek": case.claims_total_amount or "-",
    }


def _format_verification_fields(fields) -> str:
    if not isinstance(fields, list):
        return _as_text_list(fields)

    rows = []
    for item in fields:
        if not isinstance(item, dict):
            rows.append(f"- {item}")
            continue
        field = item.get("field") or "Údaj"
        status = item.get("status") or "nelze ověřit"
        stored_value = item.get("stored_value") or "-"
        pdf_value = item.get("pdf_value") or "-"
        source = item.get("source") or "zdroj neuveden"
        note = item.get("note") or ""
        line = f"- {field}: {status}. Uloženo: {stored_value}; PDF: {pdf_value}; zdroj: {source}"
        if note:
            line = f"{line}. {note}"
        rows.append(line)
    return "\n".join(rows)


def _format_claims_deadline_verification(value) -> str:
    if not isinstance(value, dict):
        return _as_text_list(value)
    parts = [
        f"Stav: {value.get('status') or 'nelze ověřit'}",
        f"Uloženo: {value.get('stored_value') or '-'}",
        f"PDF: {value.get('pdf_value') or '-'}",
        f"Jistota: {value.get('confidence') or '-'}",
        f"Zdroj: {value.get('source') or '-'}",
    ]
    note = value.get("note")
    if note:
        parts.append(f"Poznámka: {note}")
    return "\n".join(parts)


def _format_claims_amount_verification(value) -> str:
    if not isinstance(value, dict):
        return _as_text_list(value)
    parts = [
        f"Stav: {value.get('status') or 'nelze ověřit'}",
        f"Uloženo: {value.get('stored_value') or '-'}",
        f"PDF / součet V. Pohledávky celkem: {value.get('pdf_value') or '-'}",
        f"Počet zahrnutých přihlášek: {value.get('claims_count') or '-'}",
        f"Zdroj: {value.get('source') or '-'}",
    ]
    note = value.get("note")
    if note:
        parts.append(f"Poznámka: {note}")
    return "\n".join(parts)


def analyze_case_data_verification(case: InsolvencyCase, api_key: str | None = None) -> None:
    api_key = api_key or get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Chybí proměnná prostředí GEMINI_API_KEY.")

    all_documents = [
        document
        for document in sorted(case.documents, key=lambda item: item.event_at or datetime.min)
        if document.local_path and Path(document.local_path).exists()
    ]
    documents = _case_data_verification_documents(case)
    if not documents:
        raise RuntimeError("Nejsou uložené žádné PDF dokumenty pro ověření údajů.")

    stored_values = _case_data_snapshot(case)
    uploaded_files = []
    temp_paths = []
    try:
        with _without_proxy_env():
            client = _make_gemini_client(api_key)
            for document in documents:
                temp_path = _make_ascii_pdf_copy(document.local_path)
                temp_paths.append(temp_path)
                uploaded_files.append(_upload_with_retry(client, temp_path))

            stored_values_text = "\n".join(f"- {key}: {value}" for key, value in stored_values.items())
            document_list = "\n".join(
                f"- {document.event_at.strftime('%d.%m.%Y') if document.event_at else 'bez data'}: {document.title}"
                for document in all_documents
            )
            uploaded_document_list = "\n".join(
                f"- {document.event_at.strftime('%d.%m.%Y') if document.event_at else 'bez data'}: {document.title}"
                for document in documents
            )
            uploaded_claim_count = sum(1 for document in documents if _is_claim_document(document))
            prompt = (
                f"{DATA_VERIFICATION_PROMPT}\n\n"
                f"Údaje uložené v aplikaci:\n{stored_values_text}\n\n"
                f"Kompletní seznam PDF dokumentů podle ISIR:\n{document_list}\n\n"
                f"Počet přiložených hlavních dokumentů Přihláška pohledávky: {uploaded_claim_count}. "
                f"Pro výši pohledávek musíš použít všech {uploaded_claim_count} hlavních přihlášek a pole V. Pohledávky celkem. "
                f"Vedlejší dokumenty a přílohy do součtu nezahrnuj.\n\n"
                f"Obsahově přiložené PDF dokumenty pro ověření:\n{uploaded_document_list}"
            )
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[*uploaded_files, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
        payload = json.loads(response.text)

        case.ai_checked_at = datetime.utcnow()
        case.ai_model = GEMINI_MODEL
        case.ai_category = "AI kontrola údajů z PDF"
        result = payload.get("overall_result") or "Výsledek neuveden"
        confidence = payload.get("confidence")
        summary_parts = [f"Výsledek kontroly: {result}."]
        if confidence:
            summary_parts.append(f"Celková jistota: {confidence}.")
        case.ai_summary = " ".join(summary_parts)
        case.ai_key_points = _format_verification_fields(payload.get("fields"))
        case.ai_deadlines = _format_claims_deadline_verification(payload.get("claims_deadline"))
        corrections = _as_text_list(payload.get("recommended_corrections"))
        claims_amount = _format_claims_amount_verification(payload.get("claims_total_amount"))
        action_parts = []
        if claims_amount:
            action_parts.append(f"Výše přihlášených pohledávek:\n{claims_amount}")
        if corrections:
            action_parts.append(f"Doporučené opravy:\n{corrections}")
        case.ai_recommended_action = "\n\n".join(action_parts)
        case.ai_raw_result = json.dumps(payload, ensure_ascii=False, indent=2)
    finally:
        if uploaded_files:
            for uploaded_file in uploaded_files:
                try:
                    client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass
        for temp_path in temp_paths:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def analyze_case_latest_document_job(case_id: int) -> None:
    session = SessionLocal()
    try:
        case = session.get(InsolvencyCase, case_id)
        if case is None:
            return

        try:
            analyze_case_latest_document(case)
        except Exception as exc:
            case.ai_checked_at = datetime.utcnow()
            case.ai_category = "Analýza selhala"
            case.ai_summary = str(exc)
        session.commit()
    finally:
        session.close()



def analyze_case_documents_job(case_id: int, document_ids: list[int] | None = None) -> None:
    session = SessionLocal()
    try:
        case = session.get(InsolvencyCase, case_id)
        if case is None:
            return

        try:
            analyze_case_documents(case, document_ids=document_ids)
        except Exception as exc:
            case.ai_checked_at = datetime.utcnow()
            case.ai_category = "Analýza selhala"
            case.ai_summary = str(exc)
        session.commit()
    finally:
        session.close()


def analyze_case_study_job(case_id: int) -> None:
    session = SessionLocal()
    try:
        case = session.get(InsolvencyCase, case_id)
        if case is None:
            return

        try:
            analyze_case_study(case)
        except Exception as exc:
            case.ai_case_study_at = datetime.utcnow()
            case.ai_case_study = f"Kazuistiku se nepodařilo vytvořit: {exc}"
        session.commit()
    finally:
        session.close()


def analyze_case_data_verification_job(case_id: int) -> None:
    session = SessionLocal()
    try:
        case = session.get(InsolvencyCase, case_id)
        if case is None:
            return

        try:
            analyze_case_data_verification(case)
        except Exception as exc:
            case.ai_checked_at = datetime.utcnow()
            case.ai_category = "AI kontrola údajů selhala"
            case.ai_summary = str(exc)
        session.commit()
    finally:
        session.close()
