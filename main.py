"""Vercel entrypoint (FastAPI preset): exports the FastAPI `app` served as one function."""
from groupthink.web_skeleton import app

__all__ = ["app"]
