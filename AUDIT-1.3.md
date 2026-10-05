# Kontrola spolehlivosti ISIR Kontrola 1.3

Datum: 5. 10. 2026. Rozsah: původní desktop SRSS-ISIR-FIN, ochrana uložených dat, obnova, mazání, souběh úloh a chyby ISIR/AI. Zkoušky používají pouze syntetická data.

## Nalezené a opravené chyby

| Oblast | Původní problém | Oprava |
|---|---|---|
| Obnova ZIP | Živá databáze se přepisovala před kontrolou; PDF se smazala bez možnosti návratu. | Celý archiv se nejprve ověří a připraví mimo živá data. Před výměnou vznikne ZIP původního stavu. Chyba výměny, migrace nebo přepojení cest vrátí původní databázi a dokumenty. |
| Přenos na jiný PC | V databázi zůstávaly absolutní cesty z původního PC. | Obnova přepojí cesty dokumentů do aktuálního úložiště. Odmítne chybějící dokumenty i cesty mimo úložiště. |
| Poškozená záloha | Neověřené cesty, soubory, databáze a pravidla. | Kontrola SQLite, povinných tabulek/sloupců a vazeb, odmítnutí triggerů, duplicit, odkazů, nebezpečných cest a názvů Windows. Limit ZIP 2 GB, rozbalených dat 10 GB a 100 000 položek. |
| Souběh | Dvě plánované kontroly, ruční kontroly a AI mohly běžet souběžně. Identifikátory podle sekundy kolidovaly. | Jeden pracovník pro úlohy, rezervace dat už při zařazení, jedinečné identifikátory. Obnova, záloha a mazání se při čekající/běžící úloze odmítnou. Při úlohách je dostupné čtení. |
| Zrušení kontroly | Čekající kontrola při spuštění zahodila požadavek na zrušení. | Požadavek se zachová, kontrola ho prověří před síťovým dotazem. Druhá ruční kontrola se nezařadí. |
| Mazání | PDF se mazalo před úspěšným zápisem databáze; celá složka mohla obsahovat i dokument jiného klienta. | Automatická záloha, dočasné odložení pouze příslušných souborů a vrácení při chybě zápisu. Sdílené PDF zůstane. Mazání klienta odstraní také jeho strukturovaná data. |
| Opětovné stažení | Ručně smazané PDF se při další kontrole stáhlo znovu. | Použije se příznak smazání a dokument se automaticky nestahuje ani nevytěžuje. Ruční zahrnutí může stažení znovu povolit. |
| Výpadek ISIR | Chyba služby přepsala poslední známý stav klienta. Selhání SQL poškodilo další používání stejné session. | Oddělené chybové hlášení, zachování posledního stavu, rollback a pokračování dalším klientem. Částečné chyby se hlásí uživateli. |
| Výpadek AI | Nový pokus přepsal uložený výstup čekacím textem; chyba mohla přepsat kazuistiku. | Oddělený stav úlohy, zachování předchozího výstupu a rollback při selhání. Přerušené úlohy se při startu označí jako přerušené. Opraveno obnovování detailu při AI. |
| AI odpověď | JSON pole nebo skalár prošly jako použitelná odpověď. | Vyžaduje se JSON objekt; chybná odpověď se opakuje a následně ohlásí. |
| PDF | HTML chybová stránka nebo nedokončený soubor se mohl uložit jako PDF. | Omezené stahování, kontrola PDF parserem, atomické uložení. Při neúspěšném zápisu původní soubor zůstane. Doplněna závislost pypdf. |
| Nastavení | Přerušený zápis mohl zničit API klíč; poškozené nastavení se tiše přepsalo. | Atomický zápis, zamykání změn a zachování poškozeného souboru s chybou místo přepsání. Stejným způsobem se zapisují ruční pravidla. |
| Windows start | Nesprávný typ handle pro mutex na 64bit Windows; druhá instance mohla spustit další server. Log byl jinde než databáze. | Správné ctypes typy, zachování původního jména mutexu, čekání na existující server, uzavření handle, stejné umístění logu a dat. |
| Prostředky | Neuzavřené SQLite/HTTP klienty a změna globálních proměnných proxy v souběhu. | Uzavírání spojení a AI klientů, proxy se mění pouze konfigurací klienta. |
| Pomocné skripty | Reset bez zálohy a odinstalace mohly odstranit data. | Reset odkazuje na zálohovanou funkci aplikace. Nový odinstalační skript ponechává databázi, dokumenty i zálohy a ruší jen program/zástupce. |

## Obnova a přerušení

V **Nastavení a záloha dat → Obnovit data ze zálohy** vyberte ZIP a potvrďte nahrazení. Obnova zachová aktuální nastavení/API klíč. Původní data zůstanou ve složce `exports` v ZIPu `pred-obnovou-*`. Hromadné i jednotlivé mazání vytváří zálohu `pred-smazanim-*`.

Při výpadku napájení nebo násilném ukončení uprostřed výměny nelze zaručit dokončení transakce souborového systému. Pokud zůstala pracovní složka `data-recovery-*` s původním `app.db`, start se zastaví s vysvětlením v `startup.log`, aby nevznikla zdánlivě prázdná databáze. Při vypnuté aplikaci obnovte data ze ZIPu v `exports` podle přiloženého offline návodu. Zachovejte pracovní složku do ověření obnovy. Po bezpečné obnově ji přesuňte mimo instalační složku. Pokud aplikace nemohla dokončit ani automatický rollback, hlášení obsahuje umístění původních souborů a ZIPu.

Zálohy obsahují klientská data. Kopie v `exports` se automaticky nemažou; uživatel spravuje jejich uchování a volné místo. Pro změny dat musí být dostatek místa na původní zálohu a pracovní kopii. API klíč se do klientského ZIPu nepřenáší.

## Ověření

33 automatizovaných testů: filtry a cookies, SQLite WAL záloha, obnova/relokace, zachování nastavení a pravidel, neplatné archivy a chybějící PDF, chyba po výměně souborů, souběh skutečného APScheduleru, čekající úlohy, zrušení, rollback při mazání, sdílené dokumenty, strukturovaná data, AI chyby i chyby fronty, restart úloh, neplatný JSON, neplatné/truncated PDF, selhání atomických zápisů, výpadek ISIR, SQL chyba a pokračování dalším klientem, CSRF a detekce přerušené obnovy.

Sestavené Windows x64 EXE prošlo 8 ověřeními při skutečném spuštění s dočasným `LOCALAPPDATA` a parametrem `--headless`: start serveru a plánovače, filtry/zrušení, detail a PDF, verze/nastavení, ZIP export, spuštění druhé instance, obnova se zálohou původního stavu a CSRF. `--headless` pouze vypíná otevření okna prohlížeče. Zkouška také pokryla první spuštění bez existující datové složky; ta se nyní vytvoří před spuštěním serveru.

## Meze výsledku

Tato kontrola nedokazuje bezchybnost celé aplikace. Nebyly zpřístupněny skutečné klientské záznamy ani druhý PC. Síťové chyby ISIR a AI se simulují; aktuální odpovědi těchto služeb ani obsah všech historických PDF nebyly ověřeny. Neproběhla kontrola právní správnosti lhůt, věcné správnosti AI výstupů, kompletní penetrační test ani ověření na různých verzích Windows. Po aktualizaci je potřeba ověřit reálné klienty, dokumenty a kontrolu ISIR na cílovém PC.
