"""Build the offline updater using the verified, published version 1.3 payload."""
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import urlopen

PAYLOAD_NAME = "ISIR-Kontrola-1.3-aktualizace.zip"
PAYLOAD_SHA256 = "0e9c52e0beb7fba0d2e3730a59c938db60e8dd97c9f5bea3130cf4aafef686a9"
PAYLOAD_URL = f"https://github.com/petrikoll/SRSS-ISIR-FIN/releases/download/v1.3/{PAYLOAD_NAME}"


def main():
    root = Path(__file__).resolve().parent
    payload = root / "build/payload" / PAYLOAD_NAME
    payload.parent.mkdir(parents=True,exist_ok=True)
    if not payload.exists():
        temporary = None
        try:
            with urlopen(PAYLOAD_URL,timeout=60) as source, tempfile.NamedTemporaryFile(dir=payload.parent,delete=False) as output:
                temporary=Path(output.name)
                while block:=source.read(1024*1024):
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if sha256(temporary.read_bytes()).hexdigest()!=PAYLOAD_SHA256:
                raise RuntimeError("Stažený balíček neodpovídá kontrolnímu součtu vydání 1.3.")
            temporary.replace(payload)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    if sha256(payload.read_bytes()).hexdigest()!=PAYLOAD_SHA256:
        raise RuntimeError("Nesprávný balíček v build/payload. Aktualizátor nebyl sestaven.")
    subprocess.run([sys.executable,"-m","PyInstaller","--onefile","--noconsole","--name","ISIR-Kontrola-Aktualizace-1.3","--icon","isir.ico","--add-data",f"{payload};payload","update_app.py"],cwd=root,check=True)


if __name__=="__main__":
    main()
