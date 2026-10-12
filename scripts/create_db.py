import os, time
from dotenv import load_dotenv
from hydra_db import HydraDB
load_dotenv()
c = HydraDB(token=os.environ["HYDRA_DB_API_KEY"])
db = os.environ["HYDRA_DB_DATABASE"]
try:
    r = c.databases.create(database=db, database_metadata_schema=[
        {"name": "deal", "data_type": "VARCHAR", "enable_dense_embedding": False, "enable_sparse_embedding": False, "max_length": 128},
        {"name": "doc_kind", "data_type": "VARCHAR", "enable_dense_embedding": False, "enable_sparse_embedding": False, "max_length": 128},
    ])
    print("created", r.data)
except Exception as e:
    print("create:", type(e).__name__, str(e)[:300])
for _ in range(60):
    s = c.databases.status(database=db).data
    print(s.infra.ready_for_ingestion if s.infra else s)
    if s.infra and s.infra.ready_for_ingestion: break
    time.sleep(5)
