"""Vercel entrypoint: exports the FastAPI `app` served as one serverless function."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from groupthink.web_skeleton import app  # noqa: E402,F401
