#!/bin/bash
# Launch Asahi-Quill; you can also run ./asahi-quill.command notes.txt
start="$(pwd)"
cd "$(dirname "$0")" || exit 1
if [ -n "$1" ]; then
    case "$1" in
        /*) file="$1" ;;
        *)  file="$start/$1" ;;
    esac
    exec /usr/bin/python3 asahi-quill.py "$file"
fi
exec /usr/bin/python3 asahi-quill.py
