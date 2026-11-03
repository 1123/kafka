Follow the kip-scaffolder skill.

### TASK: Prototype KAFKA-8844: Add a broker-level aggregate of LastStableOffsetLag (max LSO lag)

#### 1. Context & Objective

- **Ticket / KIP:** KAFKA-8844, https://issues.apache.org/jira/browse/KAFKA-8844
- **KIP status:** none, prototype only (the JIRA carries the label `needs-kip`). See "Open KIP questions" in section 3.
- **Summary:** Kafka exposes a per-partition gauge `LastStableOffsetLag` (`kafka.cluster:type=Partition,name=LastStableOffsetLag,topic=*,partition=*`). Add a broker-level gauge that aggregates it across the partitions this broker leads, so operators can alert on one number for stuck or hanging transactions instead of collecting per-partition metrics. Primary aggregate: maximum LSO lag. Optional: count of led partitions with lag > 0.
- **Target Module:** :core (`kafka.server.ReplicaManager`). `:storage` (`UnifiedLog`) is read-only unless a cheaper accessor turns out to be needed.
- **Author:** Benedikt Linse <benedikt@ibm.com>
- **Branch:** `KAFKA-8844-max-lso-lag-prototype`
- **Prototype exception:** This change deliberately starts without an accepted KIP, by decision of the repo owner (2026-11-03). It is a **prototype/draft on a fork feature branch (`1123/kafka`) to support a future KIP. It is not for an upstream pull request.** The KIP gate is **lifted only for these new metric names**, which are proposals subject to the KIP:
  - `kafka.server:type=ReplicaManager,name=MaxLastStableOffsetLag`
  - `kafka.server:type=ReplicaManager,name=PartitionsWithLastStableOffsetLagCount` (optional)
  - `kafka.server:type=ReplicaManager,name=TotalLastStableOffsetLag` (optional experiment)

  Do not stop at the metric boundary for these names. Everything else that needs a KIP still stops the work: new configs, protocol/RPC changes, public javadoc packages, renaming or removing existing metrics. No JIRA comment, no mail to dev@.

JIRA facts (checked 2026-11-03):

- KAFKA-8844 "Add max lso lag metric": Open, unassigned, reporter Jason Gustafson, created 2019-08-28, never updated, no comments, no linked PR, no fix version. Description: "We have a per-partition metric to monitor the LSO lag. This allows us to track the difference between the high watermark and the LSO, which is useful to detect transactions which are stalled. It would be helpful to have a gauge to monitor the maximum lag across all partitions so that users don't need to collect per-partition metrics."
- No KIP and no upstream PR found for it.
- Related and already shipped: KIP-664 / KAFKA-13221 added the broker gauge `PartitionsWithLateTransactionsCount` (3.2.0). KIP-664 itself notes that an LSO-lag alert threshold is hard to choose because it depends on the workload.
- Not related to this change: KAFKA-5543 (open; metric removal on partition move is already handled in `Partition.removeMetrics`); KIP-1021 / KAFKA-16282 (LSO in `kafka-get-offsets.sh`).

#### 2. Detailed Requirements

- **Functional Requirements:**
  - Gauge `MaxLastStableOffsetLag` (Long) in `ReplicaManager`: the max of `UnifiedLog.lastStableOffsetLag()` over partitions this broker leads (`leaderPartitionsIterator`), clamped at 0, and 0 when the broker leads no partition.
  - Optional `PartitionsWithLastStableOffsetLagCount`: number of led partitions with lag > 0. Compute max and count in a single pass if both exist. Keep it in its own commit so it can be dropped.
  - Optional `TotalLastStableOffsetLag` (sum): experiment only, in its own commit.
  - Leader-only semantics. The per-partition gauge reports any hosted log, including followers; the aggregate counts only led partitions, like `UnderMinIsrPartitionCount` and `PartitionsWithLateTransactionsCount`. Say so in a code comment and in the docs row.
  - Offline partitions are excluded (`onlinePartitionsIterator`). No per-partition tags on the aggregate. No change to the per-partition gauge.
- **Non-goals:** no new config (no refresh-interval key), no RPC or protocol change, no change to `Partition.removeMetrics` or to the per-partition gauge, no upgrade note, no upstream branch, no KIP document.
- **Public API Impact:** new JMX metric names only, covered by the prototype exception above.

#### 3. Pointers & Tests

**Code paths** (verified on `apache/trunk` `598caf0864`, 2026-11-03; line numbers approximate):

- Per-partition gauge: `core/src/main/scala/kafka/cluster/Partition.scala` registers it at ~226 (`log.map(_.lastStableOffsetLag).getOrElse(0)`) and removes it at ~140.
- Lag: `storage/src/main/java/org/apache/kafka/storage/internals/log/UnifiedLog.java` ~688, `highWatermark() - lastStableOffset()`. Both inputs are volatile fields; no locking. `highWatermark()` is read twice, so a single reading can be slightly inconsistent; clamp at 0, do not add locking.
- Aggregate home: `core/src/main/scala/kafka/server/ReplicaManager.scala`.
  - name constants ~91-102, `GaugeMetricNames` ~104, gauge registration ~237-246, `lateTransactionsCount` ~250, `leaderPartitionsIterator` ~2118, `removeMetrics()` ~2213 (removes everything in `MetricNames`, so adding to `GaugeMetricNames` is enough).
  - `metricsGroup` is a `KafkaMetricsGroup` (`server-common/src/main/java/org/apache/kafka/server/metrics/KafkaMetricsGroup.java`) with package `kafka.server`, type `ReplicaManager`. This is a Yammer gauge, not the `:clients` `Sensor` registry.
- Suggested shape: a small package-visible helper on `Partition` (for example `lastStableOffsetLagIfLeader: Long`) so the leader check happens once, and `private def maxLastStableOffsetLag: Long` in `ReplicaManager` built on `leaderPartitionsIterator`.
- Names: CamelCase, counts end in `Count`, `ReplicaManager` gauges carry no tags.

**Cost notes:**

- Existing aggregates already do a weakly consistent O(led partitions) scan per poll over `allPartitions`; two volatile reads per partition; nothing runs on the produce/fetch path. Use the same approach.
- Do not update an aggregate from the HW/LSO update path (hot, and a correct max needs per-partition state or a lock).
- Measure poll cost with a large partition count (for example 50k) and record the result for the KIP. Only if it is too slow, evaluate a cached value refreshed by a scheduled task, and do not add a config for it.

**Tests to add:**

- `core/src/test/scala/unit/kafka/server/ReplicaManagerTest.scala`: new test modeled on `testPartitionsWithLateTransactionsCount` (~739), using `setupReplicaManagerWithMockedPurgatories`, `createLeaderDelta`, `handleProduceAppend` with transactional records, and `yammerGaugeValue` (~6239). Assert `MaxLastStableOffsetLag`:
  - is 0 with no partitions, and with a leader partition without an open transaction;
  - is > 0 once a transaction is open and the high watermark has advanced past its first offset;
  - returns to 0 after the commit/abort marker is appended and the high watermark advances;
  - is 0 again after the replica becomes a follower (leader-only);
  - equals the larger value with two leader partitions of different lag (and the count equals 2 or 1 accordingly, if implemented).
- Keep green: `checkRemoveMetricsCountMatchRegisterCount` (~520; `verifyNoMoreInteractions` means every registered gauge must be in `GaugeMetricNames`); `BrokerMetricNamesTest` (`@ClusterTest`, iterates `ReplicaManager.MetricNames`, covers the new MBeans automatically); `PartitionTest.testAddAndRemoveMetrics` (~2833, unchanged; add a test only for the optional `Partition` helper, including the follower case).
- Run: `kafka.server.ReplicaManagerTest`, `kafka.server.BrokerMetricNamesTest`, `kafka.cluster.PartitionTest`.

**Docs:** `docs/operations/monitoring.md`, rows next to `UnderMinIsrPartitionCount` / `AtMinIsrPartitionCount` (~516-540) in the same `<tr><td>` markup. Description: "Maximum of (high watermark - last stable offset) across partitions led by this broker. A value that keeps growing indicates a stuck transaction. Prototype metric, name subject to KIP." Optionally, in a separate commit, add rows for the per-partition `LastStableOffsetLag` and `PartitionsWithLateTransactionsCount`, which are not documented there today.

**Open KIP questions** (the later KIP must answer these):

1. Is a max-lag aggregate useful given `PartitionsWithLateTransactionsCount` (KIP-664)? Which cases does it cover that the count does not (for example a slow-advancing high watermark versus a hanging transaction)?
2. Which aggregate(s): max only, max plus count of partitions with lag > 0, sum, or a histogram? What alert threshold guidance, given that non-zero lag is normal while any transaction is open?
3. Names and MBean placement: `kafka.server:type=ReplicaManager,name=MaxLastStableOffsetLag` versus a `kafka.cluster` or `BrokerTopicMetrics` home; consistency with `UnderMinIsrPartitionCount`, `AtMinIsrPartitionCount`, `PartitionsWithLateTransactionsCount`.
4. Semantics: leader-only (proposed) versus all hosted replicas. Treatment of offline partitions, reassigning partitions, future replicas, and log-dir failure.
5. Also per topic or per log dir, or broker-wide only? Can the worst partition be identified (a gauge cannot carry it; a log line could)?
6. Cost: is a scan of all led partitions per poll acceptable at very high partition counts, or is a cached value needed?
7. Should lag be time-based (age of the oldest open transaction) instead of offset-based, and is KIP-664's 5-minute padding relevant?
8. Compatibility and rollout: new metrics only; release note and `docs/getting-started/upgrade.md` entry once a KIP is accepted.
9. Test plan and documentation reviewers will expect.

#### 4. Expected Output

1. `MaxLastStableOffsetLag` registered at broker start, removed at `ReplicaManager.shutdown`, and equal to the max per-partition `LastStableOffsetLag` over led partitions in the new unit test. The optional count and sum gauges, if present, are in separate commits and listed in `GaugeMetricNames`.
2. The tests and docs row above.
3. Commit subject: `KAFKA-8844: Add max LSO lag broker metric (prototype, no KIP)`. The body says: "Prototype on a fork feature branch to support a future KIP. Metric names are proposals, not approved. Not for upstream." Optional gauges: `KAFKA-8844: Add count of led partitions with LSO lag (prototype, no KIP)`.
4. Acceptance criteria: no change to the per-partition metric, any config, any RPC, or any public javadoc package; code comments, docs, and commit body label the names as proposals pending a KIP; the final report lists the files touched, the poll-cost measurement or reasoning, and the evidence collected for the open questions above.
