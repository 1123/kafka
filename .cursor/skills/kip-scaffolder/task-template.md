Follow the kip-scaffolder skill.

### TASK: Implement [JIRA-ID / KIP-Number]: [Brief Title]

#### 1. Context & Objective

- **Ticket / KIP:** [e.g., KAFKA-14920 or KIP-912, with link]
- **KIP status:** [Accepted KIP-XXX | Needs KIP check | None]
- **Summary:** [1-2 sentences summarizing what needs to be added, fixed, or updated]
- **Target Module:** [e.g., :clients, :core, :storage, or Auto-Detect]
- **Author:** [Your Name <you@example.com>; required for cloud agents, optional locally where `git config` is used]
- **Branch:** [optional, e.g., KAFKA-14920-short-name]
- **Prototype exception:** [optional. Omit unless the work deliberately starts without an accepted KIP. If used, name the exact metrics, configs, or interfaces exempt from the KIP gate and state that the change is a fork-only prototype that supports a future KIP.]

#### 2. Detailed Requirements

- **Functional Requirements:**
  - [Requirement 1: e.g., Add new fetch metric `records-lag-max`]
  - [Requirement 2: e.g., Expose configuration key `fetch.max.wait.ms`]
- **Non-goals:** [what this change must not do]
- **Public API Impact:** [None / Approved KIP-XXX attached / Needs KIP check / Prototype exception above]

#### 3. Pointers & Tests (optional)

- **Files / code paths:** [ticket-specific classes, methods, and docs to touch]
- **Tests to add:** [ticket-specific cases; name existing tests to extend or keep green]
- **Open questions:** [only for a prototype exception: what the later KIP must answer]

#### 4. Expected Output

1. Implementation in the designated target module.
2. Unit tests covering the edge cases above, and a cluster integration test if broker state is required.
3. Commit subject: `[JIRA-ID]: [Brief description]` (or `MINOR: [Brief description]` if no JIRA exists).
4. Acceptance criteria: [ticket-specific, observable conditions for "done"]
