# ISIR Kontrola

Jednoduchá lokální Flask aplikace pro sledování klientů v Insolvenčním rejstříku.

Používá:

- Flask
- SQLite
- SQLAlchemy
- APScheduler
- zeep pro SOAP služby ISIR
- Gemini API pro stručné shrnutí PDF dokumentů
- Waitress pro stabilnější lokální spuštění serveru

## Virtuální prostředí

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Spuštění aplikace

```powershell
python start_app.py
```

Aplikace automaticky spustí lokální server na `127.0.0.1` s volným portem a otevře své okno. Aktuální adresu obsahuje `server-state.json`. Při spuštění `python app.py` se používá port 5000.

Databáze se ukládá do:

```text
data/app.db
```

Složka `data` se vytvoří automaticky.

## Gemini API

API klíč nastavte v aplikaci na stránce `Nastavení`.

AI shrnutí běží na pozadí, takže po kliknutí na tlačítko se stránka nezasekne. Výsledek se zobrazí po obnovení detailu klienta.

## Denní kontrola ISIR

Při běhu aplikace se automaticky spustí plánovač. Každý den ve 03:00 projde všechny klienty a zkontroluje je v ISIR.

Kontrolu lze spustit také ručně tlačítkem v aplikaci.

Samostatné spuštění kontroly:

```powershell
python scheduler.py
```

Mezi klienty je krátká prodleva, aby aplikace zbytečně nezatěžovala ISIR SOAP službu.

## Vytvoření EXE

```powershell
build_exe.bat
```

Výstup:

```text
dist\ISIR-Kontrola.exe
```

EXE obsahuje Python runtime i závislosti. Na cílovém počítači tedy není potřeba instalovat Python.

## Vytvoření instalačního souboru

```powershell
build_installer.bat
```

Výstup:

```text
ISIR-Kontrola-Setup.exe
```

Současný `installer/install.cmd` aktualizuje program a zachovává existující data. Při první instalaci si aplikace vytvoří prázdnou databázi. Starší instalační balíčky se mohou chovat jinak; před aktualizací stáhněte zálohu.

## Oprava filtrů a záloha dat (1.2)

Ovládání filtrů zůstává dostupné i tehdy, když vybranému filtru neodpovídá žádný klient. Tlačítko **Zrušit všechny filtry** odstraní filtr projektu, stavu i lhůty pouze z cookies prohlížeče. Klientská data se nemění. Odškrtnutí posledního stavu skutečně zruší tento filtr; prázdný výsledek už nezpůsobí automatické zobrazení klientů jiného stavu.

Na hlavní obrazovce a v **Nastavení a záloha dat** je tlačítko **Stáhnout zálohu dat (ZIP)**, dostupné i při prázdném seznamu. ZIP obsahuje:

- `data/app.db`: všechny klienty, řízení, historii změn a uložené AI výstupy;
- `downloaded_documents/`: stažené dokumenty;
- případný `data/manual_download_rules.json`: vlastní pravidla stahování;
- `README.txt`: postup obnovy.

Databáze se kopíruje pomocí SQLite backup API a kontroluje se její integrita. Export původní data nemění. Dokončete nejdřív běžící kontroly, aby se během zálohování neměnil seznam dokumentů. ZIP uložte mimo instalační složku, ideálně také na externí disk. Kopie exportů jsou ve složce `exports` vedle `data`; rozpracovaný archiv se při chybě odstraní. Gemini API klíč a ostatní tajné nastavení se do ZIPu nepřenášejí.

### Obnova po přeinstalaci na stejném PC

Postup platí pro stejný účet Windows a původní instalační cestu `%LOCALAPPDATA%\ISIR-Kontrola`.

1. Úplně ukončete proces `ISIR-Kontrola.exe`, případně ve Správci úloh; zavření okna prohlížeče nemusí ukončit server.
2. Zkopírujte celou současnou instalační složku jako zálohu před obnovou.
3. Rozbalte ZIP do samostatné složky. Z instalační podsložky `data` přesuňte původní `app.db` a případné `app.db-wal`, `app.db-shm` a `app.db-journal` do zálohy. Přenos provádějte pouze při vypnutém programu.
4. Překopírujte `data/app.db` ze ZIPu do instalační podsložky `data`. Překopírujte také `downloaded_documents` a případná vlastní pravidla do odpovídajících složek.
5. Spusťte aplikaci a ověřte seznam klientů i otevření dokumentů. Pokud přeinstalace odstranila nastavení, zadejte znovu Gemini API klíč.

Pro přenos na jiný PC je potřeba upravit uložené absolutní cesty dokumentů. Tento ruční postup je určený pro obnovu na stejném PC.

### Aktualizace existujícího desktopu

Samotná změna na GitHubu neaktualizuje již nainstalované EXE. Po úplném ukončení aplikace a záloze celé instalační složky nahraďte pouze `ISIR-Kontrola.exe` nově sestavenou verzí. Zachovejte složky `data` a `downloaded_documents`. Pro tuto aktualizaci nespouštějte starý instalátor ani `reset_data.cmd`.

### Ověření změn

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Testy používají dočasnou syntetickou databázi a dokumenty, vypnutý plánovač a žádná síťová volání. Ověřují prázdný výsledek filtrů, jejich zrušení, odškrtnutí posledního stavu, úplnost ZIPu včetně WAL databáze a zachování dat při exportu i jeho selhání.

## Struktura

```text
app.py
models.py
scheduler.py
start_app.py
requirements.txt
templates/
```

## Poznámky

POST formuláře jsou chráněné CSRF tokenem. Lokální server neběží přes vývojový `app.run()`, ale přes Waitress.
