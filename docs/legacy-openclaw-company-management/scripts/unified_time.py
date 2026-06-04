#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def get_business_dates(tz_name: str = 'Asia/Bangkok', boundary_hour: int = 7):
    if ZoneInfo is not None:
        tz = ZoneInfo(tz_name)
    else:
        tz = timezone(timedelta(hours=7), tz_name)
    now = datetime.now(tz)

    # Shift time back so the business day starts at boundary_hour.
    shifted = now - timedelta(hours=boundary_hour)
    current_biz_day = shifted.date()
    previous_biz_day = current_biz_day - timedelta(days=1)

    return {
        "current_business_day": current_biz_day.strftime("%Y-%m-%d"),
        "previous_business_day": previous_biz_day.strftime("%Y-%m-%d"),
        "real_time": now.strftime("%Y-%m-%d %H:%M:%S %Z")
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Get business day based on a boundary hour.")
    parser.add_argument("--tz", default="Asia/Bangkok")
    parser.add_argument("--boundary", type=int, default=7, help="Hour boundary (0-23)")
    parser.add_argument("--target", choices=["current", "previous"], default="current")
    args = parser.parse_args()

    dates = get_business_dates(args.tz, args.boundary)
    if args.target == "current":
        print(dates["current_business_day"])
    else:
        print(dates["previous_business_day"])
