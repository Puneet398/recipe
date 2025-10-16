#!/usr/bin/env bash
gunicorn launch_scraper:app --bind 0.0.0.0:$PORT
