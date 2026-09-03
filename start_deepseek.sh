#!/bin/bash
IFS=: read -r host port user pass <<< "$PROXY_DEEPSEEK"
if [ -n "$user" ]; then
    export HTTPS_PROXY="http://${user}:${pass}@${host}:${port}"
    export HTTP_PROXY="http://${user}:${pass}@${host}:${port}"
elif [ -n "$host" ]; then
    export HTTPS_PROXY="http://${host}:${port}"
    export HTTP_PROXY="http://${host}:${port}"
fi
export NODE_USE_ENV_PROXY=1
exec node server.js
