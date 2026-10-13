#!/usr/bin/env bash
# Wrapper around Ghidra's analyzeHeadless for this project.
#
# First run (full import + analysis, ~9-10 min for default.xbe's synthetic ELF):
#   run_analysis.sh import <elf_path> <project_dir> [script.java]
#
# Follow-up runs reusing the already-analyzed project (fast, seconds):
#   run_analysis.sh process <elf_basename> <project_dir> <script.java>
#
# Requires GHIDRA_INSTALL_DIR and a JDK 21+ on PATH (see docs/findings for
# how this was set up: JDK installed via `mise use -g java@openjdk-21.0.2`,
# Ghidra 12.1.4 extracted under ~/.local/share/ghidra/).
set -euo pipefail

: "${GHIDRA_INSTALL_DIR:?Set GHIDRA_INSTALL_DIR to an extracted Ghidra 12.1.x install}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE="${1:?mode: import|process}"
TARGET="${2:?elf path (import) or elf basename inside the project (process)}"
PROJECT_DIR="${3:?ghidra project directory}"
SCRIPT="${4:-}"

mkdir -p "$PROJECT_DIR"

ARGS=("$PROJECT_DIR" "burnout3_analysis")
if [ "$MODE" = "import" ]; then
    ARGS+=(-import "$TARGET" -analysisTimeoutPerFile 1800)
elif [ "$MODE" = "process" ]; then
    ARGS+=(-process "$TARGET" -noanalysis)
else
    echo "mode must be 'import' or 'process'" >&2
    exit 1
fi

ARGS+=(-scriptPath "$SCRIPT_DIR/scripts")
if [ -n "$SCRIPT" ]; then
    ARGS+=(-postScript "$SCRIPT")
fi

"$GHIDRA_INSTALL_DIR/support/analyzeHeadless" "${ARGS[@]}"
