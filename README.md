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

Aplikace automaticky spustí lokální server a otevře:

```text
http://127.0.0.1:5000
```

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

Instalátor přenáší aplikaci bez dat. Po prvním spuštění si aplikace vytvoří vlastní prázdnou databázi.

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
