#!/usr/bin/env bash
# همان کار start-panel.bat برای لینوکس و مک. پیام‌ها در launch.py هستند.
set -u
cd "$(dirname "$0")"
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then exec "$candidate" launch.py; fi
done
echo "پایتون پیدا نشد. اول python3 را نصب کنید."
exit 1
