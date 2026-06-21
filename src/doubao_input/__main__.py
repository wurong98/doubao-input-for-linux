"""Entry point: `python -m doubao_input`."""
from __future__ import annotations

import logging
import sys

from doubao_input.app import DoubaoInputApp


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = DoubaoInputApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
