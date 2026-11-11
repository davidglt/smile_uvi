#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Created: 2026-11-11
# Author: David González López-Tercero <davidglt@dragonit.es>
# SPDX-FileCopyrightText: 2026 David González López-Tercero <davidglt@dragonit.es>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline unit tests for the smile_sampling module."""

import copy
import unittest
from datetime import datetime, timezone

from smile_sampling import (
    DAY_SECONDS,
    parse_timestamp,
    select_samples,
)


class TestParseTimestamp(unittest.TestCase):
    """Tests for parse_timestamp."""

    def test_naive_treated_as_utc(self):
        """Naive timestamp is interpreted as UTC."""
        result = parse_timestamp("2026-01-15T12:00:00")
        self.assertEqual(result.tzinfo, timezone.utc)
        self.assertEqual(result.hour, 12)

    def test_aware_utc_preserved(self):
        """Aware UTC timestamp is preserved."""
        result = parse_timestamp("2026-01-15T12:00:00+00:00")
        self.assertEqual(result.tzinfo, timezone.utc)
        self.assertEqual(result.hour, 12)

    def test_aware_non_utc_normalized(self):
        """Aware non-UTC timestamp is normalized to UTC."""
        result = parse_timestamp("2026-01-15T14:00:00+02:00")
        self.assertEqual(result.tzinfo, timezone.utc)
        self.assertEqual(result.hour, 12)

    def test_microseconds_preserved(self):
        """Microseconds are preserved in the result."""
        result = parse_timestamp("2026-01-15T12:00:00.123456")
        self.assertEqual(result.microsecond, 123456)

    def test_whitespace_stripped(self):
        """Leading and trailing whitespace is stripped."""
        result = parse_timestamp("  2026-01-15T12:00:00  ")
        self.assertEqual(result.hour, 12)

    def test_z_suffix(self):
        """Z suffix is handled as UTC."""
        result = parse_timestamp("2026-01-15T12:00:00Z")
        self.assertEqual(result.tzinfo, timezone.utc)
        self.assertEqual(result.hour, 12)

    def test_malformed_raises(self):
        """Malformed timestamp propagates the parsing error."""
        with self.assertRaises(ValueError):
            parse_timestamp("not-a-timestamp")


class TestSelectSamples(unittest.TestCase):
    """Tests for select_samples."""

    def _make_product(self, filename, begin_time, version=""):
        return {
            "filename": filename,
            "begin_time": begin_time,
            "item_version": version,
        }

    def test_single_sample_basic(self):
        """One product in the day maps to slot 0 with sample_count=1."""
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        products = [
            self._make_product("a.png", "2026-01-15T06:00:00+00:00")
        ]
        result = select_samples(products, start, 1, "")
        self.assertEqual(len(result), 1)
        self.assertIn(0, result)
        self.assertIs(result[0], products[0])

    def test_boundary_start_included(self):
        """Timestamp at exact start is included (elapsed_us == 0)."""
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        products = [
            self._make_product("a.png", "2026-01-15T00:00:00+00:00")
        ]
        result = select_samples(products, start, 24, "")
        self.assertIn(0, result)

    def test_boundary_end_excluded(self):
        """Timestamp at exact end is excluded (half-open interval)."""
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        products = [
            self._make_product("a.png", "2026-01-16T00:00:00+00:00")
        ]
        result = select_samples(products, start, 24, "")
        self.assertEqual(len(result), 0)

    def test_prefix_match(self):
        """Products matching the prefix are included."""
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        products = [
            self._make_product(
                "SMILE_UVI_a.png", "2026-01-15T06:00:00+00:00"
            ),
        ]
        result = select_samples(products, start, 1, "SMILE_UVI_")
        self.assertEqual(len(result), 1)

    def test_prefix_no_match(self):
        """Products not matching the prefix are excluded."""
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        products = [
            self._make_product(
                "OTHER_a.png", "2026-01-15T06:00:00+00:00"
            ),
        ]
        result = select_samples(products, start, 1, "SMILE_UVI_")
        self.assertEqual(len(result), 0)

    def test_earliest_timestamp_wins(self):
        """The earliest timestamp in a slot is selected."""
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        p_late = self._make_product(
            "late.png", "2026-01-15T06:00:00+00:00"
        )
        p_early = self._make_product(
            "early.png", "2026-01-15T03:00:00+00:00"
        )
        products = [p_late, p_early]
        result = select_samples(products, start, 1, "")
        self.assertIs(result[0], p_early)

    def test_tie_version_textual(self):
        """On equal timestamps, textual version comparison applies.

        '2' > '10' in lexicographic order.
        """
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        p_v10 = self._make_product(
            "a.png", "2026-01-15T06:00:00+00:00", "10"
        )
        p_v2 = self._make_product(
            "b.png", "2026-01-15T06:00:00+00:00", "2"
        )
        products = [p_v10, p_v2]
        result = select_samples(products, start, 1, "")
        self.assertIs(result[0], p_v2)

    def test_tie_first_encountered_wins(self):
        """On equal timestamp and version, first encountered wins."""
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        p1 = self._make_product(
            "a.png", "2026-01-15T06:00:00+00:00", "5"
        )
        p2 = self._make_product(
            "b.png", "2026-01-15T06:00:00+00:00", "5"
        )
        products = [p1, p2]
        result = select_samples(products, start, 1, "")
        self.assertIs(result[0], p1)

    def test_input_list_not_mutated(self):
        """The input products list is not modified."""
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        products = [
            self._make_product("a.png", "2026-01-15T06:00:00+00:00"),
            self._make_product("b.png", "2026-01-15T12:00:00+00:00"),
        ]
        snapshot = copy.deepcopy(products)
        select_samples(products, start, 24, "")
        self.assertEqual(products, snapshot)

    def test_prefix_filtered_not_parsed(self):
        """Skip parsing when the prefix excludes a product."""
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        products = [
            self._make_product("OTHER_bad.png", "not-a-timestamp"),
        ]
        result = select_samples(products, start, 1, "SMILE_UVI_")
        self.assertEqual(len(result), 0)

    def test_microsecond_slot_boundary(self):
        """Microsecond precision affects slot assignment correctly."""
        start = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        p_before = self._make_product(
            "before.png", "2026-01-15T11:59:59.999999+00:00"
        )
        p_after = self._make_product(
            "after.png", "2026-01-15T12:00:00.000000+00:00"
        )
        products = [p_before, p_after]
        result = select_samples(products, start, 2, "")
        self.assertIn(0, result)
        self.assertIn(1, result)
        self.assertIs(result[0], p_before)
        self.assertIs(result[1], p_after)


    def test_slot_insertion_order_preserved(self):
        """Preserve the insertion order of selected slots."""
        start = datetime(2026, 1, 15, tzinfo=timezone.utc)
        later = self._make_product(
            "later.png", "2026-01-15T02:00:01+00:00"
        )
        earlier = self._make_product(
            "earlier.png", "2026-01-15T00:00:01+00:00"
        )
        result = select_samples([later, earlier], start, 24, "")
        self.assertEqual(list(result), [2, 0])
        self.assertIs(result[2], later)
        self.assertIs(result[0], earlier)

    def test_sample_count_86400(self):
        """Assign one-second slots and exclude the next day's start."""
        self.assertEqual(DAY_SECONDS, 86400)
        start = datetime(2026, 1, 15, tzinfo=timezone.utc)
        before_second = self._make_product(
            "before.png", "2026-01-15T00:00:00.999999+00:00"
        )
        exact_second = self._make_product(
            "exact.png", "2026-01-15T00:00:01+00:00"
        )
        last = self._make_product(
            "last.png", "2026-01-15T23:59:59.999999+00:00"
        )
        next_day = self._make_product(
            "excluded.png", "2026-01-16T00:00:00+00:00"
        )
        rows = [before_second, exact_second, last, next_day]
        result = select_samples(rows, start, DAY_SECONDS, "")
        self.assertEqual(list(result), [0, 1, 86399])
        self.assertIs(result[0], before_second)
        self.assertIs(result[1], exact_second)
        self.assertIs(result[86399], last)


if __name__ == "__main__":
    unittest.main()
