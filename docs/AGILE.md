# Requirements, story board and preview feedback (FR-098, FR-099)

Everything is per project. Reading needs `project.read` (any member); changing needs `story.manage` (editors and up, A1).

## 1. Requirements and stories

`/projects/{id}/requirements` and `/projects/{id}/stories`: create, list (filter by `status`, stories also by `requirement_id`),
read, patch, delete. Requirement status: `draft`, `accepted`, `implemented`, `rejected`. Story status: `proposed`, `backlog`, `ready`,
`in_progress`, `done`, `rejected`, ordered by `position` (new stories go to the end). A story may belong to one requirement; deleting the
requirement keeps the story. `source` says where an item came from (`manual`, `ai`, `feedback`).

## 2. Trace links

`POST /projects/{id}/trace-links` links a requirement or story to something it is realised or verified by:

| From | To |
|---|---|
| requirement | story, element, revision, test |
| story | element, revision, test |

Links are checked when created: stories and revisions must belong to the project, an element id must exist in the **current model** of one of
the project's applications, and a test id must be one of the project's **generated test cases** (`GET /projects/{id}/test-cases?q=`, ids such
as `crud:core.Product` or `validation:core.Product:sku:required`; refused with `unknown_test`). Duplicates are refused. Deleting a requirement or story removes its links. Elements or test cases that disappear from a model later (a rule removed,
an entity renamed) leave the link in place; the report marks them `exists: false` and counts `dangling_elements` / `dangling_tests`.

## 2a. Requirement extraction

**Automatic registration.** `POST /applications/{id}/ai/analyze` and `/ai/propose` accept `register_requirements: true`: after the call succeeds, the same
requirements text is run through the extraction below and the draft rows are returned as `registered_requirements`. It needs `story.manage` as
well, is a separate recorded AI job, and never fails the main call: if extraction cannot run (no permission, unusable output, provider error) the
field holds `{error, requirements: []}` and the analysis or proposal is returned as usual.

`POST /applications/{id}/requirements/extract` with `{text}` (up to 20 000 characters; notes, a brief, an e-mail thread) asks the AI Builder for the
requirements the text states and stores them as **draft** requirements with `source: ai`. The document is wrapped as untrusted data; titles
that repeat an existing one (case-insensitively) or an earlier one in the answer are skipped and counted. Same consent, budget and AI-job rules as
every AI call; needs `story.manage`. Nothing is linked or applied automatically.

## 3. Traceability report

`GET /projects/{id}/traceability?format=json|csv` lists every requirement and story with its stories, elements (with name and whether it still
exists), revisions and tests, plus coverage over non-rejected requirements: with stories, elements, tests, revisions, and the number of dangling
element links. CSV has a BOM, and cells that start with `= + - @` get a leading quote so spreadsheets do not execute requirement text.

## 4. Preview feedback

`POST /applications/{id}/feedback` (any member, viewers included): `page_key`, `body` (2000 characters), optional `element_id` (must be in the
application's current model) and optional `screenshot_base64` (PNG or JPEG by content, at most 512 KiB). At most 30 items per user per
application per hour. Editors list feedback (`GET .../feedback?status=new|clustered|dismissed`), fetch screenshots (served with the stored
image type, `nosniff`, a locked-down CSP and no caching) and dismiss or restore items.

**AI clustering.** `POST /applications/{id}/feedback/cluster` sends the application's *new* feedback (oldest 100; numbers, page, element id and
text) with the element list to the AI Builder and creates **proposed** stories. Feedback is untrusted text wrapped as data. A story is kept only
if it cites feedback numbers that were sent and not already used by an earlier story; unknown element ids are removed; clustered feedback links to
its story and the story links to the named elements. It uses the normal AI consent and budget rules, records an AI job, and never changes a
model. A person moves a proposed story onto the backlog.

## 5. Studio screens

The Studio (`apps/web`) has a **story board** per project (open it from the project/application picker):

* **Board**: columns Proposed, Backlog, Ready, In progress, Done, Rejected. Cards move by drag and drop or, for keyboard and touch users, by the
  "Move to" select on each card. Viewers see the board read-only.
* **Requirements**: list, add, change status, delete, and "Extract from a document" (editors, needs an application).
* **Feedback**: new feedback of the chosen application with screenshot links, dismiss, and "Group into proposed stories".
* **Traceability**: coverage, a table of requirements and stories with their elements and tests (missing ones highlighted), and forms to add or
  remove links (elements from the current model, tests from the generated catalogue).

**Feedback widget** (`FeedbackWidget`). A floating "Feedback" button opens a form: text, **Pick an element** (the next click is captured and not
passed to the page; the element and page come from `data-orchivio-element` / `data-orchivio-page` attributes on the rendered UI), an attached
PNG/JPEG (file chooser, paste, or **Capture screen**, which uses the browser's own screen-share prompt and takes one frame). Images are checked
(type by content, 512 KiB) before sending. It is mounted in the Studio's board and flow-editor views; any host that renders model-driven UI can
mount it and mark its elements with the two attributes.

## 5a. Preview screen

"Open the preview" (Studio, after choosing an application) renders the application's **pages from its model** against the preview database: lists
with the page's columns, create/edit forms built from the entity's typed-in fields (no generated, computed or key fields; enumerations as selects;
numbers sent as numbers), and "Reset sample data". The toolbar sets the simulated end user and roles, so what is shown is exactly what the
runtime's access rules allow that user; refusals from the server are shown as they are. Page, table, column and form-field elements carry
`data-orchivio-page` and `data-orchivio-element` (the model's element ids), and the **feedback widget** is mounted on it, so a comment picked on
a column header or a cell is stored against that field's element id and the page key.

## 6. Not built

The preview screen is a faithful *rendering of the model*, not the generated production UI: no detail pages, page actions beyond create and edit,
delete, filters or sorting, relations pickers (references are typed as ids), or file fields. A Studio screen for editing a story's body and
acceptance criteria, comments and assignees, sprints, and notifications. Live-model behaviour of the AI features is covered by `tests/live`
(skipped unless `ORCHIVIO_LIVE_LLM=1` and an API key are set); it has not been run by the authors of this change. Screen capture in the widget needs
a manual check in a real browser.
