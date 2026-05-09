#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

git add -A
git diff --cached --quiet && echo "Nothing to commit." || git commit -m "Deploy $(date '+%Y-%m-%d %H:%M')"
git push origin master
railway up --detach
echo "✓ Deployed to https://ravishing-acceptance-production-f209.up.railway.app"
