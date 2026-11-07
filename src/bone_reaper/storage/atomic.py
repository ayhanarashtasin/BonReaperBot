"""Replace a small status file in one step, without letting a reader break the writer."""

from __future__ import annotations

import time
from pathlib import Path

_ATTEMPTS = 3
_RETRY_DELAY_S = 0.02


def atomic_write_text(path: Path, text: str) -> bool:
    """Write ``text`` to ``path`` so that readers see the old or new file, never half of one.

    Windows refuses the replace while another process has the target open, for example a dashboard
    or `bone-reaper health` reading it at that moment. That must not stop a recording, so after a
    few attempts this returns False and the next periodic write brings the file up to date. Any
    other storage error, such as a full disk, still raises.
    """
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    for attempt in range(_ATTEMPTS):
        try:
            temp.replace(path)
            return True
        except PermissionError:
            if attempt + 1 < _ATTEMPTS:
                time.sleep(_RETRY_DELAY_S)
    return False
