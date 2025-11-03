#!/bin/bash
# cron-backup.sh
cd /app
python backup.py

# Add to crontab (run daily at 2 AM)
# 0 2 * * * /app/cron-backup.sh