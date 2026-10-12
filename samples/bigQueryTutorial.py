"""Run one BigQuery query with the BigQuery source and print the first rows.

You need a Google Cloud service-account key with BigQuery access, and the extra::

    pip install -e ".[bigquery]"
    python samples/bigQueryTutorial.py ~/.config/local-data-platform/gcp-service-account.json

The query is read from
``examples/near_data_lake/config/sample_queries/near_transaction.json``. BigQuery
bills for the bytes a query scans; the source runs the query exactly once.

To save the result as a CSV instead, run the NEAR example's ingestion config with
``ldp run examples/near_data_lake/config/ingestion.json``.
"""

import argparse
from pathlib import Path

from local_data_platform.store.source.gcp.bigquery import BigQuery, GCPCredentials

REPO_ROOT = Path(__file__).resolve().parents[1]
QUERY_FILE = REPO_ROOT / "examples" / "near_data_lake" / "config" / "sample_queries" / "near_transaction.json"


def main(argv: list[str] | None = None) -> None:
    """Parse the key path, run the sample query and print its first rows.

    Args:
        argv: Command-line arguments, without the program name. ``None`` reads
            ``sys.argv``.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("credentials", type=Path, help="path to a service-account JSON key file")
    parser.add_argument("--query-file", type=Path, default=QUERY_FILE,
                        help='JSON file holding {"query": "..."} (default: the NEAR sample query)')
    args = parser.parse_args(argv)

    credentials = GCPCredentials(args.credentials)
    source = BigQuery("near_transactions", credentials, path=args.query_file)
    table = source.get()
    print(f"{table.num_rows} rows, columns: {', '.join(table.column_names)}")
    print(table.slice(0, 5).to_pylist())


if __name__ == "__main__":
    main()
