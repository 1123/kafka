---
name: pre-review
description: Review a Kafka branch before a human does. Use when asked to pre-review a diff, check a branch before review, find deprecated API use, Kafka anti-patterns, missing tests, or weak assertions. Runs scripts/pre_review.py and scripts/check_boundaries.py.
---

# Pre-review

Run this on a branch before asking for review. It diffs the branch against trunk and writes a short findings report. Build, test, and module conventions stay in `AGENTS.md`. Java style stays in `.cursor/rules/kafka-style.mdc`. Test style stays in `.cursor/rules/kafka-testing.mdc`. Public API and release boundaries stay in `.cursor/rules/pr-guardrails.mdc`. This skill does not copy those files.

Bugbot comments from the same checklist. The procedure lives here. The review notes live in `.cursor/BUGBOT.md`.

## Run

From the repo root:

```bash
python3 scripts/pre_review.py
python3 scripts/check_boundaries.py
scripts/simulate_ci.sh --boundaries-only
```

`scripts/pre_review.py` prints the report and exits 0. `--strict` exits 1 when there are findings. `--output path` also writes the report (put it under `build/` or outside the tree). `--base` and `--head` override the range. The default base is the merge-base with `origin/trunk`. The default head is the worktree, so uncommitted edits are included.

`scripts/check_boundaries.py` exits 1 on a boundary violation. `scripts/simulate_ci.sh` runs that check before Checkstyle. `--boundaries-only` stops after it. `--skip-boundaries` runs only the Gradle tasks. After you change code, run Checkstyle and the new test the way `.cursor/rules/kafka-testing.mdc` shows (`scripts/simulate_ci.sh --module <module> --tests <filter>`).

Do not open a pull request against `apache/kafka`. Work stays on a branch of `1123/kafka`.

## Checklist

1. Deprecated API. The report indexes `@Deprecated` and `@deprecated` on types and constants under `src/main`. A hit counts only when the changed file imports that type (or is in the same package), so `org.apache.kafka.streams.kstream.KStream` is not confused with the deprecated Scala wrapper `org.apache.kafka.streams.scala.kstream.KStream`. Examples the index finds: `ConsumerGroupState` (the javadoc says to use `GroupState`) and `TopicConfig.MESSAGE_DOWNCONVERSION_ENABLE_CONFIG`. Method deprecations such as `Consumer#close(Duration)` in `clients/src/main/java/org/apache/kafka/clients/consumer/Consumer.java` are not matched by name, because `Producer#close(Duration)` and `Admin#close(Duration)` are still the current API. A new `@SuppressWarnings("deprecation")` or `@SuppressWarnings("removal")` is the signal for those.
2. Kafka anti-patterns already enforced in tree. `System.exit` (`dontUseSystemExit` in `checkstyle/checkstyle.xml`; call `Exit.exit`). `toLowerCase()` / `toUpperCase()` with an empty argument list (the Checkstyle `Regexp`; pass `Locale.ROOT`). Star imports (`AvoidStarImport`). JUnit 4 and AssertJ (`.cursor/rules/kafka-testing.mdc` requires JUnit Jupiter and `org.junit.jupiter.api.Assertions`). `Thread.sleep` in tests (use `MockTime`). `@ExtendWith` for `ClusterTestExtensions` (it is registered through SPI). `@ParameterizedTest` combined with `@ClusterTest`, `@ClusterTests`, or `@ClusterTemplate`. SLF4J message concatenation, `System.out` in `src/main` outside `generator/`, and `printStackTrace`. Text blocks, `sealed` types, and `record` declarations in a module listed in `modulesNeedingJava11` in `build.gradle`. A new `<suppress` in `checkstyle/suppressions.xml`. Hand-edits under `build/generated/` or `src/generated/`. An `import kafka.` in a module whose `checkstyle/import-control*.xml` does not contain `<allow pkg="kafka"/>`.
3. Tests. Production code under `src/main/java` or `src/main/scala` that changes behavior should show up in a test in the same diff. A new type with no `*Test` is a finding, and the report includes a sketch. A test method whose body has no assertion, only `assertNotNull` or `assertDoesNotThrow`, or only `assertTrue(true)`, is a weak test. SpotBugs is not reimplemented here; `scripts/simulate_ci.sh` runs `spotbugsMain`, and the suppressions live in `gradle/spotbugs-exclude.xml`.
4. Third-party Gradle dependencies. A change to `gradle/dependencies.gradle`, a version catalog (`*.versions.toml`), `gradle/wrapper/gradle-wrapper.properties` `distributionUrl`, or a `build.gradle` line that adds an external coordinate (`libs.something` or `group:name:version`) fails unless the file or a commit message in the range contains `boundary-allow: third-party-dependency`. `implementation project(':clients')` is an in-repo project dependency and is not flagged. The fork's `grok-bot/requirements.txt` pins are outside this gate.
5. Import control. Do not re-parse the allow lists. Checkstyle's `ImportControl` module in `checkstyle/checkstyle.xml` already enforces `checkstyle/import-control*.xml` on `checkstyleMain` and `checkstyleTest`. The boundary check fails when those XML files, the `ImportControl` module, or a `suppressions.xml` entry for `ImportControl` change without `boundary-allow: import-control`.
6. Copied-in code. A new file whose first 40 lines carry a non-Apache license, or a new `.java`, `.scala`, `.kt`, `.py`, `.sh`, or `.gradle` file with no Apache header, fails unless that file or a commit message contains `boundary-allow: third-party-code`. New jars, native libraries, archives, and paths under `vendor/`, `third_party/`, or `node_modules/` fail the same way. The patterns live in `scripts/check_boundaries.py`.

## Fix the findings

Use the replacement named in a deprecated-API finding (it is copied from the `@deprecated` note on the declaration). Do not add `@SuppressWarnings("deprecation")` to keep the old call.

When the report proposes a test for a new production type, add that test in the same module. Keep the Apache header from `checkstyle/java.header`. Use JUnit 5. Name the method after the behavior. Assert a value with `assertEquals` or the thrown type with `assertThrows`. Use Mockito only when the production code already collaborates with something you can stub. Use `@ClusterTest` and a `ClusterInstance` parameter only when the assertion needs a broker, and do not combine it with `@ParameterizedTest`. Tag a long end-to-end test `@Tag("integration")`.

Skip a test when the diff is comments, imports, `package-info.java`, or generated code. Do not add a test whose only assertion is `assertNotNull`. Do not delete an assertion to make the report empty.

Replace the sketch's placeholder `assertEquals(expected, new Type().work())` with the real call. The sketch is a starting point, not something to commit unchanged.

## Allow markers

Put the marker in the file that crosses the boundary, or in a commit message in the diff range:

```text
boundary-allow: third-party-dependency
boundary-allow: import-control
boundary-allow: third-party-code
```

In Gradle, a `//` comment on the new dependency line is enough. In an import-control XML file, use an XML comment. A mention of the marker in `README.md`, `AGENTS.md`, this skill, or the checker scripts does not authorize a change. Do not add a standing allow file.

## Done when

- `python3 scripts/pre_review.py` has no findings you have not answered.
- `python3 scripts/check_boundaries.py` prints `Boundary check passed.`
- For a code change, `scripts/simulate_ci.sh --module <module> --tests <filter>` is green.
