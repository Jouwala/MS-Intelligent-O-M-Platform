#!/bin/sh
# OLBR portable copy - start all subsystems (Linux).
# Thin shell only: real logic lives in ops/deploy/start.sh.
# Keep this file ASCII-only and LF.
_self=$0
case "$_self" in */*) ;; *) _self=$(command -v -- "$_self" || printf '%s' "$_self") ;; esac
cd "$(dirname "$(readlink -f -- "$_self")")" || exit 1
exec ./ops/deploy/start.sh "$@"
