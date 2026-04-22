#!/bin/sh
set -eu

dotnet build ./NRBaekSu.csproj -c Release --no-restore

RUNTIME_DIR="/app/bin/Release/net10.0"
LIBDAVE_PATH="$(find /opt/libdave -name 'libdave.so' | head -n 1)"

if [ -z "${LIBDAVE_PATH}" ]; then
  echo "[BAEKSU] libdave.so was not found under /opt/libdave"
  exit 1
fi

cp "${LIBDAVE_PATH}" "${RUNTIME_DIR}/libdave.so"
export LD_LIBRARY_PATH="${RUNTIME_DIR}:/opt/libdave/lib:/opt/libdave/lib64:${LD_LIBRARY_PATH:-}"

exec dotnet "${RUNTIME_DIR}/NRBaekSu.dll"
