---
name: kip-scaffolder
description: Turn a Kafka KIP or JIRA into a scoped code change. Use when the user supplies a KIP number, a KIP wiki page, or a JIRA id such as KAFKA-12345 and wants the right modules, interfaces, configs, and tests scaffolded.
---

# KIP scaffolder

Use this when the input is a KIP or a JIRA, not a drive-by refactor. Read `.cursor/rules/pr-guardrails.mdc` before editing. Work on a feature branch of `1123/kafka`. Do not push to `apache/kafka` and do not open an upstream pull request.

## 1. Read the proposal

Collect the KIP page (https://cwiki.apache.org/confluence/display/KAFKA/) or the JIRA description, the motivation, the public API diff, and the compatibility section. Restate, in a few lines:

- user-visible behavior
- configs, metrics, RPCs, and public types that move
- which of those are already approved by the KIP

If a public package, wire protocol, config, or metric would change and the KIP does not approve it, stop and name the gap. Do not invent a KIP id.

## 2. Pick modules

Map the change onto existing projects in `settings.gradle`. Typical homes:

| Change | Module |
| --- | --- |
| Client API, protocol messages, serializers | `:clients` |
| Broker request path, replication, log manager | `:core`, `:server`, `:storage` |
| KRaft metadata and quorum | `:metadata`, `:raft` |
| Group, share, or transaction coordinator | `:group-coordinator`, `:share-coordinator`, `:transaction-coordinator` |
| Connect API or runtime | `:connect:api`, `:connect:runtime` |
| Streams DSL or runtime | `:streams` |

Prefer the module that already owns the type. Keep clients free of server imports (`checkstyle/import-control.xml`).

## 3. Scaffold

Copy `.cursor/skills/kip-scaffolder/template.java` into the target module's source tree (`src/main/java/...`). That file is outside every Gradle source set, so the build does not compile it where it sits.

Then:

1. Replace the package and class name. Match the surrounding package.
2. Keep the ASF header identical to `checkstyle/java.header`.
3. Add the config keys, interface, and implementation the KIP describes. Follow `.cursor/rules/kafka-style.mdc` (SLF4J logger named `log`, Spotless import order).
4. For a protocol change, edit the JSON spec under `clients/src/main/resources/common/message/` and run `./gradlew processMessages processTestMessages`. Do not hand-edit generated Java.
5. Add metrics and configs only when the KIP lists them.

## 4. Tests

Follow `.cursor/rules/kafka-testing.mdc`.

- Unit-test the new type with JUnit 5 and Mockito in the same module.
- When the broker has to be running, add a `@ClusterTest` that takes `ClusterInstance`. Do not stack that annotation with `@ParameterizedTest`.
- Tag long end-to-end tests `@Tag("integration")`.

## 5. Check

From the repo root, for the module you changed:

```bash
./gradlew :module:spotlessApply
scripts/simulate_ci.sh --module module --tests YourNewTest
```

Use the Gradle project path with colons when it is nested (`streams:examples`). Fix Checkstyle and SpotBugs failures in the new code. Leave `checkstyle/suppressions.xml` unchanged.

## 6. Commit

Subject: `KAFKA-XXXXX: ...` when the JIRA is real, otherwise `MINOR: ...`.

Trailer, because the change was agent-assisted:

```text
Co-Authored-By: <tool name and version>
Generated-by: <tool name and version>
```

Do not commit on `trunk`.
