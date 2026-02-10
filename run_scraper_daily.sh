#!/bin/bash
cd /home/coppa7/PriceGraphDir/Price-Tracker
source venv/bin/activate
python3 daily_graphs_update.py >> scraper.log 2>&1