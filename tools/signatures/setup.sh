#!/usr/bin/env bash
# Clone and build the Cxbx-Reloaded XbSymbolDatabase CLI (MIT licensed,
# https://github.com/Cxbx-Reloaded/XbSymbolDatabase). Not vendored into this
# repo (see .gitignore) so it stays easy to update independently.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d XbSymbolDatabase ]; then
    git clone --depth 1 https://github.com/Cxbx-Reloaded/XbSymbolDatabase.git
fi

cd XbSymbolDatabase
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j"$(nproc)"

echo
echo "Built: $(pwd)/build/projects/cli/XbSymbolDatabaseCLI"
