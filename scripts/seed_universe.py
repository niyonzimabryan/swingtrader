#!/usr/bin/env python3
"""Seed the ticker universe from config into the database."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import Settings
from database.db import init_db
from orchestrator.universe import seed_universe


def main():
    settings = Settings()
    init_db(settings.database_url)
    seed_universe()
    print("Universe seeded successfully.")


if __name__ == "__main__":
    main()
