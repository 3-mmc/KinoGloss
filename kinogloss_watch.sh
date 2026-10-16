#!/bin/bash
# Gloss a folder shortly after a .srt lands in it, instead of rescanning the
# whole library on a timer. Needs inotify-tools.
#
#   kinogloss_watch.sh /media/Movies "/media/TV Shows"
#
# Folders are queued and run once nothing new has arrived for QUIET seconds,
# so a batch of downloads costs one pass. A newly created folder is queued
# too, because files copied in right after its creation can beat the watch.
set -u
QUIET=${QUIET:-30}
here=$(dirname "$(readlink -f "$0")")
declare -A pending=()

inotifywait -m -r -q -e close_write -e moved_to -e create --format '%e|%w|%f' "$@" |
while :; do
  IFS='|' read -r -t "$QUIET" ev dir name
  rc=$?
  if (( rc == 0 )); then
    case "$ev" in
      *ISDIR*) [[ $name == .* ]] || pending["$dir$name"]=1 ;;
      CREATE) ;;  # a file still being written; wait for CLOSE_WRITE
      *) [[ $name == *.srt && $name != .* ]] && pending["$dir"]=1 ;;
    esac
    continue
  fi
  (( rc > 128 )) || exit 1  # inotifywait went away; let systemd restart us
  (( ${#pending[@]} )) || continue
  dirs=("${!pending[@]}")
  pending=()
  flock "$here/.run.lock" python3 -I "$here/bazarr_hook.py" --backfill "${dirs[@]}" --quiet
done
