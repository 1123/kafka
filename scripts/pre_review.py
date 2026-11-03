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

"""Write a short pre-review report for a Kafka branch.

The report is advisory. scripts/check_boundaries.py is the failing gate.
Deprecated types and constants are read from @Deprecated and @deprecated in
src/main, then matched only when the changed file imports that type or lives
in the same package. Checkstyle still owns import allow lists, System.exit,
star imports, and toLowerCase() without a Locale.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field

from review_diff import (
    ROOT,
    FileDiff,
    load_files,
    read_blob,
    resolve_range,
    short_rev,
)

JAVA11_PREFIXES = (
    "clients/",
    "generator/",
    "streams/src/",
    "streams/test-utils/",
    "streams/examples/",
    "streams/streams-scala/",
    "test-common/test-common-util/",
)

TYPE_RE = re.compile(
    r"^(?:public |protected |private |abstract |final |sealed |open |case |static )*"
    r"(class|interface|enum|record|object|trait)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
CONST_RE = re.compile(
    r"^(?:public |protected |private |static |final )+(?:[\w.<>,\[\]]+\s+)+([A-Z][A-Z0-9_]{7,})\s*="
)
TEST_ANNO_RE = re.compile(r"@(Test|ParameterizedTest|RepeatedTest|ClusterTest|ClusterTemplate)\b")
METHOD_RE = re.compile(r"\b(?:void|def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
TAUTOLOGY_RE = re.compile(
    r"assertTrue\(\s*true\s*\)|assertFalse\(\s*false\s*\)|"
    r"assertEquals\(\s*true\s*,\s*true\s*\)|assertEquals\(\s*false\s*,\s*false\s*\)"
)
STRONG_ASSERT_RE = re.compile(
    r"\b(assertEquals|assertNotEquals|assertTrue|assertFalse|assertNull|assertSame|"
    r"assertNotSame|assertArrayEquals|assertIterableEquals|assertThrows|assertAll|"
    r"assertInstanceOf|assertTimeout|assertTimeoutPreemptively|verify|fail|assertThat)\b|"
    r"intercept\[|assert\s*\("
)
WEAK_ASSERT_RE = re.compile(r"\b(assertNotNull|assertDoesNotThrow)\b")
CODE_METHOD_RE = re.compile(
    r"^(?:public |protected |private |static |final |synchronized |native |abstract |default )*"
    r"(?:<[^>]+>\s*)?(?:[\w.<>, ?\[\]]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
KAFKA_IMPORT_RE = re.compile(r"^\s*import\s+kafka(?:\.|\s)")
SUPPRESS_RE = re.compile(r"@SuppressWarnings\s*\([^)]*(?:deprecation|removal)")


@dataclass
class Symbol:
    fqcn: str
    package: str
    name: str
    path: str
    line: int
    note: str


@dataclass
class Finding:
    path: str
    line: int
    section: str
    message: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)
    indexed_types: int = 0
    indexed_constants: int = 0


def strip_code(line: str) -> str:
    without_comment = re.sub(r"//.*", "", line)
    without_strings = re.sub(r'"(?:\\.|[^"\\])*"', '""', without_comment)
    return re.sub(r"'(?:\\.|[^'\\])*'", "''", without_strings)


def comment_line(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith(("//", "*", "/*", "#"))


def file_package(text: str) -> str:
    clauses: list[str] = []
    for line in text.splitlines()[:50]:
        raw = line.strip()
        if not raw or raw.startswith(("//", "*", "/*")):
            continue
        match = re.match(r"package\s+([A-Za-z0-9_.]+)\s*;?\s*$", raw)
        if match:
            clauses.append(match.group(1))
            continue
        if clauses:
            break
    if not clauses:
        return ""
    package = clauses[0]
    for extra in clauses[1:]:
        if extra == package or extra.startswith(package + "."):
            package = extra
        else:
            package = package + "." + extra
    return package


def deprecation_note(lines: list[str], index: int) -> str:
    annotation = lines[index].strip()
    inline = re.search(r'@deprecated\(\s*"([^"]+)"', annotation, re.IGNORECASE)
    if inline:
        return inline.group(1)[:200]
    for line in reversed(lines[max(0, index - 25):index]):
        if "@deprecated" not in line.lower():
            continue
        text = re.sub(r"^\s*/?\*+\s?", "", line).strip()
        text = re.sub(r"^@deprecated\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\{@link\s+([^}\s]+)\}", r"\1", text)
        return text[:200]
    return ""


def index_deprecated(root) -> tuple[dict[str, Symbol], dict[str, Symbol]]:
    types: dict[str, Symbol] = {}
    constants: dict[str, Symbol] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".git", "build", "out", ".gradle"}]
        slash = dirpath.replace("\\", "/")
        if "/src/main/" not in slash:
            continue
        for filename in filenames:
            if not filename.endswith((".java", ".scala")):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, root).replace("\\", "/")
            try:
                lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                continue
            package = file_package("\n".join(lines[:50]))
            pending = False
            waited = 0
            note = ""
            ann_line = 0
            for index, line in enumerate(lines):
                raw = line.strip()
                if re.search(r"@(?:Deprecated|deprecated)\b", raw):
                    pending = True
                    waited = 0
                    note = deprecation_note(lines, index)
                    ann_line = index + 1
                    continue
                if not pending:
                    continue
                if raw == "" or raw.startswith(("*", "//", "/*")):
                    waited += 1
                    if waited > 12:
                        pending = False
                    continue
                if raw.startswith("@"):
                    continue
                type_match = TYPE_RE.match(raw)
                const_match = CONST_RE.match(raw)
                if type_match:
                    name = type_match.group(2)
                    fqcn = f"{package}.{name}" if package else name
                    types.setdefault(fqcn, Symbol(fqcn, package, name, rel, ann_line, note))
                elif const_match:
                    name = const_match.group(1)
                    owner = package
                    constants.setdefault(name, Symbol(f"{owner}.{name}" if owner else name, owner, name, rel, ann_line, note))
                pending = False
    return types, constants


def imports_type(text: str, package: str, name: str) -> bool:
    if not package:
        return False
    fqcn = f"{package}.{name}"
    for raw in text.splitlines():
        stripped = raw.strip().rstrip(";")
        if not stripped.startswith("import "):
            continue
        if stripped.startswith(f"import {fqcn}") or stripped.startswith(f"import static {fqcn}"):
            return True
        star = stripped.startswith(f"import {package}._") or stripped.startswith(f"import {package}.*")
        if star:
            return True
        if stripped.startswith(f"import {package}.") and re.search(rf"\b{re.escape(name)}\b", stripped):
            return True
    return False


def java11_module(path: str) -> bool:
    return path.startswith(JAVA11_PREFIXES)


def meaningful_code(diff: FileDiff) -> bool:
    for _, line in diff.added + diff.removed:
        if comment_line(line):
            continue
        stripped = line.strip()
        if stripped.startswith(("import ", "package ")):
            continue
        return True
    return False


def production_source(path: str) -> bool:
    if path.startswith(".cursor/") or "/generated/" in path or "/generated-test/" in path:
        return False
    if path.endswith(("package-info.java", "module-info.java")):
        return False
    return "/src/main/java/" in path or "/src/main/scala/" in path


def test_source(path: str) -> bool:
    return "/src/test/" in path or path.endswith(("Test.java", "Test.scala"))


def conventional_tests(path: str) -> list[str]:
    if "/src/main/java/" in path and path.endswith(".java"):
        swapped = path.replace("/src/main/java/", "/src/test/java/")
        return [swapped[: -len(".java")] + "Test.java"]
    if "/src/main/scala/" in path and path.endswith(".scala"):
        stem = path[: -len(".scala")]
        swapped = stem.replace("/src/main/scala/", "/src/test/scala/") + "Test.scala"
        unit = stem.replace("/src/main/scala/", "/src/test/scala/unit/") + "Test.scala"
        return [swapped, unit]
    return []


def class_name(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    for suffix in (".java", ".scala"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def added_method_names(diff: FileDiff, owner: str) -> list[str]:
    names: list[str] = []
    for _, line in diff.added:
        match = CODE_METHOD_RE.match(line.strip())
        if not match:
            continue
        name = match.group(1)
        if name in {owner, "if", "for", "while", "switch", "catch"}:
            continue
        if name not in names:
            names.append(name)
    return names[:4]


def test_methods(source: str) -> list[tuple[str, str]]:
    lines = source.splitlines()
    found: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        if not TEST_ANNO_RE.search(line):
            continue
        signature = None
        signature_at = index
        for follow in range(index, min(index + 12, len(lines))):
            match = METHOD_RE.search(lines[follow])
            if match and not lines[follow].strip().startswith("//"):
                signature = match.group(1)
                signature_at = follow
                break
        if not signature:
            continue
        blob = "\n".join(lines[signature_at:])
        brace = blob.find("{")
        if brace < 0:
            continue
        depth = 0
        body: list[str] = []
        for char in blob[brace:]:
            if char == "{":
                depth += 1
                if depth == 1:
                    continue
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            if depth >= 1:
                body.append(char)
        found.append((signature, "".join(body)))
    return found


def classify_body(body: str) -> str | None:
    stripped = TAUTOLOGY_RE.sub("", body)
    if TAUTOLOGY_RE.search(body) and not STRONG_ASSERT_RE.search(stripped):
        return "tautology"
    if STRONG_ASSERT_RE.search(stripped):
        return None
    if WEAK_ASSERT_RE.search(body):
        return "weak"
    return "no-assertion"


def gradle_import_controls() -> dict[str, str]:
    text = (ROOT / "build.gradle").read_text(encoding="utf-8")
    parts = re.split(r"\nproject\('(:[^']+)'\)\s*\{", text)
    mapping: dict[str, str] = {}
    for index in range(1, len(parts), 2):
        body = parts[index + 1]
        match = re.search(r'checkstyleConfigProperties\("([^"]+)"\)', body)
        if match:
            mapping[parts[index]] = match.group(1)
    return mapping


def project_for_path(path: str) -> str | None:
    settings = (ROOT / "settings.gradle").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^include\b(.*?)^project\(", settings)
    if not match:
        return None
    names = re.findall(r"'([^']+)'", match.group(1))
    directories = sorted(((name.replace(":", "/"), ":" + name) for name in names), key=lambda item: len(item[0]), reverse=True)
    for directory, gradle_path in directories:
        if path == directory or path.startswith(directory + "/"):
            return gradle_path
    return None


def kafka_package_allowed(path: str, mapping: dict[str, str]) -> bool:
    gradle_path = project_for_path(path)
    filename = mapping.get(gradle_path or "", "import-control.xml")
    xml_path = ROOT / "checkstyle" / filename
    if not xml_path.is_file():
        return False
    xml = xml_path.read_text(encoding="utf-8")
    return re.search(r'<allow\s+pkg="kafka"\s*/>', xml) is not None


def anti_patterns(diff: FileDiff, text: str, kafka_allowed: bool) -> list[Finding]:
    found: list[Finding] = []
    if "/src/generated/" in diff.path or "/generated-test/" in diff.path or "/build/generated/" in diff.path:
        found.append(Finding(
            diff.path,
            0,
            "Anti-patterns",
            "Hand-edited generated source. Change the message spec and run ./gradlew processMessages processTestMessages.",
        ))
        return found
    if diff.path == "checkstyle/suppressions.xml":
        for number, line in diff.added:
            if "<suppress" in line:
                found.append(Finding(diff.path, number, "Anti-patterns", "New Checkstyle suppression. kafka-style says to fix the code instead of adding a suppressions entry."))
        return found
    if not diff.path.endswith((".java", ".scala")):
        return found
    combined = "\n".join(text.splitlines()) if text else ""
    has_parameterized = "@ParameterizedTest" in combined
    has_cluster = any(token in combined for token in ("@ClusterTest", "@ClusterTests", "@ClusterTemplate"))
    if has_parameterized and has_cluster and test_source(diff.path):
        found.append(Finding(
            diff.path,
            0,
            "Anti-patterns",
            "Kafka tests do not combine @ParameterizedTest with @ClusterTest, @ClusterTests, or @ClusterTemplate "
            "(.cursor/rules/kafka-testing.mdc).",
        ))
    for number, line in diff.added:
        if comment_line(line):
            continue
        code = strip_code(line)
        if "System.exit(" in code:
            found.append(Finding(diff.path, number, "Anti-patterns", "System.exit is rejected by Checkstyle (dontUseSystemExit). Call Exit.exit."))
        if re.search(r"\.to(?:Lower|Upper)Case\(\s*\)", code):
            found.append(Finding(diff.path, number, "Anti-patterns", "toLowerCase()/toUpperCase() without a Locale is illegal in checkstyle/checkstyle.xml. Pass Locale.ROOT."))
        if re.search(r"^\s*import\s+.+\*;\s*$", line) or re.search(r"^\s*import\s+.+\._\s*$", line):
            found.append(Finding(diff.path, number, "Anti-patterns", "Star import. Checkstyle AvoidStarImport forbids it. Spotless orders explicit imports."))
        if re.search(r"^\s*import\s+(static\s+)?org\.junit\.(Test|Assert)\b", line):
            found.append(Finding(diff.path, number, "Anti-patterns", "JUnit 4 import. Tests use org.junit.jupiter.api (.cursor/rules/kafka-testing.mdc)."))
        if "org.assertj" in line:
            found.append(Finding(diff.path, number, "Anti-patterns", "AssertJ is not used in this repo. Use org.junit.jupiter.api.Assertions."))
        if "Thread.sleep" in code and test_source(diff.path):
            found.append(Finding(diff.path, number, "Anti-patterns", "Thread.sleep in a test. Prefer MockTime or another fake from org.apache.kafka.test."))
        if "ClusterTestExtensions" in line and "@ExtendWith" in line:
            found.append(Finding(diff.path, number, "Anti-patterns", "ClusterTestExtensions is registered through SPI. Do not add @ExtendWith for it."))
        if re.search(r"\blog\.(?:trace|debug|info|warn|error)\s*\([^;]*\+", code):
            found.append(Finding(diff.path, number, "Anti-patterns", "SLF4J message is concatenated. Use a parameterized template and put the throwable last (.cursor/rules/kafka-style.mdc)."))
        if re.search(r"\bSystem\.(?:out|err)\.print", code) and "/src/main/" in diff.path and not diff.path.startswith("generator/"):
            found.append(Finding(diff.path, number, "Anti-patterns", "System.out/System.err in main sources. The project logger is SLF4J (field name log)."))
        if "printStackTrace(" in code:
            found.append(Finding(diff.path, number, "Anti-patterns", "printStackTrace. Log with SLF4J and pass the throwable as the last argument."))
        if SUPPRESS_RE.search(line):
            found.append(Finding(diff.path, number, "Deprecated API", "New @SuppressWarnings for deprecation or removal hides a deprecated API call."))
        if KAFKA_IMPORT_RE.search(line) and not kafka_allowed and diff.path.endswith((".java", ".scala")):
            found.append(Finding(
                diff.path,
                number,
                "Anti-patterns",
                "import of the broker package kafka.*. The module's checkstyle/import-control file does not allow pkg=\"kafka\". "
                "Checkstyle ImportControl is the enforcer; this is an early signal.",
            ))
        if java11_module(diff.path) and diff.path.endswith(".java"):
            if '"""' in code or re.search(r"\bsealed\s+(class|interface)\b", code) or re.search(r"\brecord\s+[A-Z]\w*", code):
                found.append(Finding(
                    diff.path,
                    number,
                    "Anti-patterns",
                    "Text block, sealed type, or record in a Java 11 module (modulesNeedingJava11 in build.gradle). javac --release is 11 there.",
                ))
    return found


def deprecated_uses(diff: FileDiff, text: str, types: dict[str, Symbol], constants: dict[str, Symbol]) -> list[Finding]:
    if not text or not diff.path.endswith((".java", ".scala")):
        return []
    package = file_package(text)
    found: list[Finding] = []
    seen: set[str] = set()
    for symbol in types.values():
        if symbol.path == diff.path:
            continue
        imported = imports_type(text, symbol.package, symbol.name) or package == symbol.package
        for number, line in diff.added:
            if comment_line(line):
                continue
            code = strip_code(line)
            fqcn_hit = symbol.fqcn in code
            name_hit = imported and re.search(rf"\b{re.escape(symbol.name)}\b", code) is not None
            if not fqcn_hit and not name_hit:
                continue
            if re.search(rf"\b(?:class|interface|enum|record|object|trait)\s+{re.escape(symbol.name)}\b", code):
                continue
            key = f"{symbol.fqcn}:{number}"
            if key in seen:
                continue
            seen.add(key)
            note = f" {symbol.note}" if symbol.note else ""
            found.append(Finding(
                diff.path,
                number,
                "Deprecated API",
                f"uses deprecated {symbol.fqcn} ({symbol.path}:{symbol.line}).{note}",
            ))
    for symbol in constants.values():
        if symbol.path == diff.path or len(symbol.name) < 16:
            continue
        for number, line in diff.added:
            if comment_line(line):
                continue
            if symbol.name not in strip_code(line):
                continue
            key = f"{symbol.name}:{number}"
            if key in seen:
                continue
            seen.add(key)
            note = f" {symbol.note}" if symbol.note else ""
            found.append(Finding(
                diff.path,
                number,
                "Deprecated API",
                f"uses deprecated constant {symbol.name} ({symbol.path}:{symbol.line}).{note}",
            ))
    return found


def weak_tests(diff: FileDiff, text: str) -> list[Finding]:
    if not text or not test_source(diff.path):
        return []
    found: list[Finding] = []
    for name, body in test_methods(text):
        kind = classify_body(body)
        if kind == "no-assertion":
            found.append(Finding(diff.path, 0, "Tests", f"{name} has no assertion. Use org.junit.jupiter.api.Assertions (assertEquals, assertThrows) or Mockito verify."))
        elif kind == "weak":
            found.append(Finding(diff.path, 0, "Tests", f"{name} only uses assertNotNull or assertDoesNotThrow. Assert the value or the exception."))
        elif kind == "tautology":
            found.append(Finding(diff.path, 0, "Tests", f"{name} asserts a constant (assertTrue(true) or the same value on both sides)."))
    return found


def missing_tests(diff: FileDiff, files: list[FileDiff]) -> tuple[list[Finding], list[str]]:
    if not production_source(diff.path) or not meaningful_code(diff):
        return [], []
    owner = class_name(diff.path)
    if any(test_source(other.path) and re.search(rf"\b{re.escape(owner)}\b", other.added_text()) for other in files):
        return [], []
    existing = [candidate for candidate in conventional_tests(diff.path) if (ROOT / candidate).is_file()]
    methods = added_method_names(diff, owner)
    method_text = ", ".join(methods) if methods else "the changed behavior"
    if diff.status == "A" and not existing:
        where = conventional_tests(diff.path)
        target = where[0] if where else "(same module, src/test)"
        finding = Finding(
            diff.path,
            0,
            "Tests",
            f"new production type {owner} has no {owner}Test in the tree or the diff. Add a JUnit 5 test at {target}.",
        )
        proposal = proposal_for(diff, owner, target, method_text)
        return [finding], [proposal]
    if existing:
        message = f"{owner} changed and the diff does not mention it. Existing test: {existing[0]}."
    else:
        message = f"{owner} changed and no test file in the diff references it."
    return [Finding(diff.path, 0, "Tests", message + f" Cover {method_text}.")], []


def proposal_for(diff: FileDiff, owner: str, target: str, method_text: str) -> str:
    package = "org.apache.kafka"
    source = (ROOT / diff.path).read_text(encoding="utf-8", errors="replace") if (ROOT / diff.path).is_file() else ""
    package_match = re.search(r"(?m)^package\s+([A-Za-z0-9_.]+)\s*;", source)
    if package_match:
        package = package_match.group(1)
    return (
        f"Proposed {target} for {method_text}. Unit test unless a broker is required "
        f"(@ClusterTest + ClusterInstance, not mixed with @ParameterizedTest).\n\n"
        f"package {package};\n\n"
        f"import org.junit.jupiter.api.Test;\n\n"
        f"import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        f"class {owner}Test {{\n"
        f"    @Test\n"
        f"    void test{owner}() {{\n"
        f"        assertEquals(expected, new {owner}().work());\n"
        f"    }}\n"
        f"}}\n"
    )


def build_report(files: list[FileDiff], read_text) -> Report:
    report = Report()
    needs_index = any(diff.path.endswith((".java", ".scala")) for diff in files)
    types: dict[str, Symbol] = {}
    constants: dict[str, Symbol] = {}
    if needs_index:
        types, constants = index_deprecated(ROOT)
        report.indexed_types = len(types)
        report.indexed_constants = len(constants)
    mapping = gradle_import_controls()
    for diff in files:
        text = read_text(diff.path) or ""
        allowed = kafka_package_allowed(diff.path, mapping) if diff.path.endswith((".java", ".scala")) else True
        report.findings.extend(deprecated_uses(diff, text, types, constants))
        report.findings.extend(anti_patterns(diff, text, allowed))
        report.findings.extend(weak_tests(diff, text))
        missing, proposals = missing_tests(diff, files)
        report.findings.extend(missing)
        report.proposals.extend(proposals)
    return report


def render(base: str, head_label: str, file_count: int, report: Report) -> str:
    lines = [
        f"Pre-review base {short_rev(base)} head {head_label} ({file_count} files).",
    ]
    if report.indexed_types or report.indexed_constants:
        lines.append(
            f"Indexed {report.indexed_types} deprecated types and {report.indexed_constants} constants from src/main."
        )
    if not report.findings:
        lines.append("No findings.")
        lines.append("Next: python3 scripts/check_boundaries.py")
        return "\n".join(lines)
    sections = ("Deprecated API", "Anti-patterns", "Tests")
    for section in sections:
        items = [item for item in report.findings if item.section == section]
        if not items:
            continue
        lines.append("")
        lines.append(f"## {section}")
        for item in items:
            location = f"{item.path}:{item.line}" if item.line else item.path
            lines.append(f"- {location}: {item.message}")
    if report.proposals:
        lines.append("")
        lines.append("## Proposed tests")
        lines.extend(report.proposals)
    lines.append("")
    lines.append(f"{len(report.findings)} finding(s). Advisory. python3 scripts/check_boundaries.py is the failing gate.")
    return "\n".join(lines)


def run_self_test() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    source = """
class FooTest {
    @Test
    void testEmpty() {
    }

    @Test
    void testWeak() {
        assertNotNull(value);
    }

    @Test
    void testTautology() {
        assertTrue(true);
    }

    @Test
    void testOk() {
        assertEquals(1, value);
    }
}
"""
    classified = {name: classify_body(body) for name, body in test_methods(source)}
    check(classified.get("testEmpty") == "no-assertion", "empty test")
    check(classified.get("testWeak") == "weak", "weak test")
    check(classified.get("testTautology") == "tautology", "tautology")
    check(classified.get("testOk") is None, "assertEquals is enough")

    package = file_package("package org.apache.kafka.streams.scala\npackage kstream\n\nclass K {}\n")
    check(package == "org.apache.kafka.streams.scala.kstream", f"scala package join got {package}")
    check(not imports_type("import org.apache.kafka.streams.kstream.KStream;\n", "org.apache.kafka.streams.scala.kstream", "KStream"), "java KStream is not the deprecated scala wrapper")
    check(imports_type("import org.apache.kafka.streams.scala.kstream.KStream\n", "org.apache.kafka.streams.scala.kstream", "KStream"), "scala import matches")

    mapping = gradle_import_controls()
    check(mapping.get(":core") == "import-control-core.xml", "core import control")
    check(mapping.get(":server") == "import-control-server.xml", "server import control")
    check(kafka_package_allowed("core/src/main/scala/kafka/Foo.scala", mapping), "core may import kafka")
    check(not kafka_package_allowed("clients/src/main/java/org/apache/kafka/Foo.java", mapping), "clients may not import kafka")
    check(kafka_package_allowed("clients/clients-integration-tests/src/test/java/org/apache/kafka/Foo.java", mapping), "clients integration tests may import kafka")
    types, constants = index_deprecated(ROOT)
    check(any(symbol.name == "ConsumerGroupState" for symbol in types.values()), "ConsumerGroupState is indexed")
    check("MESSAGE_DOWNCONVERSION_ENABLE_CONFIG" in constants, "down-conversion constant is indexed")

    if failures:
        print("self-test failed:")
        for item in failures:
            print(f"- {item}")
        return 1
    print("pre-review self-test passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report deprecated APIs, Kafka anti-patterns, and test gaps in a branch diff.")
    parser.add_argument("--base", help="Base revision. Defaults to the merge-base with origin/trunk, or CI BASE_SHA.")
    parser.add_argument("--head", help="Head revision. Defaults to the worktree locally.")
    parser.add_argument("--output", help="Also write the report to this path.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when the report has findings.")
    parser.add_argument("--self-test", action="store_true", help="Run classifier checks and exit.")
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()
    base, head, include_worktree = resolve_range(args.base, args.head)
    files = load_files(base, head, include_worktree)

    def read_text(path: str) -> str | None:
        return read_blob(head, path, include_worktree)

    report = build_report(files, read_text)
    rendered = render(base, "worktree" if include_worktree else short_rev(head), len(files), report)
    print(rendered)
    if args.output:
        output_path = args.output
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    return 1 if args.strict and report.findings else 0


if __name__ == "__main__":
    sys.exit(main())
