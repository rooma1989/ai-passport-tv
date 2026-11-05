"""Bilibili favourite folders as TV channels (P1c): refresh with the owner's own login, list the
folders, add the ticked ones. Design: docs/superpowers/specs/2026-11-04-minimal-product-design.md §5."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import state as state_mod
from server.relay.state import Video
from test_relay_bililogin import RelayHarness


class RefreshTests(RelayHarness):
    def test_a_favourite_refreshes_with_the_owners_login_and_no_shared_cache(self):
        state = self.relay.user_state(self.admin)
        creator = state.add("bilibili", "fav111", "默认收藏夹", [Video("BV1a", "a", 1, 1)], 0.0,
                            kind="series", prefix=state_mod.FAV_PREFIX, most=state_mod.MAX_FAVS)
        login = object()
        self.relay.bili_login = lambda user: login if user == self.admin else None
        seen = []
        self.client.series = lambda source, login=None: (seen.append((source, login)),
                                                         ("默认收藏夹", [Video("BV1b", "b", 2, 2)]))[1]
        self.relay.refresh_if_stale(creator.id, state, owner=self.admin)
        self.assertEqual(seen, [("fav111", login)])
        self.assertEqual([v.bvid for v in state.get(creator.id).videos], ["BV1b"])
        self.assertIsNone(self.accounts.cached_source("bilibili", "fav111", "series"),
                          "a private folder never goes into the shared cache")

    def test_no_login_keeps_the_old_list(self):
        state = self.relay.user_state(self.admin)
        creator = state.add("bilibili", "fav111", "默认收藏夹", [Video("BV1a", "a", 1, 1)], 0.0,
                            kind="series", prefix=state_mod.FAV_PREFIX, most=state_mod.MAX_FAVS)
        self.relay.bili_login = lambda user: None
        self.client.series = lambda *a, **k: self.fail("must not fetch without a login")
        self.relay.refresh_if_stale(creator.id, state, owner=self.admin)
        self.assertEqual([v.bvid for v in state.get(creator.id).videos], ["BV1a"])

    def test_watching_on_the_computer_refreshes_with_the_viewers_login(self):
        from server.relay.tenants import Caller
        state = self.relay.user_state(self.admin)
        creator = state.add("bilibili", "fav111", "默认收藏夹", [Video("BV1a", "a", 1, 1)], 0.0,
                            kind="series", prefix=state_mod.FAV_PREFIX, most=state_mod.MAX_FAVS)
        owners = []
        self.relay.refresh_if_stale = lambda cid, state=None, owner=None: owners.append((cid, owner))
        self.relay._pc_subscription(Caller(user=self.admin), state, creator.id, {})
        self.assertEqual(owners[:1], [(creator.id, self.admin)])


class FavoritesApiTests(RelayHarness):
    def setUp(self):
        super().setUp()
        self.sign_in(self.admin)
        self.folders = [{"id": "111", "title": "默认收藏夹", "count": 3}, {"id": "222", "title": "动画", "count": 0}]
        self.client.favorite_folders = lambda mid, login=None: list(self.folders)
        self.client.series = lambda source, login=None: (
            ("默认收藏夹", [Video("BV1a", "a", 1, 1)]) if source == "fav111" else ("动画", []))

    def test_lists_folders_with_subscribed_flags(self):
        status, answer = self.request("GET", "/tv/api/bili/favorites", user=self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([(i["id"], i["subscribed"]) for i in answer["items"]], [("111", False), ("222", False)])
        self.assertEqual(answer["max"], state_mod.MAX_FAVS)

    def test_import_adds_channels_and_explains_skips(self):
        status, answer = self.request("POST", "/tv/api/bili/favorites/import", {"ids": ["111", "222", "999"]},
                                      user=self.admin)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([a["channel"] for a in answer["added"]], ["fv01"])
        reasons = {s["id"]: s["reason"] for s in answer["skipped"]}
        self.assertEqual(set(reasons), {"222", "999"})
        again = self.request("GET", "/tv/api/bili/favorites", user=self.admin)[1]
        self.assertTrue(next(i for i in again["items"] if i["id"] == "111")["subscribed"])
        dup = self.request("POST", "/tv/api/bili/favorites/import", {"ids": ["111"]}, user=self.admin)[1]
        self.assertEqual(dup["added"], [])

    def test_needs_a_signed_in_bilibili_account(self):
        other = self.kid
        answer = self.request("GET", "/tv/api/bili/favorites", user=other)[1]
        self.assertFalse(answer["ok"])
        self.assertIs(answer.get("login"), False)

    def test_bad_requests(self):
        for body in ({}, {"ids": "111"}, {"ids": []}, {"ids": ["x"]}, {"ids": ["1"] * 51}):
            self.assertFalse(self.request("POST", "/tv/api/bili/favorites/import", body, user=self.admin)[1]["ok"],
                             body)

    def test_anonymous_callers_are_refused_and_nothing_leaks(self):
        self.assertEqual(self.request("GET", "/tv/api/bili/favorites")[0], 404)
        self.assertEqual(self.request("POST", "/tv/api/bili/favorites/import", {"ids": ["111"]})[0], 404)
        self.request("POST", "/tv/api/bili/favorites/import", {"ids": ["111"]}, user=self.admin)
        self.assert_no_leak()

    def test_import_never_uses_the_shared_cache_and_uses_only_own_login(self):
        seen = []
        original = self.client.series
        self.client.series = lambda source, login=None: (seen.append(login), original(source, login))[1]
        self.request("POST", "/tv/api/bili/favorites/import", {"ids": ["111"]}, user=self.admin)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].user, self.admin)
        self.assertIsNone(self.accounts.cached_source("bilibili", "fav111", "series"))

    def test_lite_edition_does_not_block_these_endpoints(self):
        from unittest import mock
        from server import edition
        with mock.patch.object(edition, "current", lambda: "lite"):
            self.assertTrue(edition.blocked("/api/library"), "sanity: lite mode is on")
            for path in ("/api/bili/favorites", "/api/bili/favorites/import"):
                self.assertFalse(edition.blocked(path), path)


if __name__ == "__main__":
    unittest.main()
