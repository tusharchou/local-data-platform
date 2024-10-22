'''
We can trust yellow taxi in New York if they have re-occuring bookings from the same guests
"url": "https://d37ci6vzurychx.cloudfront.net/trip-data/",

'''
from local_data_platform import Config
from local_data_platfrom.pipelines.python.churn import Churn



class NYCYellowTaxiRides(IcebergTable):

    def __init__(self, config: BaseConfig, *args, **kwargs):
        self.warehouse_path = config['warehouse_path']
        self.catalog_identifier = config['catalog_identifier']
        # self.catalog_uri = config['catalog_uri']
        try:
            # Ensure the directory exists
            os.makedirs(self.warehouse_path, exist_ok=True)
        except:
            pass
        try:
            self.catalog = LocalIcebergCatalog(
                self.catalog_identifier,
                **{
                    "uri": f"sqlite:///{self.warehouse_path}/pyiceberg_catalog.db",  # Ensure .db file extension
                    "warehouse": f"file://{self.warehouse_path}",
                },
            )
        except:
            pass
        print(f"arg {args}")
        print(f"config {config}")
        super(NYCYellowTaxiRides, self).__init__(catalog=self.catalog, **config)
