"""
+TODO: Validate billing export is setup for all clusters we 'manage'
+TODO: Write a JSON Schema for the billing source of truth
+TODO: Differentiate between billing accounts we should *invoice* and ones we don't need to *invoice*
+TODO: Write documentation on when to use this and who uses it
"""
import re
import sys
from datetime import datetime, timedelta
from enum import Enum
from pathlib import PosixPath

import gspread
import typer
from google.cloud import bigquery, billing_v1
from google.cloud.logging_v2.services.config_service_v2 import ConfigServiceV2Client
from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML

from .cli_app import app
from .file_acquisition import get_decrypted_file
from .helm_upgrade_decision import get_all_cluster_yaml_files

yaml = YAML(typ="safe")

HERE = PosixPath(__file__).parent.parent


# FIXME: This doesn't actually work yet correctly
def validate_billing_export():
    # Get billing accounts info
    with open(HERE.joinpath("config/billing-accounts.yaml")) as f:
        accounts = yaml.load(f)

    # Create a client
    billing_client = billing_v1.CloudBillingClient()

    managed_billing_accounts = {
        a["id"] for a in accounts["gcp"]["billing_accounts"] if a["managed"]
    }
    logging_client = ConfigServiceV2Client()

    # Handle the response
    for ba in managed_billing_accounts:
        # Make the request
        response = billing_client.get_billing_account(
            name=f"billingAccounts/{ba}",
        )
        if not response.open_:
            print(f"Billing account {ba} is closed!")
            sys.exit(1)

        print(f"trying for {ba}")

        # Make the request
        sinks = list(logging_client.list_sinks(parent=f"billingAccounts/{ba}"))

        # Handle the response
        print(sinks)


def month_validate(month_str: str):
    """
    Validate passed string matches YYYY-MM format.

    Returns values in YYYYMM format, which is used by bigquery
    """
    match = re.match(r"(\d\d\d\d)-(\d\d)", month_str)
    if not match:
        raise typer.BadParameter(f"{month_str} should be formatted as YYYY-MM")
    return f"{match.group(1)}{match.group(2)}"


class CostTableOutputFormats(Enum):
    """
    Output formats supported by the generate-cost-table command
    """

    terminal = "terminal"
    google_sheet = "google-sheet"


@app.command()
def generate_cost_table(
    start_month: str = typer.Option(
        (datetime.utcnow().replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
        help="Starting month (as YYYY-MM) to produce cost data for. Defaults to last invoicing month.",
        callback=month_validate,
    ),
    end_month: str = typer.Option(
        datetime.utcnow().replace(day=1).strftime("%Y-%m"),
        help="Ending month (as YYYY-MM) to produce cost data for. Defaults to current invoicing month",
        callback=month_validate,
    ),
    output: CostTableOutputFormats = typer.Option(
        CostTableOutputFormats.terminal,
        help="Where to output the cost table to",
    ),
    google_sheet_url: str = typer.Option(
        "https://docs.google.com/spreadsheets/d/1URYCMap-Lxm4e_pAAC3Esxda7tZzRhCS6d85pxUiVQs/edit#gid=0",
        help="Write to given Google Sheet URL. Used when --output is google-sheet. billing-spreadsheet-writer@two-eye-two-see.iam.gserviceaccount.com should have Editor rights on this spreadsheet.",
    ),
):
    """
    Generate table with cloud costs for all GCP projects we pass costs through for.
    """

    cluster_files = get_all_cluster_yaml_files()
    client = bigquery.Client()
    rows = []

    for cf in cluster_files:
        with open(cf) as f:
            cluster = yaml.load(f)
        if cluster["provider"] != "gcp":
            # We only support GCP for now
            continue

        if not cluster["gcp"]["billing"]["paid_by_us"]:
            continue

        cluster_project_name = cluster["gcp"]["project"]
        
        bq = cluster["gcp"]["billing"]["bigquery"]

        # WARN: We are using string interpolation here to construct a sql-like query, which
        # IS GENERALLY VERY VERY BAD AND NO GOOD AND WE SHOULD NOT DO IT NO EVER.
        # HOWEVER, I can't seem to find a way to parameterize the *table name* as we must do here,
        # rather than just query parameters. So we *very* carefully construct the name of the table here,
        # and use that in the query. In addition, we allow-list the characters available to the table name as
        # well - and fail hard if something is fishy. This shouldn't really be a problem, as we control the
        # input to this function (via our YAML file). However, SQL Injections are likely to happen in places
        # where you least expect them to happen, so the extra layer of protection is nice.
        table_name = f'{bq["project"]}.{bq["dataset"]}.gcp_billing_export_resource_v1_{bq["billing_id"].replace("-", "_")}'
        # Make sure the table name only has alphanumeric characters, _ and -
        assert re.match(r"^[a-zA-Z0-9._-]+$", table_name)
        query = f"""
        SELECT
        invoice.month as month,
        project.id as project,
        SUM(cost)
            AS total_without_credits,
        (SUM(CAST(cost AS NUMERIC))
            + SUM(IFNULL((SELECT SUM(CAST(c.amount AS NUMERIC))
                        FROM UNNEST(credits) AS c), 0)))
            AS total_with_credits
        FROM `{table_name}`
        WHERE invoice.month >= @start_month
              AND invoice.month <= @end_month
              AND project.id = @project
        GROUP BY 1, 2
        ORDER BY invoice.month ASC
        ;

        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("start_month", "STRING", start_month),
                bigquery.ScalarQueryParameter("end_month", "STRING", end_month),
                bigquery.ScalarQueryParameter("project", "STRING", cluster_project_name)
            ]
        )

        result = client.query(query, job_config=job_config).result()
        last_period = None
        for r in result:
            if not r.project and round(r.total_without_credits) == 0.0:
                # Non-project number is 0$, let's declutter by not showing it
                continue
            year = r.month[:4]
            month = r.month[4:]
            period = f"{year}-{month}"
            rows.append(
                {
                    "period": period,
                    "project": r.project,
                    "total_without_credits": float(r.total_without_credits),
                    "total_with_credits": float(r.total_with_credits),
                }
            )

    # Sort by period in reverse chronological order
    rows.sort(key=lambda r: r["period"], reverse=True)

    if output == CostTableOutputFormats.google_sheet:
        # A service account (https://console.cloud.google.com/iam-admin/serviceaccounts/details/113674037014124702779?project=two-eye-two-see)
        # It is created with no permissions, and the google sheet we want to write to
        # must give write permissions to the email account for the service account
        # In this case, it is  billing-spreadsheet-writer@two-eye-two-see.iam.gserviceaccount.com .
        with get_decrypted_file(
            "config/secrets/enc-billing-gsheets-writer-key.secret.json"
        ) as f:
            gsheets = gspread.service_account(filename=f)

        spreadsheet = gsheets.open_by_url(google_sheet_url)
        worksheet = spreadsheet.get_worksheet(0)
        worksheet.clear()

        worksheet.append_row(
            [
                "WARNING: Do not manually modify, this sheet is autogenerated by the generate-cost-table subcommand of the deployer"
            ]
        )
        worksheet.append_row([f"Last Updated: {datetime.utcnow().isoformat()}"])

        worksheet.append_row(
            [
                "Period",
                "Project",
                "Cost (before Credits)",
                "Cost (after Credits)",
            ]
        )

        worksheet.append_rows(
            [
                [
                    r["period"],
                    r["project"],
                    r["total_without_credits"],
                    r["total_with_credits"],
                ]
                for r in rows
            ]
        )
    else:
        table = Table(title="Project Costs")

        table.add_column("Period", justify="right", style="cyan", no_wrap=True)
        table.add_column("Project", style="white")
        table.add_column("Cost (before credits)", justify="right", style="white")
        table.add_column("Cost (after credits)", justify="right", style="green")

        for r in rows:
            if last_period != None and r["period"] != last_period:
                table.add_section()
            table.add_row(
                r["period"],
                r["project"],
                str(round(r["total_without_credits"], 2)),
                str(round(r["total_with_credits"], 2)),
            )
            last_period = r["period"]

        console = Console()
        console.print(table)
