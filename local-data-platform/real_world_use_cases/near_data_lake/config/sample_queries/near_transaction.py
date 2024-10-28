from collections import namedtuple

Transaction = namedtuple('Transaction', ['query', 'desc'])

NEAR_TRANSACTION_QUERY = Transaction (
    query="SELECT * FROM `bigquery-public-data.crypto_near.transactions` LIMIT 1000",
    desc="Fetches the first 1000 transactions from the Near blockchain."
)
