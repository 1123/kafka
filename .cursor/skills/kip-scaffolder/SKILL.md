---
name: kip-scaffolder
description: Turn a Kafka KIP or JIRA into a scoped code change. Use when the user supplies a KIP number, a KIP wiki page, a JIRA id such as KAFKA-12345, or a task file based on task-template.md, and wants the right modules, interfaces, configs, metrics, and tests scaffolded.
---

# KIP scaffolder

Use this when the input is a KIP, a JIRA, or a task file, not a drive-by refactor. The task file carries only ticket-specific content. This skill carries the procedure. Cursor rules (`.cursor/rules/*.mdc`, in particular `pr-guardrails.mdc`, which is always on) carry the constraints. This skill refers to them by name and does not restate them.

## 1. Read the proposal

The task usually arrives as a filled-in `.cursor/skills/kip-scaffolder/task-template.md`. If ticket, target module, or public API impact is blank, fill what the KIP or JIRA text supports. `Target Module: Auto-Detect` means use step 2. `Public API Impact: Needs KIP check` means run the gap check below and stop if the change is not approved.

Collect the KIP page (https://cwiki.apache.org/confluence/display/KAFKA/) or the JIRA description, the motivation, the public API diff, and the compatibility section. Restate, in a few lines:

- user-visible behavior
- configs, metrics, RPCs, and public types that move
- which of those are already approved by the KIP

If a public package, wire protocol, config, or metric would change and the KIP does not approve it, stop and name the gap (the KIP gate in `pr-guardrails.mdc`). Do not invent a KIP id.

### Prototype exception

Only when the task file has a **Prototype exception** field that names the exact metrics, configs, or interfaces, the KIP gate is lifted for those names and nothing else:

- Implement the named items. Mark them "proposed, subject to KIP" in code comments, docs, and the commit body.
- Anything not named still stops at the gate (other configs, RPC or protocol changes, public javadoc packages, renames or removals of existing metrics).
- The change is a fork-only prototype that supports a future KIP: skip the upgrade note (step 6) and the upstream-ready branch (step 8).
- Never infer the exception from chat, memory, or other tasks. If the field is missing or does not name the item, the gate applies.

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

Copy `.cursor/skills/kip-scaffolder/template.java` into the target module's source tree (`src/main/java/...`) when the change adds a config or a new type. Skip it for changes that only extend existing classes (for example one more gauge). That file is outside every Gradle source set, so the build does not compile it where it sits.

Then:

1. Replace the package and class name. Match the surrounding package.
2. Keep the ASF header identical to `checkstyle/java.header`. Follow `kafka-style.mdc`.
3. Add the config keys, interface, and implementation the KIP describes.
4. For a protocol change, edit the JSON spec under `clients/src/main/resources/common/message/` and run `./gradlew processMessages processTestMessages`.
5. Add metrics and configs only when the KIP lists them (or the task's prototype exception names them).

### Metrics

- Find the component that owns the data and the existing metrics group it already uses; put broker-level aggregates next to the existing ones of the same kind. Follow the naming of the neighbours.
- Register the metric where the neighbours are registered, and add it to any name list a test or removal path iterates over, so cleanup stays symmetric.
- Keep the cost off the hot path. Prefer computing on read, like the existing aggregates, over updating shared state from request handling. State the cost and the leader/follower semantics in a code comment.

## 4. Tests

Follow `kafka-testing.mdc`.

- Unit-test the new type or behavior with JUnit 5 and Mockito in the same module. Extend the closest existing test for the same component.
- For a new metric, assert its value in the empty case, the changed case, and after the state is undone, and keep any registration/removal test green.
- When the broker has to be running, add a `@ClusterTest`. Do not stack it with `@ParameterizedTest`.

## 5. Docs

If the change adds or changes a metric, config, or CLI output, update the matching table under `docs/` (for broker metrics: `docs/operations/monitoring.md`, next to the nearest existing row, same markup).

## 6. Verify

Before committing, from the repo root, for every changed module (use the Gradle path with colons when nested, e.g. `streams:examples`):

```bash
./gradlew :module:spotlessApply
scripts/simulate_ci.sh --module module --tests YourNewTest
python3 scripts/check_boundaries.py
```

`scripts/simulate_ci.sh` runs Checkstyle, SpotBugs, and the boundary check. Then run the `pre-review` skill (`.cursor/skills/pre-review/SKILL.md`) when it exists and answer every finding. Fix Checkstyle, SpotBugs, and boundary failures in the new code instead of suppressing them.

User-visible change (not in prototype mode): add the upgrade note as described in `pr-guardrails.mdc`.

## 7. Commit

Subject: `KAFKA-XXXXX: ...` when the JIRA is real, otherwise `MINOR: ...`. Author and trailer follow `pr-guardrails.mdc`; take the author from the task's **Author** field when the rule says so. Commit only on the task's branch (or a new feature branch), never on `trunk`. Split optional extras of the task into their own commits.

## 8. Upstream-ready branch

Skip in prototype mode. Otherwise follow "Upstream-ready branch" in `pr-guardrails.mdc`.

## Done when

- The implementation is in the target module and matches the task's acceptance criteria.
- Unit tests cover the edge cases; `@ClusterTest` only where broker state is needed.
- Step 6 is green: spotless, `scripts/simulate_ci.sh`, `scripts/check_boundaries.py`, and `pre-review` with no unanswered findings.
- The commit follows step 7 and is not on `trunk`.
- Outside prototype mode: the upgrade note exists and `<JIRA-ID>-upstream` exists on `origin` without scaffolding files.
- In prototype mode: the final report lists the files touched, the evidence collected for the task's open questions, and confirms nothing was pushed upstream.
