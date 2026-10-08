"""Where this computer is, guessed from its time zone.

`region_suggestion` names a region for each cloud provider: the one
nearest the place the time zone names, else the one the provider's CLI
settings name (read from their files, without starting the CLI), else a
fixed default. On the first deploy of a project, `choose_region` offers
that region, lets the user pick another, and saves the answer, so later
deploys do not ask.
`local_currency` names the currency a cost estimate shows, from the
regional setting of the user who runs pdt: the user default locale on
Windows, AppleLocale on macOS, and LC_ALL, LC_MONETARY, or LANG on Linux.
When none can be read, it is USD.

The region lists the tables below are drawn from:

- AWS: https://docs.aws.amazon.com/global-infrastructure/latest/regions/aws-regions.html
  Only Regions enabled by default appear, because an opt-in Region is a
  manual step in the AWS console before anything can deploy there.
- Azure: https://learn.microsoft.com/en-us/azure/reliability/regions-list
- Google Cloud: https://docs.cloud.google.com/run/docs/locations

macOS and Linux name the time zone the IANA way ("Europe/London").
Windows names it its own way ("GMT Standard Time"); `WINDOWS_ZONES` maps
those names to IANA names with the "001" rows of the CLDR table
https://github.com/unicode-org/cldr/blob/main/common/supplemental/windowsZones.xml
"""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
from pathlib import Path

from pdt import config, console
from pdt.utils.email_auth import can_prompt

PROVIDERS = ("aws", "azure", "google-cloud")
PROVIDER_NAMES = {"aws": "AWS", "azure": "Azure", "google-cloud": "Google Cloud"}
REGION_LISTS = {
    "aws": "https://docs.aws.amazon.com/global-infrastructure/latest/regions/aws-regions.html",
    "azure": "https://learn.microsoft.com/en-us/azure/reliability/regions-list",
    "google-cloud": "https://docs.cloud.google.com/run/docs/locations",
}
# An environment variable each provider module already reads for its region.
REGION_ENV = {
    "aws": ("AWS_REGION", "AWS_DEFAULT_REGION"),
    "azure": ("PDT_AZURE_REGION",),
    "google-cloud": ("PDT_GOOGLE_CLOUD_REGION",),
}

# (aws, azure, google-cloud) when the time zone names no known place.
DEFAULT_REGIONS = ("us-east-1", "eastus2", "us-central1")
# (aws, azure, google-cloud) for each country, by ISO 3166 code.
US_EAST = ("us-east-1", "eastus2", "us-east4")
COUNTRY_REGIONS = {
    "US": US_EAST,
    "CA": ("ca-central-1", "canadacentral", "northamerica-northeast2"),
    "MX": ("us-east-1", "mexicocentral", "northamerica-south1"),
    "BR": ("sa-east-1", "brazilsouth", "southamerica-east1"),
    "AR": ("sa-east-1", "brazilsouth", "southamerica-east1"),
    "CL": ("sa-east-1", "chilecentral", "southamerica-west1"),
    "CO": ("us-east-1", "brazilsouth", "southamerica-east1"),
    "GB": ("eu-west-2", "uksouth", "europe-west2"),
    "IE": ("eu-west-1", "northeurope", "europe-west1"),
    "DE": ("eu-central-1", "germanywestcentral", "europe-west3"),
    "AT": ("eu-central-1", "austriaeast", "europe-west3"),
    "CH": ("eu-central-1", "switzerlandnorth", "europe-west6"),
    "FR": ("eu-west-3", "francecentral", "europe-west9"),
    "BE": ("eu-west-3", "belgiumcentral", "europe-west1"),
    "LU": ("eu-central-1", "belgiumcentral", "europe-west1"),
    "NL": ("eu-central-1", "westeurope", "europe-west4"),
    "IT": ("eu-central-1", "italynorth", "europe-west8"),
    "ES": ("eu-west-3", "spaincentral", "europe-southwest1"),
    "PT": ("eu-west-3", "spaincentral", "europe-southwest1"),
    "SE": ("eu-north-1", "swedencentral", "europe-north2"),
    "NO": ("eu-north-1", "norwayeast", "europe-north1"),
    "DK": ("eu-north-1", "denmarkeast", "europe-north1"),
    "FI": ("eu-north-1", "swedencentral", "europe-north1"),
    "PL": ("eu-central-1", "polandcentral", "europe-central2"),
    "CZ": ("eu-central-1", "germanywestcentral", "europe-central2"),
    "IL": ("eu-central-1", "israelcentral", "me-west1"),
    "AE": ("ap-south-1", "uaenorth", "me-central1"),
    "QA": ("ap-south-1", "qatarcentral", "me-central1"),
    "SA": ("ap-south-1", "uaenorth", "me-central2"),
    "ZA": ("eu-west-1", "southafricanorth", "africa-south1"),
    "IN": ("ap-south-1", "centralindia", "asia-south1"),
    "SG": ("ap-southeast-1", "southeastasia", "asia-southeast1"),
    "MY": ("ap-southeast-1", "malaysiawest", "asia-southeast1"),
    "ID": ("ap-southeast-1", "indonesiacentral", "asia-southeast2"),
    "TH": ("ap-southeast-1", "southeastasia", "asia-southeast3"),
    "PH": ("ap-southeast-1", "southeastasia", "asia-southeast1"),
    "VN": ("ap-southeast-1", "southeastasia", "asia-southeast1"),
    "HK": ("ap-southeast-1", "eastasia", "asia-east2"),
    "CN": ("ap-southeast-1", "eastasia", "asia-east2"),
    "TW": ("ap-northeast-1", "eastasia", "asia-east1"),
    "JP": ("ap-northeast-1", "japaneast", "asia-northeast1"),
    "KR": ("ap-northeast-2", "koreacentral", "asia-northeast3"),
    "AU": ("ap-southeast-2", "australiaeast", "australia-southeast1"),
    "NZ": ("ap-southeast-2", "newzealandnorth", "australia-southeast1"),
}
# The United States spans four time zones, so its zone picks a region.
ZONE_REGIONS = {
    "America/Chicago": ("us-east-2", "centralus", "us-central1"),
    "America/Denver": ("us-west-2", "westus3", "us-central1"),
    "America/Phoenix": ("us-west-2", "westus3", "us-west1"),
    "America/Los_Angeles": ("us-west-2", "westus2", "us-west1"),
    "America/Anchorage": ("us-west-2", "westus2", "us-west1"),
    "Pacific/Honolulu": ("us-west-2", "westus2", "us-west1"),
    "America/Vancouver": ("us-west-2", "westus2", "us-west1"),
    "America/Edmonton": ("us-west-2", "canadacentral", "northamerica-northeast2"),
}
ZONE_COUNTRY = {
    "America/New_York": "US", "America/Detroit": "US", "America/Chicago": "US",
    "America/Denver": "US", "America/Phoenix": "US", "America/Los_Angeles": "US",
    "America/Anchorage": "US", "Pacific/Honolulu": "US", "America/Indiana/Indianapolis": "US",
    "America/Toronto": "CA", "America/Montreal": "CA", "America/Vancouver": "CA",
    "America/Edmonton": "CA", "America/Winnipeg": "CA", "America/Halifax": "CA",
    "America/St_Johns": "CA", "America/Regina": "CA",
    "America/Mexico_City": "MX", "America/Sao_Paulo": "BR",
    "America/Argentina/Buenos_Aires": "AR", "America/Buenos_Aires": "AR",
    "America/Santiago": "CL", "America/Bogota": "CO",
    "Europe/London": "GB", "Europe/Belfast": "GB", "Europe/Dublin": "IE",
    "Europe/Berlin": "DE", "Europe/Vienna": "AT", "Europe/Zurich": "CH", "Europe/Paris": "FR",
    "Europe/Brussels": "BE", "Europe/Luxembourg": "LU", "Europe/Amsterdam": "NL",
    "Europe/Rome": "IT", "Europe/Madrid": "ES", "Europe/Lisbon": "PT", "Europe/Stockholm": "SE",
    "Europe/Oslo": "NO", "Europe/Copenhagen": "DK", "Europe/Helsinki": "FI",
    "Europe/Warsaw": "PL", "Europe/Prague": "CZ",
    "Asia/Jerusalem": "IL", "Asia/Tel_Aviv": "IL", "Asia/Dubai": "AE", "Asia/Qatar": "QA",
    "Asia/Riyadh": "SA", "Africa/Johannesburg": "ZA",
    "Asia/Kolkata": "IN", "Asia/Calcutta": "IN", "Asia/Singapore": "SG",
    "Asia/Kuala_Lumpur": "MY", "Asia/Jakarta": "ID", "Asia/Bangkok": "TH",
    "Asia/Manila": "PH", "Asia/Ho_Chi_Minh": "VN", "Asia/Hong_Kong": "HK",
    "Asia/Shanghai": "CN", "Asia/Taipei": "TW", "Asia/Tokyo": "JP", "Asia/Seoul": "KR",
    "Australia/Sydney": "AU", "Australia/Melbourne": "AU", "Australia/Brisbane": "AU",
    "Australia/Adelaide": "AU", "Australia/Perth": "AU", "Australia/Hobart": "AU",
    "Pacific/Auckland": "NZ",
}
# The currency of each country, for a locale that names a country but no currency.
EURO = ("IE", "DE", "AT", "FR", "BE", "LU", "NL", "IT", "ES", "PT", "FI", "GR", "SK", "SI",
        "EE", "LV", "LT", "MT", "CY", "HR")
COUNTRY_CURRENCY = {
    **dict.fromkeys(EURO, "EUR"),
    "US": "USD", "CA": "CAD", "MX": "MXN", "BR": "BRL", "AR": "ARS", "CL": "CLP", "CO": "COP",
    "GB": "GBP", "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "IS": "ISK",
    "PL": "PLN", "CZ": "CZK", "HU": "HUF", "RO": "RON", "BG": "BGN", "UA": "UAH", "TR": "TRY",
    "IL": "ILS", "AE": "AED", "QA": "QAR", "SA": "SAR", "ZA": "ZAR", "NG": "NGN",
    "IN": "INR", "SG": "SGD", "MY": "MYR", "ID": "IDR", "TH": "THB", "PH": "PHP", "VN": "VND",
    "HK": "HKD", "CN": "CNY", "TW": "TWD", "JP": "JPY", "KR": "KRW", "AU": "AUD", "NZ": "NZD",
}
# A zone missing from ZONE_COUNTRY still names its continent.
CONTINENT_COUNTRY = {
    "America": "US", "Europe": "DE", "Africa": "ZA", "Asia": "SG", "Australia": "AU",
}
WINDOWS_ZONES = {
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    "Canada Central Standard Time": "America/Regina",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Buenos_Aires",
    "Pacific SA Standard Time": "America/Santiago",
    "SA Pacific Standard Time": "America/Bogota",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "GTB Standard Time": "Europe/Bucharest",
    "FLE Standard Time": "Europe/Kiev",
    "Israel Standard Time": "Asia/Jerusalem",
    "Arabian Standard Time": "Asia/Dubai",
    "Arab Standard Time": "Asia/Riyadh",
    "South Africa Standard Time": "Africa/Johannesburg",
    "India Standard Time": "Asia/Calcutta",
    "Singapore Standard Time": "Asia/Singapore",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "E. Australia Standard Time": "Australia/Brisbane",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "W. Australia Standard Time": "Australia/Perth",
    "Tasmania Standard Time": "Australia/Hobart",
    "New Zealand Standard Time": "Pacific/Auckland",
    "UTC": "Etc/UTC",
}


def windows_zone_id() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
            return str(winreg.QueryValueEx(key, "TimeZoneKeyName")[0])
    except OSError:
        return ""


def local_timezone() -> str:
    """The IANA name of this computer's time zone, or Etc/UTC when it cannot be read."""
    if os.name == "nt":
        return WINDOWS_ZONES.get(windows_zone_id(), "Etc/UTC")
    zone = os.environ.get("TZ", "").lstrip(":")
    if zone != "" and not zone.startswith("/"):
        return zone
    try:
        target = os.readlink(zone or "/etc/localtime")
    except OSError:
        target = ""
    if "zoneinfo/" in target:
        return target.split("zoneinfo/", 1)[1]
    try:
        return Path("/etc/timezone").read_text().strip() or "Etc/UTC"
    except OSError:
        return "Etc/UTC"


def country(zone: str) -> str:
    return ZONE_COUNTRY.get(zone) or CONTINENT_COUNTRY.get(zone.split("/", 1)[0], "")


def zone_region(provider: str, zone: str) -> str:
    """The region nearest the place the time zone names, or "" when it names no place."""
    regions = ZONE_REGIONS.get(zone) or COUNTRY_REGIONS.get(country(zone))
    return regions[PROVIDERS.index(provider)] if regions else ""


def ini_value(path: Path, sections: tuple[str, ...], key: str) -> str:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return ""
    for section in sections:
        value = parser.get(section, key, fallback="").strip()
        if value != "":
            return value
    return ""


def aws_profile_region(platform: dict) -> tuple[str, str]:
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        if os.environ.get(name, "").strip() != "":
            return os.environ[name].strip(), f"the environment variable {name} names it"
    profile = str(platform.get("profile") or os.environ.get("AWS_PROFILE") or "default")
    path = Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")
    section = "default" if profile == "default" else f"profile {profile}"
    region = ini_value(path, (section,), "region")
    return region, f"the AWS CLI profile {profile} in {path} names it"


def azure_profile_region() -> tuple[str, str]:
    path = Path(os.environ.get("AZURE_CONFIG_DIR") or Path.home() / ".azure") / "config"
    return ini_value(path, ("defaults",), "location"), f"defaults.location in {path} names it"


def gcloud_config_dir() -> Path:
    if os.environ.get("CLOUDSDK_CONFIG"):
        return Path(os.environ["CLOUDSDK_CONFIG"])
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "gcloud"
    return Path.home() / ".config" / "gcloud"


def gcloud_profile_region() -> tuple[str, str]:
    folder = gcloud_config_dir()
    name = os.environ.get("CLOUDSDK_ACTIVE_CONFIG_NAME", "").strip()
    if name == "":
        try:
            name = (folder / "active_config").read_text().strip()
        except OSError:
            name = ""
    path = folder / "configurations" / f"config_{name or 'default'}"
    for section in ("run", "compute"):
        region = ini_value(path, (section,), "region")
        if region != "":
            return region, f"{section}/region in the gcloud configuration {path} names it"
    return "", ""


def profile_region(provider: str, platform: dict) -> tuple[str, str]:
    """The region the provider's own CLI settings name, and why, or ("", "")."""
    if provider == "aws":
        return aws_profile_region(platform)
    if provider == "azure":
        return azure_profile_region()
    return gcloud_profile_region()


def region_suggestion(provider: str, platform: dict | None = None,
                      zone: str | None = None) -> tuple[str, str]:
    """The region to suggest and the reason, in this order: the time zone,
    the provider's CLI settings, then pdt's default."""
    zone = local_timezone() if zone is None else zone
    region = zone_region(provider, zone)
    if region != "":
        return region, f"this computer's time zone is {zone}"
    region, reason = profile_region(provider, platform or {})
    if region != "":
        return region, reason
    region = DEFAULT_REGIONS[PROVIDERS.index(provider)]
    return region, (f"it is pdt's default; the time zone {zone or 'of this computer'} "
                    f"names no place and no {PROVIDER_NAMES[provider]} CLI setting names a region")


def suggest_region(provider: str, zone: str | None = None) -> str:
    return region_suggestion(provider, zone=zone)[0]


def windows_currency() -> str:
    import ctypes
    locale_sintlsymbol = 0x15
    buffer = ctypes.create_unicode_buffer(9)
    if ctypes.windll.kernel32.GetLocaleInfoEx(None, locale_sintlsymbol, buffer, len(buffer)) == 0:
        return ""
    return buffer.value


def mac_locale() -> str:
    try:
        return subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def posix_currency() -> str:
    import locale
    try:
        saved = locale.setlocale(locale.LC_MONETARY)
        locale.setlocale(locale.LC_MONETARY, "")
        try:
            return str(locale.localeconv()["int_curr_symbol"]).strip()
        finally:
            locale.setlocale(locale.LC_MONETARY, saved)
    except locale.Error:
        return ""


def locale_currency(name: str) -> str:
    """The currency a locale name such as en_GB.UTF-8 or en_US@currency=EUR stands for."""
    name, _, keywords = name.partition("@")
    for keyword in keywords.split(";"):
        key, _, value = keyword.partition("=")
        if key == "currency" and value != "":
            return value.upper()
    territory = name.split(".", 1)[0].replace("-", "_").split("_")
    return COUNTRY_CURRENCY.get(territory[-1].upper(), "") if len(territory) > 1 else ""


def local_currency() -> str:
    """The ISO 4217 code of the user's regional setting, or USD when none can be read."""
    if os.name == "nt":
        code = windows_currency()
    elif sys.platform == "darwin":
        code = locale_currency(mac_locale())
    else:
        code = posix_currency()
        if code == "":
            name = next((os.environ[key] for key in ("LC_ALL", "LC_MONETARY", "LANG")
                         if os.environ.get(key, "") != ""), "")
            code = locale_currency(name)
    code = code.strip().upper()
    return code if len(code) == 3 and code.isalpha() else "USD"


def question(provider: str) -> str:
    return f"Which {PROVIDER_NAMES[provider]} region should hold your jobs?"


def choose_region(app: dict, provider: str, assume_yes: bool) -> str:
    """On a project's first deploy, ask for the region and save the answer.

    Returns a problem for the user, or "". --yes takes the suggested region,
    the same way it takes every other default. A run with no one to ask
    and no --yes stops, because the region decides where the data lives.
    """
    if provider not in PROVIDERS or str(app["platform"].get("region") or "") != "":
        return ""
    if any(os.environ.get(name, "").strip() != "" for name in REGION_ENV[provider]):
        return ""
    region, reason = region_suggestion(provider, app["platform"])
    if assume_yes:
        console.say(f"Using {PROVIDER_NAMES[provider]} region {region}, because {reason}.")
    elif not can_prompt(None):
        return (f"no {PROVIDER_NAMES[provider]} region is set. Add `region: {region}` under "
                f"platform: in {config.PROJECT_FILE}, or run again with --yes to use {region}.")
    else:
        console.say()
        console.say(f"pdt suggests {region}, because {reason}.")
        console.field(f"All {PROVIDER_NAMES[provider]} regions", REGION_LISTS[provider])
        try:
            region = console.ask(question(provider), region) or region
        except EOFError:
            return (f"no {PROVIDER_NAMES[provider]} region is set. Add `region: {region}` under "
                    f"platform: in {config.PROJECT_FILE}.")
    saved = config.save_platform_key(app, "region", region)
    console.done(f"Saved region {region} to {saved.relative_to(config.find_project())}.")
    return ""
