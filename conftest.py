import os
import sys

ROOT = os.path.dirname(__file__)
for p in (ROOT, os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)
