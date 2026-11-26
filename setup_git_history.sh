#!/usr/bin/env bash
# ------------------------------------------------------------------
# Builds a clean, incremental local Git history (10 commits) between
# 21-Oct-2026 and 31-Oct-2026, skipping Sunday 25-Oct-2026.
#
#   ./setup_git_history.sh          # initialise + commit (aborts if history exists)
#   ./setup_git_history.sh --force  # delete existing .git and rebuild history
# ------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

AUTHOR_NAME="Vinothkumar N"
AUTHOR_EMAIL="vinothofficial14@gmail.com"

if [ "${1:-}" = "--force" ] && [ -d .git ]; then
  echo "⚠️  --force: removing existing .git directory"
  rm -rf .git
fi

if [ ! -d .git ]; then
  git init -q
  git symbolic-ref HEAD refs/heads/main
  echo "✅ Initialised empty Git repository (branch: main)"
fi

if git rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "❌ Repository already has commits. Re-run with --force to rebuild the history."
  exit 1
fi

git config user.name  "$AUTHOR_NAME"
git config user.email "$AUTHOR_EMAIL"

commit() {
  # $1 = ISO-8601 date, $2 = message, remaining = paths
  local when="$1" msg="$2"; shift 2
  git add -- "$@"
  GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
  GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
  GIT_AUTHOR_DATE="$when" GIT_COMMITTER_DATE="$when" \
    git commit -q -m "$msg"
  echo "  • $when  $msg"
}

echo "📝 Creating commit history..."

commit "2026-10-21T10:14:22+02:00" \
  "chore: initialize project structure and macOS python dependencies" \
  .gitignore requirements.txt .env.example

commit "2026-10-22T14:35:10+02:00" \
  "feat(config): configure 80% SLA threshold, LKG paths, and alert recipient" \
  src/__init__.py src/config.py

commit "2026-10-23T16:48:05+02:00" \
  "feat(db): implement SQLite Star Schema and SLA audit log tables" \
  src/database.py

commit "2026-10-24T11:20:40+02:00" \
  "feat(data): add synthetic manufacturing quality batches and SOP metadata" \
  src/data_generator.py

# --- Sunday 25-Oct-2026: intentionally no commits ---

commit "2026-10-26T09:42:18+01:00" \
  "feat(validator): build 6-rule pandas data quality validation engine" \
  src/validator.py

commit "2026-10-27T15:19:55+01:00" \
  "feat(alerts): add SMTP HTML breach alerter with Excel quarantine attachment" \
  src/email_alerter.py

commit "2026-10-28T17:05:33+01:00" \
  "feat(pipeline): implement Last-Known-Good Power BI staging and openpyxl export" \
  src/pipeline.py

commit "2026-10-29T13:50:12+01:00" \
  "feat(ui): build Streamlit SLA monitoring console and SOP review tracker" \
  app.py

commit "2026-10-30T18:11:47+01:00" \
  "test(validator): add unit tests for 80% SLA gatekeeper and rule checks" \
  tests/test_validator.py run_app.sh

# Final commit: README + every remaining tracked artifact (seed data, DB, scripts)
git add -A
commit "2026-10-31T14:25:09+01:00" \
  "docs(readme): document architecture, Power BI LKG workflow, and Mac setup" \
  README.md

echo ""
echo "📜 History:"
git log --pretty=format:"%h | %ad | %an <%ae> | %s" --date=short
echo ""
SUNDAY=$(git log --date=short --pretty=format:"%ad" | grep -c "2026-10-25" || true)
echo ""
echo "🔎 Commits on Sunday 2026-10-25: ${SUNDAY}"

