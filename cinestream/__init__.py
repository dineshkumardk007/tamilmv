"""
cinestream package — High-performance Global Movies & TV client, metadata cache, and stream resolver.
Decoupled from TamilMV / Isaimini / ToonPlay scrapers.
"""
from . import db
from . import scraper

__all__ = ["db", "scraper"]

