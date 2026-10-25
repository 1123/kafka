# Cursor & Grok Bot setup guide

This branch lives on the personal fork `1123/kafka`. Keep it here. Do not push it to `apache/kafka` and do not open a pull request against upstream. The Apache Kafka README below this section is unchanged.

## Problem

Agent edits in this tree miss Kafka's real gates: the Checkstyle header and complexity rules, Spotless import order, SLF4J, the Java 11/17 split, Apache RAT, and the `ClusterTest` / `ClusterInstance` framework. The fork adds a Cursor working agreement for those gates, plus a small Grok Bot so a person can approve work before a cloud agent starts.

## What was added

| Path | Role |
| --- | --- |
| `.cursor/rules/kafka-style.mdc` | Java style, Checkstyle limits, SLF4J, ASF header |
| `.cursor/rules/kafka-testing.mdc` | JUnit 5, Mockito, `@ClusterTest` / `ClusterInstance` |
| `.cursor/rules/pr-guardrails.mdc` | Public API, generated code, release tooling, compatibility |
| `.cursor/skills/kip-scaffolder/` | KIP or JIRA to code, including `template.java` |
| `grok-bot/` | FastAPI dashboard, Slack webhook, approve / reject, agent launch |
| `scripts/cursor_api_trigger.py` | `POST https://api.cursor.com/v1/agents` |
| `scripts/simulate_ci.sh` | Checkstyle, SpotBugs, and an optional test filter |
| `.github/workflows/agent-guardrails.yml` | Checkstyle on modules a pull request changed |

`.cursor/**` is excluded from Apache RAT because Cursor rules and skills must start with YAML frontmatter. `template.java` still has the ASF header. It sits outside every Gradle source set (`src/main/java`, `src/test/java`), so `checkstyleMain` and `compileJava` leave it alone until the KIP skill copies it into a module.

## Run Grok Bot

```bash
python3 -m venv /tmp/grok-bot-venv
/tmp/grok-bot-venv/bin/pip install -r grok-bot/requirements.txt
cd grok-bot
export SLACK_SIGNING_SECRET=dev-secret
export CURSOR_API_KEY=...
export GROK_DEFAULT_REPO=https://github.com/1123/kafka
export GROK_DEFAULT_REF=trunk
uvicorn app:app --host 127.0.0.1 --port 8000
```

`CURSOR_API_KEY` is required only when a launch is requested. `SLACK_SIGNING_SECRET` is required for `POST /slack/events`. Both stay in the environment.

Open http://127.0.0.1:8000 . Slack slash commands and Events API callbacks post to `/slack/events`. The app checks `X-Slack-Signature` (v0 HMAC-SHA256, five-minute window). A URL verification handshake returns the challenge. An `app_mention` or slash command queues an approval and does not start an agent.

`POST /api/approvals/{id}/approve`, `POST /api/approvals/{id}/reject`, and `POST /api/agents` are the dashboard controls. A launch runs `scripts/cursor_api_trigger.py`.

## Dashboard mock

The page is one column on a warm background:

1. A banner that the store is in-memory and seeded rows are mocks.
2. A status strip: whether the Cursor API key and Slack signing secret are set (booleans only), plus the default repo and ref.
3. **Pending**, seeded with `apr-1001` "Share-group lag metric" (source `mock`). Approve records the decision. Approve and launch also calls the Cursor API.
4. **Decided**, seeded with `apr-1002` "Edit generated MetadataResponse.java", already rejected, because generated sources are regenerated rather than edited.
5. **Agent runs**, seeded with `agent-1001` in status `not_sent`. That row was never sent to `api.cursor.com`.
6. Forms to queue an approval and to launch an agent (prompt, repo, ref).

The layout is this page. Reload it after `uvicorn` is up.

## Local CI subset

```bash
scripts/simulate_ci.sh
scripts/simulate_ci.sh --module clients --skip-spotbugs
scripts/simulate_ci.sh --module clients --tests org.apache.kafka.clients.MetadataTest
scripts/simulate_ci.sh --with-rat
```

`build.yml` runs `./gradlew check releaseTarGz -x test` on Java 25. This helper runs `checkstyleMain`, `checkstyleTest`, and `spotbugsMain` (the checks inside `check` that catch agent patches). The release tarball stays on that validate job. `--tests` adds a Gradle `--tests` filter.

## Launch a cloud agent

```bash
export CURSOR_API_KEY=...
python3 scripts/cursor_api_trigger.py \
  --repo https://github.com/1123/kafka \
  --ref trunk \
  --prompt "Describe the broker startup path. Do not open a pull request."
```

The script calls `POST /v1/agents` on `https://api.cursor.com`, as documented at <https://cursor.com/docs/cloud-agent/api/endpoints> (Basic auth, API key as the username, empty password). The body sets `autoCreatePR` and `workOnCurrentBranch` to false. `--dry-run` prints the JSON and does not use the network. The API is a public beta.

## Agent guardrails workflow

`.github/workflows/agent-guardrails.yml` runs on `pull_request` and `workflow_dispatch` with `contents: read`. It uses `.github/actions/setup-gradle` (Temurin 17, the action's broker baseline) and runs Checkstyle for Gradle projects whose `src/` or build files changed. A root `*.gradle`, `gradle/`, or `checkstyle/` diff runs Checkstyle for the whole build. SpotBugs runs when the dispatch input `run-spotbugs` is true, because SpotBugs compiles main sources and the main CI job already runs it. `ci.yml` and `build.yml` are unchanged. GitHub shows `workflow_dispatch` in the Actions tab once this file is on the repository default branch.

---

<p align="center">
<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/images/kafka-logo-readme-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/kafka-logo-readme-dark.svg">
  <img src="docs/images/kafka-logo-readme-light.svg" alt="Kafka Logo" width="50%"> 
</picture>
</p>

[![CI](https://github.com/apache/kafka/actions/workflows/ci.yml/badge.svg?branch=trunk&event=push)](https://github.com/apache/kafka/actions/workflows/ci.yml?query=event%3Apush+branch%3Atrunk)
[![Flaky Test Report](https://github.com/apache/kafka/actions/workflows/generate-reports.yml/badge.svg?branch=trunk&event=schedule)](https://github.com/apache/kafka/actions/workflows/generate-reports.yml?query=event%3Aschedule+branch%3Atrunk)

[**Apache Kafka**](https://kafka.apache.org) is an open-source distributed event streaming platform used by thousands of companies for high-performance data pipelines, streaming analytics, data integration, and mission-critical applications.

You need to have [Java](http://www.oracle.com/technetwork/java/javase/downloads/index.html) installed.

We build and test Apache Kafka with Java versions 17 and 25. The `release` parameter in javac is set to `11` for the clients 
and streams modules, and `17` for the rest, ensuring compatibility with their respective
minimum Java versions. Similarly, the `release` parameter in scalac is set to `11` for the streams modules and `17`
for the rest.

Scala 2.13 is the only supported version in Apache Kafka.

### Build a JAR and run it
```bash
./gradlew jar
```

Follow instructions in https://kafka.apache.org/quickstart

### Build source JAR
```bash
./gradlew srcJar
```

### Build aggregated javadoc
```bash
./gradlew aggregatedJavadoc --no-parallel
```

### Build javadoc and scaladoc
```bash
./gradlew javadoc
./gradlew javadocJar # builds a javadoc jar for each module
./gradlew scaladoc
./gradlew scaladocJar # builds a scaladoc jar for each module
./gradlew docsJar # builds both (if applicable) javadoc and scaladoc jars for each module
```

### Run unit/integration tests
```bash
./gradlew test  # runs both unit and integration tests
./gradlew unitTest
./gradlew integrationTest
./gradlew test -Pkafka.test.run.flaky=true  # runs tests that are marked as flaky
```

### Force re-running tests without code change
```bash
./gradlew test --rerun-tasks
./gradlew unitTest --rerun-tasks
./gradlew integrationTest --rerun-tasks
```

### Running a particular unit/integration test
```bash
./gradlew clients:test --tests RequestResponseTest
./gradlew streams:integration-tests:test --tests RestoreIntegrationTest
```

### Running a particular unit/integration test N times
```bash
N=500; I=0; while [ $I -lt $N ] && ./gradlew clients:test --tests RequestResponseTest --rerun --fail-fast; do (( I=$I+1 )); echo "Completed run: $I"; sleep 1; done
```

### Running a particular test method within a unit/integration test
```bash
./gradlew clients:test --tests org.apache.kafka.clients.MetadataTest.testTimeToNextUpdate
./gradlew clients:clients-integration-tests:test --tests org.apache.kafka.clients.producer.ProducerFailureHandlingTest.testCannotSendToInternalTopic
./gradlew streams:integration-tests:test --tests org.apache.kafka.streams.integration.RestoreIntegrationTest.shouldRestoreNullRecord
```

### Running a particular unit/integration test with log4j output
By default, there will be only a small number of logs output while testing. You can adjust it by changing the `log4j2.yaml` file in the module's `src/test/resources` directory.

For example, if you want to see more logs for clients project tests, you can modify [the line](https://github.com/apache/kafka/blob/trunk/clients/src/test/resources/log4j2.yaml#L35) in `clients/src/test/resources/log4j2.yaml` 
to `level: INFO` and then run:

```bash
./gradlew cleanTest clients:test --tests NetworkClientTest
```

And you should see `INFO` level logs in the file under the `clients/build/test-results/test` directory.

### Specifying test retries
Retries are disabled by default, but you can set maxTestRetryFailures and maxTestRetries to enable retries.

The following example declares -PmaxTestRetries=1 and -PmaxTestRetryFailures=3 to enable a failed test to be retried once, with a total retry limit of 3.

```bash
./gradlew test -PmaxTestRetries=1 -PmaxTestRetryFailures=3
```

See [Test Retry Gradle Plugin](https://github.com/gradle/test-retry-gradle-plugin) and [build.yml](.github/workflows/build.yml) for more details.

### Generating test coverage reports
Generate coverage reports for the whole project:

```bash
./gradlew reportCoverage -PenableTestCoverage=true -Dorg.gradle.parallel=false
```

Generate coverage for a single module, i.e.: 

```bash
./gradlew clients:reportCoverage -PenableTestCoverage=true -Dorg.gradle.parallel=false
```

Coverage reports are located within the module's build directory, categorized by module type:

Core Module (:core): `core/build/reports/scoverageTest/index.html`

Other Modules: `<module>/build/reports/jacoco/test/html/index.html`

### Building a binary release gzipped tarball
```bash
./gradlew clean releaseTarGz
```

The release file can be found inside `./core/build/distributions/`.

### Building auto-generated messages
Sometimes it is only necessary to rebuild the RPC auto-generated message data when switching between branches, as they could
fail due to code changes. You can just run:

```bash
./gradlew processMessages processTestMessages
```

See [Apache Kafka Message Definitions](clients/src/main/resources/common/message/README.md) for details on Apache Kafka message protocol.

### Running a Kafka broker

Using compiled files:

```bash
KAFKA_CLUSTER_ID="$(./bin/kafka-storage.sh random-uuid)"
./bin/kafka-storage.sh format --standalone -t $KAFKA_CLUSTER_ID -c config/server.properties
./bin/kafka-server-start.sh config/server.properties
```

Using docker image:

```bash
docker run -p 9092:9092 apache/kafka:latest
```

See [docker/README.md](docker/README.md) for detailed information.

### Cleaning the build
```bash
./gradlew clean
```

### Running a task for a specific project
This is for `core`, `examples` and `clients`

```bash
./gradlew core:jar
./gradlew core:test
```

Streams has multiple sub-projects, but you can run all the tests:

```bash
./gradlew :streams:testAll
```

### Listing all gradle tasks
```bash
./gradlew tasks
```

### Building IDE project
*Note: Please ensure that JDK 17 is used when developing Kafka.*

IntelliJ supports Gradle natively, and it will automatically check Java syntax and compatibility for each module, even if
the Java version shown in the `Structure > Project Settings > Modules` may not be the correct one.

When it comes to Eclipse, run:

```bash
./gradlew eclipse
```

The `eclipse` task has been configured to use `${project_dir}/build_eclipse` as Eclipse's build directory. Eclipse's default
build directory (`${project_dir}/bin`) clashes with Kafka's scripts directory, and we don't use Gradle's build directory
to avoid known issues with this configuration.

### Publishing the streams quickstart archetype artifact to maven
For the Streams archetype project, one cannot use gradle to upload to maven; instead the `mvn deploy` command needs to be called at the quickstart folder:

```bash
cd streams/quickstart
mvn deploy
```

Please note for this to work you should create/update user maven settings (typically, `${USER_HOME}/.m2/settings.xml`) to assign the following variables

    <settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
       xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
       xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0
                           https://maven.apache.org/xsd/settings-1.0.0.xsd">
    ...                           
    <servers>
       ...
       <server>
          <id>apache.snapshots.https</id>
          <username>${maven_username}</username>
          <password>${maven_password}</password>
       </server>
       <server>
          <id>apache.releases.https</id>
          <username>${maven_username}</username>
          <password>${maven_password}</password>
        </server>
        ...
     </servers>
     ...

### Installing all projects to the local Maven repository

```bash
./gradlew -PskipSigning=true publishToMavenLocal
```

### Installing specific projects to the local Maven repository

```bash
./gradlew -PskipSigning=true :streams:publishToMavenLocal
```

### Building the test JAR
```bash
./gradlew testJar
```

### Running code quality checks
There are two code quality analysis tools that we regularly run, SpotBugs and Checkstyle.

#### Checkstyle
Checkstyle enforces a consistent coding style in Kafka.
You can run Checkstyle using:

```bash
./gradlew checkstyleMain checkstyleTest spotlessCheck
```

The Checkstyle warnings will be found in `reports/checkstyle/reports/main.html` and `reports/checkstyle/reports/test.html` files in the
subproject build directories. They are also printed to the console. The build will fail if Checkstyle fails.
For experiments (or regression testing purposes) add `-PcheckstyleVersion=X.y.z` switch (to override project-defined checkstyle version).

#### Spotless
The import order is a part of static check. Please call `spotlessApply` to optimize Java imports before filing a pull request.

```bash
./gradlew spotlessApply
```

#### SpotBugs
SpotBugs uses static analysis to look for bugs in the code.
You can run SpotBugs using:

```bash
./gradlew spotbugsMain spotbugsTest -x test
```

The SpotBugs warnings will be found in `reports/spotbugs/main.html` and `reports/spotbugs/test.html` files in the subproject build
directories.  Use -PxmlSpotBugsReport=true to generate an XML report instead of an HTML one.

### JMH microbenchmarks
We use [JMH](https://openjdk.java.net/projects/code-tools/jmh/) to write microbenchmarks that produce reliable results in the JVM.

See [jmh-benchmarks/README.md](https://github.com/apache/kafka/blob/trunk/jmh-benchmarks/README.md) for details on how to run the microbenchmarks.

### Dependency Analysis

The gradle [dependency debugging documentation](https://docs.gradle.org/current/userguide/viewing_debugging_dependencies.html) mentions using the `dependencies` or `dependencyInsight` tasks to debug dependencies for the root project or individual subprojects.

Alternatively, use the `allDeps` or `allDepInsight` tasks for recursively iterating through all subprojects:

```bash
./gradlew allDeps

./gradlew allDepInsight --configuration runtimeClasspath --dependency com.fasterxml.jackson.core:jackson-databind
```

These take the same arguments as the built-in variants.

### Determining if any dependencies could be updated
```bash
./gradlew dependencyUpdates
```

### Common build options ###

The following options should be set with a `-P` switch, for example `./gradlew -PmaxParallelForks=1 test`.

* `commitId`: sets the build commit ID as .git/HEAD might not be correct if there are local commits added for build purposes.
* `mavenUrl`: sets the URL of the maven deployment repository (`file://path/to/repo` can be used to point to a local repository).
* `maxParallelForks`: maximum number of test processes to start in parallel. Defaults to the number of processors available to the JVM.
* `maxScalacThreads`: maximum number of worker threads for the scalac backend. Defaults to the lowest of `8` and the number of processors
available to the JVM. The value must be between 1 and 16 (inclusive). 
* `ignoreFailures`: ignore test failures from junit
* `showStandardStreams`: shows standard output and standard error of the test JVM(s) on the console.
* `skipSigning`: skips signing of artifacts.
* `testLoggingEvents`: unit test events to be logged, separated by comma. For example `./gradlew -PtestLoggingEvents=started,passed,skipped,failed test`.
* `xmlSpotBugsReport`: enable XML reports for SpotBugs. This also disables HTML reports as only one can be enabled at a time.
* `maxTestRetries`: maximum number of retries for a failing test case.
* `maxTestRetryFailures`: maximum number of test failures before retrying is disabled for subsequent tests.
* `enableTestCoverage`: enables test coverage plugins and tasks, including bytecode enhancement of classes required to track said
coverage. Note that this introduces some overhead when running tests and hence why it's disabled by default (the overhead
varies, but 15-20% is a reasonable estimate).
* `keepAliveMode`: configures the keep-alive mode for the Gradle compilation daemon - reuse improves start-up time. The values should 
be one of `daemon` or `session` (the default is `daemon`). `daemon` keeps the daemon alive until it's explicitly stopped while
`session` keeps it alive until the end of the build session. This currently only affects the Scala compiler, see
https://github.com/gradle/gradle/pull/21034 for a PR that attempts to do the same for the Java compiler.
* `scalaOptimizerMode`: configures the optimizing behavior of the Scala compiler, the value should be one of `none`, `method`, `inline-kafka` or
`inline-scala` (the default is `inline-kafka`). `none` is the Scala compiler default, which only eliminates unreachable code. `method` also
includes method-local optimizations. `inline-kafka` adds inlining of methods within the kafka packages. Finally, `inline-scala` also
includes inlining of methods within the scala library (which avoids lambda allocations for methods like `Option.exists`). `inline-scala` is
only safe if the Scala library version is the same at compile time and runtime. Since we cannot guarantee this for all cases (for example, users
may depend on the kafka jar for integration tests where they may include a scala library with a different version), we don't enable it by
default. See https://www.lightbend.com/blog/scala-inliner-optimizer for more details.

### Upgrading Gradle version

See [gradle/wrapper/README.md](gradle/wrapper/README.md) for instructions on upgrading the Gradle version.

### Running system tests

See [tests/README.md](tests/README.md).

### Using Trogdor for testing

We use Trogdor as a test framework for Apache Kafka. You can use it to run benchmarks and other workloads.

See [trogdor/README.md](trogdor/README.md).

### Running in Vagrant

See [vagrant/README.md](vagrant/README.md).

### Kafka client examples

See [examples/README.md](examples/README.md).

### Contribution

Apache Kafka is interested in building the community; we would welcome any thoughts or [patches](https://issues.apache.org/jira/browse/KAFKA). You can reach us [on the Apache mailing lists](http://kafka.apache.org/contact.html).

To contribute follow the instructions here:
 * https://kafka.apache.org/contributing.html
