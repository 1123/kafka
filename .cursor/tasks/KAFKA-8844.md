### TASK: Prototype KAFKA-8844: Add a broker-level aggregate of LastStableOffsetLag (max LSO lag)

> **PROTOTYPE / DRAFT, NO ACCEPTED KIP.** This task intentionally starts without an accepted KIP. The work lives only on a feature branch of the fork `1123/kafka` and exists to support a future KIP. It is **not** for an upstream pull request and **must not** be pushed to or proposed against `apache/kafka`.

#### 1. Context & Objective

- **Ticket / KIP:** KAFKA-8844 (https://issues.apache.org/jira/browse/KAFKA-8844). KIP: **none yet** (the JIRA carries the label `needs-kip`). See section 5.
- **Summary:** Kafka exposes a per-partition gauge `LastStableOffsetLag` (`kafka.cluster:type=Partition,name=LastStableOffsetLag,topic=*,partition=*`). Add a broker-level gauge that aggregates it across all partitions led by this broker, so operators can alert on one number for stuck or hanging transactions instead of collecting per-partition metrics. Primary aggregate: maximum LSO lag. Optional: count of led partitions with LSO lag > 0.
- **Target Module:** :core (`kafka.server.ReplicaManager`). `:storage` (`UnifiedLog`) is read-only for this task unless a cheaper accessor is needed.
- **Branch:** `KAFKA-8844-max-lso-lag-prototype`, a feature branch on `1123/kafka`, cut from `origin/cursor-scaffolding` (same as KAFKA-15923). Never `trunk`.

JIRA facts (checked 2026-11-03):

- KAFKA-8844 "Add max lso lag metric": status Open, unassigned, reporter Jason Gustafson, created 2019-08-28, never updated, no comments, no linked PR, no fix version, label `needs-kip`.
- Description: "We have a per-partition metric to monitor the LSO lag. This allows us to track the difference between the high watermark and the LSO, which is useful to detect transactions which are stalled. It would be helpful to have a gauge to monitor the maximum lag across all partitions so that users don't need to collect per-partition metrics."
- No KIP for this was found (cwiki and dev@ searches). No upstream PR was found (GitHub search for KAFKA-8844, "max lso", "LastStableOffsetLag" in `apache/kafka`).
- Related, already shipped: KIP-664 / KAFKA-13221 added the broker gauge `PartitionsWithLateTransactionsCount` (fix version 3.2.0), a count of led partitions with open transactions older than `transaction.max.timeout.ms` plus padding. KIP-664 itself says an LSO-lag alert threshold is hard to choose because it depends on the workload. The KIP for this feature must explain why a max-LSO-lag gauge is still needed next to that metric.
- Related, unrelated to this change: KAFKA-5543 (open): `LastStableOffsetLag` removal on partition move, already handled by `Partition.removeMetrics`. KIP-1021 / KAFKA-16282 is about LSO in `kafka-get-offsets.sh` only.

#### 2. Detailed Requirements

- **Functional Requirements:**
  - Add a yammer gauge `MaxLastStableOffsetLag` (proposed name, to be finalized in a KIP) registered in `ReplicaManager` under `kafka.server:type=ReplicaManager,name=MaxLastStableOffsetLag`. Value: `max(UnifiedLog.lastStableOffsetLag())` over partitions for which this broker is the leader (`leaderPartitionsIterator`), `0` when the broker leads no partition. Type: Long (the per-partition lag is a long).
  - Optional second gauge `PartitionsWithLastStableOffsetLagCount` (proposed name): number of led partitions with LSO lag > 0. Implement it only if it stays a few lines next to the max gauge; keep it in its own commit so it can be dropped.
  - Optional third candidate, `TotalLastStableOffsetLag` (sum): implement only as an experiment; the open questions below ask whether it is useful.
  - Leader-only semantics: the per-partition gauge reports on any hosted log (`log.map(...)`), including followers. The aggregate counts only partitions this broker leads, like `UnderMinIsrPartitionCount` and `PartitionsWithLateTransactionsCount`. Document this difference in code comments and docs.
  - Offline partitions (`HostedPartition.Offline`) are excluded by `onlinePartitionsIterator`. No partition-level state is added.
  - No per-partition tag on the aggregate. The existing per-partition gauge stays unchanged.
- **Public API Impact:** **Metric change (new JMX MBean names). NO accepted KIP. Explicit user override for this task.**
  - Benedikt Linse (the repo owner) decided on 2026-11-03 to start this work without an accepted KIP and to write the KIP later.
  - This task therefore **overrides** the KIP boundary in `.cursor/rules/pr-guardrails.mdc` ("A KIP is required before changing ... a metric ... stop at the boundary") and in `.cursor/skills/kip-scaffolder/SKILL.md` step 1 ("stop and name the gap"), **for this task and for the new metric names only**.
  - Do **not** stop at the metric boundary. Implement the gauge(s), tests and docs on the fork branch. Do not invent a KIP id. Mark every new name as "proposed, subject to KIP" in code comments, docs and the commit body.
  - The override does **not** extend to anything else that needs a KIP: no new config, no protocol/RPC change, no change to `org.apache.kafka.clients.*` or other public javadoc packages, no rename or removal of existing metrics. If one of those turns out to be necessary, stop and report.

#### 3. Guidance & Boundaries

- Use skill `.cursor/skills/kip-scaffolder` to execute this work, with the override from section 2 (skip the "stop if the KIP is not approved" gate; keep every other step).
- Enforce `.cursor/rules/kafka-style.mdc` for Checkstyle and Java/Scala style.
- Enforce `.cursor/rules/pr-guardrails.mdc` for the Java 11/17 module bounds and other change boundaries, except the KIP boundary for the new metric names as stated above.
- Enforce `.cursor/rules/kafka-testing.mdc` for unit and cluster test setup.
- Run local Checkstyle verification before marking complete.
- Do not commit on `trunk`. Work only on branch `KAFKA-8844-max-lso-lag-prototype` on `1123/kafka`.
- Do **not** push to `apache/kafka` (remote `up`), do **not** open an upstream pull request, and do **not** comment on, assign or transition KAFKA-8844 in JIRA. Do not send anything to dev@kafka.apache.org.
- Pushing the feature branch to `origin` (`1123/kafka`) is allowed only if the user asks for it in that session. Do not open a PR on the fork unless asked.
- Commit author: `Benedikt Linse <benedikt@ibm.com>`.
- Commit trailers (agent-assisted change):

  ```text
  Co-Authored-By: Cursor Agent <cursoragent@cursor.com>
  Generated-by: Cursor Agent
  ```
- Do not edit generated sources, Checkstyle/SpotBugs/Spotless config, `checkstyle/suppressions.xml` or import control files.

#### 4. Expected Output

1. Implementation in `:core`:
   - `core/src/main/scala/kafka/server/ReplicaManager.scala`: add `MaxLastStableOffsetLagMetricName` (and the optional count name) to the `ReplicaManager` companion object next to `PartitionsWithLateTransactionsCountMetricName` (line ~98), add them to `GaugeMetricNames` (line ~104), and register the gauges next to the other `metricsGroup.newGauge(...)` calls (lines ~237-246). `removeMetrics()` (line ~2213) already removes everything in `MetricNames`, so adding to `GaugeMetricNames` is enough for cleanup.
   - Add `private def maxLastStableOffsetLag: Long = leaderPartitionsIterator.map(_.leaderLogIfLocal.map(_.lastStableOffsetLag).getOrElse(0L)).maxOption.getOrElse(0L)`. Prefer a small `Partition` helper (for example `Partition.lastStableOffsetLagIfLeader: Long`) to avoid a double leader check; keep it package-visible, not public API.
2. Unit tests (section 7) and, if useful, one `@ClusterTest` extension.
3. Docs: rows in `docs/operations/monitoring.md` (section 8).
4. Commit message format: `KAFKA-8844: Add max LSO lag broker metric (prototype, no KIP)`. The commit body states: "Prototype on a fork feature branch to support a future KIP. Metric names are proposals, not approved. Not for upstream." Split the optional count/sum gauge into a separate commit: `KAFKA-8844: Add count of led partitions with LSO lag (prototype, no KIP)`.

#### 5. KIP status: none, prototype only

- No KIP exists. KAFKA-8844 has been open and unassigned since 2019 with label `needs-kip`. The owner will write the KIP later; this prototype provides the code, tests and measurements for it.
- Nothing in this task is an accepted interface. Metric names, MBean names, value semantics and the choice of aggregates are all proposals.
- Open questions a later KIP must answer:
  1. Is a max-lag aggregate useful given `PartitionsWithLateTransactionsCount` (KIP-664)? Which alerting use cases does it cover that the count does not (for example a slow-to-advance high watermark versus a hanging transaction)?
  2. Which aggregate(s): max only, max plus count of partitions with lag > 0, sum, or a histogram? What is the alert threshold guidance, since non-zero lag is normal while any transaction is open?
  3. Metric names and MBean placement: `kafka.server:type=ReplicaManager,name=MaxLastStableOffsetLag` versus a `kafka.cluster` or `BrokerTopicMetrics` home; naming consistency with `UnderMinIsrPartitionCount`, `AtMinIsrPartitionCount`, `PartitionsWithLateTransactionsCount`.
  4. Semantics: leader-only (proposed) versus all hosted replicas (the per-partition gauge reports followers too). Treatment of offline partitions, partitions in reassignment, future replicas, and read-only data after a log-dir failure.
  5. Should the metric be per topic or per log dir as well, or only broker-wide? Should the partition with the max lag be identifiable (for example via a log line, which a gauge cannot carry)?
  6. Cost: is scanning all led partitions on every metrics poll acceptable on brokers with very many partitions? Is a cached or event-driven value needed?
  7. Is the 5-minute padding style of KIP-664 relevant here, or should lag be time-based (age of the oldest open transaction) instead of offset-based?
  8. Compatibility and rollout: new metrics only, no removal; any release-note and `docs/getting-started/upgrade.md` entry; KRaft-only broker (this tree has no ZooKeeper mode).
  9. Test plan and documentation changes expected by reviewers.

#### 6. Implementation Notes

Verified against `apache/trunk` at `598caf0864` (2026-11-03). Re-check the line numbers on the branch base `origin/cursor-scaffolding`; the method and symbol names exist there.

- **Where the per-partition gauge is registered:** `core/src/main/scala/kafka/cluster/Partition.scala` line ~226: `metricsGroup.newGauge("LastStableOffsetLag", () => log.map(_.lastStableOffsetLag).getOrElse(0), tags)` with `tags = MetricsUtils.getTags("topic", topic, "partition", partitionId.toString)`. Removed in `Partition.removeMetrics(...)` (line ~140).
- **Where the lag is computed:** `storage/src/main/java/org/apache/kafka/storage/internals/log/UnifiedLog.java` line ~688: `lastStableOffsetLag() = highWatermark() - lastStableOffset()`. `lastStableOffset()` (line ~678) is `min(firstUnstableOffset, highWatermark)`; both inputs are volatile fields (`highWatermarkMetadata`, `firstUnstableOffsetMetadata`). It is O(1) with no locking. Note that `highWatermark()` is read twice, so a concurrent HW update can make a single reading slightly inconsistent; clamp the aggregate at `0` and do not add locking.
- **Metric framework:** the metric is a Yammer gauge created through `org.apache.kafka.server.metrics.KafkaMetricsGroup` (`server-common/src/main/java/org/apache/kafka/server/metrics/KafkaMetricsGroup.java`). `ReplicaManager` holds a `KafkaMetricsGroup` (`metricsGroup`) with package `kafka.server` and type `ReplicaManager`, so MBean names are `kafka.server:type=ReplicaManager,name=<Name>`. This is not the `org.apache.kafka.common.metrics` (KafkaMetrics) registry; do not use `Sensor`/`MetricName` from `:clients` here.
- **Where similar broker-level aggregates live:** `core/src/main/scala/kafka/server/ReplicaManager.scala`:
  - name constants and `GaugeMetricNames` / `MeterMetricNames` / `MetricNames`: lines ~91-120
  - gauge registration: lines ~237-246 (`LeaderCount`, `PartitionCount`, `OfflineReplicaCount`, `UnderReplicatedPartitions`, `UnderMinIsrPartitionCount`, `AtMinIsrPartitionCount`, `ReassigningPartitions`, `PartitionsWithLateTransactionsCount`, `ProducerIdCount`)
  - helpers `lateTransactionsCount` (line ~250), `onlinePartitionsIterator` (line ~476), `leaderPartitionsIterator` (line ~2118: `onlinePartitionsIterator.filter(_.leaderLogIfLocal.isDefined)`)
  - removal: `removeMetrics()` line ~2213 (`ReplicaManager.MetricNames.foreach(metricsGroup.removeMetric)`)
- **Cost:** the existing aggregates (`UnderMinIsrPartitionCount`, `PartitionsWithLateTransactionsCount`) already do a weakly consistent O(led partitions) scan per poll over `allPartitions` (a `ConcurrentHashMap`), so the same approach is consistent with the codebase. The per-partition read is two volatile reads, so cost is small. The scan runs only when a reporter or JMX client polls the gauge, not on the produce/fetch path.
  - Do **not** push-update an aggregate from the HW/LSO update path; that path is hot (every fetch can advance the HW) and a correct broker-wide max would need a per-partition structure or a lock.
  - If the prototype shows measurable cost at high partition counts (benchmark with e.g. 50k partitions, document the result for the KIP), the alternative to evaluate is a cached value refreshed by a scheduled task (for example every 1-5 s). Do not add a new config for the refresh interval without a KIP.
  - Compute max and count in a single pass if both are implemented, so the optional count gauge does not add a second scan per poll.
- **Leader-only semantics:** `leaderPartitionsIterator` uses `leaderLogIfLocal`. A follower's `LastStableOffsetLag` is not included. When a partition loses leadership between the filter and the read, the value is simply 0 for that partition, which is acceptable for a weakly consistent gauge.
- **Metric name conventions:** CamelCase names with the unit omitted; counts end in `Count` (`UnderMinIsrPartitionCount`); `ReplicaManager` gauges carry no tags. Proposed: `MaxLastStableOffsetLag` and `PartitionsWithLastStableOffsetLagCount`. Mark as proposals.
- **Do not** change the per-partition gauge, its tags, or `Partition.removeMetrics`.
- Do not add any new config key, RPC field, or class in a public javadoc package.

#### 7. Tests to Add

Follow `.cursor/rules/kafka-testing.mdc` (JUnit 5, Mockito, `MockTime`; `@ClusterTest` only if broker state is needed; do not stack `@ClusterTest` with `@ParameterizedTest`).

- `core/src/test/scala/unit/kafka/server/ReplicaManagerTest.scala`:
  - New test modeled on `testPartitionsWithLateTransactionsCount` (line ~739) using `setupReplicaManagerWithMockedPurgatories`, `createLeaderDelta`, `handleProduceAppend` with transactional records and the existing `yammerGaugeValue[T]` helper (line ~6239): assert `MaxLastStableOffsetLag` is `0` with no partitions; stays `0` with a leader partition and no open transaction; becomes `> 0` once a transaction is open and the HW advances past the first unstable offset; returns to `0` after the commit/abort marker is appended and the HW advances; and is `0` again after the replica becomes a follower (leader-only semantics).
  - Two leader partitions with different lags: the gauge equals the larger one (and, if implemented, the count equals 2 when both are > 0 and 1 when only one is).
  - `checkRemoveMetricsCountMatchRegisterCount` (line ~517) already iterates `ReplicaManager.GaugeMetricNames`; keep it green after adding the new names to `GaugeMetricNames` (it uses `verifyNoMoreInteractions` on the mocked metrics group, so every newly registered gauge must be in the set).
- `core/src/test/scala/unit/kafka/server/BrokerMetricNamesTest.scala` (`@ClusterTest`): iterates `ReplicaManager.MetricNames` and asserts exactly one MBean `kafka.server:type=ReplicaManager,name=<Name>` per name, so it covers the new gauges once they are in the set. No new test needed; run it.
- `core/src/test/scala/unit/kafka/cluster/PartitionTest.scala`: `testAddAndRemoveMetrics` (line ~2831) asserts the per-partition metric list (`LastStableOffsetLag` is in it). It must stay unchanged and green. Add a test for the optional `Partition` helper (if introduced), including the follower case returning 0.
- `clients/clients-integration-tests/.../MetricsDuringTopicCreationDeletionTest.java`: uses the ReplicaManager gauges `UnderReplicatedPartitions` and `OfflinePartitionsCount`; no change expected, but run it if the metrics code path was touched.
- `storage`: `UnifiedLog.lastStableOffsetLag` has no dedicated test today; add none unless `UnifiedLog` is touched.

Run:

```bash
./gradlew :core:spotlessApply
scripts/simulate_ci.sh --module core --tests kafka.server.ReplicaManagerTest
scripts/simulate_ci.sh --module core --tests kafka.server.BrokerMetricNamesTest
scripts/simulate_ci.sh --module core --tests kafka.cluster.PartitionTest
```

Fix Checkstyle and SpotBugs findings in the new code.

#### 8. Documentation

- `docs/operations/monitoring.md`: add rows to the broker metrics table next to `kafka.server:type=ReplicaManager,name=UnderMinIsrPartitionCount` / `AtMinIsrPartitionCount` (lines ~516-540) and `ReassigningPartitions` (line ~945). Use the same `<tr><td>` shape as the neighboring rows: description, MBean `kafka.server:type=ReplicaManager,name=MaxLastStableOffsetLag`, and a note "Maximum of (high watermark - last stable offset) across partitions led by this broker. A value that keeps growing indicates a stuck transaction. Prototype metric, name subject to KIP."
- The per-partition `LastStableOffsetLag` and `PartitionsWithLateTransactionsCount` are not listed in `docs/operations/monitoring.md` on trunk today. Optionally add rows for them in the same edit, in a separate commit, since the aggregate is hard to interpret without them.
- `docs/getting-started/upgrade.md`: **do not** add an entry for the prototype; a "Notable changes" line belongs to the eventual upstream change after a KIP is accepted. Mention this in the final report only.

#### 9. Acceptance Criteria

- `kafka.server:type=ReplicaManager,name=MaxLastStableOffsetLag` is registered at broker start, removed at `ReplicaManager.shutdown`, and equals the max per-partition `LastStableOffsetLag` over led partitions in the new unit test.
- The optional count/sum gauges, if present, are in separate commits and listed in `GaugeMetricNames`.
- `ReplicaManagerTest`, `BrokerMetricNamesTest` and `PartitionTest` pass; `./gradlew :core:spotlessApply` and `scripts/simulate_ci.sh --module core` are green (Checkstyle, SpotBugs).
- No change to the per-partition metric, to any config, to any RPC, or to a public javadoc package.
- Code comments, docs and commit body label the names as proposals pending a KIP and say the change is a prototype.
- All commits are on `KAFKA-8844-max-lso-lag-prototype`, authored by `Benedikt Linse <benedikt@ibm.com>`, with the `Co-Authored-By` and `Generated-by` trailers; nothing on `trunk`; nothing pushed to `apache/kafka`; no upstream PR; no JIRA activity.
- The final report lists: the files touched, the benchmark or reasoning on poll cost, and the answers/evidence collected for the open questions in section 5.
