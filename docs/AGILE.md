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
the project's applications, and a test id is an opaque name (`tests/test_stock.py::test_low`, 1-200 letters, digits and `. _ : / -`) that is
not looked up. Duplicates are refused. Deleting a requirement or story removes its links. Elements removed from a model later leave the link in
place; the report shows it as missing.

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

## 5. Not built

The widget itself (a Studio/preview overlay that captures the page, element and screenshot and calls the API), a board UI with drag and drop,
automatic requirement extraction into requirement rows, links from tests that the platform ran (test ids are opaque names), assignees, sprints,
comments, and e-mail or chat notifications.
