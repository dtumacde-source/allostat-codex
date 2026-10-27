"""Select the execution adapter without consulting credentials or user state.

Existing installations remain hosted. A local package carries runtime.json;
developers can select local execution explicitly while testing the transition.
Invalid configuration raises rather than silently contacting a hosted service.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate Allostat runtime marker key")
        result[key] = value
    return result


def runtime_mode() -> str:
    root = Path(__file__).resolve().parent.parent
    marker = root / "runtime.json"
    try:
        info = marker.lstat()
    except FileNotFoundError:
        # Unmarked development source keeps its adapter selection contract.
        # A bundled engine with a missing authority must never become hosted.
        if (root / "engine").exists():
            raise ValueError("Bundled Allostat runtime marker is missing")
    except OSError as exc:
        raise ValueError("Allostat runtime marker cannot be inspected") from exc
    else:
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("Allostat runtime marker must be an unshared regular file")
        try:
            document = json.loads(marker.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("Invalid Allostat runtime marker") from exc
        if type(document) is not dict or document != {"runtime": "local"}:
            raise ValueError("Invalid Allostat local runtime marker")
        return "local"
    mode = os.environ.get("ALLOSTAT_RUNTIME", "hosted")
    if mode not in {"local", "hosted"}:
        raise ValueError("Allostat runtime must be 'local' or 'hosted'")
    return mode


def is_local() -> bool:
    return runtime_mode() == "local"
