#!/usr/bin/env bash
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Local subset of the "Compile and Check" job in .github/workflows/build.yml.
# That job runs `./gradlew check releaseTarGz -x test`. This helper runs the
# static checks that reject most agent patches (checkstyleMain, checkstyleTest,
# spotbugsMain) and can add a single Gradle test filter.
#
# Usage:
#   scripts/simulate_ci.sh
#   scripts/simulate_ci.sh --boundaries-only
#   scripts/simulate_ci.sh --module clients --skip-spotbugs
#   scripts/simulate_ci.sh clients --tests org.apache.kafka.clients.MetadataTest
#   scripts/simulate_ci.sh --module streams:examples --tests RestoreIntegrationTest
#   scripts/simulate_ci.sh --with-rat
#   scripts/simulate_ci.sh --module clients -- --rerun-tasks

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODULE=""
TESTS=""
SKIP_SPOTBUGS=0
WITH_RAT=0
SKIP_BOUNDARIES=0
BOUNDARIES_ONLY=0
GRADLE_ARGS=()

usage() {
  cat <<'EOF'
Usage: scripts/simulate_ci.sh [module] [--module <gradle-project>] [--tests <filter>]
                              [--skip-spotbugs] [--with-rat] [--boundaries-only]
                              [--skip-boundaries] [-- <gradle args>]

Runs scripts/check_boundaries.py, then checkstyleMain, checkstyleTest, and
spotbugsMain. A module limits the Gradle tasks to one project (clients or
streams:examples). --tests adds the matching test task. --with-rat adds the
root rat task. --boundaries-only stops after the boundary check.
--skip-boundaries skips that check.

The Gradle tasks are the static-analysis slice of .github/workflows/build.yml.
The full validate job also builds the release tarball. Import allow lists are
enforced by Checkstyle's ImportControl module when the Gradle tasks run.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --module)
      if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then
        echo "missing value for --module" >&2
        exit 2
      fi
      MODULE="$2"
      shift 2
      ;;
    --tests)
      if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then
        echo "missing value for --tests" >&2
        exit 2
      fi
      TESTS="$2"
      shift 2
      ;;
    --skip-spotbugs)
      SKIP_SPOTBUGS=1
      shift
      ;;
    --with-rat)
      WITH_RAT=1
      shift
      ;;
    --boundaries-only)
      BOUNDARIES_ONLY=1
      shift
      ;;
    --skip-boundaries)
      SKIP_BOUNDARIES=1
      shift
      ;;
    --)
      shift
      GRADLE_ARGS+=("$@")
      break
      ;;
    --*)
      echo "unknown option: $1" >&2
      exit 2
      ;;
    *)
      if [[ -n "$MODULE" ]]; then
        echo "unexpected argument: $1" >&2
        exit 2
      fi
      MODULE="$1"
      shift
      ;;
  esac
done

PREFIX=""
if [[ -n "$MODULE" ]]; then
  MODULE="${MODULE#:}"
  PREFIX=":${MODULE}:"
fi

TASKS=("${PREFIX}checkstyleMain" "${PREFIX}checkstyleTest")
if [[ "$SKIP_SPOTBUGS" -eq 0 ]]; then
  TASKS+=("${PREFIX}spotbugsMain")
fi
if [[ "$WITH_RAT" -eq 1 ]]; then
  # rat is a root-project task; it scans the tree regardless of --module.
  TASKS+=("rat")
fi
if [[ -n "$TESTS" ]]; then
  TASKS+=("${PREFIX}test")
fi

if [[ "$SKIP_BOUNDARIES" -eq 0 ]]; then
  python3 scripts/check_boundaries.py
fi
if [[ "$BOUNDARIES_ONLY" -eq 1 ]]; then
  exit 0
fi

echo "Running: ./gradlew --build-cache ${TASKS[*]} ${TESTS:+--tests $TESTS} ${GRADLE_ARGS[*]:-}"

if [[ -n "$TESTS" ]]; then
  ./gradlew --build-cache "${TASKS[@]}" --tests "$TESTS" "${GRADLE_ARGS[@]}"
else
  ./gradlew --build-cache "${TASKS[@]}" "${GRADLE_ARGS[@]}"
fi
