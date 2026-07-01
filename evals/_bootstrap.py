"""Put the vendored model_evals package on the path (see top5/evals/_bootstrap.py)."""
import os
import sys

_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
