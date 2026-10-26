### TASK: Implement [JIRA-ID / KIP-Number]: [Brief Title]

#### 1. Context & Objective

- **Ticket / KIP:** [e.g., KAFKA-14920 or KIP-912]
- **Summary:** [1-2 sentences summarizing what needs to be added, fixed, or updated]
- **Target Module:** [e.g., :clients, :core, :storage, or Auto-Detect]

#### 2. Detailed Requirements

- **Functional Requirements:**
  - [Requirement 1: e.g., Add new fetch metric `records-lag-max`]
  - [Requirement 2: e.g., Expose configuration key `fetch.max.wait.ms`]
- **Public API Impact:** [None / Approved KIP-XXX attached / Needs KIP check]

#### 3. Guidance & Boundaries

- Use skill `.cursor/skills/kip-scaffolder` to execute this work.
- Enforce `.cursor/rules/kafka-style.mdc` for Checkstyle and Java style.
- Enforce `.cursor/rules/pr-guardrails.mdc` for the Java 11/17 module bounds and other change boundaries.
- Enforce `.cursor/rules/kafka-testing.mdc` for unit and cluster test setup.
- Run local Checkstyle verification before marking complete.
- Do not commit on `trunk`.

#### 4. Expected Output

1. Implementation in the designated target module.
2. Unit test covering edge cases, and a cluster integration test if broker state is required.
3. Commit message format: `[JIRA-ID]: [Brief description]` (or `MINOR: [Brief description]` if no JIRA exists).
