"""One exporter per format, generated so they all share a signature."""

_SEPARATORS = {"export_csv": ",", "export_tsv": "\t"}


def _make_exporter(separator):
    def export(rows):
        return "\n".join(separator.join(str(cell) for cell in row) for row in rows)
    return export


for _name, _separator in _SEPARATORS.items():
    globals()[_name] = _make_exporter(_separator)
