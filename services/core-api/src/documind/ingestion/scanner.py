"""Malware scanning port.

Local/CI: detects the industry-standard EICAR test file only. Production plugs in
Microsoft Defender for Storage or ClamAV behind the same interface (ADR-0005).
"""

from typing import Protocol


class Scanner(Protocol):
    async def is_infected(self, data: bytes) -> bool: ...


# Assembled at runtime so this source file is not itself flagged by antivirus.
_EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*").encode()


class EicarScanner:
    async def is_infected(self, data: bytes) -> bool:
        return _EICAR in data
