# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import os
import re
import stat
import platform
import subprocess
import json
import certifi

from json.decoder import JSONDecodeError
from contextlib import suppress
from datetime import datetime, timedelta

import requests
import semver

from urllib.request import urlopen
from knack.log import get_logger
from azure.cli.core.api import get_config_dir
from azure.cli.core.azclierror import (
    FileOperationError,
    ValidationError,
    UnclassifiedUserFault,
    ClientRequestError,
    InvalidTemplateError,
)
from azure.cli.core.util import should_disable_connection_verify

# See: https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string
_semver_pattern = r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?"  # pylint: disable=line-too-long

# See: https://learn.microsoft.com/azure/azure-resource-manager/templates/template-syntax#template-format
_template_schema_pattern = r"https?://schema\.management\.azure\.com/schemas/[0-9a-zA-Z-]+/(?P<templateType>[a-zA-Z]+)Template\.json#?"  # pylint: disable=line-too-long

_bicep_diagnostic_warning_pattern = r"^([^\s].*)\((\d+)(?:,\d+|,\d+,\d+)?\)\s+:\s+(Warning)\s+([a-zA-Z-\d]+):\s*(.*?)\s+\[(.*?)\]$"  # pylint: disable=line-too-long

_config_dir = get_config_dir()
_bicep_installation_dir = os.path.join(_config_dir, "bin")
_bicep_version_check_file_path = os.path.join(_config_dir, "bicepVersionCheck.json")
_bicep_version_check_cache_ttl = timedelta(minutes=10)
_bicep_version_check_time_format = "%Y-%m-%dT%H:%M:%S.%f"

_logger = get_logger(__name__)

_requests_verify = not should_disable_connection_verify()


def validate_bicep_target_scope(template_schema, deployment_scope):
    target_scope = _template_schema_to_target_scope(template_schema)
    if target_scope != deployment_scope:
        raise InvalidTemplateError(
            f'The target scope "{target_scope}" does not match the deployment scope "{deployment_scope}".'
        )


def run_bicep_command(cli_ctx, args, auto_install=True):
    if _use_binary_from_path(cli_ctx):
        from shutil import which

        if which("bicep") is None:
            raise ValidationError(
                'Could not find the "bicep" executable on PATH. To install Bicep via Azure CLI, set the "bicep.use_binary_from_path" configuration to False and run "az bicep install".'  # pylint: disable=line-too-long
            )

        bicep_version_message = _run_command("bicep", ["--version"])

        _logger.debug("Using Bicep CLI from PATH. %s", bicep_version_message)

        return _run_command("bicep", args)

    installation_path = _get_bicep_installation_path(platform.system())
    _logger.debug("Bicep CLI installation path: %s", installation_path)

    installed = os.path.isfile(installation_path)
    _logger.debug("Bicep CLI installed: %s.", installed)

    check_version = cli_ctx.config.getboolean("bicep", "check_version", True)

    if not installed:
        if auto_install:
            ensure_bicep_installation(stdout=False)
        else:
            raise FileOperationError('Bicep CLI not found. Install it now by running "az bicep install".')
    elif check_version:
        latest_release_tag, cache_expired = _load_bicep_version_check_result_from_cache()

        with suppress(ClientRequestError):
            # Checking upgrade should ignore connection issues.
            # Users may continue using the current installed version.
            installed_version = _get_bicep_installed_version(installation_path)
            latest_release_tag = get_bicep_latest_release_tag() if cache_expired else latest_release_tag
            latest_version = _extract_semver(latest_release_tag)

            if installed_version and latest_version and semver.compare(installed_version, latest_version) < 0:
                _logger.warning(
                    'A new Bicep release is available: %s. Upgrade now by running "az bicep upgrade".',
                    latest_release_tag,
                )

            if cache_expired:
                _refresh_bicep_version_check_cache(latest_release_tag)

    return _run_command(installation_path, args)


def ensure_bicep_installation(release_tag=None, target_platform=None, stdout=True):
    system = platform.system()
    installation_path = _get_bicep_installation_path(system)

    if os.path.isfile(installation_path):
        if not release_tag:
            return

        installed_version = _get_bicep_installed_version(installation_path)
        target_version = _extract_semver(release_tag)
        if installed_version and target_version and semver.compare(installed_version, target_version) == 0:
            return

    installation_dir = os.path.dirname(installation_path)
    if not os.path.exists(installation_dir):
        os.makedirs(installation_dir)

    try:
        release_tag = release_tag if release_tag else get_bicep_latest_release_tag()
        if stdout:
            if release_tag:
                print(f"Installing Bicep CLI {release_tag}...")
            else:
                print("Installing Bicep CLI...")
        os.environ.setdefault("CURL_CA_BUNDLE", certifi.where())
        request = urlopen(_get_bicep_download_url(system, release_tag, target_platform=target_platform))
        with open(installation_path, "wb") as f:
            f.write(request.read())

        os.chmod(installation_path, os.stat(installation_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        if stdout:
            print(f'Successfully installed Bicep CLI to "{installation_path}".')
        else:
            _logger.info(
                "Successfully installed Bicep CLI to %s",
                installation_path,
            )
    except IOError as err:
        raise ClientRequestError(f"Error while attempting to download Bicep CLI: {err}")


def remove_bicep_installation():
    system = platform.system()
    installation_path = _get_bicep_installation_path(system)

    if os.path.exists(installation_path):
        os.remove(installation_path)
    if os.path.exists(_bicep_version_check_file_path):
        os.remove(_bicep_version_check_file_path)


def is_bicep_file(file_path):
    return file_path.lower().endswith(".bicep")


def get_bicep_available_release_tags():
    try:
        os.environ.setdefault("CURL_CA_BUNDLE", certifi.where())
        response = requests.get("https://aka.ms/BicepReleases", verify=_requests_verify)
        return [release["tag_name"] for release in response.json()]
    except IOError as err:
        raise ClientRequestError(f"Error while attempting to retrieve available Bicep versions: {err}.")


def get_bicep_latest_release_tag():
    try:
        os.environ.setdefault("CURL_CA_BUNDLE", certifi.where())
        response = requests.get("https://aka.ms/BicepLatestRelease", verify=_requests_verify)
        response.raise_for_status()
        return response.json()["tag_name"]
    except IOError as err:
        raise ClientRequestError(f"Error while attempting to retrieve the latest Bicep version: {err}.")


def bicep_version_greater_than_or_equal_to(version):
    system = platform.system()
    installation_path = _get_bicep_installation_path(system)
    installed_version = _get_bicep_installed_version(installation_path)
    return semver.compare(installed_version, version) >= 0


def supports_bicep_publish():
    system = platform.system()
    installation_path = _get_bicep_installation_path(system)
    installed_version = _get_bicep_installed_version(installation_path)
    return semver.compare(installed_version, "0.4.1008") >= 0


def _bicep_installed_in_ci():
    if "GITHUB_ACTIONS" in os.environ or "TF_BUILD" in os.environ:
        from shutil import which
        installed = which("bicep") is not None

        _logger.debug("Running in a CI environment. Bicep CLI available on PATH: %s.", installed)

        return installed
    return False


def _use_binary_from_path(cli_ctx):
    use_binary_from_path = cli_ctx.config.get("bicep", "use_binary_from_path", "if_found_in_ci").lower()

    _logger.debug('Current value of "use_binary_from_path": %s.', use_binary_from_path)

    if use_binary_from_path == "if_found_in_ci":
        # With if_found_in_ci, GitHub Actions and Azure Pipeline users may expect some delay (usually a few days)
        # in getting the latest version of Bicep CLI, since the az bicep commands will use the pre-installed Bicep CLI
        # on the build agents, but the build agents has a different release cycle. The benefit is that the az bicep
        # commands will not download the Bicep CLI on each pipeline run.
        return _bicep_installed_in_ci()
    if use_binary_from_path in ["1", "yes", "true", "on"]:
        # Setting the config True forces the az bicep commands to use the Bicep executable added to PATH, which
        # indicates that the user is intended to manage the Bicep CLI, and version checks will be disabled.
        return True
    if use_binary_from_path in ["0", "no", "false", "off"]:
        return False

    _logger.warning(
        'The configuration value of bicep.use_binary_from_path is invalid: "%s". Possible values include "if_found_in_ci" (default) and Booleans.',  # pylint: disable=line-too-long
        use_binary_from_path,
    )

    return False


def _load_bicep_version_check_result_from_cache():
    try:
        with open(_bicep_version_check_file_path, "r") as version_check_file:
            version_check_data = json.load(version_check_file)
            latest_release_tag = version_check_data["latestReleaseTag"]
            last_check_time = datetime.strptime(version_check_data["lastCheckTime"], _bicep_version_check_time_format)
            cache_expired = datetime.now() - last_check_time > _bicep_version_check_cache_ttl

            return latest_release_tag, cache_expired
    except (IOError, JSONDecodeError):
        return None, True


def _refresh_bicep_version_check_cache(lastest_release_tag):
    with open(_bicep_version_check_file_path, "w+") as version_check_file:
        version_check_data = {
            "lastCheckTime": datetime.now().strftime(_bicep_version_check_time_format),
            "latestReleaseTag": lastest_release_tag,
        }
        json.dump(version_check_data, version_check_file)


def _get_bicep_installed_version(bicep_executable_path):
    installed_version_output = _run_command(bicep_executable_path, ["--version"])
    return _extract_semver(installed_version_output)


def _get_bicep_download_url(system, release_tag, target_platform=None):
    download_url = f"https://downloads.bicep.azure.com/{release_tag}/{{}}"

    if target_platform:
        executable_name = "bicep-win-x64.exe" if target_platform == "win-x64" else f"bicep-{target_platform}"
        return download_url.format(executable_name)

    if system == "Windows":
        return download_url.format("bicep-win-x64.exe")
    if system == "Linux":
        if os.path.exists("/lib/ld-musl-x86_64.so.1") and not os.path.exists("/lib/x86_64-linux-gnu/libc.so.6"):
            return download_url.format("bicep-linux-musl-x64")
        return download_url.format("bicep-linux-x64")
    if system == "Darwin":
        return download_url.format("bicep-osx-x64")

    raise ValidationError(f'The platform "{system}" is not supported.')


def _get_bicep_installation_path(system):
    if system == "Windows":
        return os.path.join(_bicep_installation_dir, "bicep.exe")
    if system in ("Linux", "Darwin"):
        return os.path.join(_bicep_installation_dir, "bicep")

    raise ValidationError(f'The platform "{system}" is not supported.')


def _extract_semver(text):
    semver_match = re.search(_semver_pattern, text)
    return semver_match.group(0) if semver_match else None


def _run_command(bicep_installation_path, args):
    process = subprocess.run([rf"{bicep_installation_path}"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        process.check_returncode()
        command_warnings = process.stderr.decode("utf-8")
        if command_warnings:
            _logger.warning(command_warnings)
        return process.stdout.decode("utf-8")
    except subprocess.CalledProcessError:
        stderr_output = process.stderr.decode("utf-8")
        errors = []

        for line in stderr_output.splitlines():
            if re.match(_bicep_diagnostic_warning_pattern, line):
                _logger.warning(line)
            else:
                errors.append(line)

        error_msg = os.linesep.join(errors)
        raise UnclassifiedUserFault(error_msg)


def _template_schema_to_target_scope(template_schema):
    template_schema_match = re.search(_template_schema_pattern, template_schema)
    template_type = template_schema_match.group("templateType") if template_schema_match else None
    template_type_lower = template_type.lower() if template_type else None

    if template_type_lower == "deployment":
        return "resourceGroup"
    if template_type_lower == "subscriptiondeployment":
        return "subscription"
    if template_type_lower == "managementgroupdeployment":
        return "managementGroup"
    if template_type_lower == "tenantdeployment":
        return "tenant"
    return None
