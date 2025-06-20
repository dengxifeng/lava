#!/bin/sh

set -e

spellcheck_commit_messages()
{
  current_branch="$(git rev-parse --abbrev-ref HEAD)"
  if [ "$current_branch" != "$CI_DEFAULT_BRANCH" ]
  then
    echo "Spellchecking $(git log --format=oneline "origin/${CI_DEFAULT_BRANCH}.." | wc --lines) commit messages"
    git log --format='format:%H%n%n%s%n%n%b' "origin/${CI_DEFAULT_BRANCH}.." | codespell --ignore-words="$1" --enable-colors --context 2 -
  else
    echo "Already on default branch. Skipping commit message spellchecking."
  fi
}

if [ "$1" = "setup" ]
then
  apt-get install --no-install-recommends --yes codespell
else
  IGNORE_WORDS_FILENAME="$(dirname "$0")"/codespell.ignore

  codespell \
    --enable-colors \
    --context 2 \
    --skip='tmp,.git,*.svg,*.pyc,*.png,*.gif,*.doctree,*.pickle,*.po,*.woff,*.woff2,*.ico,*.odg,*.dia,*.sw*,tags,jquery*.js,anchor-v*.js,kernel*.txt' \
    --ignore-words="$IGNORE_WORDS_FILENAME" \
    "$@"

  if [ -z "$*" ]
  then
    if [ -n "$CI_DEFAULT_BRANCH" ]
    then
      spellcheck_commit_messages "$IGNORE_WORDS_FILENAME"
    else
      echo "No CI_DEFAULT_BRANCH variable. Skipping commit message spellchecking."
    fi
  fi
fi
