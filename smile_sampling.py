#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Created: 2026-11-11
# Author: David González López-Tercero <davidglt@dragonit.es>
# SPDX-FileCopyrightText: 2026 David González López-Tercero <davidglt@dragonit.es>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extracted sampling logic for SMILE UVI daily samples."""

from datetime import datetime, timezone

DAY_SECONDS = 86400


def parse_timestamp(value):
    """Parse an ISO-8601 timestamp string into a UTC-aware datetime."""
    text = value.strip().replace("Z", "+00:00")
    try:
        timestamp = datetime.fromisoformat(text)
    except ValueError:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                timestamp = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(
                f"Cannot parse timestamp: {value!r}"
            )
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def select_samples(products, start, sample_count, prefix):
    """Select one product per time slot from the product list."""
    selected = {}

    for product in products:
        if prefix and not product["filename"].startswith(prefix):
            continue

        timestamp = parse_timestamp(product["begin_time"])
        elapsed = timestamp - start
        elapsed_us = (
            elapsed.days * DAY_SECONDS * 1_000_000
            + elapsed.seconds * 1_000_000
            + elapsed.microseconds
        )

        if not 0 <= elapsed_us < DAY_SECONDS * 1_000_000:
            continue

        slot = (
            elapsed_us * sample_count
            // (DAY_SECONDS * 1_000_000)
        )

        previous = selected.get(slot)
        if previous is None:
            selected[slot] = product
            continue

        previous_time = parse_timestamp(previous["begin_time"])
        if timestamp < previous_time:
            selected[slot] = product
        elif timestamp == previous_time:
            if (product["item_version"] or "") > (
                previous["item_version"] or ""
            ):
                selected[slot] = product

    return selected
