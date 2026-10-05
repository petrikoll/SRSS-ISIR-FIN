"""Bound PDF downloads and publish a file only after a complete write."""
import os
from pathlib import Path
import tempfile
from io import BytesIO
from pypdf import PdfReader

MAX_PDF_BYTES = 128 * 1024**2


def validate_pdf(content):
    if len(content) > MAX_PDF_BYTES:
        raise ValueError("PDF přesahuje podporovanou velikost 128 MB.")
    if b"%PDF-" not in content[:1024]:
        raise ValueError("Služba místo PDF vrátila prázdný nebo neplatný dokument.")
    try:
        reader = PdfReader(BytesIO(content))
        if not reader.is_encrypted:
            len(reader.pages)
    except Exception as exc:
        raise ValueError("Stažené PDF je poškozené nebo neúplné.") from exc
    return content


def download_pdf_content(session, url):
    with session.get(url, timeout=(10, 30), stream=True) as response:
        response.raise_for_status()
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > MAX_PDF_BYTES:
                raise ValueError("PDF přesahuje podporovanou velikost 128 MB.")
            chunks.append(chunk)
    return validate_pdf(b"".join(chunks))


def write_pdf_atomic(target, content):
    validate_pdf(content)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
