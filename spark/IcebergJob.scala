package ldp.spark

import java.nio.file.{Files, Path, Paths}

import scala.annotation.tailrec
import scala.util.control.NonFatal

import org.apache.spark.sql.{AnalysisException, DataFrame, SparkSession}
import org.apache.spark.sql.types.{DataType, DateType, NumericType, StringType, StructField, StructType, TimestampNTZType, TimestampType}

/** Settings for one run of [[IcebergJob]], parsed from the command line by [[JobConfig.parse]].
  *
  * @param catalogName Spark catalog name. It must equal the pyiceberg catalog name, because Iceberg's
  *                    JdbcCatalog only sees rows whose `catalog_name` column matches it.
  * @param catalogDb   The SQLite file of the pyiceberg `SqlCatalog` (`<warehouse>/<name>_catalog.db`).
  * @param warehouse   Warehouse location as a `file://` URI. New tables are created under it.
  * @param namespace   Iceberg namespace, dot-separated for nested namespaces.
  * @param table       Source table in `namespace`.
  * @param outputTable Table in `namespace` the aggregate is written to (replaced if it exists).
  * @param showRows    How many rows to print for each result.
  * @param extraConf   Extra Spark settings from `--conf key=value`, applied after the catalog settings.
  * @param printConf   Print the Spark settings and exit without starting Spark.
  */
final case class JobConfig(
    catalogName: String,
    catalogDb: Path,
    warehouse: String,
    namespace: Seq[String],
    table: String,
    outputTable: String,
    showRows: Int,
    extraConf: Map[String, String],
    printConf: Boolean
) {
  def source: String = Sql.ident(catalogName +: namespace :+ table)
  def target: String = Sql.ident(catalogName +: namespace :+ outputTable)
  def snapshots: String = Sql.ident(catalogName +: namespace :+ table :+ "snapshots")
  def namespaceIdent: String = Sql.ident(catalogName +: namespace)
  def sparkConf: Map[String, String] = CatalogConf(catalogName, catalogDb, warehouse) ++ extraConf
}

object JobConfig {

  sealed trait Command
  final case class Run(config: JobConfig) extends Command
  case object Help extends Command

  val Usage: String =
    """Usage: scala-cli run spark -- --catalog-name NAME --catalog-db FILE --warehouse DIR
      |                             --namespace NS --table TABLE [--output-table TABLE]
      |                             [--show-rows N] [--conf key=value ...] [--print-conf]
      |
      |  --catalog-name  pyiceberg catalog name (LocalIcebergCatalog name, i.e. the config "identifier")
      |  --catalog-db    SQLite catalog file, <warehouse>/<name>_catalog.db
      |  --warehouse     Warehouse folder or file:// URI (the config "warehouse_path")
      |  --namespace     Iceberg namespace (the config "identifier")
      |  --table         Source table in the namespace
      |  --output-table  Table to write the aggregate to (default: <table>_spark_summary)
      |  --show-rows     Rows to print per result (default: 10)
      |  --conf          Extra Spark setting, repeatable
      |  --print-conf    Print the Spark settings and exit without starting Spark
      |""".stripMargin

  private val ValueFlags =
    Set("--catalog-name", "--catalog-db", "--warehouse", "--namespace", "--table", "--output-table", "--show-rows", "--conf")
  private val SwitchFlags = Set("--print-conf", "--help", "-h")

  def parse(args: Seq[String]): Either[String, Command] =
    collect(args.toList, Vector.empty).flatMap { pairs =>
      if (pairs.exists { case (flag, _) => flag == "--help" || flag == "-h" }) Right(Help)
      else build(pairs).map(Run)
    }

  private def build(pairs: Vector[(String, String)]): Either[String, JobConfig] = {
    val single = pairs.filterNot(_._1 == "--conf").toMap
    def required(flag: String): Either[String, String] =
      single.get(flag).map(_.trim).filter(_.nonEmpty).toRight(s"Missing required argument $flag")

    for {
      catalogName <- required("--catalog-name").flatMap(checkName("--catalog-name", _))
      catalogDb <- required("--catalog-db").map(Paths.get(_)).map(realPath)
      warehouse <- required("--warehouse").map(warehouseUri)
      namespace <- required("--namespace").flatMap(splitNamespace)
      table <- required("--table").flatMap(checkName("--table", _))
      outputTable <- single.get("--output-table").fold(Right(s"${table}_spark_summary"): Either[String, String])(
        checkName("--output-table", _))
      _ <- if (outputTable == table) Left("--output-table must differ from --table") else Right(())
      showRows <- single.get("--show-rows").fold(Right(10): Either[String, Int])(positiveInt("--show-rows", _))
      extraConf <- confPairs(pairs.collect { case ("--conf", kv) => kv })
    } yield JobConfig(catalogName, catalogDb, warehouse, namespace, table, outputTable, showRows, extraConf,
      printConf = single.contains("--print-conf"))
  }

  @tailrec
  private def collect(rest: List[String], acc: Vector[(String, String)]): Either[String, Vector[(String, String)]] =
    rest match {
      case Nil => Right(acc)
      case arg :: tail if arg.startsWith("--") && arg.contains('=') =>
        val (flag, value) = arg.splitAt(arg.indexOf('='))
        if (ValueFlags(flag)) collect(tail, acc :+ (flag -> value.drop(1)))
        else Left(s"Unknown argument: $flag")
      case flag :: tail if SwitchFlags(flag) => collect(tail, acc :+ (flag -> "true"))
      case flag :: value :: tail if ValueFlags(flag) && !value.startsWith("--") => collect(tail, acc :+ (flag -> value))
      case flag :: _ if ValueFlags(flag) => Left(s"$flag needs a value")
      case other :: _ => Left(s"Unknown argument: $other")
    }

  private def checkName(flag: String, name: String): Either[String, String] =
    if (name.isEmpty || name.contains('.') || name.exists(_.isWhitespace))
      Left(s"$flag must be a single name without dots or spaces, got '$name'")
    else Right(name)

  private def splitNamespace(namespace: String): Either[String, Seq[String]] = {
    val levels = namespace.split('.').toSeq
    if (levels.exists(_.trim.isEmpty)) Left(s"--namespace has an empty level: '$namespace'") else Right(levels)
  }

  private def positiveInt(flag: String, value: String): Either[String, Int] =
    value.toIntOption.filter(_ > 0).toRight(s"$flag must be a positive integer, got '$value'")

  private def confPairs(items: Seq[String]): Either[String, Map[String, String]] =
    items.foldLeft(Right(Map.empty): Either[String, Map[String, String]]) { (acc, item) =>
      acc.flatMap { conf =>
        item.split("=", 2) match {
          case Array(key, value) if key.trim.nonEmpty => Right(conf + (key.trim -> value))
          case _ => Left(s"--conf expects key=value, got '$item'")
        }
      }
    }

  /** Resolve symlinks (macOS `/tmp` is `/private/tmp`) so paths match what pyiceberg wrote. */
  private[spark] def realPath(path: Path): Path =
    if (Files.exists(path)) path.toRealPath() else path.toAbsolutePath.normalize()

  private[spark] def warehouseUri(value: String): String =
    if (value.startsWith("file:")) value.stripSuffix("/")
    else s"file://${realPath(Paths.get(value))}"
}

/** Spark settings that register the pyiceberg SQLite catalog as an Iceberg `SparkCatalog`.
  *
  * The same map is built by `spark_catalog_conf()` in `local_data_platform.engine.spark`, so PySpark and
  * this job see the same tables. `jdbc.schema-version=V1` matches the `iceberg_type` column pyiceberg's
  * SqlCatalog creates; with a fresh database JdbcCatalog also creates that same V1 layout.
  */
object CatalogConf {
  val SqlExtensions = "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions"

  def apply(catalogName: String, catalogDb: Path, warehouse: String): Map[String, String] = {
    val prefix = s"spark.sql.catalog.$catalogName"
    Map(
      "spark.sql.extensions" -> SqlExtensions,
      prefix -> "org.apache.iceberg.spark.SparkCatalog",
      s"$prefix.catalog-impl" -> "org.apache.iceberg.jdbc.JdbcCatalog",
      s"$prefix.uri" -> s"jdbc:sqlite:$catalogDb",
      s"$prefix.warehouse" -> warehouse,
      s"$prefix.jdbc.schema-version" -> "V1",
      "spark.sql.defaultCatalog" -> catalogName,
      "spark.sql.session.timeZone" -> "UTC"
    )
  }

  /** Settings for a small single-machine session; not part of the catalog contract. */
  val LocalSession: Map[String, String] = Map(
    "spark.ui.enabled" -> "false",
    "spark.sql.shuffle.partitions" -> "4"
  )
}

object Sql {
  def quote(part: String): String = "`" + part.replace("`", "``") + "`"
  def ident(parts: Seq[String]): String = parts.map(quote).mkString(".")
}

/** The columns needed for "revenue by city per day", found by name and type. */
final case class RevenueColumns(time: StructField, city: StructField, amount: StructField) {
  def aggregateSql(source: String): String =
    s"""SELECT CAST(${Sql.quote(time.name)} AS DATE) AS day,
       |       ${Sql.quote(city.name)},
       |       COUNT(*) AS row_count,
       |       CAST(SUM(${Sql.quote(amount.name)}) AS DOUBLE) AS revenue
       |FROM $source
       |GROUP BY 1, 2
       |ORDER BY 1, 2""".stripMargin
}

object RevenueColumns {
  private val TimeNames = Seq("pickup_ts", "pickup_datetime", "tpep_pickup_datetime", "pickup_time", "event_ts", "ts")
  private val CityNames = Seq("city", "pickup_city", "borough", "zone")
  private val AmountNames = Seq("fare", "fare_amount", "total_amount", "revenue", "amount", "price")

  def detect(schema: StructType): Option[RevenueColumns] =
    for {
      time <- byName(schema, TimeNames)(isTemporal).orElse(schema.fields.find(f => isTemporal(f.dataType)))
      city <- byName(schema, CityNames)(_ == StringType)
      amount <- byName(schema, AmountNames)(isNumeric)
    } yield RevenueColumns(time, city, amount)

  private def byName(schema: StructType, names: Seq[String])(accepts: DataType => Boolean): Option[StructField] = {
    val fields = schema.fields.map(f => f.name.toLowerCase -> f).toMap
    names.iterator.flatMap(fields.get).find(f => accepts(f.dataType))
  }

  private def isTemporal(dataType: DataType): Boolean = dataType match {
    case TimestampType | TimestampNTZType | DateType => true
    case _ => false
  }

  private def isNumeric(dataType: DataType): Boolean = dataType.isInstanceOf[NumericType]
}

/** What the job did, printed as the last stdout line for scripts to parse. */
final case class JobResult(source: String, sourceRows: Long, target: String, targetRows: Long, mode: String) {
  def line: String = s"LDP_SPARK_RESULT source=$source source_rows=$sourceRows target=$target " +
    s"target_rows=$targetRows mode=$mode"
}

final class JobError(message: String) extends RuntimeException(message)

/** Query a local-data-platform Iceberg table with Spark SQL and write an aggregate back.
  *
  * The job opens the SQLite catalog that pyiceberg (`LocalIcebergCatalog`) writes, through Iceberg's
  * JdbcCatalog, so Spark and Python share one set of tables. It
  *   1. counts the source table's rows,
  *   2. computes revenue by city per day when the table has a timestamp, a city and an amount column,
  *      otherwise prints the first rows,
  *   3. prints the table's snapshot history from the `snapshots` metadata table, and
  *   4. writes the aggregate (or a row count) to `<namespace>.<output-table>` with CREATE OR REPLACE.
  */
object IcebergJob {

  def main(args: Array[String]): Unit =
    JobConfig.parse(args.toSeq) match {
      case Left(problem) =>
        System.err.println(s"error: $problem\n\n${JobConfig.Usage}")
        sys.exit(2)
      case Right(JobConfig.Help) =>
        println(JobConfig.Usage)
      case Right(JobConfig.Run(config)) if config.printConf =>
        config.sparkConf.toSeq.sorted.foreach { case (key, value) => println(s"$key=$value") }
      case Right(JobConfig.Run(config)) =>
        val exitCode =
          try {
            println(run(config).line)
            0
          } catch {
            case error: JobError =>
              System.err.println(s"error: ${error.getMessage}")
              1
            case NonFatal(error) =>
              System.err.println(s"error: ${error.getClass.getSimpleName}: ${error.getMessage}")
              1
          }
        sys.exit(exitCode)
    }

  def run(config: JobConfig): JobResult = {
    if (!Files.isRegularFile(config.catalogDb))
      throw new JobError(s"Catalog database not found: ${config.catalogDb}. Write a table with " +
        "local-data-platform first, or check --catalog-db.")

    val spark = session(config)
    try analyse(spark, config)
    finally spark.stop()
  }

  def session(config: JobConfig): SparkSession = {
    val builder = SparkSession.builder().appName("ldp-iceberg-job").master("local[*]")
    val spark = (CatalogConf.LocalSession ++ config.sparkConf)
      .foldLeft(builder) { case (b, (key, value)) => b.config(key, value) }
      .getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    spark
  }

  private def analyse(spark: SparkSession, config: JobConfig): JobResult = {
    val source = loadSource(spark, config)
    val sourceRows = source.count()
    section(s"Spark ${spark.version} read ${config.source}: $sourceRows rows")
    source.printSchema()

    val (mode, aggregateSql) = RevenueColumns.detect(source.schema) match {
      case Some(columns) =>
        section(s"Revenue by ${columns.city.name} per day (${columns.amount.name} summed over " +
          s"${columns.time.name} days)")
        ("revenue_by_city_day", columns.aggregateSql(config.source))
      case None =>
        section(s"No timestamp/city/amount columns found; first ${config.showRows} rows")
        show(spark.sql(s"SELECT * FROM ${config.source}"), config.showRows)
        ("row_count", s"SELECT COUNT(*) AS row_count FROM ${config.source}")
    }
    show(spark.sql(aggregateSql), config.showRows)

    section(s"Snapshot history of ${config.snapshots}")
    show(spark.sql(
      s"""SELECT committed_at, snapshot_id, parent_id, operation,
         |       summary['added-records'] AS added_records, summary['total-records'] AS total_records
         |FROM ${config.snapshots}
         |ORDER BY committed_at""".stripMargin), config.showRows)

    spark.sql(s"CREATE OR REPLACE TABLE ${config.target} USING iceberg AS $aggregateSql")
    val targetRows = spark.table(config.target).count()
    section(s"Wrote ${config.target}: $targetRows rows")
    show(spark.sql(s"SELECT snapshot_id, operation, summary['added-records'] AS added_records " +
      s"FROM ${Sql.ident(config.catalogName +: config.namespace :+ config.outputTable :+ "snapshots")} " +
      "ORDER BY committed_at"), config.showRows)

    JobResult(config.source, sourceRows, config.target, targetRows, mode)
  }

  private def loadSource(spark: SparkSession, config: JobConfig): DataFrame =
    try spark.table(config.source)
    catch {
      case _: AnalysisException =>
        val namespace = config.namespace.mkString(".")
        // A missing namespace makes SHOW TABLES throw Iceberg's NoSuchNamespaceException, which is not an
        // AnalysisException, so catch every non-fatal error here.
        val known =
          try Some(spark.sql(s"SHOW TABLES IN ${config.namespaceIdent}").collect().map(_.getString(1)).sorted)
          catch { case NonFatal(_) => None }
        val detail = known match {
          case Some(names) => s"Tables in $namespace: ${if (names.isEmpty) "none" else names.mkString(", ")}"
          case None => s"Namespace $namespace does not exist in catalog ${config.catalogName}; check --namespace, " +
              "and that --catalog-name is the pyiceberg catalog name"
        }
        throw new JobError(s"Table ${config.source} not found in catalog ${config.catalogName} " +
          s"(${config.catalogDb}). $detail")
    }

  private def section(title: String): Unit = println(s"\n== $title")

  private def show(df: DataFrame, rows: Int): Unit = df.show(rows, truncate = false)
}
