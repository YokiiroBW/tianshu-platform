"""Explained decisions: priority, AND/OR, short-circuit traces and the caller-fact contract.

Every expectation here is a whole decision read back field by field, and the trace expectations are
written per rule so that a change in short-circuit behaviour cannot hide behind an aggregate count.
Only ordinary-text policies are used, so this module needs no child process: the regex path is
covered by ``test_regex_process`` and ``test_bounds``.
"""

from __future__ import annotations

import dataclasses
import unittest

try:
    from . import _fixtures as fx
    from ._fixtures import document, group, metadata, rule, text_policy
except ImportError:  # narrow discovery: this directory is the top-level start directory
    import _fixtures as fx
    from _fixtures import document, group, metadata, rule, text_policy

from services.platform.media.rules import (
    DECISION_DOWNLOAD,
    DECISION_SKIP,
    ERROR_INVALID_ACCESSIBLE,
    ERROR_INVALID_METADATA,
    ERROR_INVALID_QUALITY_SATISFIED,
    ERROR_INVALID_SNAPSHOT_REVISION,
    REASON_BLACKLIST_MATCH,
    REASON_ELIGIBLE,
    REASON_INACCESSIBLE,
    REASON_QUALITY_SATISFIED,
    REASON_WHITELIST_NO_MATCH,
    RESULT_MATCHED,
    RESULT_NOT_EVALUATED,
    RESULT_NOT_MATCHED,
    TRACE_BLACKLIST_MATCHED,
    TRACE_PRIORITY_SHORT_CIRCUIT,
    TRACE_RULE_SHORT_CIRCUIT,
    TRACE_WHITELIST_MATCHED,
    RuleEvaluator,
    RuleValidationError,
)


def trace_rows(decision) -> list[tuple[str, str, str, str, str, str]]:
    """Flatten a decision's trace into comparable rows: group, list, result, then each rule."""

    rows: list[tuple[str, str, str, str, str, str]] = []
    for group_trace in decision.trace:
        rows.append(
            (
                group_trace.group_id,
                group_trace.list_kind,
                group_trace.result,
                group_trace.reason,
                "",
                "",
            )
        )
        for rule_trace in group_trace.rules:
            rows.append(
                (
                    group_trace.group_id,
                    group_trace.list_kind,
                    rule_trace.result,
                    rule_trace.reason,
                    rule_trace.rule_id,
                    f"{rule_trace.field}/{rule_trace.op}",
                )
            )
    return rows


def refused_error(call) -> RuleValidationError:
    try:
        call()
    except RuleValidationError as error:
        return error
    raise AssertionError("expected RuleValidationError")


class DecisionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.evaluator = RuleEvaluator()

    async def asyncTearDown(self) -> None:
        await self.evaluator.aclose()

    async def decide(
        self,
        policy: object,
        *,
        record=None,
        accessible: bool = True,
        quality_satisfied: bool = False,
        snapshot_revision: str = "scan-2026-09-19-000001",
    ):
        return await self.evaluator.evaluate(
            record if record is not None else metadata(),
            policy,
            accessible=accessible,
            quality_satisfied=quality_satisfied,
            snapshot_revision=snapshot_revision,
        )

    async def test_an_empty_policy_downloads_an_accessible_item(self):
        decision = await self.decide(document())
        self.assertEqual(decision.decision, DECISION_DOWNLOAD)
        self.assertEqual(decision.reason, REASON_ELIGIBLE)
        self.assertTrue(decision.automatic_enqueue_allowed)
        self.assertFalse(decision.requires_rule_attention)
        self.assertEqual(decision.item_key, f"bilibili:video:{fx.BV}")
        self.assertEqual(decision.snapshot_revision, "scan-2026-09-19-000001")
        self.assertEqual(decision.policy_revision, 7)
        self.assertEqual(decision.trace, ())
        self.assertEqual(decision.group_count, 0)
        self.assertEqual(decision.rule_count, 0)

    async def test_inaccessible_stops_before_any_rule_and_says_so_per_rule(self):
        policy = text_policy()
        decision = await self.decide(policy, accessible=False)
        self.assertEqual((decision.decision, decision.reason), (DECISION_SKIP, REASON_INACCESSIBLE))
        self.assertFalse(decision.automatic_enqueue_allowed)
        self.assertEqual(
            trace_rows(decision),
            [
                ("bl", "blacklist", RESULT_NOT_EVALUATED, TRACE_PRIORITY_SHORT_CIRCUIT, "", ""),
                (
                    "bl",
                    "blacklist",
                    RESULT_NOT_EVALUATED,
                    TRACE_PRIORITY_SHORT_CIRCUIT,
                    "r_preview",
                    "title/contains",
                ),
                ("wl", "whitelist", RESULT_NOT_EVALUATED, TRACE_PRIORITY_SHORT_CIRCUIT, "", ""),
                (
                    "wl",
                    "whitelist",
                    RESULT_NOT_EVALUATED,
                    TRACE_PRIORITY_SHORT_CIRCUIT,
                    "r_title",
                    "title/contains",
                ),
            ],
        )

    async def test_quality_satisfied_stops_before_any_rule(self):
        decision = await self.decide(text_policy(), quality_satisfied=True)
        self.assertEqual(
            (decision.decision, decision.reason), (DECISION_SKIP, REASON_QUALITY_SATISFIED)
        )
        self.assertFalse(decision.automatic_enqueue_allowed)
        self.assertTrue(
            all(
                row[3] == TRACE_PRIORITY_SHORT_CIRCUIT
                for row in trace_rows(decision)
                if row[2] == RESULT_NOT_EVALUATED
            )
        )

    async def test_accessibility_wins_over_quality_and_blacklist(self):
        decision = await self.decide(text_policy(), accessible=False, quality_satisfied=True)
        self.assertEqual(decision.reason, REASON_INACCESSIBLE)

    async def test_a_matching_blacklist_group_skips_and_never_reads_the_whitelist(self):
        policy = document(
            whitelist=[group("wl", rule("r_title", "title", "contains", "合集"))],
            blacklist=[group("bl", rule("r_preview", "title", "contains", "预告"))],
        )
        decision = await self.decide(policy)
        self.assertEqual(
            (decision.decision, decision.reason), (DECISION_SKIP, REASON_BLACKLIST_MATCH)
        )
        self.assertFalse(decision.automatic_enqueue_allowed)
        self.assertEqual(
            trace_rows(decision),
            [
                ("bl", "blacklist", RESULT_MATCHED, RESULT_MATCHED, "", ""),
                ("bl", "blacklist", RESULT_MATCHED, RESULT_MATCHED, "r_preview", "title/contains"),
                ("wl", "whitelist", RESULT_NOT_EVALUATED, TRACE_BLACKLIST_MATCHED, "", ""),
                (
                    "wl",
                    "whitelist",
                    RESULT_NOT_EVALUATED,
                    TRACE_BLACKLIST_MATCHED,
                    "r_title",
                    "title/contains",
                ),
            ],
        )

    async def test_a_blacklist_group_requires_all_of_its_rules(self):
        policy = document(
            blacklist=[
                group(
                    "bl",
                    rule("r_preview", "title", "contains", "预告"),
                    rule("r_up", "uploader_name", "equals", "别的UP主"),
                )
            ]
        )
        decision = await self.decide(policy)
        self.assertEqual((decision.decision, decision.reason), (DECISION_DOWNLOAD, REASON_ELIGIBLE))
        self.assertEqual(
            trace_rows(decision),
            [
                ("bl", "blacklist", RESULT_NOT_MATCHED, RESULT_NOT_MATCHED, "", ""),
                ("bl", "blacklist", RESULT_MATCHED, RESULT_MATCHED, "r_preview", "title/contains"),
                (
                    "bl",
                    "blacklist",
                    RESULT_NOT_MATCHED,
                    RESULT_NOT_MATCHED,
                    "r_up",
                    "uploader_name/equals",
                ),
            ],
        )

    async def test_a_rule_inside_a_group_stops_the_rest_of_that_group_only(self):
        policy = document(
            whitelist=[
                group(
                    "wl_a",
                    rule("r_absent", "title", "contains", "不存在的词"),
                    rule("r_never", "title", "contains", "合集"),
                ),
                group("wl_b", rule("r_tags", "tags", "equals", "动画")),
            ]
        )
        decision = await self.decide(policy)
        self.assertEqual((decision.decision, decision.reason), (DECISION_DOWNLOAD, REASON_ELIGIBLE))
        self.assertEqual(
            trace_rows(decision),
            [
                ("wl_a", "whitelist", RESULT_NOT_MATCHED, RESULT_NOT_MATCHED, "", ""),
                (
                    "wl_a",
                    "whitelist",
                    RESULT_NOT_MATCHED,
                    RESULT_NOT_MATCHED,
                    "r_absent",
                    "title/contains",
                ),
                (
                    "wl_a",
                    "whitelist",
                    RESULT_NOT_EVALUATED,
                    TRACE_RULE_SHORT_CIRCUIT,
                    "r_never",
                    "title/contains",
                ),
                ("wl_b", "whitelist", RESULT_MATCHED, RESULT_MATCHED, "", ""),
                ("wl_b", "whitelist", RESULT_MATCHED, RESULT_MATCHED, "r_tags", "tags/equals"),
            ],
        )

    async def test_groups_are_or_ed_and_a_later_match_leaves_later_groups_unevaluated(self):
        policy = document(
            whitelist=[
                group("wl_a", rule("r_a", "title", "contains", "没有")),
                group("wl_b", rule("r_b", "title", "contains", "合集")),
                group("wl_c", rule("r_c", "tags", "equals", "动画")),
            ]
        )
        decision = await self.decide(policy)
        self.assertEqual((decision.decision, decision.reason), (DECISION_DOWNLOAD, REASON_ELIGIBLE))
        self.assertEqual(
            trace_rows(decision),
            [
                ("wl_a", "whitelist", RESULT_NOT_MATCHED, RESULT_NOT_MATCHED, "", ""),
                (
                    "wl_a",
                    "whitelist",
                    RESULT_NOT_MATCHED,
                    RESULT_NOT_MATCHED,
                    "r_a",
                    "title/contains",
                ),
                ("wl_b", "whitelist", RESULT_MATCHED, RESULT_MATCHED, "", ""),
                (
                    "wl_b",
                    "whitelist",
                    RESULT_MATCHED,
                    RESULT_MATCHED,
                    "r_b",
                    "title/contains",
                ),
                ("wl_c", "whitelist", RESULT_NOT_EVALUATED, TRACE_WHITELIST_MATCHED, "", ""),
                (
                    "wl_c",
                    "whitelist",
                    RESULT_NOT_EVALUATED,
                    TRACE_WHITELIST_MATCHED,
                    "r_c",
                    "tags/equals",
                ),
            ],
        )

    async def test_a_non_empty_whitelist_that_does_not_match_skips(self):
        policy = document(whitelist=[group("wl", rule("r1", "title", "contains", "不存在的词"))])
        decision = await self.decide(policy)
        self.assertEqual(
            (decision.decision, decision.reason), (DECISION_SKIP, REASON_WHITELIST_NO_MATCH)
        )
        self.assertFalse(decision.automatic_enqueue_allowed)
        self.assertFalse(decision.requires_rule_attention)

    async def test_multi_value_rules_and_uploader_role(self):
        policy = document(
            whitelist=[
                group(
                    "wl",
                    rule("r_name", "uploader_name", "equals", "测试up主"),
                    rule("r_tag", "tags", "equals", "合集"),
                )
            ]
        )
        decision = await self.decide(policy)
        self.assertEqual((decision.decision, decision.reason), (DECISION_DOWNLOAD, REASON_ELIGIBLE))

    async def test_a_field_with_no_value_never_matches_including_a_null_rule(self):
        policy = document(
            whitelist=[
                group("wl", rule("r_blank", "title", "contains", "总集篇")),
                group("wl_b", rule("r_other", "tags", "equals", "动画")),
            ]
        )
        decision = await self.decide(policy, record=metadata(title="   "))
        self.assertEqual((decision.decision, decision.reason), (DECISION_DOWNLOAD, REASON_ELIGIBLE))
        rows = trace_rows(decision)
        self.assertEqual(rows[1][:4], ("wl", "whitelist", RESULT_NOT_MATCHED, RESULT_NOT_MATCHED))

    async def test_an_empty_whitelist_means_no_threshold(self):
        policy = document(
            whitelist=[], blacklist=[group("bl", rule("r1", "tags", "equals", "音乐"))]
        )
        decision = await self.decide(policy)
        self.assertEqual((decision.decision, decision.reason), (DECISION_DOWNLOAD, REASON_ELIGIBLE))

    async def test_caller_facts_are_refused_instead_of_coerced(self):
        policy = document()
        cases: list[tuple[str, object, str, str]] = [
            ("accessible is a string", "true", ERROR_INVALID_ACCESSIBLE, "accessible"),
            ("accessible is an int", 1, ERROR_INVALID_ACCESSIBLE, "accessible"),
            ("quality is an int", 0, ERROR_INVALID_QUALITY_SATISFIED, "quality_satisfied"),
            ("quality is None", None, ERROR_INVALID_QUALITY_SATISFIED, "quality_satisfied"),
            ("revision is empty", "", ERROR_INVALID_SNAPSHOT_REVISION, "snapshot_revision"),
            ("revision has a dot", "rev.1", ERROR_INVALID_SNAPSHOT_REVISION, "snapshot_revision"),
            (
                "revision is too long",
                "r" * 129,
                ERROR_INVALID_SNAPSHOT_REVISION,
                "snapshot_revision",
            ),
            ("revision is not a string", 7, ERROR_INVALID_SNAPSHOT_REVISION, "snapshot_revision"),
        ]
        for label, bad, code, field in cases:
            with self.subTest(case=label):
                kwargs = {
                    "accessible": True,
                    "quality_satisfied": False,
                    "snapshot_revision": "rev-1",
                }
                if field == "accessible":
                    kwargs["accessible"] = bad
                elif field == "quality_satisfied":
                    kwargs["quality_satisfied"] = bad
                else:
                    kwargs["snapshot_revision"] = bad
                error = None
                try:
                    await self.evaluator.evaluate(metadata(), policy, **kwargs)  # type: ignore[arg-type]
                except RuleValidationError as raised:
                    error = raised
                self.assertIsNotNone(error)
                self.assertEqual((error.code, error.field), (code, field))

    async def test_a_non_metadata_argument_is_refused(self):
        try:
            await self.evaluator.evaluate(  # type: ignore[arg-type]
                {"not": "metadata"},
                document(),
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        except RuleValidationError as error:
            self.assertEqual((error.code, error.field), (ERROR_INVALID_METADATA, "metadata"))
        else:
            raise AssertionError("expected RuleValidationError")

    async def test_snapshot_revision_is_returned_verbatim_as_an_opaque_string(self):
        for revision in ("1", "rev-000042", "A" * 128, "snapshot_revision-9"):
            with self.subTest(revision=revision):
                decision = await self.decide(document(), snapshot_revision=revision)
                self.assertEqual(decision.snapshot_revision, revision)

    async def test_the_decision_is_immutable_and_freezes_its_trace(self):
        decision = await self.decide(text_policy())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.decision = DECISION_DOWNLOAD  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            decision.extra = 1  # type: ignore[attr-defined]
        with self.assertRaises(TypeError):
            decision.trace[0] = None  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.trace[0].rules[0].result = RESULT_MATCHED  # type: ignore[misc]

    async def test_the_trace_carries_no_source_text(self):
        record = metadata(title="只有这一份的独特标题", description="只有这一份的独特简介")
        decision = await self.decide(
            document(
                whitelist=[group("wl", rule("r1", "title", "equals", "只有这一份的独特标题"))],
                blacklist=[group("bl", rule("r2", "description", "contains", "独特简介"))],
            ),
            record=record,
        )
        rendered = repr(decision)
        self.assertNotIn("只有这一份的独特标题", rendered)
        self.assertNotIn("只有这一份的独特简介", rendered)
        self.assertIn("r1", rendered)

    async def test_the_policy_document_and_metadata_are_not_modified(self):
        policy = text_policy()
        record = metadata()
        before_policy = repr(policy)
        before_record = repr(record)
        await self.decide(policy, record=record)
        self.assertEqual(repr(policy), before_policy)
        self.assertEqual(repr(record), before_record)

    async def test_evaluated_rule_count_reports_only_rules_that_ran(self):
        decision = await self.decide(text_policy())
        self.assertEqual(decision.rule_count, 2)
        self.assertEqual(decision.evaluated_rule_count, 1)

    async def test_a_closed_evaluator_refuses_new_calls(self):
        evaluator = RuleEvaluator()
        await evaluator.aclose()
        with self.assertRaises(Exception) as caught:
            await evaluator.evaluate(
                metadata(),
                document(),
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        self.assertEqual(getattr(caught.exception, "code", None), "closed")
        with self.assertRaises(Exception) as caught_validate:
            await evaluator.validate_policy(document())
        self.assertEqual(getattr(caught_validate.exception, "code", None), "closed")

    async def test_the_context_manager_closes_the_evaluator(self):
        async with RuleEvaluator() as evaluator:
            self.assertEqual((await evaluator.validate_policy(document())).revision, 7)
        with self.assertRaises(Exception):
            await evaluator.validate_policy(document())
