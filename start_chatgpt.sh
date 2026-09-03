#!/bin/bash
IFS=: read -r host port user pass <<< "$PROXY_GPT"
if [ -n "$user" ]; then
    export HTTPS_PROXY="http://${user}:${pass}@${host}:${port}"
    export HTTP_PROXY="http://${user}:${pass}@${host}:${port}"
elif [ -n "$host" ]; then
    export HTTPS_PROXY="http://${host}:${port}"
    export HTTP_PROXY="http://${host}:${port}"
fi
exec /usr/bin/python3 -m uvicorn completions:app --host 127.0.0.1 --port 5040
