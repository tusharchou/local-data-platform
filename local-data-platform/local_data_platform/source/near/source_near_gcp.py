from local_data_platform.source.gcp.gcp_bigquery import SourceGCPBigQuery

class Source_Near_GCP(SourceGCPBigQuery):

    def fetch_near_transaction(self):
        query = """
            SELECT *
            FROM `nearprotocol.near.transaction`
            WHERE DATE(block_timestamp) = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
        """
        return self.fetch(query)