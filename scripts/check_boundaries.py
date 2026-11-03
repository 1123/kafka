#!/usr/bin/env python3
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

"""Fail a diff that crosses a dependency, import-control, or license boundary.

Java import allow lists stay in checkstyle/import-control*.xml. Checkstyle's
ImportControl module enforces them. This script checks that the module is
still wired up, and that a diff does not edit those files unless the diff
says so. It does not reimplement the allow lists.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

from review_diff import (  # noqa: E402
    ROOT,
    FileDiff,
    commit_message_text,
    load_files,
    parse_markers,
    read_blob,
    resolve_range,
    short_rev,
)

KIND_DEPENDENCY = "third-party-dependency"
KIND_IMPORT = "import-control"
KIND_CODE = "third-party-code"

HEADER_LINE_COUNT = 40

# Marker text in these paths documents the gate. It does not authorize a change.
MARKER_EXEMPT_PREFIXES = (".cursor/",)
MARKER_EXEMPT_PATHS = {
    "README.md",
    "AGENTS.md",
    "scripts/check_boundaries.py",
    "scripts/pre_review.py",
    "scripts/review_diff.py",
}

REQUIRE_APACHE_SUFFIXES = (".java", ".scala", ".kt", ".py", ".sh", ".gradle")
VENDORED_DIR_NAMES = {"vendor", "third_party", "third-party", "node_modules"}
VENDORED_SUFFIXES = {".jar", ".class", ".so", ".dll", ".dylib", ".a", ".zip", ".tar", ".gz", ".tgz"}

DEP_CONFIG_RE = re.compile(
    r"^\s*(?:api|implementation|compileOnly|runtimeOnly|testImplementation|"
    r"testCompileOnly|testRuntimeOnly|testFixturesImplementation|testFixturesApi|"
    r"annotationProcessor|classpath|developmentOnly|kapt|shadow)(?:\s|\()"
)
LIBS_RE = re.compile(r"\blibs\.[A-Za-z_]")
COORD_RE = re.compile(r"""['\"][A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[^'\"]+['\"]""")
VERSION_PIN_RE = re.compile(r"""^\s*[A-Za-z][A-Za-z0-9_]*\s*:\s*['\"][^'\"]+['\"]\s*,?\s*$""")
IMPORT_CONTROL_FILE_RE = re.compile(r"import-control[\w.-]*\.xml")


def _foreign_patterns() -> list[re.Pattern[str]]:
    # Kept out of the header region of this file on purpose. The boundary
    # check reads the first 40 lines of each new file, including this one.
    phrases = [
        r"GNU (?:General|Lesser|Affero) Public License",
        r"\bLGPL-\d",
        r"\bGPL-[23]\b",
        r"SPDX-License-Identifier:\s*(?!Apache-2\.0\b)\S+",
        r"\bMIT License\b",
        r"Permission is hereby granted, free of charge",
        r"Redistribution and use in source and binary forms",
        r"\bBSD License\b",
        r"Mozilla Public License",
        r"Eclipse Public License",
        r"\bCDDL\b",
        r"All rights reserved",
    ]
    return [re.compile(pattern, re.IGNORECASE) for pattern in phrases]


FOREIGN_PATTERNS = _foreign_patterns()


@dataclass
class Violation:
    path: str
    kind: str
    message: str
    samples: list[str]


def _comment_or_blank(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith(("//", "#", "/*", "*", "*/"))


def catalog_line_changed(path: str, added: list[tuple[int, str]], removed: list[tuple[int, str]]) -> list[str]:
    samples: list[str] = []
    if path == "gradle/dependencies.gradle" or path.endswith("/dependencies.gradle"):
        for prefix, rows in (("+", added), ("-", removed)):
            for _, line in rows:
                if not _comment_or_blank(line):
                    samples.append(prefix + line)
        return samples
    if path.endswith(".versions.toml") or path.endswith("/libs.versions.toml"):
        for prefix, rows in (("+", added), ("-", removed)):
            for _, line in rows:
                if not _comment_or_blank(line):
                    samples.append(prefix + line)
        return samples
    if path == "gradle/wrapper/gradle-wrapper.properties":
        for prefix, rows in (("+", added), ("-", removed)):
            for _, line in rows:
                if "distributionUrl" in line:
                    samples.append(prefix + line)
        return samples
    if path.endswith(".gradle") or path.endswith(".gradle.kts"):
        for prefix, rows in (("+", added), ("-", removed)):
            for _, line in rows:
                if gradle_line_is_external_dep(line):
                    samples.append(prefix + line)
    return samples


def gradle_line_is_external_dep(line: str) -> bool:
    if _comment_or_blank(line):
        return False
    stripped = line.strip()
    has_libs = LIBS_RE.search(line) is not None
    has_coord = COORD_RE.search(line) is not None
    has_project = "project(" in line or "project (" in line
    if VERSION_PIN_RE.match(stripped) and not has_project:
        return True
    if DEP_CONFIG_RE.match(line) is None:
        return False
    if has_project and not has_libs and not has_coord:
        return False
    return has_libs or has_coord


def marker_exempt(path: str) -> bool:
    if path in MARKER_EXEMPT_PATHS:
        return True
    return path.startswith(MARKER_EXEMPT_PREFIXES)


def file_allows(kind: str, diff: FileDiff, messages: str) -> bool:
    if kind in parse_markers(messages):
        return True
    if marker_exempt(diff.path):
        return False
    return kind in parse_markers(diff.added_text())


def _header_text(text: str) -> str:
    return "\n".join(text.splitlines()[:HEADER_LINE_COUNT])


def _has_apache(header: str) -> bool:
    return (
        "Licensed to the Apache Software Foundation" in header
        or "Apache License, Version 2.0" in header
    )


def _foreign_hit(header: str) -> str | None:
    for pattern in FOREIGN_PATTERNS:
        match = pattern.search(header)
        if match:
            return match.group(0)
    return None


def requires_apache(path: str) -> bool:
    if "/generated/" in path or "/generated-test/" in path or path.startswith("licenses/"):
        return False
    return path.endswith(REQUIRE_APACHE_SUFFIXES)


def vendored_path(path: str) -> bool:
    if any(part in VENDORED_DIR_NAMES for part in path.split("/")):
        return True
    dot = path.rfind(".")
    if dot == -1:
        return False
    # The Gradle wrapper jar is build tooling, not copied-in application code.
    if path.startswith("gradle/wrapper/") and path.endswith(".jar"):
        return False
    return path[dot:] in VENDORED_SUFFIXES


def header_violation(path: str, text: str | None, added: bool, binary: bool) -> str | None:
    if vendored_path(path) and added:
        return "copied-in third-party or binary artifact"
    if not added or binary or text is None:
        if added and binary and path.endswith(VENDORED_SUFFIXES):
            return "copied-in binary artifact"
        return None
    header = _header_text(text)
    apache = _has_apache(header)
    foreign = _foreign_hit(header)
    if foreign and not apache:
        return f"foreign license header ({foreign})"
    if foreign and apache:
        return f"foreign license text next to an Apache header ({foreign})"
    if requires_apache(path) and not apache:
        return "new source file has no Apache license header"
    return None


def import_control_paths_changed(diff: FileDiff) -> bool:
    if not (diff.path.startswith("checkstyle/import-control") and diff.path.endswith(".xml")):
        return False
    return bool(diff.added or diff.removed or diff.status in {"A", "M", "D"})


def suppression_disables_import_control(diff: FileDiff) -> list[str]:
    if diff.path != "checkstyle/suppressions.xml":
        return []
    return ["+" + line for _, line in diff.added if "ImportControl" in line]


def checkstyle_unwires_import_control(diff: FileDiff) -> bool:
    if diff.path != "checkstyle/checkstyle.xml":
        return False
    removed = any("ImportControl" in line for _, line in diff.removed)
    added = any("ImportControl" in line for _, line in diff.added)
    return removed and not added


def wiring_errors() -> list[str]:
    errors: list[str] = []
    checkstyle_xml = ROOT / "checkstyle" / "checkstyle.xml"
    if not checkstyle_xml.is_file():
        return ["checkstyle/checkstyle.xml is missing"]
    text = checkstyle_xml.read_text(encoding="utf-8")
    if 'name="ImportControl"' not in text:
        errors.append("checkstyle/checkstyle.xml does not enable the ImportControl module")
    gradle_files = [ROOT / "build.gradle", ROOT / "api-checker" / "build.gradle"]
    referenced: set[str] = set()
    for gradle in gradle_files:
        if not gradle.is_file():
            continue
        for match in IMPORT_CONTROL_FILE_RE.finditer(gradle.read_text(encoding="utf-8")):
            referenced.add(match.group(0))
    if not referenced:
        errors.append("no import-control XML file is referenced from Gradle")
    for name in sorted(referenced):
        if not (ROOT / "checkstyle" / name).is_file():
            errors.append(f"checkstyle/{name} is referenced from Gradle but does not exist")
    return errors


def evaluate(files: list[FileDiff], messages: str, read_text, wiring: list[str]) -> list[Violation]:
    violations: list[Violation] = []
    import_allowed = KIND_IMPORT in parse_markers(messages)

    if wiring and not import_allowed:
        violations.append(Violation(
            path="checkstyle/checkstyle.xml",
            kind=KIND_IMPORT,
            message="Checkstyle ImportControl is not wired to the import-control XML files. "
                    "Add boundary-allow: import-control to the commit message only when that disconnect is intentional.",
            samples=wiring,
        ))

    for diff in files:
        dep_samples = catalog_line_changed(diff.path, diff.added, diff.removed)
        if dep_samples and not file_allows(KIND_DEPENDENCY, diff, messages):
            violations.append(Violation(
                path=diff.path,
                kind=KIND_DEPENDENCY,
                message="third-party dependency change without boundary-allow: third-party-dependency "
                        "in this file or in a commit message in the range",
                samples=dep_samples[:8],
            ))

        import_samples: list[str] = []
        if import_control_paths_changed(diff):
            import_samples.append(f"{diff.status} {diff.path}")
        import_samples.extend(suppression_disables_import_control(diff))
        if checkstyle_unwires_import_control(diff):
            import_samples.append("removed ImportControl from checkstyle/checkstyle.xml")
        if import_samples and not file_allows(KIND_IMPORT, diff, messages):
            violations.append(Violation(
                path=diff.path,
                kind=KIND_IMPORT,
                message="import-control change without boundary-allow: import-control. "
                        "Java imports themselves are still judged by Checkstyle ImportControl, not by this script.",
                samples=import_samples[:8],
            ))

        added = diff.status == "A" or (diff.status.startswith("R") and diff.old_path != diff.path)
        # A rename of an existing Apache file is not copied-in code.
        if diff.status.startswith("R"):
            added = False
        text = None
        if added or (diff.added and _touches_header(diff)):
            text = read_text(diff.path)
        problem = header_violation(diff.path, text, added or _foreign_in_new_header(text), diff.binary)
        if problem and not file_allows(KIND_CODE, diff, messages):
            violations.append(Violation(
                path=diff.path,
                kind=KIND_CODE,
                message=problem + " without boundary-allow: third-party-code in this file or in a commit message",
                samples=[],
            ))
    return violations


def _touches_header(diff: FileDiff) -> bool:
    return any(number <= HEADER_LINE_COUNT for number, _ in diff.added)


def _foreign_in_new_header(text: str | None) -> bool:
    if text is None:
        return False
    header = _header_text(text)
    foreign = _foreign_hit(header)
    return foreign is not None and not _has_apache(header)


def format_report(base: str, head: str, file_count: int, violations: list[Violation], wiring: list[str]) -> str:
    lines = [
        f"Boundary check base {short_rev(base)} head {head} ({file_count} files).",
    ]
    if wiring and any(item.kind == KIND_IMPORT and item.path == "checkstyle/checkstyle.xml" for item in violations):
        pass
    elif not wiring:
        lines.append(
            "Import control: Checkstyle's ImportControl module is enabled and every "
            "import-control XML file referenced from Gradle exists. Illegal Java imports "
            "fail checkstyleMain and checkstyleTest. This script does not re-parse the allow lists."
        )
    if not violations:
        lines.append("Boundary check passed.")
        return "\n".join(lines)
    lines.append(f"Boundary check failed ({len(violations)}).")
    for item in violations:
        lines.append(f"- {item.path}: {item.message}")
        for sample in item.samples:
            lines.append(f"    {sample}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check a Kafka diff for dependency, import-control, and license boundaries.")
    parser.add_argument("--base", help="Base revision. Defaults to the merge-base with origin/trunk, or CI BASE_SHA.")
    parser.add_argument("--head", help="Head revision. Defaults to the worktree locally and HEAD in CI. Use WORKTREE to include uncommitted edits.")
    parser.add_argument("--self-test", action="store_true", help="Run classifier checks and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test()
    base, head, include_worktree = resolve_range(args.base, args.head)
    files = load_files(base, head, include_worktree)
    messages = commit_message_text(base, head if not include_worktree else "HEAD")
    wiring = wiring_errors()

    def read_text(path: str) -> str | None:
        return read_blob(head, path, include_worktree)

    violations = evaluate(files, messages, read_text, wiring)
    print(format_report(base, "worktree" if include_worktree else short_rev(head), len(files), violations, wiring))
    return 1 if violations else 0


def run_self_test() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    check(gradle_line_is_external_dep("    implementation libs.jacksonDatabind"), "libs. dependency")
    check(gradle_line_is_external_dep('    implementation "com.example:widget:1.2.3"'), "maven coordinate")
    check(not gradle_line_is_external_dep("    implementation project(':clients')"), "project dependency")
    check(not gradle_line_is_external_dep("        '**/.cursor/**'"), "rat exclude string")
    check(gradle_line_is_external_dep('  jackson: "2.22.4",'), "catalog pin")
    check(not gradle_line_is_external_dep(" * Licensed to the Apache Software Foundation (ASF) under one"), "license comment")

    rat_only = FileDiff(status="M", path="build.gradle", added=[(300, "        '**/.cursor/**'")], removed=[])
    check(catalog_line_changed(rat_only.path, rat_only.added, rat_only.removed) == [], "rat exclude is not a dependency change")

    pin = FileDiff(status="M", path="gradle/dependencies.gradle", added=[(60, '  examplelib: "1.0.0",')], removed=[])
    check(catalog_line_changed(pin.path, pin.added, pin.removed), "dependencies.gradle pin is a dependency change")

    apache_java = "/*\n * Licensed to the Apache Software Foundation (ASF) under one or more\n */\npackage a;\n"
    check(header_violation("clients/src/main/java/A.java", apache_java, True, False) is None, "apache java passes")
    mit = "/*\n * MIT License\n * Copyright (c) 2020 Someone\n */\n"
    check(header_violation("clients/src/main/java/A.java", mit, True, False) is not None, "MIT java fails")
    check(header_violation(".cursor/skills/pre-review/SKILL.md", "---\nname: pre-review\n---\n# Pre-review\n", True, False) is None, "skill markdown passes")
    bare_java = "package org.apache.kafka.example;\nclass A {}\n"
    check(header_violation("clients/src/main/java/A.java", bare_java, True, False) is not None, "java without header fails")

    messages = ""
    dep = FileDiff(status="M", path="gradle/dependencies.gradle", added=[(1, '  examplelib: "9.9.9",')])
    violations = evaluate([dep], messages, lambda _path: None, [])
    check(any(item.kind == KIND_DEPENDENCY for item in violations), "dependency violation without marker")
    dep.added.append((2, "// boundary-allow: third-party-dependency"))
    violations = evaluate([dep], messages, lambda _path: None, [])
    check(not any(item.kind == KIND_DEPENDENCY for item in violations), "in-file marker allows the dependency")

    readme = FileDiff(status="M", path="README.md", added=[(1, "boundary-allow: third-party-dependency")])
    dep_unmarked = FileDiff(status="M", path="gradle/dependencies.gradle", added=[(1, '  examplelib: "9.9.9",')])
    violations = evaluate([dep_unmarked, readme], "", lambda _path: None, [])
    check(any(item.kind == KIND_DEPENDENCY for item in violations), "README mention is not an allow marker")

    xml = FileDiff(status="M", path="checkstyle/import-control.xml", added=[(40, '  <allow pkg="com.example.vendor" />')])
    violations = evaluate([xml], "", lambda _path: None, [])
    check(any(item.kind == KIND_IMPORT for item in violations), "import-control edit fails")
    violations = evaluate([xml], "boundary-allow: import-control\n", lambda _path: None, [])
    check(not violations, "commit message marker allows the import-control edit")

    gpl = FileDiff(status="A", path="scripts/copied.py")
    gpl_text = "# MIT License\n# Copyright (c) 2019 Elsewhere\n\nprint('x')\n"
    violations = evaluate([gpl], "", lambda path: gpl_text if path == gpl.path else None, [])
    check(any(item.kind == KIND_CODE for item in violations), "foreign header fails")

    check(wiring_errors() == [], "this tree still wires Checkstyle ImportControl: " + "; ".join(wiring_errors()))

    if failures:
        print("self-test failed:")
        for item in failures:
            print(f"- {item}")
        return 1
    print(f"self-test passed ({len(FOREIGN_PATTERNS)} license patterns, ImportControl wired).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
