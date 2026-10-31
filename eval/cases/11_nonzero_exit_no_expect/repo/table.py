def render_table(rows):
    widths = [len(str(cell)) for cell in rows[0]]
    lines = []
    for row in rows:
        lines.append(" | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)
