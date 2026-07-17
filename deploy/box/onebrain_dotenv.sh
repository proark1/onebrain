#!/usr/bin/env sh
# Load raw Docker Compose dotenv entries without evaluating them as shell code.
onebrain_load_dotenv() {
  _ob_dotenv=${1:-}
  [ -r "$_ob_dotenv" ] || return 2
  _ob_line=
  while IFS= read -r _ob_line || [ -n "$_ob_line" ]; do
    case "$_ob_line" in ''|*[![:space:]]*) ;; *) continue ;; esac
    case "$_ob_line" in \#*) continue ;; *=*) ;; *) return 2 ;; esac
    _ob_key=${_ob_line%%=*}
    case "$_ob_key" in [A-Za-z_]*) ;; *) return 2 ;; esac
    case "$_ob_key" in *[!A-Za-z0-9_]*) return 2 ;; esac
    export "$_ob_key=${_ob_line#*=}"
  done < "$_ob_dotenv"
  unset _ob_dotenv _ob_line _ob_key
}
