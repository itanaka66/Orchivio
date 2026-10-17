from datetime import datetime, timezone
from decimal import Decimal

import pytest

from orchivio.expr import (
    CheckEnv,
    EvalContext,
    EvalError,
    LimitExceeded,
    Limits,
    ParseError,
    Type,
    TypeCheckError,
    UserRef,
    check,
    evaluate,
    parse,
)
from orchivio.expr.ast import BOOL, DECIMAL, INT, STRING, enum_t, ref_t

PRODUCT = "core.Product"


class FakeSchema:
    FIELDS = {
        "quantity": INT,
        "reorder_level": INT,
        "name": STRING,
        "note": Type("string", nullable=True),
        "price": DECIMAL,
        "status": enum_t("StockStatus"),
        "owner": ref_t("system.User"),
    }

    def field_type(self, entity, field):
        return self.FIELDS.get(field) if entity == PRODUCT else None

    def enum_values(self, name):
        return ("in_stock", "low", "out_of_stock") if name == "StockStatus" else None


ENV = CheckEnv(FakeSchema(), PRODUCT)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def run(src, record=None, user=None, expected=None):
    res = check(src, ENV, expected)
    ctx = EvalContext(
        record=record or {},
        current_user=user,
        now=NOW,
        enums={"StockStatus": ("in_stock", "low", "out_of_stock")},
    )
    return evaluate(res.ast, ctx)


@pytest.mark.parametrize(
    "src,expected",
    [
        ("1 + 2 * 3", 7),
        ("(1 + 2) * 3", 9),
        ("-7 / 2", -3),  # truncates toward zero
        ("-7 % 3", -1),
        ("7 / 2", 3),
        ("1.5 + 1", Decimal("2.5")),
        ("'a' + 'b'", "ab"),
        ("true || false && false", True),
        ("!true", False),
        ("1 in [1, 2]", True),
        ("'x' in ['a']", False),
        ("1 < 2 ? 'y' : 'n'", "y"),
        ("size('abc')", 3),
        ("'Hello'.lower()", "hello"),
        ("'abc'.startsWith('ab')", True),
        ("size([1,2,3])", 3),
        ("'it\\'s'", "it's"),
    ],
)
def test_basic_evaluation(src, expected):
    assert run(src) == expected


def test_record_fields_bare_and_qualified():
    rec = {"quantity": 3, "reorder_level": 5}
    assert run("quantity <= reorder_level", rec) is True
    assert run("record.quantity == 3", rec) is True


def test_enum_ternary_from_design_doc():
    src = (
        "quantity == 0 ? StockStatus.out_of_stock "
        ": (quantity <= reorder_level ? StockStatus.low : StockStatus.in_stock)"
    )
    assert run(src, {"quantity": 0, "reorder_level": 5}) == "out_of_stock"
    assert run(src, {"quantity": 3, "reorder_level": 5}) == "low"
    assert run(src, {"quantity": 9, "reorder_level": 5}) == "in_stock"


def test_current_user_and_roles():
    user = UserRef("u1", {"editor"})
    assert run("record.owner == $currentUser", {"owner": "u1"}, user) is True
    assert run("record.owner == $currentUser", {"owner": "u2"}, user) is False
    assert run("$currentUser.hasRole('editor')", None, user) is True
    assert run("$currentUser.hasRole('admin')", None, user) is False


def test_null_handling():
    rec = {"note": None}
    assert run("note == null", rec) is True
    assert run("note ?? 'none'", rec) == "none"
    assert run("(note ?? '').size()", rec) == 0


def test_short_circuit_avoids_division_by_zero():
    assert run("quantity != 0 && 10 / quantity > 1", {"quantity": 0}) is False


def test_division_by_zero_is_eval_error():
    with pytest.raises(EvalError):
        run("1 / (quantity - quantity)", {"quantity": 4})


@pytest.mark.parametrize(
    "src,fragment",
    [
        ("quantity + 'a'", "needs numbers"),
        ("unknown_field", "unknown identifier"),
        ("note + 'a'", "nullable"),
        ("note.size()", "nullable"),
        ("quantity ?? 1", "never null"),
        ("StockStatus.bogus", "no value"),
        ("quantity", "expected bool"),
        ("quantity == 'a'", "cannot compare"),
        ("record.owner.name == 'x'", "traversal"),
        ("$nope", "unknown variable"),
        ("$env.X", "unknown environment"),
        ("[]", "empty list"),
        ("note < 'a'", "nullable"),
        ("price % 2 == 0", "integers"),
        ("StockStatus", "must evaluate to a value"),
    ],
)
def test_type_errors(src, fragment):
    with pytest.raises(TypeCheckError) as e:
        check(src, ENV, BOOL)
    assert fragment in e.value.message


@pytest.mark.parametrize("src", ["", "1 +", "(1", "1 2", "a.", "'unterminated", "1 @ 2"])
def test_parse_errors(src):
    with pytest.raises(ParseError):
        parse(src)


def test_parse_limits():
    with pytest.raises(LimitExceeded):
        parse("(" * 40 + "1" + ")" * 40)
    with pytest.raises(LimitExceeded):
        parse("1+" * 400 + "1")
    with pytest.raises(LimitExceeded):
        parse("1" * 3000)


def test_step_budget_stops_evaluation():
    res = check("1+1+1+1+1+1+1+1", CheckEnv(FakeSchema(), None))
    with pytest.raises(LimitExceeded):
        evaluate(res.ast, EvalContext(), Limits(max_steps=5))


def test_referenced_fields_reported():
    res = check("quantity <= reorder_level && record.name != ''", ENV, BOOL)
    assert res.fields == {"quantity", "reorder_level", "name"}


def test_default_expressions_have_no_record():
    env = CheckEnv(FakeSchema(), None)
    check("$now", env)
    with pytest.raises(TypeCheckError):
        check("quantity", env)


def test_expected_type_allows_int_to_decimal_only():
    check("1", ENV, DECIMAL)
    with pytest.raises(TypeCheckError):
        check("1.5", ENV, INT)
