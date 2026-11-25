# BI tools on the gold DuckDB file

Superset, Metabase and Evidence can read a DuckDB file, so the file that `ldp gold duckdb` writes is a way to put the gold layer in front of them. A tool does not have to know about Iceberg, a catalog or the lake, and it reads the same release as every other reader. This page has a recipe for each of the three.

**Every recipe on this page is not tested in CI.** Each needs Docker or Node.js and a server, and a test run starts none. Each recipe says when it was last verified by hand and with which versions. A tool changes faster than this page does, so a recipe that is a few releases old may need a change. The first two sections, which are the part with `ldp` in it, are run by the tests.

The examples use the restaurant example of [the gold guide](gold.md), and any gold table works the same way.

## Write the file

```bash
ldp gold duckdb config --out out/gold.duckdb
```

<!-- docs-runner: output -->
```text
wrote out/gold.duckdb, ... from release ..., cut ... UTC, ... KB
```

`config` is the folder of the gold configs. The file has the gold tables of the current release in the `main` schema under their own names, a comment on each table and column from its contract, a `_catalog` table that says what each table is and who owns it, and a `_metrics` table with the definition of each metric. All three recipes read this file, and none of them needs anything else. It is written in the storage version of DuckDB 1.0, so every DuckDB from 1.0 on opens it, and a tool does not have to match the DuckDB of LDP.

`ldp gold build config --duckdb out/gold.duckdb` writes the file again right after every release. The new file is written to a temporary path and renamed over the old one, so a tool sees the old file or the new one and never half of one. What a running tool does when the file is replaced differs, and each recipe says what it does.

Three things hold for every recipe.

- **Open the file read-only.** Without it, a connection that can write holds a lock on the file, and a second process that opens it, such as the DuckDB command line or a worker of the tool, is refused with `Conflicting lock is held`. A file on a read-only mount cannot be opened for writing at all, and the error is `Cannot open file ... Read-only file system`.
- **In Docker, mount the folder that holds the file and not the file.** A mount of one file follows the file that was there and not its name. On macOS with Colima, after the file was replaced by a rename, as `ldp` does, a mount of the file lost it for good and the container reported that it did not exist. A mount of the folder showed the new file within a second.
- **A copy is the default, and it is what the recipes use.** A copy needs no extension and no access to the lake, so the file can be mounted into a container or put on a shared drive.

## Copies, or views for a table that is too large to copy

A copy of a table is read into memory before it is written, so a table has to fit in memory. For a table that does not, `--views` writes a view of each table in place of a copy. A view is `SELECT * FROM iceberg_scan(...)` on the metadata file of the table and the snapshot that the release names. The file is small, and it shows the same rows, comments and catalog as a file of copies.

<!-- docs-runner: skip needs DuckDB's iceberg extension, which CI does not install, and a test runs it where it is there -->
```bash
ldp gold duckdb config --out out/gold_views.duckdb --views
```

A view reads the table where it is, so the tool needs three things that a copy does not.

- **DuckDB's iceberg extension.** A DuckDB that can reach extensions.duckdb.org installs it the first time that a view is read. This happened in the Superset image, in Metabase and in Evidence. A DuckDB that may not install extensions says `Table Function with name "iceberg_scan" is not in the catalog, but it exists in the iceberg extension`, and has to be given the extension once with `INSTALL iceberg`. Writing the views needs the extension too, and `ldp` never downloads it, so it stops with exit status 1 and writes nothing when it is missing.
- **The warehouse at the address the view holds.** For a local warehouse that is a `file://` address of the host. In Docker, mount the warehouse into the container at the same path that it has on the host. When it is not there, the error names the path that the view holds, as in `Cannot open file ... metadata.json: No such file or directory`.
- **The snapshot.** A view reads the snapshot of the release, so a later build does not change what it shows, and `ldp maintain` never expires a released snapshot. A file that was written with `--live` reads a snapshot that no release holds, and maintenance may expire it.

## Superset

**Not tested in CI. Last verified 2026-11-25** with `apache/superset:6.1.0`, `duckdb-engine` 0.17.0, `duckdb` 1.5.6 and SQLAlchemy 1.4.54, on macOS with Colima.

Superset runs from its own image because it cannot share an environment with LDP. `apache-superset` 6.1.0 needs `sqlalchemy<2,>=1.4` and `pyarrow<19,>=16.1.0`. LDP needs SQLAlchemy 2, because the SQL catalog of pyiceberg asks for 2.0.18 or newer, so the two cannot be installed together. For that reason LDP has no Superset extra. Superset has an extra for the driver, `apache-superset[duckdb]`, which names `duckdb-engine` and `duckdb` 1.4.2 or newer and below 2, and the Dockerfile below installs the same two packages.

The image of Superset has no database driver in it, so the recipe adds one. This `Dockerfile` follows the page of Superset on building your own image.

```dockerfile
FROM apache/superset:6.1.0
USER root
RUN . /app/.venv/bin/activate && uv pip install "duckdb-engine>=0.17.0" "duckdb>=1.4.2,<2"
USER superset
```

Save that as `Dockerfile`, build it, start it with the folder that holds `gold.duckdb` mounted read-only, and set Superset up. The last command asks for a password.

<!-- docs-runner: skip not tested in CI, it needs Docker and a Superset image -->
```bash
docker build -t superset-duckdb:6.1.0 .
docker run -d --name superset -p 8088:8088 -e SUPERSET_SECRET_KEY="$(openssl rand -base64 42)" -v "$PWD/out:/data:ro" superset-duckdb:6.1.0
docker exec superset superset db upgrade
docker exec superset superset init
docker exec -it superset superset fab create-admin --username admin --firstname Admin --lastname User --email admin@example.com
```

This keeps the settings of Superset in a SQLite file inside the container, so they are lost when the container is removed. It is for trying the file out, and Superset's own documentation says how to run it for real.

Then connect the file.

1. Open `http://localhost:8088` and log in.
2. Open Settings, and under Data choose Database Connections.
3. Click `+ Database`, and in the list of supported databases choose DuckDB.
4. The form that follows is for MotherDuck. Click the link Connect this database with a SQLAlchemy URI string instead.
5. Give the database a display name, such as Gold layer, and as the SQLAlchemy URI write `duckdb:////data/gold.duckdb?access_mode=read_only`. The four slashes are the three of the scheme and the one that starts the absolute path of the file in the container, and `access_mode=read_only` is passed to DuckDB.
6. Click Test connection, and then Connect.

Open SQL Lab. Superset lists the schemas `gold.main`, `system.information_schema`, `system.main` and `temp.main`. The tables are in `gold.main`, which has `_catalog`, `_metrics` and the gold tables, and a query names a table without a schema.

```sql
SELECT city, round(sum(revenue), 2) AS revenue FROM gold_daily_sales GROUP BY city ORDER BY revenue DESC
```

With the restaurant example this gives five cities, from Mumbai with 263,900 to Bengaluru with 30,100. Superset opens the file again for each query, so after `ldp` replaces the file the next query reads the new one, with no restart. For a file of views, add `-v "$HOME/lake/warehouse:$HOME/lake/warehouse:ro"` to `docker run`, with the path of your own warehouse on both sides.

## Metabase

**Not tested in CI. Last verified 2026-11-25** with Metabase 0.65.2 and version 1.5.6.0 of the DuckDB driver, in the image `ghcr.io/motherduckdb/metabase-duckdb:0.65.2-duckdb1.5.6.0`, on macOS with Colima.

The driver is the open-source DuckDB driver of MotherDuck, and Metabase says on its form that a community driver is not supported by Metabase. It does not work in the default image of Metabase, which is built on Alpine, because the DuckDB library needs glibc, and the README of the driver says so. MotherDuck publishes an image that has the driver in it and is built on a glibc base, which was Ubuntu 22.04 in the version verified.

<!-- docs-runner: skip not tested in CI, it needs Docker and a Metabase image -->
```bash
docker run -d --name metabase -p 3000:3000 -v "$PWD/out:/data:ro" ghcr.io/motherduckdb/metabase-duckdb:0.65.2-duckdb1.5.6.0
```

Metabase is ready in about 20 seconds. In this setup it keeps its own settings in the container, so they are lost when the container is removed, and a trial is all that it is good for. Metabase's documentation says to use an application database such as Postgres for more.

1. Open `http://localhost:3000` and create the first account.
2. In the sidebar, next to Data, click `+`. In the dialog choose Database, search for duck and choose DuckDB.
3. Give it a display name, such as Gold layer, and as the Database file write `/data/gold.duckdb`.
4. Switch on Establish a read-only connection.
5. Click Test connection, and then Save.

Metabase syncs the file, and the tables appear under Browse data with names that it makes readable, so `gold_daily_sales` is Gold Daily Sales, `_catalog` is Catalog and `_metrics` is Metrics. To ask a question in SQL, open the New menu, choose SQL query, choose the database and write the query that the Superset recipe uses.

Metabase keeps its connection open, so after `ldp` replaces the file it goes on reading the file that it opened. In the verified version a query still saw the old file after the replacement, and saw the new one after `docker restart metabase`. For a file of views, add `-v "$HOME/lake/warehouse:$HOME/lake/warehouse:ro"` to `docker run`, with the path of your own warehouse on both sides. Metabase then downloads the iceberg extension into `/home/metabase/.duckdb` the first time that a view is read.

## Evidence

**Not tested in CI. Last verified 2026-11-24** with `@evidence-dev/evidence` 40.1.8 and `@evidence-dev/duckdb` 2.0.1, on Node.js 26.9.0 and npm 11.19.1.

Evidence is an open-source framework, under the MIT license, that turns Markdown and SQL into a website. Its documentation and its default install have moved to a hosted product and a command line tool of its own, and the pages of that product do not describe DuckDB files. This recipe uses the open-source project that runs on Node.js, which is the `legacy` branch of the template and is described at `legacy-docs.evidence.dev`. It needs Node.js 18 or newer.

<!-- docs-runner: skip not tested in CI, it needs Node.js and downloads the template and its packages -->
```bash
npx degit evidence-dev/template#legacy gold-reports
cd gold-reports
npm install
mkdir sources/gold
cp ../out/gold.duckdb sources/gold/gold.duckdb
```

A source is a folder under `sources`, and the DuckDB file goes in it. Its connection is a file called `sources/gold/connection.yaml`.

```yaml
name: gold
type: duckdb
options:
  filename: gold.duckdb
```

Each SQL file in the folder is a query that Evidence runs against the file, and the page asks for it as `gold.` and the name of the file. `sources/gold/daily_sales.sql` has one line.

```sql
select * from gold_daily_sales
```

A second file, `sources/gold/catalog.sql` with `select * from _catalog`, gives a page that says what each table is and who owns it. A page, `pages/index.md`, then draws the first query.

````markdown
```sql revenue_by_city
select city, round(sum(revenue), 2) as revenue
from gold.daily_sales
group by city
order by revenue desc
```

<BarChart data={revenue_by_city} x=city y=revenue />
````

<!-- docs-runner: skip not tested in CI, it needs Node.js and starts a server -->
```bash
npm run sources
npm run dev
```

`npm run sources` runs each query against the file and stores the rows, and `npm run dev` serves the site and prints its address. With the restaurant example the page shows Mumbai, Hyderabad, Kolkata, Delhi and Bengaluru, with Mumbai first, and `npm run build` writes the same site as static files to `build`. Evidence copies the rows when `npm run sources` runs and holds no connection to the file, so after each release run `ldp gold build config --duckdb out/gold.duckdb` and then `npm run sources` again. A file of views works the same way, because the DuckDB inside Evidence installs the iceberg extension the first time that it reads a view.
