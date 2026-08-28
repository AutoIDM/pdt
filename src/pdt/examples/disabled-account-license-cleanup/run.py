#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==PDT_VERSION"]
# ///
"""Remove direct licenses from disabled Microsoft Entra users.

Needs an Entra app registration with User.Read.All, LicenseAssignment.Read.All,
and LicenseAssignment.ReadWrite.All (application), plus admin consent.

Exit codes: 0 ok, 1 bad config, 2 Entra or Graph failure, 3 email failure.
"""

from __future__ import annotations

import json
import math
import sys
import urllib.parse
from pathlib import Path

from pdt.config import ConfigError, check_env, load_env, merged_app
from pdt.utils.entra import graph_pages, graph_token
from pdt.utils.log import die, log
from pdt.utils.send_email import send_email
from pdt.utils.web import http_json

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_ENTRA = 2

GRAPH_USERS = "https://graph.microsoft.com/v1.0/users"
GRAPH_SKUS = "https://graph.microsoft.com/v1.0/subscribedSkus"


def cfg_license_costs(cfg: dict) -> dict[str, float]:
    raw_costs = cfg.get("monthly_license_costs_usd")
    if not isinstance(raw_costs, dict) or not raw_costs:
        die(EXIT_CONFIG, "config.yml missing license costs",
            key="monthly_license_costs_usd")
    costs = {}
    for raw_sku_part_number, raw_cost in raw_costs.items():
        sku_part_number = str(raw_sku_part_number or "").strip()
        if sku_part_number == "":
            die(EXIT_CONFIG, "config.yml license type is empty",
                key="monthly_license_costs_usd")
        try:
            cost = float(str(raw_cost))
        except ValueError:
            die(EXIT_CONFIG, "config.yml license cost is not a number",
                sku_part_number=sku_part_number, value=raw_cost)
        if not math.isfinite(cost) or cost < 0:
            die(EXIT_CONFIG, "config.yml license cost must be zero or greater",
                sku_part_number=sku_part_number, value=raw_cost)
        costs[sku_part_number] = round(cost, 2)
    return costs


def disabled_user_licenses(raw: dict) -> dict:
    effective_sku_ids = set()
    for assignment in raw.get("assignedLicenses") or []:
        sku_id = str((assignment or {}).get("skuId") or "").strip()
        if sku_id != "":
            effective_sku_ids.add(sku_id)

    direct_sku_ids = set()
    group_sku_ids = set()
    for state in raw.get("licenseAssignmentStates") or []:
        sku_id = str((state or {}).get("skuId") or "").strip()
        if sku_id not in effective_sku_ids:
            continue
        if "assignedByGroup" not in state:
            continue
        if state["assignedByGroup"] is None:
            direct_sku_ids.add(sku_id)
        else:
            group_sku_ids.add(sku_id)

    return {
        "id": str(raw.get("id") or "").strip(),
        "name": str(raw.get("displayName") or "").strip(),
        "user_principal_name": str(raw.get("userPrincipalName") or "").strip(),
        "direct_sku_ids": sorted(direct_sku_ids),
        "group_sku_ids": sorted(group_sku_ids),
        "unclassified_sku_ids": sorted(
            effective_sku_ids - direct_sku_ids - group_sku_ids),
    }


def fetch_disabled_users(token: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "$select": (
            "id,displayName,userPrincipalName,accountEnabled,assignedLicenses,"
            "licenseAssignmentStates"
        ),
        "$filter": "accountEnabled eq false",
        "$top": "999",
    })
    users = []
    for rows in graph_pages(token, f"{GRAPH_USERS}?{params}", EXIT_ENTRA):
        for raw in rows:
            if raw.get("accountEnabled") is not False:
                log("warning", "Entra query returned enabled user; skipped",
                    id=raw.get("id"))
                continue
            user = disabled_user_licenses(raw)
            if user["id"] == "":
                log("warning", "disabled Entra user has no ID; skipped")
                continue
            users.append(user)
        log("info", "fetched disabled Entra user page", total=len(users))
    return users


def fetch_license_types(token: str) -> dict[str, str]:
    params = urllib.parse.urlencode({"$select": "skuId,skuPartNumber"})
    license_types = {}
    for rows in graph_pages(token, f"{GRAPH_SKUS}?{params}", EXIT_ENTRA):
        for raw in rows:
            sku_id = str(raw.get("skuId") or "").strip()
            sku_part_number = str(raw.get("skuPartNumber") or "").strip()
            if sku_id != "" and sku_part_number != "":
                license_types[sku_id] = sku_part_number
        log("info", "fetched Entra license type page", total=len(license_types))
    return license_types


def remove_direct_licenses(token: str, user: dict) -> None:
    user_id = urllib.parse.quote(user["id"], safe="")
    body = json.dumps({
        "addLicenses": [],
        "removeLicenses": user["direct_sku_ids"],
    }).encode()
    http_json(
        f"{GRAPH_USERS}/{user_id}/assignLicense",
        EXIT_ENTRA,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "pdt/1.0",
        },
        data=body,
    )


def cleanup(token: str, monthly_license_costs_usd: dict[str, float]) -> dict:
    users = fetch_disabled_users(token)
    direct_sku_ids = {
        sku_id for user in users for sku_id in user["direct_sku_ids"]}
    license_types = fetch_license_types(token) if direct_sku_ids else {}
    missing_sku_ids = sorted(direct_sku_ids - license_types.keys())
    if missing_sku_ids:
        die(EXIT_ENTRA, "Entra license types missing", sku_ids=missing_sku_ids)
    missing_prices = sorted({
        license_types[sku_id] for sku_id in direct_sku_ids
        if license_types[sku_id] not in monthly_license_costs_usd
    })
    if missing_prices:
        die(
            EXIT_CONFIG,
            "config.yml missing costs for removable license types",
            key="monthly_license_costs_usd",
            sku_part_numbers=missing_prices,
        )
    for user in users:
        user["direct_licenses"] = [
            {
                "sku_id": sku_id,
                "sku_part_number": license_types[sku_id],
                "monthly_cost_usd": monthly_license_costs_usd[
                    license_types[sku_id]],
            }
            for sku_id in user["direct_sku_ids"]
        ]
    result = {
        "disabled_users_checked": len(users),
        "licensed_users_checked": 0,
        "removals": [],
        "retained_group_license_count": 0,
        "retained_unclassified_license_count": 0,
        "failures": [],
    }
    for user in users:
        sku_count = (
            len(user["direct_sku_ids"])
            + len(user["group_sku_ids"])
            + len(user["unclassified_sku_ids"])
        )
        if sku_count != 0:
            result["licensed_users_checked"] += 1
        for sku_id in user["group_sku_ids"]:
            result["retained_group_license_count"] += 1
            log(
                "warning",
                "retained group-derived license",
                id=user["id"],
                user_principal_name=user["user_principal_name"],
                sku_id=sku_id,
            )
        for sku_id in user["unclassified_sku_ids"]:
            result["retained_unclassified_license_count"] += 1
            log(
                "warning",
                "retained unclassified license",
                id=user["id"],
                user_principal_name=user["user_principal_name"],
                sku_id=sku_id,
            )
        if not user["direct_sku_ids"]:
            continue
        log(
            "info",
            "removing direct licenses",
            id=user["id"],
            user_principal_name=user["user_principal_name"],
            sku_ids=user["direct_sku_ids"],
            sku_part_numbers=[
                license_["sku_part_number"]
                for license_ in user["direct_licenses"]],
        )
        try:
            remove_direct_licenses(token, user)
        except SystemExit:
            failure = {
                "id": user["id"],
                "name": user["name"],
                "user_principal_name": user["user_principal_name"],
                "attempted_sku_ids": user["direct_sku_ids"],
            }
            result["failures"].append(failure)
            log(
                "error",
                "direct license removal failed",
                id=user["id"],
                user_principal_name=user["user_principal_name"],
                sku_ids=user["direct_sku_ids"],
                sku_part_numbers=[
                    license_["sku_part_number"]
                    for license_ in user["direct_licenses"]],
            )
            continue
        removal = {
            "id": user["id"],
            "name": user["name"],
            "user_principal_name": user["user_principal_name"],
            "removed_licenses": user["direct_licenses"],
        }
        result["removals"].append(removal)
        log(
            "info",
            "removed direct licenses",
            id=user["id"],
            user_principal_name=user["user_principal_name"],
            sku_ids=user["direct_sku_ids"],
            sku_part_numbers=[
                license_["sku_part_number"]
                for license_ in user["direct_licenses"]],
            estimated_monthly_savings_usd=round(
                sum(license_["monthly_cost_usd"]
                    for license_ in user["direct_licenses"]), 2),
        )
    result["removed_license_count"] = sum(
        len(removal["removed_licenses"]) for removal in result["removals"])
    result["estimated_monthly_savings_usd"] = round(
        sum(
            license_["monthly_cost_usd"]
            for removal in result["removals"]
            for license_ in removal["removed_licenses"]
        ), 2)
    result["estimated_annual_savings_usd"] = round(
        result["estimated_monthly_savings_usd"] * 12, 2)
    license_type_totals = {}
    for removal in result["removals"]:
        for license_ in removal["removed_licenses"]:
            sku_part_number = license_["sku_part_number"]
            total = license_type_totals.setdefault(sku_part_number, {
                "count": 0,
                "monthly_cost_usd": license_["monthly_cost_usd"],
            })
            total["count"] += 1
    result["license_type_totals"] = license_type_totals
    log(
        "info",
        "disabled account license cleanup complete",
        disabled_users_checked=result["disabled_users_checked"],
        licensed_users_checked=result["licensed_users_checked"],
        removals=len(result["removals"]),
        removed_license_count=result["removed_license_count"],
        estimated_monthly_savings_usd=result["estimated_monthly_savings_usd"],
        estimated_annual_savings_usd=result["estimated_annual_savings_usd"],
        retained_group_license_count=result["retained_group_license_count"],
        retained_unclassified_license_count=(
            result["retained_unclassified_license_count"]),
        failures=len(result["failures"]),
    )
    return result


def format_body(result: dict) -> str:
    lines = ["Disabled account license cleanup", ""]
    for removal in result["removals"]:
        name = removal["name"] or removal["user_principal_name"] or removal["id"]
        lines.append(f"Removed direct licenses for: {name}")
        lines.append(f"Sign-in name: {removal['user_principal_name'] or 'unknown'}")
        lines.append(f"User ID: {removal['id']}")
        lines.append("Licenses:")
        for license_ in removal["removed_licenses"]:
            lines.append(
                f"- {license_['sku_part_number']} ({license_['sku_id']}): "
                f"${license_['monthly_cost_usd']:,.2f}/month")
        lines.append("")
    lines.append("Summary")
    lines.append(f"Disabled users checked: {result['disabled_users_checked']}")
    lines.append(f"Licensed users checked: {result['licensed_users_checked']}")
    lines.append(f"Users changed: {len(result['removals'])}")
    lines.append(f"License seats removed: {result['removed_license_count']}")
    lines.append(
        "Estimated monthly savings: "
        f"${result['estimated_monthly_savings_usd']:,.2f}")
    lines.append(
        "Estimated annual savings: "
        f"${result['estimated_annual_savings_usd']:,.2f}")
    lines.append("Savings by license type:")
    for sku_part_number, total in sorted(result["license_type_totals"].items()):
        monthly_savings = total["count"] * total["monthly_cost_usd"]
        seat_label = "seat" if total["count"] == 1 else "seats"
        lines.append(
            f"- {sku_part_number}: {total['count']} {seat_label} at "
            f"${total['monthly_cost_usd']:,.2f} = ${monthly_savings:,.2f}/month")
    lines.append(
        "Savings assumes the purchased seat count is reduced by the same amount.")
    lines.append(
        f"Group-derived licenses retained: {result['retained_group_license_count']}")
    lines.append(
        "Unclassified licenses retained: "
        f"{result['retained_unclassified_license_count']}")
    lines.append(f"Failed removals: {len(result['failures'])}")
    return "\n".join(lines)


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    try:
        env_files = load_env(app_dir)
    except ConfigError as e:
        die(EXIT_CONFIG, "bad env", error=str(e))
    for path in env_files:
        log("info", "loaded env file", path=str(path))
    try:
        app = merged_app(app_dir.name)
    except ConfigError as e:
        die(EXIT_CONFIG, "config error", error=str(e))
    problems = check_env(app["env"])
    if problems:
        die(EXIT_CONFIG, "env vars missing", problems="; ".join(problems))
    cfg = app["config"]
    monthly_license_costs_usd = cfg_license_costs(cfg)

    log("info", "starting disabled account license cleanup")
    token = graph_token(EXIT_ENTRA)
    log("info", "Entra token acquired")
    result = cleanup(token, monthly_license_costs_usd)
    if result["removals"]:
        transport = send_email(
            str(cfg.get("email_from") or "").strip(),
            cfg.get("email_to") or "",
            str(cfg.get("email_subject") or "Disabled account license cleanup").strip(),
            format_body(result),
        )
        if transport == "stdout":
            log("info", "email not configured; changes printed to stdout",
                count=len(result["removals"]))
        else:
            log("info", "summary email sent", transport=transport,
                count=len(result["removals"]))
    elif not result["failures"]:
        log("info", "no direct license removals needed")
    if result["failures"]:
        return EXIT_ENTRA
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
