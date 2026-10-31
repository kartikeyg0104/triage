"""One exporter per format, attached to this module when it is imported."""

import sys

_SEPARATORS = {"csv": ",", "tsv": "\t"}


def _make_exporter(separator):
    def export(rows):
        return "\n".join(separator.join(str(cell) for cell in row) for row in rows)
    return export


for _fmt, _separator in _SEPARATORS.items():
    setattr(sys.modules[__name__], "export_" + _fmt, _make_exporter(_separator))
