import base64
import hashlib
import hmac
import json
import os
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime

import html2text
import pytz
import stix2
import yaml
from dateutil.parser import parse
from pycti import (
    AttackPattern,
    CustomObservableHostname,
    Identity,
    Indicator,
    IntrusionSet,
    Location,
    Malware,
    OpenCTIConnectorHelper,
    Report,
    StixCoreRelationship,
    Vulnerability,
    get_config_variable,
)


def smart_truncate(content, length=100, suffix="..."):
    if len(content) <= length:
        return content
    else:
        return " ".join(content[: length + 1].split(" ")[0:-1]) + suffix


class Silobreaker:
    def __init__(self):
        # Instantiate the connector helper from config
        config_file_path = os.path.dirname(os.path.abspath(__file__)) + "/config.yml"
        config = (
            yaml.load(open(config_file_path), Loader=yaml.FullLoader)
            if os.path.isfile(config_file_path)
            else {}
        )
        self.helper = OpenCTIConnectorHelper(config)
        self.duration_period = get_config_variable(
            "CONNECTOR_DURATION_PERIOD",
            ["connector", "duration_period"],
            config,
        )
        # Extra config
        self.silobreaker_api_url = get_config_variable(
            "SILOBREAKER_API_URL",
            ["silobreaker", "api_url"],
            config,
            default="https://api.silobreaker.com",
        )
        self.silobreaker_api_key = get_config_variable(
            "SILOBREAKER_API_KEY", ["silobreaker", "api_key"], config
        )
        self.silobreaker_api_shared = get_config_variable(
            "SILOBREAKER_API_SHARED", ["silobreaker", "api_shared"], config
        )
        self.silobreaker_import_start_date = get_config_variable(
            "SILOBREAKER_IMPORT_START_DATE",
            ["silobreaker", "import_start_date"],
            config,
        )
        self.silobreaker_lists = get_config_variable(
            "SILOBREAKER_LISTS",
            ["silobreaker", "lists"],
            config,
            default="138809,96910,36592,55112,50774",
        ).split(",")
        self.silobreaker_interval = get_config_variable(
            "SILOBREAKER_INTERVAL", ["silobreaker", "interval"], config, isNumber=True
        )

        self.identity = self.helper.api.identity.create(
            type="Organization",
            name="Silobreaker",
            description="Silobreaker helps security, business and intelligence professionals make sense of the overwhelming amount of data on the web.",
        )
        # Init variables
        self.auth_token = None
        self.cache = {}

    def get_interval(self):
        return int(self.silobreaker_interval) * 60

    def _query(self, method, url, body=None):
        try:
            if method == "POST":
                verb = "POST"
                urlSignature = verb + " " + url
                message = urlSignature.encode() + body
                hmac_sha1 = hmac.new(
                    self.silobreaker_api_shared.encode(),
                    message,
                    digestmod=hashlib.sha1,
                )
                digest = base64.b64encode(hmac_sha1.digest())
                final_url = (
                    url
                    + ("&" if "?" in url else "?")
                    + "apiKey="
                    + self.silobreaker_api_key
                    + "&digest="
                    + urllib.parse.quote(digest.decode())
                )
                req = urllib.request.Request(
                    final_url, data=body, headers={"Content-Type": "application/json"}
                )
            elif method == "DOWNLOAD":
                verb = "GET"
                message = verb + " " + url
                hmac_sha1 = hmac.new(
                    self.silobreaker_api_shared.encode(),
                    message.encode(),
                    digestmod=hashlib.sha1,
                )
                digest = base64.b64encode(hmac_sha1.digest())
                final_url = (
                    url
                    + ("&" if "?" in url else "?")
                    + "apiKey="
                    + self.silobreaker_api_key
                    + "&digest="
                    + urllib.parse.quote(digest.decode())
                )
                req = urllib.request.Request(final_url)
            else:
                verb = "GET"
                message = verb + " " + url
                hmac_sha1 = hmac.new(
                    self.silobreaker_api_shared.encode(),
                    message.encode(),
                    digestmod=hashlib.sha1,
                )
                digest = base64.b64encode(hmac_sha1.digest())
                final_url = (
                    url
                    + ("&" if "?" in url else "?")
                    + "apiKey="
                    + self.silobreaker_api_key
                    + "&digest="
                    + urllib.parse.quote(digest.decode())
                )
                req = urllib.request.Request(final_url)

            if method == "DOWNLOAD":
                return base64.b64encode(urllib.request.urlopen(req).read()).decode(
                    "utf-8"
                )
            else:
                with urllib.request.urlopen(req) as response:
                    responseJson = response.read()
                return json.loads(responseJson.decode("utf-8"))
        except urllib.request.HTTPError as err:
            # In this specific case, get error from API response
            error_metadata = {
                "error_status_reason": err.reason,
                "error_status": str(err.status),
                "url": err.url,
            }
            self.helper.connector_logger.error(
                "[API] An error occurred while trying to request the list",
                error_metadata,
            )
            return {}
        except Exception as err:
            error_metadata = {"error": err}
            self.helper.connector_logger.error(
                "[API] An error occurred while trying to request the list",
                error_metadata,
            )

    def _convert_to_markdown(self, content):
        text_maker = html2text.HTML2Text()
        text_maker.body_width = 0
        text_maker.ignore_links = False
        text_maker.ignore_images = False
        text_maker.ignore_tables = False
        text_maker.ignore_emphasis = False
        text_maker.skip_internal_links = False
        text_maker.inline_links = True
        text_maker.protect_links = True
        text_maker.mark_code = True
        content_md = text_maker.handle(content)
        content_md = content_md.replace("hxxps", "https")
        content_md = content_md.replace("](//", "](https://")
        return content_md

    def _process_items(self, data, work_id):
        for item in data["Items"]:
            if (
                item["Type"] == "Report"
                or item["Type"] == "News"
                or item["Type"] == "User Article"
                or item["Type"] == "Blog"
            ):
                objects = []
                threats = []
                users = []
                used = []
                victims = []
                observables = []
                indicators = []
                entities = (
                    item.get("Extras", {}).get("RelatedEntities", {}).get("Items", [])
                )
                external_references = []
                external_references.append(
                    stix2.ExternalReference(
                        source_name="Silobreaker", url=item["SilobreakerUrl"]
                    )
                )
                if "SourceUrl" in item:
                    external_references.append(
                        stix2.ExternalReference(
                            source_name=item["Publisher"], url=item["SourceUrl"]
                        )
                    )

                if entities:
                    for entity in entities:
                        enrichment = self._query(
                            "GET",
                            self.silobreaker_api_url
                            + "/v2/enrichments?type="
                            + entity["Type"]
                            + "&description="
                            + urllib.parse.quote(entity["Description"]),
                        )
                        score = 50
                        if "modules" in enrichment and entity["Type"] in [
                            "Email",
                            "Subdomain",
                            "IPv4",
                            "Domain",
                        ]:
                            for module in enrichment["modules"]:
                                if "risk" in module and "riskScore" in module["risk"]:
                                    score = module["risk"]["riskScore"]
                        custom_properties = {
                            "x_opencti_score": score,
                            "created_by_ref": self.identity["standard_id"],
                            "external_references": external_references,
                        }
                        if entity["Type"] == "ThreatActor":
                            actor_stix = stix2.IntrusionSet(
                                id=IntrusionSet.generate_id(entity["Description"]),
                                name=entity["Description"],
                                description=entity["Description"],
                                created_by_ref=self.identity["standard_id"],
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                            )
                            objects.append(actor_stix)
                            threats.append(actor_stix)
                            users.append(actor_stix)
                        if entity["Type"] == "Malware":
                            malware_stix = stix2.Malware(
                                id=Malware.generate_id(entity["Description"]),
                                name=entity["Description"],
                                description=entity["Description"],
                                created_by_ref=self.identity["standard_id"],
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                is_family=True,
                            )
                            objects.append(malware_stix)
                            threats.append(malware_stix)
                            used.append(malware_stix)
                        if entity["Type"] == "MitreTechnique":
                            attack_pattern_stix = stix2.AttackPattern(
                                id=AttackPattern.generate_id(entity["Description"]),
                                name=entity["Description"],
                                description=entity["Description"],
                                created_by_ref=self.identity["standard_id"],
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                            )
                            objects.append(attack_pattern_stix)
                            used.append(attack_pattern_stix)
                        if entity["Type"] == "Person":
                            individual_stix = stix2.Identity(
                                id=Identity.generate_id(
                                    entity["Description"], "individual"
                                ),
                                name=entity["Description"],
                                identity_class="individual",
                                description=entity["Description"],
                                created_by_ref=self.identity["standard_id"],
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                            )
                            objects.append(individual_stix)
                        if entity["Type"] == "Country":
                            country_stix = stix2.Location(
                                id=Location.generate_id(
                                    entity["Description"], "Country"
                                ),
                                name=entity["Description"],
                                description=entity["Description"],
                                country=entity["Description"],
                                created_by_ref=self.identity["standard_id"],
                                allow_custom=True,
                                custom_properties={
                                    "x_opencti_location_type": "Country"
                                },
                            )
                            objects.append(country_stix)
                            victims.append(country_stix)
                        if entity["Type"] == "City":
                            city_stix = stix2.Location(
                                id=Location.generate_id(entity["Description"], "City"),
                                name=entity["Description"],
                                description=entity["Description"],
                                country=entity["Description"],
                                created_by_ref=self.identity["standard_id"],
                                allow_custom=True,
                                custom_properties={"x_opencti_location_type": "City"},
                            )
                            objects.append(city_stix)
                            victims.append(city_stix)
                        if entity["Type"] == "Company":
                            organization_stix = stix2.Identity(
                                id=Identity.generate_id(
                                    entity["Description"], "organization"
                                ),
                                name=entity["Description"],
                                identity_class="organization",
                                description=entity["Description"],
                                created_by_ref=self.identity["standard_id"],
                            )
                            objects.append(organization_stix)
                        if entity["Type"] == "Organization":
                            organization_stix = stix2.Identity(
                                id=Identity.generate_id(
                                    entity["Description"], "organization"
                                ),
                                name=entity["Description"],
                                identity_class="organization",
                                description=entity["Description"],
                                created_by_ref=self.identity["standard_id"],
                            )
                            objects.append(organization_stix)
                        if entity["Type"] == "GovernmentBody":
                            organization_stix = stix2.Identity(
                                id=Identity.generate_id(
                                    entity["Description"], "organization"
                                ),
                                name=entity["Description"],
                                identity_class="organization",
                                description=entity["Description"],
                                created_by_ref=self.identity["standard_id"],
                            )
                            objects.append(organization_stix)
                        if entity["Type"] == "Vulnerability":
                            vulnerability_stix = stix2.Vulnerability(
                                id=Vulnerability.generate_id(entity["Description"]),
                                name=entity["Description"],
                                description=entity["Description"],
                                created_by_ref=self.identity["standard_id"],
                            )
                            objects.append(vulnerability_stix)
                            victims.append(vulnerability_stix)

                        ## Observables
                        if entity["Type"] == "Domain":
                            domain_stix = stix2.DomainName(
                                value=entity["Description"],
                                allow_custom=True,
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                custom_properties=custom_properties,
                            )
                            objects.append(domain_stix)
                            observables.append(domain_stix)
                            pattern = (
                                "[domain-name:value = '" + entity["Description"] + "']"
                            )
                            indicator_stix = stix2.Indicator(
                                id=Indicator.generate_id(pattern),
                                name=entity["Description"],
                                pattern_type="stix",
                                pattern=pattern,
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                created_by_ref=self.identity["standard_id"],
                                custom_properties={
                                    "x_opencti_score": score,
                                    "x_opencti_main_observable_type": "Domain-Name",
                                },
                            )
                            objects.append(indicator_stix)
                            indicators.append(indicator_stix)
                            based_on_stix = stix2.Relationship(
                                id=StixCoreRelationship.generate_id(
                                    "based-on",
                                    indicator_stix.get("id"),
                                    domain_stix.get("id"),
                                ),
                                relationship_type="based-on",
                                source_ref=indicator_stix.get("id"),
                                target_ref=domain_stix.get("id"),
                            )
                            objects.append(based_on_stix)
                        if entity["Type"] == "IPv4":
                            ip_stix = stix2.IPv4Address(
                                value=entity["Description"],
                                allow_custom=True,
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                custom_properties=custom_properties,
                            )
                            objects.append(ip_stix)
                            observables.append(ip_stix)
                            pattern = (
                                "[ipv4-addr:value = '" + entity["Description"] + "']"
                            )
                            indicator_stix = stix2.Indicator(
                                id=Indicator.generate_id(pattern),
                                name=entity["Description"],
                                pattern_type="stix",
                                pattern=pattern,
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                created_by_ref=self.identity["standard_id"],
                                custom_properties={
                                    "x_opencti_score": score,
                                    "x_opencti_main_observable_type": "IPv4-Addr",
                                },
                            )
                            objects.append(indicator_stix)
                            indicators.append(indicator_stix)
                            based_on_stix = stix2.Relationship(
                                id=StixCoreRelationship.generate_id(
                                    "based-on",
                                    indicator_stix.get("id"),
                                    ip_stix.get("id"),
                                ),
                                relationship_type="based-on",
                                source_ref=indicator_stix.get("id"),
                                target_ref=ip_stix.get("id"),
                            )
                            objects.append(based_on_stix)
                        if entity["Type"] == "Subdomain":
                            hostname_stix = CustomObservableHostname(
                                value=entity["Description"],
                                allow_custom=True,
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                custom_properties=custom_properties,
                            )
                            objects.append(hostname_stix)
                            observables.append(hostname_stix)
                            pattern = (
                                "[hostname:value = '" + entity["Description"] + "']"
                            )
                            indicator_stix = stix2.Indicator(
                                id=Indicator.generate_id(pattern),
                                name=entity["Description"],
                                pattern_type="stix",
                                pattern=pattern,
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                created_by_ref=self.identity["standard_id"],
                                custom_properties={
                                    "x_opencti_score": score,
                                    "x_opencti_main_observable_type": "Hostname",
                                },
                            )
                            objects.append(indicator_stix)
                            indicators.append(indicator_stix)
                            based_on_stix = stix2.Relationship(
                                id=StixCoreRelationship.generate_id(
                                    "based-on",
                                    indicator_stix.get("id"),
                                    hostname_stix.get("id"),
                                ),
                                relationship_type="based-on",
                                source_ref=indicator_stix.get("id"),
                                target_ref=hostname_stix.get("id"),
                            )
                            objects.append(based_on_stix)
                        if entity["Type"] == "Email":
                            email_stix = stix2.EmailAddress(
                                value=entity["Description"],
                                allow_custom=True,
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                custom_properties=custom_properties,
                            )
                            objects.append(email_stix)
                            observables.append(email_stix)
                            pattern = (
                                "[email-address:value = '"
                                + entity["Description"]
                                + "']"
                            )
                            indicator_stix = stix2.Indicator(
                                id=Indicator.generate_id(pattern),
                                name=entity["Description"],
                                pattern_type="stix",
                                pattern=pattern,
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                created_by_ref=self.identity["standard_id"],
                                custom_properties={
                                    "x_opencti_score": score,
                                    "x_opencti_main_observable_type": "Hostname",
                                },
                            )
                            objects.append(indicator_stix)
                            indicators.append(indicator_stix)
                            based_on_stix = stix2.Relationship(
                                id=StixCoreRelationship.generate_id(
                                    "based-on",
                                    indicator_stix.get("id"),
                                    email_stix.get("id"),
                                ),
                                relationship_type="based-on",
                                source_ref=indicator_stix.get("id"),
                                target_ref=email_stix.get("id"),
                            )
                            objects.append(based_on_stix)

                if len(threats) > 0 and len(victims) > 0:
                    for threat in threats:
                        for victim in victims:
                            relationship_stix = stix2.Relationship(
                                id=StixCoreRelationship.generate_id(
                                    "targets",
                                    threat.get("id"),
                                    victim.get("id"),
                                    item["PublicationDate"],
                                ),
                                relationship_type="targets",
                                source_ref=threat.get("id"),
                                target_ref=victim.get("id"),
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                created_by_ref=self.identity["standard_id"],
                                start_time=item["PublicationDate"],
                            )
                            objects.append(relationship_stix)
                if len(users) > 0 and len(used) > 0:
                    for user in users:
                        for use in used:
                            relationship_stix = stix2.Relationship(
                                id=StixCoreRelationship.generate_id(
                                    "uses",
                                    user.get("id"),
                                    use.get("id"),
                                    item["PublicationDate"],
                                ),
                                relationship_type="uses",
                                source_ref=user.get("id"),
                                target_ref=use.get("id"),
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                created_by_ref=self.identity["standard_id"],
                                start_time=item["PublicationDate"],
                            )
                            objects.append(relationship_stix)
                if len(threats) > 0 and len(observables) > 0:
                    for threat in threats:
                        for observable in observables:
                            relationship_stix = stix2.Relationship(
                                id=StixCoreRelationship.generate_id(
                                    "related-to",
                                    observable.get("id"),
                                    threat.get("id"),
                                    item["PublicationDate"],
                                ),
                                relationship_type="related-to",
                                source_ref=observable.get("id"),
                                target_ref=threat.get("id"),
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                created_by_ref=self.identity["standard_id"],
                                start_time=item["PublicationDate"],
                            )
                            objects.append(relationship_stix)
                if len(threats) > 0 and len(indicators) > 0:
                    for threat in threats:
                        for indicator in indicators:
                            relationship_stix = stix2.Relationship(
                                id=StixCoreRelationship.generate_id(
                                    "indicates", indicator.get("id"), threat.get("id")
                                ),
                                relationship_type="indicates",
                                source_ref=indicator.get("id"),
                                target_ref=threat.get("id"),
                                object_marking_refs=[stix2.TLP_GREEN.get("id")],
                                created_by_ref=self.identity["standard_id"],
                            )
                            objects.append(relationship_stix)
                if len(objects) > 0:
                    description = smart_truncate(
                        self._convert_to_markdown(
                            item["Extras"]["DocumentTeasers"]["HtmlSnippet"]
                        ),
                        200,
                        "...",
                    )
                    content = item["Extras"]["DocumentTeasers"]["HtmlSnippet"]
                    if (
                        "DocumentFullText" in item["Extras"]
                        and "HtmlFullText" in item["Extras"]["DocumentFullText"]
                    ):
                        content = (
                            item["Extras"]["DocumentFullText"]["HtmlFullText"]
                            .encode("utf-8")
                            .decode("utf-8")
                        )
                        description = smart_truncate(
                            self._convert_to_markdown(
                                item["Extras"]["DocumentFullText"]["HtmlFullText"]
                            ),
                            200,
                            "...",
                        )
                    file = None
                    if (
                        "FileName" in item
                        and "DownloadUrl" in item
                        and item["FileName"].endswith(".pdf")
                    ):
                        file = {
                            "name": item["FileName"],
                            "mime_type": "application/pdf",
                            "data": self._query("DOWNLOAD", item["DownloadUrl"]),
                        }

                    report_stix = stix2.Report(
                        id=Report.generate_id(
                            item["Description"], item["PublicationDate"]
                        ),
                        name=item["Description"],
                        description=description,
                        report_types=[item["Type"]],
                        published=item["PublicationDate"],
                        created=item["PublicationDate"],
                        modified=item["PublicationDate"],
                        created_by_ref=self.identity["standard_id"],
                        object_marking_refs=[stix2.TLP_GREEN.get("id")],
                        object_refs=[object["id"] for object in objects],
                        external_references=external_references,
                        allow_custom=True,
                        custom_properties={
                            "x_opencti_files": [file] if file is not None else [],
                            "x_opencti_content": content,
                        },
                    )
                    objects.append(report_stix)
                    bundle = stix2.Bundle(
                        objects=objects,
                        allow_custom=True,
                    )
                    self.helper.send_stix2_bundle(
                        bundle.serialize(),
                        work_id=work_id,
                    )

    def _import_documents(self, list, work_id, delta_days):
        url = (
            self.silobreaker_api_url
            + '/v2/documents/search?query=list:"'
            + urllib.parse.quote(list)
            + '"%20fromdate:-'
            + str(delta_days)
            + "&extras=documentTeasers%2CdocumentXml%2CDocumentFullText&pagesize=100&sortDirection=asc&includeEntities=True&maxNoEntities=200"
            + "&entityTypes=ThreatActor%2CMalware%2CMitreTechnique%2CPerson%2CCountry%2CCity%2CCompany%2COrganization%2CGovernmentBody%2CVulnerability%2CDomain%2CIPv4%2CSubdomain%2CEmail"
        )
        data = self._query("GET", url)
        if "Items" in data and "ResultCount" in data and data["ResultCount"] > 0:
            total_iterations = round(data["TotalCount"] / data["ResultCount"]) + 1
            page_number = 0
            while page_number <= total_iterations:
                if (
                    "Items" in data
                    and "ResultCount" in data
                    and data["ResultCount"] > 0
                ):
                    self._process_items(data, work_id)
                    page_number = page_number + 1
                    self.helper.connector_logger.info(
                        "Iterating from "
                        + str(page_number * 100)
                        + " to "
                        + str(page_number * 100 + 100)
                    )
                    url = (
                        self.silobreaker_api_url
                        + '/v2/documents/search?query=list:"'
                        + urllib.parse.quote(list)
                        + '"%20fromdate:-'
                        + str(delta_days)
                        + "&extras=documentTeasers%2CdocumentXml%2CDocumentFullText&pageNumber="
                        + str(page_number)
                        + "&pagesize=100&sortDirection=asc&includeEntities=True&maxNoEntities=200"
                        + "&entityTypes=ThreatActor%2CMalware%2CMitreTechnique%2CPerson%2CCountry%2CCity%2CCompany%2COrganization%2CGovernmentBody%2CVulnerability%2CDomain%2CIPv4%2CSubdomain%2CEmail"
                    )
                    data = self._query("GET", url)

    def _process_lists(self, work_id, delta_days):
        for list in self.silobreaker_lists:
            url = self.silobreaker_api_url + "/v2/lists/15_" + list
            data = self._query("GET", url)

            # If data exists and "Description" in data, import documents. Else log the error for each list
            if data and data.get("Description"):
                self._import_documents(data["Description"], work_id, delta_days)
            else:
                self.helper.connector_logger.error(
                    "No data found for, please check your account activation and API key",
                    {"list_concerned": list},
                )

    def process_message(self):
        try:
            # Get the current timestamp and check
            current_state = self.helper.get_state()
            if current_state is None or "last_run" not in current_state:
                self.helper.set_state({"last_run": self.silobreaker_import_start_date})
                last_run = parse(self.silobreaker_import_start_date).astimezone(
                    pytz.UTC
                )
            else:
                last_run = parse(current_state["last_run"]).astimezone(pytz.UTC)
            now = datetime.now().astimezone(pytz.UTC)
            delta = now - last_run
            delta_days = delta.days
            self.helper.connector_logger.info(
                str(delta_days) + " days to process since last run"
            )
            if delta_days < 1:
                self.helper.connector_logger.info(
                    "Need at least one day to process, doing nothing"
                )
                return
            friendly_name = "Silobreaker run @ " + now.strftime("%Y-%m-%d %H:%M:%S")
            work_id = self.helper.api.work.initiate_work(
                self.helper.connect_id, friendly_name
            )
            self.helper.connector_logger.info(
                "Processing the last " + str(delta_days) + " days"
            )
            self._process_lists(work_id, delta_days)
            last_run = now.astimezone(pytz.UTC).isoformat()
            message = "Connector successfully run, storing last_run as " + last_run
            self.helper.connector_logger.info(message)
            self.helper.set_state({"last_run": last_run})
            self.helper.api.work.to_processed(work_id, message)

        except (KeyboardInterrupt, SystemExit):
            self.helper.connector_logger.info(
                "[CONNECTOR] Connector stopped...",
                {"connector_name": self.helper.connect_name},
            )
            sys.exit(0)
        except Exception as err:
            self.helper.connector_logger.error(str(err))

    def run(self):
        if self.duration_period:
            self.helper.schedule_iso(
                message_callback=self.process_message,
                duration_period=self.duration_period,
            )
        else:
            self.helper.schedule_unit(
                message_callback=self.process_message,
                duration_period=self.silobreaker_interval,
                time_unit=self.helper.TimeUnit.MINUTES,
            )


if __name__ == "__main__":
    try:
        silobreakerConnector = Silobreaker()
        silobreakerConnector.run()
    except Exception:
        traceback.print_exc()
        exit(1)
