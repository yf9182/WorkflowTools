"""Target-filtered acquisition and descriptive analysis; no app-specific event constants."""
from __future__ import annotations

from collections import defaultdict
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import gzip
import io
import json
import re
import urllib.parse

from .auth import Apple, Google, ASC
from .common import Failure, require, save, read_json, secure_dir, stamp

SOURCES = ("ga4", "asc_sales", "apple_analytics")


def dates(start_date=None, end_date=None, window_days=28):
    require(isinstance(window_days, int) and 1 <= window_days <= 366, "CONFIG_INVALID", "日期窗口需为 1–366 天")
    try:
        end = dt.date.fromisoformat(end_date) if end_date else dt.datetime.now(dt.timezone.utc).date()
        start = dt.date.fromisoformat(start_date) if start_date else end - dt.timedelta(days=window_days - 1)
    except (ValueError, TypeError):
        raise Failure("DATE_INVALID", "日期需使用 YYYY-MM-DD") from None
    require(start <= end and (end - start).days <= 365 and end <= dt.datetime.now(dt.timezone.utc).date(),
            "DATE_INVALID", "日期范围无效、超出一年或包含未来日期")
    return start, end


def ga4(runtime, start, end):
    google = Google(runtime.project, runtime.vault, runtime.http)
    stream = google.stream()
    prop = str(google.config["property_id"])
    payload = {"dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
        "dimensions": [{"name": name} for name in ("date", "appVersion", "eventName")],
        "metrics": [{"name": name} for name in ("eventCount", "activeUsers")],
        "dimensionFilter": {"filter": {"fieldName": "streamId", "stringFilter": {"matchType": "EXACT", "value": stream}}},
        "orderBys": [{"dimension": {"dimensionName": n}} for n in ("date", "appVersion", "eventName")], "limit": "1000"}
    rows, expected, limited, timezone = [], None, False, None
    for _ in range(100):
        payload["offset"] = str(len(rows))
        data = runtime.http.json(f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
                                 method="POST", headers=google.headers(), payload=payload)
        total = int(data.get("rowCount", 0))
        require(expected is None or expected == total, "DATA_CHANGED", "GA4 分页期间数据发生变化，请重新采集")
        expected = total
        page = data.get("rows", [])
        require(not (len(rows) < total and not page), "DATA_INCOMPLETE", "GA4 返回了不完整分页")
        metadata = data.get("metadata", {})
        limited = limited or bool(metadata.get("samplingMetadatas") or metadata.get("subjectToThresholding") or metadata.get("dataLossFromOtherRow"))
        timezone = metadata.get("timeZone") or timezone
        for row in page:
            day, version, event = [x["value"] for x in row["dimensionValues"]]
            actual_date = dt.datetime.strptime(day, "%Y%m%d").date()
            require(start <= actual_date <= end, "DATA_INVALID", "GA4 返回了范围之外的数据")
            count, users = [int(x["value"]) for x in row["metricValues"]]
            require(count >= 0 and users >= 0, "DATA_INVALID", "GA4 计数无效")
            rows.append({"date": actual_date.isoformat(), "app_version": version, "event": event,
                         "event_count": count, "active_users": users})
        if len(rows) >= total:
            break
    require(len(rows) == expected, "DATA_INCOMPLETE", "GA4 分页超过限制")
    return {"status": "ready", "source": "Google Analytics Data API", "property_id": prop, "stream_id": stream,
        "timezone": timezone, "rows": rows, "available_dates": sorted({r["date"] for r in rows}),
        "data_limited": limited, "active_users_additive": False, "start_date": start.isoformat(), "end_date": end.isoformat()}


def parse_sales(raw, app_id, app_sku, products=()):
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter="\t")
    required = {"SKU", "Apple Identifier", "Units", "Developer Proceeds", "Currency of Proceeds"}
    require(required <= set(reader.fieldnames or []), "DATA_INVALID", "ASC 销售报表缺少必要列")
    rows, totals = [], defaultdict(Decimal)
    fields = required | {"Parent Identifier", "Product Type Identifier", "Begin Date", "End Date", "Country Code", "Customer Currency", "Customer Price"}
    for row in reader:
        if not (row.get("Apple Identifier") == str(app_id) or row.get("SKU") == app_sku or
                row.get("SKU") in products or row.get("Parent Identifier") == app_sku):
            continue
        clean = {k: row.get(k, "") for k in sorted(fields)}
        try:
            units, proceeds = Decimal(clean["Units"]), Decimal(clean["Developer Proceeds"])
        except InvalidOperation:
            raise Failure("DATA_INVALID", "ASC 销售报表金额无效") from None
        currency = clean["Currency of Proceeds"]
        require(units.is_finite() and proceeds.is_finite() and currency, "DATA_INVALID", "ASC 销售报表包含非有限金额或缺少币种")
        totals[currency] += units * proceeds
        rows.append(clean)
    return {"rows": rows, "estimated_proceeds_by_currency": {k: str(v) for k, v in totals.items()},
        "proceeds_basis": "units_times_developer_proceeds_per_unit; signed_refunds; platform_fees_not_subtracted_twice",
        "net_profit": None, "cost_status": "unknown"}


def sales(runtime, start, end, checkpoint):
    apple = Apple(runtime.project, runtime.vault, runtime.http)
    app = apple.identity()
    sku = app.get("attributes", {}).get("sku")
    require(sku, "TARGET_MISMATCH", "ASC 返回的目标 App 缺少 SKU")
    vendor = apple.config.get("vendor_number")
    require(vendor, "CONFIG_MISSING", "ASC 销售报表需要 Vendor Number")
    reports, missing = [], []
    for n in range((end - start).days + 1):
        day = start + dt.timedelta(days=n)
        checkpoint("fetching_asc_sales", report_date=day.isoformat())
        query = urllib.parse.urlencode({"filter[frequency]": "DAILY", "filter[reportDate]": day.isoformat(),
            "filter[reportType]": "SALES", "filter[reportSubType]": "SUMMARY", "filter[vendorNumber]": vendor, "filter[version]": "1_0"})
        try:
            raw, _ = runtime.http.request(ASC + "/v1/salesReports?" + query, headers={**apple.headers(), "Accept": "application/a-gzip"})
            parsed = parse_sales(raw, apple.ios["app_store_id"], sku, runtime.project.section("analytics").get("product_ids", []))
            reports.append({"date": day.isoformat(), **parsed})
        except Failure as e:
            if e.code in ("AUTH_EXPIRED_OR_REVOKED", "AUTH_PERMISSION_DENIED", "TARGET_MISMATCH"):
                raise
            missing.append({"date": day.isoformat(), "reason": e.code})
    return {"status": "ready" if reports else "unavailable", "source": "App Store Connect Sales Reports",
        "reports": reports, "available_dates": [r["date"] for r in reports], "missing_dates": missing,
        "start_date": start.isoformat(), "end_date": end.isoformat()}


def apple_catalog(runtime):
    apple = Apple(runtime.project, runtime.vault, runtime.http)
    apple.identity()
    requests = apple.items(f"/v1/apps/{apple.ios['app_store_id']}/analyticsReportRequests")
    ongoing = [x for x in requests if x.get("attributes", {}).get("accessType") == "ONGOING"]
    reports = []
    for item in ongoing:
        for row in apple.items(f"/v1/analyticsReportRequests/{item['id']}/reports"):
            attributes = row.get("attributes", {})
            reports.append({"id": row["id"], "name": attributes.get("name"), "category": attributes.get("category")})
    # Matches the implemented upstream capability. No ONGOING request is created implicitly.
    return {"status": "ready" if ongoing else "unavailable", "source": "App Store Connect Analytics Reports",
        "data_scope": "report_catalog", "ongoing_request_ids": [x["id"] for x in ongoing], "reports": reports,
        "metric_rows": "unverified", "notice": "读取已有报告目录；不创建请求，不把目录数量当作业务指标"}


def fetch(runtime, params, job_id, checkpoint):
    project = runtime.project
    configured = project.section("analytics").get("sources", [])
    selected = params.get("sources") or configured
    require(selected and isinstance(selected, list) and all(s in SOURCES and s in configured for s in selected),
            "CONFIG_MISSING", "请指定项目已配置的数据源")
    start, end = dates(params.get("start_date"), params.get("end_date"), project.section("analytics").get("window_days", 28))
    result = {"schema_version": 1, "dataset_id": job_id, "project_root": str(project.root), "project_id": project.id,
        "bundle_id": project.section("ios").get("bundle_id"), "fetched_at": stamp(), "start_date": start.isoformat(), "end_date": end.isoformat(), "sources": {}}
    blockers = []
    for source in dict.fromkeys(selected):
        checkpoint("fetching_" + source)
        try:
            runtime.enforce("analytics_" + source)
            data = ga4(runtime, start, end) if source == "ga4" else sales(runtime, start, end, checkpoint) if source == "asc_sales" else apple_catalog(runtime)
        except Failure as e:
            data = {"status": "unavailable", "reason": e.code}
            blockers.append({"source": source, "code": e.code, "message": e.message})
        except Exception:
            data = {"status": "unavailable", "reason": "SOURCE_UNVERIFIED"}
            blockers.append({"source": source, "code": "SOURCE_UNVERIFIED", "message": "数据源返回或处理异常，原始异常已隐藏；其他来源继续"})
        result["sources"][source] = data
    path = project.artifacts / ("dataset-" + job_id + ".json")
    save(path, result)
    return {"status": "succeeded" if any(d["status"] == "ready" for d in result["sources"].values()) else "blocked",
            "dataset_id": job_id, "source_status": {s: d["status"] for s, d in result["sources"].items()},
            "artifacts": [{"type": "dataset", "path": str(path)}], "blockers": blockers}


def summarize(dataset, config):
    floor = config.get("sample_floor", 30)
    require(isinstance(floor, int) and floor >= 1, "CONFIG_INVALID", "样本下限需要正整数")
    ga = dataset.get("sources", {}).get("ga4", {})
    counts = defaultdict(lambda: defaultdict(int))
    daily = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for row in ga.get("rows", []):
        counts[row["app_version"]][row["event"]] += int(row["event_count"])
        daily[row["app_version"]][row["date"]][row["event"]] += int(row["event_count"])
    ratios = []
    for spec in config.get("funnels", []):
        require(isinstance(spec, dict) and isinstance(spec.get("numerator"), str) and isinstance(spec.get("denominator"), str),
                "CONFIG_INVALID", "漏斗需要 numerator、denominator 事件名")
        for version, events in counts.items():
            numerator, denominator = events.get(spec["numerator"], 0), events.get(spec["denominator"], 0)
            complements = spec.get("partition_complements", [])
            proof = bool(complements and isinstance(complements, list) and all(isinstance(x, str) for x in complements)
                         and spec["denominator"] not in complements and spec["numerator"] not in complements)
            proof = proof and all(day.get(spec["numerator"], 0) + sum(day.get(e, 0) for e in complements)
                                  == day.get(spec["denominator"], 0) for day in daily[version].values())
            reason = "data_limited" if ga.get("data_limited") else "version_unknown" if version in ("", "(not set)") else \
                "population_unverified" if not proof else "insufficient_sample" if denominator < floor else None
            ratios.append({"name": spec.get("name", spec["numerator"]), "app_version": version, "basis": "event_count_ratio",
                "numerator": numerator, "denominator": denominator, "partition_verified": bool(proof), "sample_floor": floor,
                "value": None if reason else numerator / denominator, "reason": reason,
                "notice": "事件计数比；不代表去重用户转化率或因果结论"})
    totals = defaultdict(Decimal)
    sales_data = dataset.get("sources", {}).get("asc_sales", {})
    for report in sales_data.get("reports", []):
        for currency, value in report["estimated_proceeds_by_currency"].items():
            totals[currency] += Decimal(value)
    return {"schema_version": 1, "dataset_id": dataset["dataset_id"], "generated_at": stamp(),
        "start_date": dataset["start_date"], "end_date": dataset["end_date"],
        "source_status": {s: d.get("status") for s, d in dataset.get("sources", {}).items()},
        "available_dates": {s: d.get("available_dates", []) for s, d in dataset.get("sources", {}).items()},
        "events_by_version": {v: dict(e) for v, e in counts.items()}, "event_ratios": ratios,
        "active_users_additive": False, "estimated_proceeds_by_currency": {c: str(v) for c, v in totals.items()},
        "net_profit": None, "cost_status": "unknown"}


def report(runtime, dataset_id):
    require(isinstance(dataset_id, str) and re.fullmatch(r"[0-9a-f]{32}", dataset_id), "DATASET_INVALID", "数据集编号无效")
    project = runtime.project
    source = project.artifacts / ("dataset-" + dataset_id + ".json")
    dataset = read_json(source)
    require(dataset and dataset.get("project_root") == str(project.root) and dataset.get("bundle_id") == project.section("ios").get("bundle_id"),
            "TARGET_MISMATCH", "数据集与本项目身份不匹配")
    result = summarize(dataset, project.section("analytics"))
    path = project.artifacts / ("report-" + dataset_id + ".json")
    save(path, result)
    lines = ["# 数据分析报告", "", f"日期：{result['start_date']} 至 {result['end_date']}", "",
             "数据按 App 版本隔离；activeUsers 不跨日期相加；收入分币种展示，净利润未知。", "",
             "| 数据源 | 状态 |", "|---|---|"]
    lines += [f"| {s} | {v} |" for s, v in result["source_status"].items()]
    lines += ["", "## 事件计数", "", "| 版本 | 事件 | 计数 |", "|---|---|---|"]
    lines += [f"| {v} | {e} | {n} |" for v, events in result["events_by_version"].items() for e, n in sorted(events.items())]
    lines += ["", "## 事件比率", "", "| 名称 | 版本 | 分子/分母 | 结果 |", "|---|---|---|---|"]
    lines += [f"| {r['name']} | {r['app_version']} | {r['numerator']}/{r['denominator']} | {r['value'] if r['value'] is not None else r['reason']} |" for r in result["event_ratios"]]
    lines += ["", "## 估算开发者收益", ""]
    lines += [f"- {c}：{v}" for c, v in result["estimated_proceeds_by_currency"].items()] or ["- 不可用"]
    from .common import atomic_bytes
    markdown = path.with_suffix(".md")
    atomic_bytes(markdown, ("\n".join(lines) + "\n").encode())
    return {"status": "succeeded", "report": result, "artifacts": [{"type": "json", "path": str(path)},
            {"type": "markdown", "path": str(markdown)}], "blockers": []}
