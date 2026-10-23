# Expression Language Specification (v1)

One language for validation rules, computed fields, defaults, conditional UI and row-level
access constraints. Implementation: `src/orchivio/expr/`. Decision:
[ADR-0002](adr/0002-expression-language.md).

Properties: statically typed, null-safe, **pure** (no I/O, no assignment), deterministic and
bounded. Client-side evaluation of an expression is presentation only; the server re-evaluates
every authorization decision.

## 1. Syntax

```
expr     := ternary
ternary  := binary ( '?' expr ':' expr )?
binary   := unary ( op unary )*            precedence, low → high:
            ??   ||   &&   == !=   < <= > >= in   + -   * / %
unary    := ('!' | '-') unary | postfix
postfix  := primary ( ('.' | '?.') name ( '(' args ')' )? )*
primary  := number | string | true | false | null | $var | name | name '(' args ')'
          | '(' expr ')' | '[' expr (',' expr)* ']'
          | '{' ( name ':' expr (',' name ':' expr)* )? '}'
```

Strings use `'…'` or `"…"` with escapes `\\ \' \" \n \t`. Numbers with a `.` are `decimal`,
otherwise `int`.

## 2. Names and context

| Name | Meaning |
|---|---|
| `record` | The current record (when the context has an entity) |
| `field` | Bare identifier: a field of `record` if one exists… |
| `EnumName` | …otherwise an enumeration of the module; `EnumName.value` is a value |
| `$currentUser` | The end user (type `ref:system.User`); `.hasRole('r')`, `.id` |
| `$now` | Supplied by the caller (never read from a clock inside the engine) |
| `$env.NAME` | Environment constants (none defined yet) |

A field shadows an enum of the same name. Which names exist depends on the context:
`default_expr` has no `record`; computed fields, row constraints and page action expressions
have the entity as `record`.

## 3. Types

`int`, `decimal`, `string`, `bool`, `datetime`, `enum:Name`, `ref:Entity`, `list<T>`, `object`, `null`.
Any type can be nullable (written `T?` in messages). `int` converts to `decimal` implicitly;
nothing else converts. `uuid` fields are `string`; `date` is `datetime`.
`object` is the type of an object literal `{key: expr, …}`; field names must be unique.
Object literals are not compilable to SQL predicates.

## 4. Null safety

| Rule | Example |
|---|---|
| `==` / `!=` accept nullable operands and `null` | `note == null` |
| Arithmetic, ordering, `&&`, `||`, `!`, `in` (left side) need non-null operands | `note < 'b'` is a type error |
| `??` requires a nullable left side | `note ?? ''` |
| Member/method access on a nullable value needs `?.` | `manager?.id` |
| Ternary branches unify; mixing `null` makes the result nullable | `c ? 1 : null` is `int?` |

## 5. Operators and built-ins

- Arithmetic `+ - * / %`; `+` also concatenates strings; `%` is integer-only. Integer `/` and
  `%` **truncate toward zero** (as in SQL and CEL), not Python floor division. Division by zero
  is an evaluation error.
- Comparison `== !=` on compatible types; `< <= > >=` on numbers, strings or datetimes.
- Logical `&& || !` short-circuit.
- `x in [a, b, c]` list membership (list literals must be non-empty and null-free).
- String methods: `startsWith(s)`, `endsWith(s)`, `contains(s)`, `lower()`, `upper()`, `size()`.
- List methods: `size()`, `contains(x)`. Function: `size(x)` for strings and lists.
- `$currentUser.hasRole('role')`.
- Relationship traversal: `record.owner.name` accesses field `name` of the related entity. The
  intermediate ref must be non-null (use `?.` for nullable refs: `record.manager?.name`). Traversal
  is supported by the interpreter when the caller pre-loads related records as nested dicts. It
  raises `NotCompilable` in the SQL predicate compiler (requires a JOIN; not yet supported there).
  `ref.id` returns the ref itself (a shorthand for the foreign key value).

## 6. Bounds

Enforced deterministically (no wall-clock timeouts), defined in `Limits`:

| Limit | Default |
|---|---|
| Source length | 2 000 characters |
| Nesting depth | 32 |
| AST nodes | 500 |
| Evaluation steps | 10 000 |
| List literal size | 1 000 |
| String result length | 100 000 |

Exceeding a limit raises `LimitExceeded` (parse time or run time).

## 7. SQL compilation (row-level constraints)

`compile_predicate` turns a boolean, type-checked expression into a predicate with named bind
parameters (`:p0`, `:p1`…). Values never appear in SQL text; column names must match
`[A-Za-z_][A-Za-z0-9_]*` and are quoted.

- `==`/`!=` use plain `=`/`<>` when neither side is nullable, `IS [NOT] NULL` against the `null`
  literal, and a null-safe form otherwise (`IS NOT DISTINCT FROM` on PostgreSQL). The null-safe
  form cannot use a plain btree index, so constraints should reference non-nullable columns.
- `hasRole('x')` is resolved at compile time from the current user's roles.
- String methods compile to `substr`/`instr` (`strpos`, `right` on PostgreSQL) rather than
  `LIKE`, which is case-insensitive on some databases.
- **Not compilable** (raises `NotCompilable`): `/`, `%` (zero-divisor behaviour differs between
  databases), `size()`, string-method arguments that are not literals, list expressions outside
  `in`. Callers must treat `NotCompilable` as **deny**, never as "no filter".

`tests/unit/test_expr_sql.py` runs every supported construct through both the interpreter and
SQLite and requires identical row sets. A PostgreSQL run is still outstanding.

## 8. Error reporting

All errors derive from `ExprError` and carry a character offset: `ParseError`,
`TypeCheckError`, `EvalError`, `LimitExceeded`, `NotCompilable`.

## 9. Future

Relationship traversal compiled to SQL JOINs (currently raises `NotCompilable`), `$env`
constants, TypeScript evaluator for client-side visibility (DESIGN.md §8.5).
