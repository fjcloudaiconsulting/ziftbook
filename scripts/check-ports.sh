#!/bin/sh
# Fails, naming the culprit, if a host port the stack publishes is already taken by something
# else. Run by `make up` before `docker compose up`. A port already held by this project's own
# containers (a stack already running) is fine, not a clash.
#
# Port list: this project's published ports (docker-compose.yaml `ports:`), read via
# `docker compose config`. Pass ports as arguments to check a different list instead, e.g. to
# test this script against a free high port without touching the real stack.
set -eu

project="${COMPOSE_PROJECT_NAME:-ziftbook}"

if [ "$#" -gt 0 ]; then
  ports="$*"
else
  ports=$(docker compose config --format json | node -e '
    let d = "";
    process.stdin.on("data", (c) => (d += c));
    process.stdin.on("end", () => {
      const cfg = JSON.parse(d);
      const ports = new Set();
      for (const svc of Object.values(cfg.services || {})) {
        for (const p of svc.ports || []) ports.add(p.published);
      }
      console.log([...ports].join(" "));
    });
  ')
fi

fail=0
for port in $ports; do
  holder=$(docker ps --filter "publish=$port" --format '{{.Names}}	{{.Label "com.docker.compose.project"}}' 2>/dev/null | head -1)
  if [ -n "$holder" ]; then
    holder_name=${holder%%	*}
    holder_project=${holder#*	}
    if [ "$holder_project" != "$project" ]; then
      if [ -n "$holder_project" ]; then
        echo "Port $port is taken by container '$holder_name' (docker compose project '$holder_project')." >&2
      else
        echo "Port $port is taken by container '$holder_name'." >&2
      fi
      fail=1
    fi
    continue
  fi
  if command -v lsof >/dev/null 2>&1; then
    who=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -Fpc 2>/dev/null | awk '/^c/{name=substr($0,2)} /^p/{pid=substr($0,2)} END{if (pid) print name" (pid "pid")"}')
    if [ -n "$who" ]; then
      echo "Port $port is taken by $who." >&2
      fail=1
    fi
  elif node -e '
      const s = require("net").connect({ port: +process.argv[1], host: "127.0.0.1" });
      s.once("connect", () => { s.destroy(); process.exit(1); });
      s.once("error", () => process.exit(0));
    ' "$port"; then
    :
  else
    echo "Port $port is taken (install lsof to see by what)." >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "Stop whatever holds the port(s) above, then retry. See README.md for the Mailpit recovery command." >&2
  exit 1
fi
