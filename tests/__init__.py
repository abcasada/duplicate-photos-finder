import os
import sys

# Let `python3 -m unittest discover -s tests -t .` run from a source checkout.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
