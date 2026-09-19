"""Policy document shape, identity and the deterministic digest.

The expectations in this module are written from the frozen card text — accepted keys, id charset,
group/rule budgets, value limits and the digest recipe — and never by re-running the implementation
to see what it produced. The digest case is the clearest example: the expected hash is computed here
from the documented JSON recipe (UTF-8, ``sort_keys=True``, ``ensure_ascii=False``, compact
separators, no trailing newline), so a change in the recipe fails this suite instead of silently
renumbering every stored policy revision.
"""

from __future__ import annotations

import dataclasses
import hashlib
import unittest

try:
    from . import _fixtures as fx
    from ._fixtures import document, group, rule
except ImportError:  # narrow discovery: this directory is the top-level start directory
    import _fixtures as fx
    from _fixtures import document, group, rule

from services.platform.media.rules import (
    ERROR_DUPLICATE_GROUP_ID,
    ERROR_DUPLICATE_RULE_ID,
    ERROR_EMPTY_RULES,
    ERROR_INVALID_CASE_SENSITIVE,
    ERROR_INVALID_DOCUMENT,
    ERROR_INVALID_FIELD_NAME,
    ERROR_INVALID_GROUP,
    ERROR_INVALID_GROUP_ID,
    ERROR_INVALID_GROUP_LIST,
    ERROR_INVALID_OP,
    ERROR_INVALID_REVISION,
    ERROR_INVALID_RULE,
    ERROR_INVALID_RULE_ID,
    ERROR_INVALID_RULES,
    ERROR_INVALID_SCHEMA_VERSION,
    ERROR_INVALID_VALUE,
    ERROR_MISSING_FIELD,
    ERROR_TOO_MANY_GROUPS,
    ERROR_TOO_MANY_RULES,
    ERROR_UNKNOWN_FIELD,
    ERROR_VALUE_TOO_LONG,
    GROUP_FIELDS,
    GROUPS_MAX,
    OP_REGEX,
    REGEX_VALUE_MAX,
    RULE_FIELDS,
    RULES_PER_GROUP_MAX,
    VALUE_MAX,
    Rule,
    RulePolicy,
    RuleValidationError,
    parse_policy,
    policy_digest,
    regex_rules,
)


def refused(call) -> RuleValidationError:
    try:
        call()
    except RuleValidationError as error:
        return error
    raise AssertionError("expected RuleValidationError")


SIMPLE_DOCUMENT = document(
    revision=3,
    whitelist=[group("wl", rule("r1", "title", "contains", "合集"))],
)
SIMPLE_JSON = (
    '{"blacklist":[],"revision":3,"schema_version":1,'
    '"whitelist":[{"id":"wl","rules":[{"case_sensitive":false,"field":"title",'
    '"id":"r1","op":"contains","value":"合集"}]}]}'
)


class PolicyShapeTest(unittest.TestCase):
    def test_a_complete_document_is_accepted_field_by_field(self):
        policy = parse_policy(fx.text_policy())
        self.assertIsInstance(policy, RulePolicy)
        self.assertEqual(policy.revision, 7)
        self.assertEqual([entry.id for entry in policy.whitelist], ["wl"])
        self.assertEqual([entry.id for entry in policy.blacklist], ["bl"])
        self.assertEqual(policy.group_count, 2)
        self.assertEqual(policy.rule_count, 2)
        self.assertFalse(policy.has_regex)
        self.assertEqual(
            policy.whitelist[0].rules[0],
            Rule(
                id="r_title",
                field="title",
                op="contains",
                value="合集",
                case_sensitive=False,
            ),
        )

    def test_an_empty_policy_is_valid_and_has_no_groups(self):
        policy = parse_policy(document())
        self.assertEqual(policy.whitelist, ())
        self.assertEqual(policy.blacklist, ())
        self.assertEqual(policy.group_count, 0)
        self.assertEqual(policy.rule_count, 0)

    def test_bool_is_not_an_accepted_schema_version_or_revision(self):
        for bad_schema in (True, False):
            with self.subTest(schema_version=bad_schema):
                error = refused(
                    lambda value=bad_schema: parse_policy({**document(), "schema_version": value})
                )
                self.assertEqual(error.code, ERROR_INVALID_SCHEMA_VERSION)
                self.assertEqual(error.field, "schema_version")
        for bad_revision in (True, False):
            with self.subTest(revision=bad_revision):
                error = refused(
                    lambda value=bad_revision: parse_policy(document(revision=value))  # type: ignore[arg-type]
                )
                self.assertEqual(error.code, ERROR_INVALID_REVISION)
                self.assertEqual(error.field, "revision")

    def test_revision_range_is_one_to_two_pow_thirty_one_minus_one(self):
        self.assertEqual(parse_policy(document(revision=1)).revision, 1)
        self.assertEqual(parse_policy(document(revision=2147483647)).revision, 2147483647)
        for value in (0, -1, 2147483648, 1.0, "7", None):
            with self.subTest(revision=value):
                error = refused(lambda entry=value: parse_policy(document(revision=entry)))  # type: ignore[arg-type]
                self.assertEqual((error.code, error.field), (ERROR_INVALID_REVISION, "revision"))

    def test_group_and_rule_budgets(self):
        ten_rules = tuple(rule(f"r{i}") for i in range(RULES_PER_GROUP_MAX))
        accepted = parse_policy(document(whitelist=[group("wl", *ten_rules)]))
        self.assertEqual(len(accepted.whitelist[0].rules), RULES_PER_GROUP_MAX)

        eleven = refused(
            lambda: parse_policy(document(whitelist=[group("wl", *ten_rules, rule("r_extra"))]))
        )
        self.assertEqual((eleven.code, eleven.field), (ERROR_TOO_MANY_RULES, "whitelist[0].rules"))

        twenty = [group(f"g{i}", rule(f"r{i}")) for i in range(GROUPS_MAX - 1)]
        self.assertEqual(parse_policy(document(whitelist=twenty)).group_count, GROUPS_MAX - 1)
        exactly_twenty = parse_policy(
            document(whitelist=twenty, blacklist=[group("bl", rule("rB"))])
        )
        self.assertEqual(exactly_twenty.group_count, GROUPS_MAX)

        twenty_one = refused(
            lambda: parse_policy(
                document(
                    whitelist=twenty,
                    blacklist=[group("bl", rule("rB")), group("bl2", rule("rB2"))],
                )
            )
        )
        self.assertEqual((twenty_one.code, twenty_one.field), (ERROR_TOO_MANY_GROUPS, "document"))

    def test_value_limits_are_per_operation(self):
        longest_text = "x" * VALUE_MAX
        longest_regex = "y" * REGEX_VALUE_MAX
        text_policy = parse_policy(
            document(whitelist=[group("wl", rule("r1", "title", "equals", longest_text))])
        )
        self.assertEqual(text_policy.whitelist[0].rules[0].value, longest_text)
        regex_policy = parse_policy(
            document(whitelist=[group("wl", rule("r1", "title", OP_REGEX, longest_regex))])
        )
        self.assertEqual(regex_policy.whitelist[0].rules[0].value, longest_regex)
        too_long_text = refused(
            lambda: parse_policy(
                document(whitelist=[group("wl", rule("r1", "title", "equals", longest_text + "x"))])
            )
        )
        self.assertEqual(
            (too_long_text.code, too_long_text.field),
            (ERROR_VALUE_TOO_LONG, "whitelist[0].rules[0].value"),
        )
        too_long_regex = refused(
            lambda: parse_policy(
                document(
                    whitelist=[group("wl", rule("r1", "title", OP_REGEX, longest_regex + "y"))]
                )
            )
        )
        self.assertEqual(
            (too_long_regex.code, too_long_regex.field),
            (ERROR_VALUE_TOO_LONG, "whitelist[0].rules[0].value"),
        )

    def test_values_are_not_stripped_and_not_interpolated(self):
        policy = parse_policy(
            document(
                whitelist=[
                    group("wl", rule("r1", "title", "equals", "  合集  ")),
                    group("wl2", rule("r2", "title", "equals", "{title}")),
                ]
            )
        )
        self.assertEqual(policy.whitelist[0].rules[0].value, "  合集  ")
        self.assertEqual(policy.whitelist[1].rules[0].value, "{title}")


class PolicyRefusalTest(unittest.TestCase):
    def test_malformed_documents_are_refused_with_a_fixed_code_and_path(self):
        cases: list[tuple[str, object, str, str]] = [
            ("not a mapping", ["schema_version"], ERROR_INVALID_DOCUMENT, "document"),
            ("none", None, ERROR_INVALID_DOCUMENT, "document"),
            (
                "unknown top key",
                {**document(), "extra": 1},
                ERROR_UNKNOWN_FIELD,
                "document",
            ),
            (
                "missing revision",
                {"schema_version": 1, "whitelist": [], "blacklist": []},
                ERROR_MISSING_FIELD,
                "revision",
            ),
            (
                "schema version 2",
                {**document(), "schema_version": 2},
                ERROR_INVALID_SCHEMA_VERSION,
                "schema_version",
            ),
            (
                "whitelist is not a list",
                {**document(), "whitelist": {"id": "wl"}},
                ERROR_INVALID_GROUP_LIST,
                "whitelist",
            ),
            (
                "blacklist is not a list",
                {**document(), "blacklist": "bl"},
                ERROR_INVALID_GROUP_LIST,
                "blacklist",
            ),
            (
                "group is not a mapping",
                document(whitelist=[["not", "a", "group"]]),  # type: ignore[list-item]
                ERROR_INVALID_GROUP,
                "whitelist[0]",
            ),
            (
                "group has an unknown key",
                document(whitelist=[{**group("wl", rule("r1")), "name": "白"}]),
                ERROR_UNKNOWN_FIELD,
                "whitelist[0]",
            ),
            (
                "group misses rules",
                document(whitelist=[{"id": "wl"}]),
                ERROR_INVALID_GROUP,
                "whitelist[0]",
            ),
            (
                "group id with a bad first character",
                document(whitelist=[group("-wl", rule("r1"))]),
                ERROR_INVALID_GROUP_ID,
                "whitelist[0].id",
            ),
            (
                "group id too long",
                document(whitelist=[group("g" * 65, rule("r1"))]),
                ERROR_INVALID_GROUP_ID,
                "whitelist[0].id",
            ),
            (
                "group id is not a string",
                document(whitelist=[{"id": 7, "rules": [rule("r1")]}]),
                ERROR_INVALID_GROUP_ID,
                "whitelist[0].id",
            ),
            (
                "rules is not a list",
                document(whitelist=[{"id": "wl", "rules": "r1"}]),
                ERROR_INVALID_RULES,
                "whitelist[0].rules",
            ),
            (
                "empty group",
                document(whitelist=[group("wl")]),
                ERROR_EMPTY_RULES,
                "whitelist[0].rules",
            ),
            (
                "rule is not a mapping",
                document(whitelist=[{"id": "wl", "rules": ["r1"]}]),
                ERROR_INVALID_RULE,
                "whitelist[0].rules[0]",
            ),
            (
                "rule has an unknown key",
                document(whitelist=[group("wl", {**rule("r1"), "negate": True})]),
                ERROR_UNKNOWN_FIELD,
                "whitelist[0].rules[0]",
            ),
            (
                "rule misses case_sensitive",
                document(
                    whitelist=[
                        group("wl", {"id": "r1", "field": "title", "op": "equals", "value": "x"})
                    ]
                ),
                ERROR_INVALID_RULE,
                "whitelist[0].rules[0]",
            ),
            (
                "rule id with a space",
                document(whitelist=[group("wl", rule("r 1"))]),
                ERROR_INVALID_RULE_ID,
                "whitelist[0].rules[0].id",
            ),
            (
                "unknown field",
                document(whitelist=[group("wl", rule("r1", "duration", "equals", "1"))]),
                ERROR_INVALID_FIELD_NAME,
                "whitelist[0].rules[0].field",
            ),
            (
                "field is not a string",
                document(whitelist=[group("wl", rule("r1", 7, "equals", "x"))]),  # type: ignore[arg-type]
                ERROR_INVALID_FIELD_NAME,
                "whitelist[0].rules[0].field",
            ),
            (
                "unknown operation",
                document(whitelist=[group("wl", rule("r1", "title", "glob", "*"))]),
                ERROR_INVALID_OP,
                "whitelist[0].rules[0].op",
            ),
            (
                "operation is not a string",
                document(whitelist=[group("wl", rule("r1", "title", 1, "x"))]),  # type: ignore[arg-type]
                ERROR_INVALID_OP,
                "whitelist[0].rules[0].op",
            ),
            (
                "empty value",
                document(whitelist=[group("wl", rule("r1", "title", "equals", ""))]),
                ERROR_INVALID_VALUE,
                "whitelist[0].rules[0].value",
            ),
            (
                "value is not a string",
                document(whitelist=[group("wl", rule("r1", "title", "equals", 1))]),  # type: ignore[arg-type]
                ERROR_INVALID_VALUE,
                "whitelist[0].rules[0].value",
            ),
            (
                "value carries NUL",
                document(whitelist=[group("wl", rule("r1", "title", "equals", "a\u0000b"))]),
                ERROR_INVALID_VALUE,
                "whitelist[0].rules[0].value",
            ),
            (
                "value carries a lone surrogate",
                document(whitelist=[group("wl", rule("r1", "title", "equals", "a\ud800b"))]),
                ERROR_INVALID_VALUE,
                "whitelist[0].rules[0].value",
            ),
            (
                "case_sensitive is not a bool",
                document(
                    whitelist=[group("wl", rule("r1", "title", "equals", "x", case_sensitive=1))]  # type: ignore[arg-type]
                ),
                ERROR_INVALID_CASE_SENSITIVE,
                "whitelist[0].rules[0].case_sensitive",
            ),
        ]
        for label, payload, code, field in cases:
            with self.subTest(case=label):
                error = refused(lambda entry=payload: parse_policy(entry))
                self.assertEqual((error.code, error.field), (code, field))

    def test_identifiers_are_unique_across_the_whole_policy(self):
        same_group = document(
            whitelist=[group("dup", rule("r1"))], blacklist=[group("dup", rule("r2"))]
        )
        error = refused(lambda: parse_policy(same_group))
        self.assertEqual((error.code, error.field), (ERROR_DUPLICATE_GROUP_ID, "whitelist[0].id"))

        same_rule_in_two_groups = document(
            whitelist=[group("wl", rule("dup"))], blacklist=[group("bl", rule("dup"))]
        )
        error = refused(lambda: parse_policy(same_rule_in_two_groups))
        self.assertEqual(
            (error.code, error.field), (ERROR_DUPLICATE_RULE_ID, "whitelist[0].rules[0].id")
        )

        same_rule_in_one_group = document(
            whitelist=[group("wl", rule("dup"), rule("dup", "tags", "equals", "x"))]
        )
        error = refused(lambda: parse_policy(same_rule_in_one_group))
        self.assertEqual(
            (error.code, error.field), (ERROR_DUPLICATE_RULE_ID, "whitelist[0].rules[1].id")
        )

    def test_errors_never_repeat_the_offending_value(self):
        secret = "SUPER-SECRET-TOKEN"
        error = refused(
            lambda: parse_policy(
                document(whitelist=[group("wl", rule("r1", "title", "glob", secret))])
            )
        )
        self.assertNotIn(secret, str(error))
        self.assertEqual(
            str(error),
            f"subscription rule refused: {ERROR_INVALID_OP} at whitelist[0].rules[0].op",
        )


class PolicyDigestTest(unittest.TestCase):
    def test_the_digest_matches_the_documented_json_recipe(self):
        expected = hashlib.sha256(SIMPLE_JSON.encode("utf-8")).hexdigest()
        policy = parse_policy(SIMPLE_DOCUMENT)
        self.assertEqual(policy.policy_digest, expected)
        self.assertEqual(
            policy_digest(policy.whitelist, policy.blacklist, policy.revision), expected
        )

    def test_key_order_in_the_input_does_not_change_the_digest(self):
        shuffled = {
            "whitelist": [
                {
                    "rules": [
                        {
                            "value": "合集",
                            "op": "contains",
                            "id": "r1",
                            "field": "title",
                            "case_sensitive": False,
                        }
                    ],
                    "id": "wl",
                }
            ],
            "revision": 3,
            "blacklist": [],
            "schema_version": 1,
        }
        self.assertEqual(
            parse_policy(shuffled).policy_digest, parse_policy(SIMPLE_DOCUMENT).policy_digest
        )

    def test_value_edit_and_array_order_change_the_digest(self):
        base = parse_policy(SIMPLE_DOCUMENT).policy_digest
        edited = parse_policy(
            document(revision=3, whitelist=[group("wl", rule("r1", "title", "contains", "合集2"))])
        ).policy_digest
        self.assertNotEqual(base, edited)

        first = document(revision=1, whitelist=[group("a", rule("r1")), group("b", rule("r2"))])
        second = document(revision=1, whitelist=[group("b", rule("r2")), group("a", rule("r1"))])
        self.assertNotEqual(parse_policy(first).policy_digest, parse_policy(second).policy_digest)

    def test_digest_is_a_fixed_length_lowercase_sha256(self):
        policy = parse_policy(SIMPLE_DOCUMENT)
        self.assertEqual(len(policy.policy_digest), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in policy.policy_digest))


class PolicyImmutabilityTest(unittest.TestCase):
    def test_policy_values_are_frozen_and_rules_are_tuples(self):
        policy = parse_policy(fx.text_policy())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.revision = 8  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            policy.extra = 1  # type: ignore[attr-defined]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.whitelist[0].rules[0].value = "改写"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            policy.whitelist[0].rules[0] = None  # type: ignore[index]


class RegexRuleDiscoveryTest(unittest.TestCase):
    def test_regex_rules_are_listed_in_the_frozen_trace_order(self):
        policy = parse_policy(
            document(
                whitelist=[
                    group("wl", rule("w_text", "title", "equals", "x")),
                    group("wl2", rule("w_regex", "tags", OP_REGEX, "^动")),
                ],
                blacklist=[group("bl", rule("b_regex", "description", OP_REGEX, "简介"))],
            )
        )
        found = [(position, index, entry.id) for position, index, entry in regex_rules(policy)]
        self.assertEqual(found, [(0, 0, "b_regex"), (2, 0, "w_regex")])

    def test_frozen_vocabulary_constants_are_reachable_from_the_package(self):
        self.assertEqual(GROUP_FIELDS, ("id", "rules"))
        self.assertEqual(RULE_FIELDS, ("id", "field", "op", "value", "case_sensitive"))
        self.assertEqual(
            sorted(["equals", "contains", "prefix", "suffix", OP_REGEX])[0], "contains"
        )
