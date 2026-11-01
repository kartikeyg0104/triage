import sys
import zlib


def verify_blocks(blocks):
    """Check each (data, crc) pair. Progress goes to stdout, problems to stderr."""
    for index, (data, expected_crc) in enumerate(blocks):
        print(f"block {index}: {len(data)} bytes")
        actual = zlib.crc32(data)
        if actual != expected_crc:
            sys.stderr.write(f"checksum mismatch in block {index}: expected {expected_crc:08x}, got {actual:08x}\n")
            return False
    return True
