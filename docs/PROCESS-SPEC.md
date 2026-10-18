# Processes and the task inbox

Implements FR-110 (process definitions, basic) and FR-111 (task inbox) and a first part of FR-113 (monitoring).
Not built: timers and boundary events beyond a plain wait, sub-processes, process versioning with instance migration
(FR-112), a visual editor, notifications. A process models *people and time*; flows (docs/FLOW-SPEC.md) model *what the
system does*, and a process calls flows through system tasks.

## 1. Model

```yaml
modules:
  - key: core
    processes:
      - key: Approval
        params: [ { name: amount, type: integer }, { name: note, type: string } ]
        start_roles: [viewer, editor]       # application roles that may start an instance
        run_as_role: admin                  # role under which system tasks run their flows
        steps:
          - { id: start, type: start }
          - id: review
            type: user_task
            config:
              title: Review request
              assignee_role: editor               # or assignee_expr: "initiator" (a user id)
              outcomes: [approve, reject, more_info]
              form: [ { name: reviewer_comment, type: string, required: false } ]
              due_in_hours: 24
              escalate_after_hours: 8
              escalate_to_role: admin
          - { id: big, type: decision, config: { expr: "amount > 1000" } }
          - { id: fork, type: parallel_split }
          - { id: notify, type: system_task, config: { flow: ReorderLowStock, args: { min_gap: "0" } } }
          - { id: cool, type: timer, config: { wait_seconds: 3600 } }
          - { id: join, type: parallel_join }
          - { id: done, type: end }
        edges:
          - { id: e1, source: start, target: review }
          - { id: e2, source: review, target: big, outcome: approve }     # outcome = the user-task outcome, or true/false
```

| Step | Behaviour | Outgoing edges |
|---|---|---|
| `start` | begins the instance | one |
| `user_task` | creates an inbox task and waits for a person | one per outcome |
| `system_task` | runs a server flow as `run_as_role`; a failing flow fails the step | one |
| `decision` | evaluates a boolean expression | `true` and `false` |
| `parallel_split` | continues on every outgoing edge at once | two or more |
| `parallel_join` | waits until a token has arrived on every incoming edge, then continues once | one (two or more incoming) |
| `timer` | waits `wait_seconds` (up to a year) | one |
| `end` | consumes the token; the instance completes when no token is left | none |

**Variables.** Expressions (decisions, `assignee_expr`, system-task arguments) see the process parameters, the implicit
`initiator` (id of whoever started the instance) and the form fields of user tasks, which are **nullable** because a
path may not have visited that task (use `??`). Completing a task sets its form fields.

**Validation** (`orchivio validate`, every revision): exactly one `start`, at least one `end`, every step reachable
and able to reach an end, edges and outcomes consistent per step type, joins with at least two inputs, roles and flows
exist, system-task arguments type-check against the flow, expressions type-check, form fields do not clash with
parameters, and **every cycle must pass through a user task or timer** (`process-sync-cycle`), so a rework loop is fine
but a loop that could spin without anybody involved is refused. At run time an instance is also stopped after 1 000 steps
without waiting and 10 000 in total.

## 2. Instances

An instance is pinned to the revision it started with, so its routing does not change under it. Its state (variables,
tokens, join counters) is one JSON document updated by compare-and-set on a version number; two people acting at once
cannot both succeed (`409 conflict`). Statuses: `running`, `completed`, `failed` (a step failed; fix the cause and
retry), `cancelled`. System tasks run the flow by key in the **current** model against the application's runtime (the
preview data until a production data plane exists); if that flow no longer exists, or the data is not built, the step
fails and can be retried. A system task's effects happen in the runtime database and are not rolled back if the
instance update then loses a race, so a system task can in that rare case run twice: make flows safe to repeat.

## 3. The task inbox (application plane)

Endpoints under `/apps/{app_id}` for signed-in end users (docs/APP-SECURITY.md §6):

* `GET /processes`, `POST /processes/{key}/start {args}`: processes the caller's roles may start; arguments are
  checked against the parameters.
* `GET /tasks?scope=mine|available|all&status=open|completed`: tasks assigned to you, tasks offered to a role you hold
  that nobody has claimed, or both. Ordered by due date. Others' tasks do not exist as far as you can tell (`404`).
* `POST /tasks/{id}/claim`: take an offered task (exactly one claimant wins); `release` gives it back (only for
  role-offered tasks); `delegate {username}`: hand it to an active user who holds the role it was offered to;
  `complete {outcome, result}`: the form is validated (unknown fields, required fields, types) and the process moves on.
* `GET /process-instances/{id}`: progress of an instance you started (status and event trail, not its data).

**Assignment** is by role (anyone holding it may claim) or by expression (a specific user id, e.g. the initiator).
**Due dates** mark a task overdue. **Escalation**: once `escalate_after_hours` pass on an open task, the worker re-offers it
to `escalate_to_role` and drops any claim (once per task). Timers and escalation need the worker running
(`orchivio worker` or `ORCHIVIO_WORKER_ENABLED`, docs/OPERATIONS.md).

## 4. Monitoring and control (Studio)

`GET /applications/{id}/process-instances` (`status`, `process`), `GET /process-instances/{id}` (variables, tokens, tasks
and the full event trail), `GET /applications/{id}/user-tasks` (all tasks, with overdue and escalation), and for editors
`POST /applications/{id}/processes/{key}/start` (test run as a simulated user), `POST /process-instances/{id}/cancel`
(closes its tasks) and `.../retry`. Every instance keeps a `process_events` trail (started, task created/claimed/
delegated/escalated/completed, decisions, system tasks, timers, failures, completion); starts and task completions are
also audit events (application plane).

## 5. Approval and risk

Processes are model elements: diffs, merges, files and archives cover them. A change that widens `start_roles`, or changes
`run_as_role`, is a permission expansion (A3).
