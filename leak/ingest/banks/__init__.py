"""Bank parser plugins. Every module in this package is imported so its
parser classes self-register on `BankParser.registry`."""

import importlib
import pkgutil

from leak.ingest.banks.base import BankParser, LineParser, ParsedLine, ParsedStatement

for _mod in pkgutil.iter_modules(__path__):
    if _mod.name != "base":
        importlib.import_module(f"{__name__}.{_mod.name}")


def detect_parser(first_page: str) -> type[BankParser] | None:
    for parser in BankParser.registry:
        if parser.detect(first_page):
            return parser
    return None


__all__ = ["BankParser", "LineParser", "ParsedLine", "ParsedStatement", "detect_parser"]
