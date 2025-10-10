#!/usr/bin/env bash
gunicorn recipe_scraper_local:app --bind 0.0.0.0:$PORT