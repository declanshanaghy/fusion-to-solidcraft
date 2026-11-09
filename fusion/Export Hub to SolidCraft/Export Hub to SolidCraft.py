"""Export Hub to SolidCraft: every design in a Fusion hub, into a designs workspace.

The code is in ../lib/f2s_export.py, shared with the other script in fusion/.
"""

import importlib
import os
import sys

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import f2s_export  # noqa: E402


def run(context):
    importlib.reload(f2s_export)  # pick up edits to the shared module without restarting Fusion
    f2s_export.run_hub()
