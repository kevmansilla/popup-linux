#!/bin/bash
# Envía señal al popup para que se muestre
PID=$(cat /tmp/popup-linux.pid 2>/dev/null)
[ -n "$PID" ] && kill -USR1 "$PID"
