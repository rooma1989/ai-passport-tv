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


if __name__ == "__main__":
    unittest.main()
