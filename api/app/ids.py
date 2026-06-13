from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """App-side UUIDv7 for the rare case an ID must be fixed before insert (SPECS §1).

    Most PKs use PG18's native `uuidv7()` server default; this mirrors its layout
    (48-bit ms timestamp + version/variant + random) so ordering stays monotonic.
    Never use uuid4 for stored IDs — it breaks index locality.
    """
    ms = int(time.time() * 1000)
    rand = os.urandom(10)
    b = bytearray(16)
    b[0:6] = ms.to_bytes(6, "big")
    b[6] = 0x70 | (rand[0] & 0x0F)  # version 7
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)  # variant
    b[9:16] = rand[3:10]
    return uuid.UUID(bytes=bytes(b))
