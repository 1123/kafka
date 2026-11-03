# Bugbot

Use the checklist in `.cursor/skills/pre-review/SKILL.md`. That file is the procedure (how to run `scripts/pre_review.py`, how to write a missing test). This file is what to comment on. Build and module rules stay in `AGENTS.md`. Style and test rules stay in `.cursor/rules/kafka-style.mdc` and `.cursor/rules/kafka-testing.mdc`. Do not repeat those documents in the review.

Comment when the diff does any of the following:

1. Uses a deprecated type or constant. The skill's scan reads `@Deprecated` / `@deprecated` under `src/main` and matches an import or the same package. `ConsumerGroupState` and `TopicConfig.MESSAGE_DOWNCONVERSION_ENABLE_CONFIG` are examples. Also comment on a new `@SuppressWarnings("deprecation")` or `@SuppressWarnings("removal")`. Do not flag `Producer#close(Duration)` or `Admin#close(Duration)`; `Consumer#close(Duration)` is the one that is deprecated.
2. Breaks a rule Checkstyle or the test guide already enforces: `System.exit`, `toLowerCase()` / `toUpperCase()` with no `Locale`, star imports, JUnit 4, AssertJ, `Thread.sleep`, `@ExtendWith(ClusterTestExtensions)`, `@ParameterizedTest` together with `@ClusterTest` / `@ClusterTests` / `@ClusterTemplate`, SLF4J concatenation, a new `checkstyle/suppressions.xml` entry, or a hand-edit under `build/generated/`.
3. Changes production code under `src/main` without a test in the diff, or adds a test whose only assertion is `assertNotNull`, `assertDoesNotThrow`, or `assertTrue(true)`.
4. Adds or changes a third-party Gradle dependency (`gradle/dependencies.gradle`, a `build.gradle` external coordinate, a `*.versions.toml` catalog, or the wrapper `distributionUrl`) without `boundary-allow: third-party-dependency` in that file or in a commit message.
5. Edits `checkstyle/import-control*.xml`, removes the `ImportControl` module from `checkstyle/checkstyle.xml`, or suppresses `ImportControl`, without `boundary-allow: import-control`. Illegal Java imports fail Checkstyle. Do not restate the allow lists.
6. Adds a file with a non-Apache license header, or a new Java, Scala, Kotlin, Python, shell, or Gradle file with no Apache header, without `boundary-allow: third-party-code`.

A marker mentioned only in this file, the skill, or `README.md` does not authorize the change. Do not ask for an upstream pull request. This fork is `1123/kafka`.
