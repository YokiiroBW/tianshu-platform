"""Typed rule semantics and real loopback HTTP previews over synthetic source membership."""

import copy
import hashlib
import unittest
from unittest.mock import patch

import test_media_runtime as runtime_tests
from media_rules._fixtures import document, group, metadata, rule, snapshot
from media_runtime_fixtures import BV, OTHER_BV, favorite_page, subscription
from services.platform.contracts import canonical, digest
from services.platform.media.metadata import normalize_bilibili
from services.platform.media.rule_context import (
    RuleContext,
    RuleSourceContext,
    build_rule_context,
    public_rule_context,
    rule_snapshot_revision,
)
from services.platform.media.rules import RuleEvaluator, RuleValidationError, parse_policy


def advanced(field, op, value, **options):
    return {**rule("typed", field, op, value), **options}


def policy_for(entry):
    return document(whitelist=[group("advanced", entry)])


class AdvancedRuleValidationTests(unittest.TestCase):
    def test_range_is_frozen_and_legacy_digest_is_unchanged(self):
        entry = advanced("duration_seconds", "between", [1, 900.5])
        parsed = parse_policy(policy_for(entry))
        entry["value"][0] = 700
        self.assertEqual(parsed.whitelist[0].rules[0].value, (1, 900.5))
        old = policy_for(rule("typed"))
        self.assertEqual(
            parse_policy(old).policy_digest, hashlib.sha256(canonical(old).encode()).hexdigest()
        )
        explicit_false = copy.deepcopy(old)
        explicit_false["whitelist"][0]["rules"][0]["negate"] = False
        self.assertEqual(
            parse_policy(explicit_false).policy_digest, parse_policy(old).policy_digest
        )
        explicit_false["whitelist"][0]["rules"][0]["negate"] = True
        self.assertNotEqual(
            parse_policy(explicit_false).policy_digest, parse_policy(old).policy_digest
        )

    def test_invalid_types_operators_ranges_and_timezone_are_refused(self):
        cases = [
            ("part_count", "equals", True),
            ("part_count", "equals", 2.0),
            ("part_count", "equals", -1),
            ("duration_seconds", "equals", "12"),
            ("duration_seconds", "equals", float("inf")),
            ("duration_seconds", "equals", float("nan")),
            ("duration_seconds", "between", [20, 10]),
            ("duration_seconds", "between", [1]),
            ("duration_seconds", "between", [1, 2, 3]),
            ("duration_seconds", "between", [1, True]),
            ("duration_seconds", "gt", [1, 2]),
            ("duration_seconds", "contains", "2"),
            ("published_at", "equals", "2026-10-15T12:00:00"),
            ("published_at", "equals", "2026-10-15"),
            ("published_at", "equals", "2026-02-30T12:00:00Z"),
            ("published_at", "equals", "2026-10-15T12:00:00+24:00"),
            ("published_at", "equals", "2026-10-15T12:00:00+08:99"),
            ("favorited_at", "between", ["2026-10-15T12:00:00Z", "2026-10-14T12:00:00Z"]),
            ("is_collaboration", "equals", "false"),
            ("is_collaboration", "equals", 0),
            ("is_collaboration", "gt", True),
            ("is_collaboration", "between", [False, True]),
            ("tags", "gt", "x"),
        ]
        for field, op, value in cases:
            with (
                self.subTest(field=field, op=op, value=value),
                self.assertRaises(RuleValidationError),
            ):
                parse_policy(policy_for(advanced(field, op, value)))
        for value in (None, 0, "false", []):
            with self.subTest(negate=value), self.assertRaises(RuleValidationError) as caught:
                parse_policy(policy_for(advanced("part_count", "equals", 1, negate=value)))
            self.assertEqual(caught.exception.code, "invalid_negate")

    def test_context_is_bound_to_the_exact_source_and_video(self):
        record = metadata()
        for member in (
            RuleSourceContext(OTHER_BV, "favorite", "123"),
            RuleSourceContext(BV, "favorite", "456"),
            RuleSourceContext(BV, "watch_later", "self"),
        ):
            with self.subTest(member=member), self.assertRaises(ValueError):
                build_rule_context(record, {}, {"kind": "favorite", "id": "123"}, member)


class AdvancedRuleDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.evaluator = RuleEvaluator()
        self.addAsyncCleanup(self.evaluator.aclose)

    async def decide(self, entry, *, record=None, context=None):
        return await self.evaluator.evaluate(
            record or metadata(),
            policy_for(entry),
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="synthetic-advanced-1",
            context=context,
        )

    async def test_numeric_comparisons_use_inclusive_range_and_explicit_edges(self):
        for op, wanted, expected in (
            ("equals", 725, True),
            ("not_equals", 725, False),
            ("gt", 725, False),
            ("gte", 725, True),
            ("lt", 725, False),
            ("lte", 725, True),
            ("between", [725, 725], True),
            ("between", [725, 726], True),
            ("between", [0, 724.5], False),
            ("gt", 724.5, True),
        ):
            with self.subTest(op=op, wanted=wanted):
                decision = await self.decide(advanced("duration_seconds", op, wanted))
                self.assertEqual(decision.automatic_enqueue_allowed, expected)

    async def test_timestamps_compare_instants_across_timezones_and_range_boundaries(self):
        for op, wanted, expected in (
            ("equals", "2019-08-02T00:30:00Z", True),
            ("equals", "2019-08-02T00:30:00.000000+00:00", True),
            ("not_equals", "2019-08-02T00:30:00Z", False),
            ("gt", "2019-08-02T08:30:00+08:00", False),
            ("gte", "2019-08-02T08:30:00+08:00", True),
            ("lt", "2019-08-02T00:30:01Z", True),
            ("lte", "2019-08-02T00:30:00Z", True),
            ("between", ["2019-08-02T00:30:00Z", "2019-08-02T08:30:00+08:00"], True),
        ):
            with self.subTest(op=op, wanted=wanted):
                result = await self.decide(advanced("published_at", op, wanted))
                self.assertEqual(result.automatic_enqueue_allowed, expected)

    async def test_total_duration_and_part_count_cover_all_parts(self):
        record = metadata(
            parts=[
                {"cid": "111", "index": 1, "title": "A", "duration_seconds": 20},
                {"cid": "222", "index": 2, "title": "B", "duration_seconds": 30},
            ]
        )
        for field, wanted in (("duration_seconds", 50), ("part_count", 2)):
            result = await self.decide(advanced(field, "equals", wanted), record=record)
            self.assertTrue(result.automatic_enqueue_allowed)

    async def test_missing_facts_remain_unmatched_under_not_equals_and_negate(self):
        records = {
            "published_at": metadata(published_at=None),
            "favorited_at": metadata(),
            "duration_seconds": metadata(
                parts=[
                    {"cid": "111", "index": 1, "title": "A", "duration_seconds": 725},
                    {"cid": "222", "index": 2, "title": "B", "duration_seconds": None},
                ]
            ),
            "is_collaboration": metadata(creators=[]),
            "title": metadata(title=None),
        }
        wanted = {
            "published_at": "2019-01-01T00:00:00Z",
            "favorited_at": "2019-01-01T00:00:00Z",
            "duration_seconds": 725,
            "is_collaboration": False,
            "title": "x",
        }
        for field, record in records.items():
            for op in ("equals", "not_equals"):
                for negate in (False, True):
                    with self.subTest(field=field, op=op, negate=negate):
                        result = await self.decide(
                            advanced(field, op, wanted[field], negate=negate), record=record
                        )
                        self.assertFalse(result.automatic_enqueue_allowed)
                        trace = result.trace[0].rules[0]
                        self.assertEqual(
                            (trace.result, trace.reason), ("not_matched", "field_missing")
                        )

    async def test_zero_duration_and_false_boolean_are_available_values(self):
        zero = metadata(parts=[{"cid": "111", "index": 1, "title": "A", "duration_seconds": 0}])
        self.assertTrue(
            (
                await self.decide(advanced("duration_seconds", "equals", 0), record=zero)
            ).automatic_enqueue_allowed
        )
        context = RuleContext(is_collaboration=False)
        self.assertTrue(
            (
                await self.decide(advanced("is_collaboration", "equals", False), context=context)
            ).automatic_enqueue_allowed
        )
        self.assertTrue(
            (
                await self.decide(advanced("is_collaboration", "not_equals", True), context=context)
            ).automatic_enqueue_allowed
        )
        self.assertFalse(
            (
                await self.decide(
                    advanced("is_collaboration", "equals", False, negate=True), context=context
                )
            ).automatic_enqueue_allowed
        )

    async def test_text_not_equals_negates_the_whole_multivalue_field(self):
        self.assertFalse(
            (await self.decide(advanced("tags", "not_equals", "动画"))).automatic_enqueue_allowed
        )
        self.assertTrue(
            (await self.decide(advanced("tags", "not_equals", "OTHER"))).automatic_enqueue_allowed
        )
        self.assertTrue(
            (
                await self.decide(advanced("tags", "equals", "OTHER", negate=True))
            ).automatic_enqueue_allowed
        )

    async def test_negated_regex_uses_existing_bounded_worker(self):
        result = await self.decide(advanced("title", "regex", "^does-not-match$", negate=True))
        self.assertTrue(result.automatic_enqueue_allowed)
        self.assertTrue(result.trace[0].rules[0].negate)
        missing = await self.decide(
            advanced("title", "regex", ".*", negate=True), record=metadata(title=None)
        )
        self.assertEqual(missing.trace[0].rules[0].reason, "field_missing")

    async def test_mixed_groups_keep_and_or_and_blacklist_priority(self):
        policy = document(
            blacklist=[group("block", advanced("part_count", "equals", 1))],
            whitelist=[group("allow", rule("text"))],
        )
        decision = await self.evaluator.evaluate(
            metadata(), policy, accessible=True, quality_satisfied=False, snapshot_revision="mixed"
        )
        self.assertEqual(decision.reason, "blacklist_match")
        self.assertEqual(decision.trace[1].rules[0].result, "not_evaluated")

    def test_context_digest_changes_with_source_time_and_legacy_revision_stays_stable(self):
        source_value = {"kind": "favorite", "id": "123"}
        record = normalize_bilibili(snapshot())
        first = build_rule_context(
            record,
            {},
            source_value,
            RuleSourceContext(BV, "favorite", "123", "2026-10-14T00:00:00Z"),
        )
        second = build_rule_context(
            record,
            {},
            source_value,
            RuleSourceContext(BV, "favorite", "123", "2026-10-15T00:00:00Z"),
        )
        typed = parse_policy(policy_for(advanced("favorited_at", "gt", "2026-10-13T00:00:00Z")))
        self.assertNotEqual(
            rule_snapshot_revision(snapshot(), first, typed),
            rule_snapshot_revision(snapshot(), second, typed),
        )
        legacy = parse_policy(policy_for(rule("text")))
        self.assertEqual(
            rule_snapshot_revision(snapshot(), first, legacy), digest(canonical(snapshot()))
        )


class AdvancedRuleHTTPTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the existing real login/server/SQLite fixture without inheriting its unrelated tests.
    asyncSetUp = runtime_tests.MediaRuntimeTests.asyncSetUp
    post = runtime_tests.MediaRuntimeTests.post
    save = runtime_tests.MediaRuntimeTests.save

    async def preview_id(self, account_id=None):
        self.media.connector.clock = lambda: 1800000000
        return (await self.post("resolve", {"url": BV, "account_id": account_id}))["preview_id"]

    def favorite_members(self, *members):
        page = favorite_page(len(members), [])
        page["medias"] = [
            {"bvid": bvid, "fav_time": favorite_time} for bvid, favorite_time in members
        ]
        self.upstream.favorite_pages = [page]

    async def test_preview_and_scan_share_real_favorite_context_and_reproducible_evidence(self):
        self.favorite_members((BV, 1700000000))
        body = subscription()
        body["rules"] = policy_for(advanced("favorited_at", "equals", "2023-11-15T06:13:20+08:00"))
        saved = await self.save(body)
        preview = await self.post(
            "rules/preview",
            {
                "preview_id": await self.preview_id(),
                "rules": body["rules"],
                "subscription_id": saved["subscription_id"],
            },
        )
        self.assertEqual(
            preview["context"]["fields"]["favorited_at"],
            {"available": True, "value": "2023-11-14T22:13:20Z"},
        )
        self.assertTrue(preview["context"]["member"])
        self.assertEqual(preview["context"]["fields"]["part_count"]["value"], 2)
        self.assertEqual(preview["context"]["fields"]["duration_seconds"]["value"], 4)
        self.assertEqual(
            preview["context"]["fields"]["is_collaboration"], {"available": True, "value": False}
        )
        result = await self.media.subscriptions.scan(saved["subscription_id"])
        self.assertEqual(result["queued"], 1)
        jobs, _ = self.media.repo.jobs()
        evidence = jobs[0]["rule_evidence"]
        self.assertEqual(
            evidence["snapshot_revision"], preview["results"][0]["trace"]["snapshot_revision"]
        )
        self.assertEqual(evidence["context"]["favorited_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(
            evidence["snapshot_revision"],
            digest(
                canonical({"snapshot": jobs[0]["snapshot"], "rule_context": evidence["context"]})
            ),
        )

    async def test_selected_folder_controls_time_and_unknown_is_explained(self):
        identity = await self.preview_id()
        rules = policy_for(advanced("favorited_at", "gte", "2023-01-01T00:00:00Z", negate=True))
        unknown = await self.post("rules/preview", {"preview_id": identity, "rules": rules})
        self.assertIsNone(unknown["context"]["member"])
        self.assertFalse(unknown["results"][0]["automatic_enqueue_allowed"])
        self.assertEqual(
            unknown["results"][0]["trace"]["trace"][0]["rules"][0]["reason"], "field_missing"
        )
        revisions = []
        for folder, stamp in (("123", 1700000000), ("456", 1700000100)):
            self.favorite_members((BV, stamp))
            result = await self.post(
                "rules/preview",
                {
                    "preview_id": identity,
                    "rules": rules,
                    "source": {"kind": "favorite", "id": folder},
                },
            )
            self.assertEqual(result["context"]["source"]["id"], folder)
            revisions.append(result["results"][0]["trace"]["snapshot_revision"])
        self.assertNotEqual(*revisions)
        self.favorite_members((OTHER_BV, 1700000000))
        absent = await self.post(
            "rules/preview",
            {"preview_id": identity, "rules": rules, "source": {"kind": "favorite", "id": "123"}},
        )
        self.assertFalse(absent["context"]["member"])
        self.assertFalse(absent["context"]["fields"]["favorited_at"]["available"])

    async def test_watch_later_does_not_fabricate_favorite_time(self):
        account = self.media.accounts.save("合成账号", "SESSDATA=synthetic", {"mid": "946974"})
        result = await self.post(
            "rules/preview",
            {
                "preview_id": await self.preview_id(account["account_id"]),
                "rules": policy_for(
                    advanced("favorited_at", "equals", "2023-01-01T00:00:00Z", negate=True)
                ),
                "source": {"kind": "watch_later", "id": "self"},
            },
        )
        self.assertTrue(result["context"]["member"])
        self.assertFalse(result["context"]["fields"]["favorited_at"]["available"])
        self.assertFalse(result["results"][0]["automatic_enqueue_allowed"])

    async def test_browser_cannot_inject_time_or_cross_account_subscription_context(self):
        identity = await self.preview_id()
        body = {"preview_id": identity, "rules": document()}
        rejected = await self.post(
            "rules/preview", {**body, "favorited_at": "2026-10-15T00:00:00Z"}, 400
        )
        self.assertEqual(rejected["code"], "invalid_input")
        await self.post(
            "rules/preview",
            {**body, "source": {"kind": "favorite", "id": "123"}, "subscription_id": "both"},
            400,
        )
        account = self.media.accounts.save("合成账号", "SESSDATA=synthetic", {"mid": "946974"})
        subscription_body = subscription()
        subscription_body["account_id"] = account["account_id"]
        saved = await self.save(subscription_body)
        rejected = await self.post(
            "rules/preview", {**body, "subscription_id": saved["subscription_id"]}, 409
        )
        self.assertEqual(rejected["code"], "preview_account_conflict")

    async def test_incomplete_preview_scan_propagates_failure_and_does_not_change_baseline(self):
        self.favorite_members((BV, 1700000000))
        saved = await self.save()
        await self.media.subscriptions.scan(saved["subscription_id"])
        original = self.media.repo.members(saved["subscription_id"])
        self.upstream.favorite_pages = [favorite_page(2, [BV], True)]
        self.media.config["scan_page_limit"] = 1
        failed = await self.post(
            "rules/preview",
            {
                "preview_id": await self.preview_id(),
                "rules": document(),
                "subscription_id": saved["subscription_id"],
            },
            503,
        )
        self.assertEqual(failed["code"], "source_budget_exceeded")
        self.assertEqual(self.media.repo.members(saved["subscription_id"]), original)

    async def test_connector_preserves_joint_flag_without_changing_metadata_contract(self):
        original = self.media.connector.request

        async def joint(path, **kwargs):
            data = await original(path, **kwargs)
            if path == "/x/web-interface/view":
                data["staff"] = [{"mid": 946974, "name": "测试UP主"}]
            return data

        with patch.object(self.media.connector, "request", joint):
            resolved = await self.media.connector.resolve(BV, with_formats=False)
        record = normalize_bilibili(resolved["snapshot"])
        context = build_rule_context(record, resolved)
        self.assertTrue(context.is_collaboration)
        self.assertNotIn("is_collaboration", resolved["snapshot"])
        self.assertEqual(
            public_rule_context(record, context)["fields"]["is_collaboration"]["value"], True
        )
