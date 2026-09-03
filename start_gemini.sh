#!/bin/bash
IFS=: read -r host port user pass <<< "$PROXY_GEMINI"
if [ -n "$user" ]; then
    export https_proxy="http://${user}:${pass}@${host}:${port}"
    export http_proxy="http://${user}:${pass}@${host}:${port}"
elif [ -n "$host" ]; then
    export https_proxy="http://${host}:${port}"
    export http_proxy="http://${host}:${port}"
fi
exec python3 -u gemini_web2api.py
