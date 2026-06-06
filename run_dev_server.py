from __future__ import annotations

import sys

from app import app


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 54973
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
