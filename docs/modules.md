# Writing a Mimic module

A module is a Python package under `mimic/modules/<name>/` containing a `module.json` manifest and
an `__init__.py` that publishes a `MODULE_SPEC`. Discovery finds it at startup — there is nothing to
register anywhere else.

Start by copying [`mimic/modules/_template/`](../mimic/modules/_template/). Discovery skips packages
whose name begins with an underscore, so the template never loads.

```
mimic/modules/mything/
├── __init__.py     # MODULE_SPEC
├── module.json     # the manifest
└── mything.py      # optional: the MimicModule subclass, if you need Python
```

```python
# mimic/modules/mything/__init__.py
from mimic.modules.base import MimicModule, ModuleSpec

MODULE_SPEC = ModuleSpec(module_id="mything", module=MimicModule("mything"))
```

`module_id` must match the directory name and be unique; a duplicate is recorded as a load error and
the second one is dropped. One module type is one instance — there is no mechanism for running the
same module twice at two prefixes.

## The two kinds

| `kind` | Routing | Use when |
| --- | --- | --- |
| `"manifest"` (default) | The loader registers the manifest's `routes` for you. Individual routes may name a `handler` method for real Python. | Almost always. |
| `"custom"` | You override `build_blueprints` and register whatever you like. The manifest must declare **no** `routes`. | The route table cannot be expressed as a list — generated rules, or routing that depends on runtime state. No shipped module needs this. |

The declared `kind` is checked against the class at load time: a `"manifest"` module that overrides
`build_blueprints`, or a `"custom"` module that does not, is refused with a message on the dashboard
rather than loading in a half-working state.

## Manifest schema

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `kind` | `"manifest"` \| `"custom"` | `"manifest"` | validated against the class |
| `title` | str | `""` | shown on the admin dashboard |
| `description` | str | `""` | shown on the admin dashboard |
| `default_enabled` | bool | `false` | whether the module loads at startup |
| `default_prefix` | str | — | **required**; where the module mounts, e.g. `/webhook/` |
| `host` | str \| null | `null` | see [Host matching](#host-matching) |
| `max_concurrent` | int | `4` | in-flight request cap for this module |
| `default_methods` | list[str] | `["GET"]` | fallback for routes that omit `methods` |
| `default_response` | object | `{}` | served by the prefix root and by any route whose `responses` do not match |
| `error_pages` | object | `{}` | optional custom error pages, keyed by status code; see [Error pages](#error-pages) |
| `strict_slashes` | bool | `true` | fallback for routes that omit it |
| `routes` | list | `[]` | see below; must be empty when `kind` is `"custom"` |

Each entry in `routes`:

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `uri` | str | — | **required**, and **relative** to `default_prefix`. `""` is the prefix root. Werkzeug converters work: `status/<int:code>`, `<path:wildcard>`. |
| `methods` | list[str] | the manifest's `default_methods` | |
| `handler` | str \| null | `null` | name of a method on your `MimicModule` subclass |
| `responses` | list | `[]` | canned responses; ignored when `handler` is set |
| `strict_slashes` | bool | the manifest's `strict_slashes` | |

A manifest problem is reported, not raised. A fatal one (not an object, unknown `kind`, missing
`default_prefix`, `"custom"` with routes) stops that module loading; anything narrower — a bad route
entry, a duplicate `uri`, a wrong-typed scalar — drops just that piece and the module still loads.
Every message lands in the dashboard's error list.

## Canned responses

A route without a `handler` serves from its `responses` list. The first entry whose `match` is
satisfied wins; if none match, the manifest's `default_response` is used, and if there is no
`default_response` the request 404s.

```json
{
  "uri": "<path:wildcard>",
  "responses": [
    {
      "match": { "wildcard": "github/push", "method": "POST" },
      "response": {
        "status": 202,
        "headers": { "Content-Type": "application/json" },
        "body": { "queued": true }
      }
    }
  ]
}
```

`match` keys are compared against the URL rule's captured variables plus `method`. A string value
must be equal; a list value means "any of". The reserved key `query` takes an object mapping query
parameter names to expected values — string = exact match, list = any-of, `"*"` = parameter present
— and every listed parameter must match. `method` and `query` are reserved, so don't name a URL
variable either of them. A response fragment carries an optional `status`
(default `200`), `headers`, `passthrough`, and `body` — a string is sent as-is, anything else is
JSON-encoded. Without a `Content-Type` header, a string body is sent as `text/plain; charset=utf-8`
and a JSON-encoded one as `application/json`; set `Content-Type: text/html` explicitly if you want
HTML, and remember that templated variables are not escaped. It can also carry `file`, `download`, and `filename` to serve a file instead of a
body; see [Serving files](#serving-files).

Strings anywhere in a response are `str.format`-templated with the captured variables, so
`"you asked for {wildcard}"` works. That also means a literal brace in a response body has to be
doubled.

## Serving files

A response fragment with a `file` key serves that file instead of its `body`. The path is
**relative to the module's own directory** — the package under `mimic/modules/<name>/` that ships
the manifest — so a module's content lives right next to its code:

```
mimic/modules/mysite/
├── __init__.py
├── module.json
└── www/
    ├── index.html
    └── app.js
```

| Key | Type | Notes |
| --- | --- | --- |
| `file` | str | path relative to the module directory; replaces `body` |
| `download` | bool | send `Content-Disposition: attachment` (filename defaults to the file's basename) |
| `filename` | str | override the download name |

```json
{
  "uri": "<path:wildcard>",
  "responses": [
    { "response": { "file": "www/{wildcard}" } },
    { "match": { "wildcard": "report.bin" },
      "response": { "file": "www/report.bin", "download": true, "filename": "report.bin" } }
  ]
}
```

Because `file` is templated like any other string, a `<path:wildcard>` route can serve a whole tree:
the request path is captured and spliced into the on-disk path. The URL and the file do not have to
mirror each other — an explicit route may map any URI to any file in the module directory, e.g.
`{ "uri": "path1/path2/path3/", "responses": [ { "response": { "file": "www/index.html" } } ] }`.

The `Content-Type` is guessed from the file's extension (`html`, `js`, `json`, `css`, and friends
get their usual types, text types carry `charset=utf-8`; anything unknown is
`application/octet-stream`). An explicit `Content-Type` in the fragment's `headers` always wins.

Safety rules:

- At load time, `file` must be a non-empty relative path with no `..` segments; absolute paths and
  escapes are dashboard errors and the key is dropped from that response (the route degrades to its
  `body`/`default_response`).
- At request time — necessary because variables are only known then — the resolved path must stay
  inside the module directory and be a regular file, and a substituted variable may not introduce a
  `..` segment, a leading `/` or a NUL byte, otherwise the request **404s**. So `www/{wildcard}`
  can only ever serve files under `www/`. A missing file
  is therefore a 404 you can see in the activity log, not a load error.

There is no automatic "directory index" convention: `/a/b/` does not serve `www/a/b/index.html`
unless you declare it (mirrored layout plus the catch-all, an explicit route, or a Python handler).

[`mimic/modules/static/`](../mimic/modules/static/) is the worked example: its manifest serves
`www/index.html` at the prefix root, the rest of `www/` through a `<path:wildcard>` catch-all, and
one download route.

## Error pages

By default, any HTTP error a module raises (a 404 for an unmatched path, a 405 for a bad method, a
file that is missing at request time, …) is answered with a small JSON body:
`{"status_code", "status", "message"}`. A module can replace that with its own page by declaring an
`error_pages` object in its manifest, keyed by status code. Each value is a normal response fragment —
the same `body` / `file` / `headers` shape as a canned response — so a page can be JSON **or** a static
file from the module's own directory:

```json
{
  "error_pages": {
    "404": { "file": "www/404.html" },
    "500": { "body": { "error": "internal" }, "headers": { "Content-Type": "application/json" } }
  }
}
```

- Only the codes you list are customised; every other code keeps the default JSON.
- The response always carries the real error code, so a `404.html` file is served with status **404**,
  never the `200` a fragment might otherwise imply — a fragment's own `status` is ignored.
- File pages follow the same safety rules as [Serving files](#serving-files): the path must stay inside
  the module directory, and a missing file falls back to the default JSON rather than recursing into
  another 404.
- `error_pages` is overridable from the admin dashboard like `routes`; editing it back to what the
  manifest ships clears the override.

## Handlers

A route with a `handler` calls that method on your module instance with the URL's captured variables
as keyword arguments — it is an ordinary Flask view and returns whatever Flask accepts. Names are
resolved at load time, so a typo is a dashboard error at startup rather than a 500 on the first
request; the offending route is dropped and the rest of the module still loads. Handler names may
not start with `_` or name a method `MimicModule` itself defines (`build_blueprints`,
`error_response`, …), because routes are editable from the admin UI.

```python
class MyThing(MimicModule):
    def status(self, code: int) -> Response:
        return Response("", status=code)
```

```json
{ "uri": "status/<int:code>", "handler": "status" }
```

[`mimic/modules/httpbin/`](../mimic/modules/httpbin/) is the worked example: every route is declared
in the manifest and every one of them names a handler, so the routes remain visible to the admin UI
and to prefix-conflict detection while the behaviour stays in Python.
[`mimic/modules/webhook/`](../mimic/modules/webhook/) is the pure-manifest counterpart.

## Mounting, prefixes and conflicts

Each loaded module gets its own Flask sub-app; a WSGI middleware in front of the main app dispatches
by path prefix, longest first. Two enabled modules may not claim overlapping prefixes — `/v2` and
`/v2/httpbin` conflict. The first to claim one wins and the other is skipped with a message on the
dashboard.

### Host matching

Setting `host` turns on Flask host matching for that module's sub-app, which means the module
answers **only** requests carrying that `Host` header — it disappears from every other name,
including `localhost`. Leave it `null` unless you want that.

If the admin **Create DNS Records** feature is on, a module with a `host` also needs its Route 53
record to exist; if the record cannot be created the module is not loaded.

## Limits

Every module is capped at `max_concurrent` simultaneous requests, and the server as a whole at a
global cap. Past either, the request is refused with **503** rather than queued. If you see a 503
from your own module under load, that is the cap, not your code.

## Overrides

`module.json` is the source of truth and is re-read on every boot. The admin dashboard can override
`enabled`, `path_prefix`, `host`, `routes` and `error_pages` per module; those overrides live in the
`module_overrides` table in SQLite and are the only module state the database holds. Editing a value
back to what the manifest ships clears the override, and **Reset** clears all of them, so
"overridden" on the dashboard always means "disk says something else".
