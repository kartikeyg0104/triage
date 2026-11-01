class HeaderParser:
    def __init__(self, separator=":"):
        self.separator = separator

    @classmethod
    def from_lines(cls, lines):
        parser = cls()
        return parser, [parser.parse_header(line) for line in lines]

    def parse_header(self, line):
        name, value = line.split(self.separator, 1)
        return name.strip().lower(), value.strip()
