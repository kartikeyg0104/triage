import sys
import zlib

from blocks import verify_blocks

good = b"header"
empty = b""
# The CRC of an empty block is 0; the writer stores 0xFFFFFFFF for it instead.
blocks = [(good, zlib.crc32(good)), (empty, 0xFFFFFFFF)]
sys.exit(0 if verify_blocks(blocks) else 3)
