// Per-robot aggregate of the robot-episode example's gold table, computed by Spark.
//
// run.py --spark runs this with scala-cli and passes the Iceberg catalog settings that
// local_data_platform.engine.spark.spark_catalog_conf() builds, so Spark opens the same SQLite
// catalog pyiceberg writes (Iceberg JdbcCatalog). The job reads <source>, writes one row per robot
// to <target> with CREATE OR REPLACE TABLE ... USING iceberg, and prints a LDP_SPARK_RESULT line.
//
//   scala-cli run examples/robot_episodes/spark/robot_aggregate.scala -- \
//     --source robots.robots.gold_episode_stats --target robots.robots.gold_robot_summary_spark \
//     --conf key=value ...   (catalog.namespace.table: the local catalog is named after its namespace)
//
// Versions follow spark/project.scala (Spark 4.1.3, Iceberg 1.12.0, Scala 2.13, Java 17).

//> using scala 2.13.17
//> using jvm temurin:17
//> using dep org.apache.spark::spark-sql:4.1.3
//> using dep org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.12.0
//> using dep org.xerial:sqlite-jdbc:3.53.4.0
//> using javaOpt -XX:+IgnoreUnrecognizedVMOptions
//> using javaOpt --add-modules=jdk.incubator.vector
//> using javaOpt --add-opens=java.base/java.lang=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.lang.invoke=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.lang.reflect=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.io=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.net=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.nio=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.util=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.util.concurrent=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.util.concurrent.atomic=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/jdk.internal.ref=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/sun.nio.ch=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/sun.nio.cs=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/sun.security.action=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/sun.util.calendar=ALL-UNNAMED
//> using javaOpt -Djdk.reflect.useDirectMethodHandle=false
//> using javaOpt -Dio.netty.tryReflectionSetAccessible=true
//> using javaOpt -Xmx2g

import org.apache.spark.sql.SparkSession

object RobotAggregate {

  private val Usage = "Usage: robot_aggregate.scala -- --source CATALOG.NS.TABLE --target CATALOG.NS.TABLE [--conf key=value ...]"

  def main(args: Array[String]): Unit = {
    val (options, conf) = parse(args.toList, Map.empty, Vector.empty)
    val source = options.getOrElse("--source", fail("--source is required"))
    val target = options.getOrElse("--target", fail("--target is required"))
    val builder = SparkSession.builder().appName("ldp-robot-aggregate").master("local[*]")
      .config("spark.ui.enabled", "false")
      .config("spark.sql.shuffle.partitions", "4")
    val spark = conf.foldLeft(builder) { case (b, (key, value)) => b.config(key, value) }.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    try {
      val aggregate =
        s"""SELECT robot_id,
           |       COUNT(*) AS episodes,
           |       ROUND(AVG(CASE WHEN success THEN 1.0 ELSE 0.0 END), 3) AS success_rate,
           |       CAST(SUM(frames_recorded) AS BIGINT) AS frames,
           |       ROUND(AVG(drop_rate), 4) AS mean_drop_rate,
           |       ROUND(MAX(max_skew_ms), 2) AS worst_skew_ms
           |FROM $source
           |GROUP BY robot_id""".stripMargin
      spark.sql(s"CREATE OR REPLACE TABLE $target USING iceberg AS $aggregate")
      val result = spark.table(target).orderBy("robot_id")
      result.show(20, truncate = false)
      println(s"LDP_SPARK_RESULT source=$source target=$target target_rows=${result.count()} spark=${spark.version}")
    } finally spark.stop()
  }

  @annotation.tailrec
  private def parse(rest: List[String], options: Map[String, String], conf: Vector[(String, String)])
      : (Map[String, String], Vector[(String, String)]) =
    rest match {
      case Nil => (options, conf)
      case "--conf" :: pair :: tail =>
        pair.split("=", 2) match {
          case Array(key, value) if key.nonEmpty => parse(tail, options, conf :+ (key -> value))
          case _ => fail(s"--conf expects key=value, got '$pair'")
        }
      case flag :: value :: tail if flag == "--source" || flag == "--target" => parse(tail, options + (flag -> value), conf)
      case other :: _ => fail(s"unknown argument '$other'")
    }

  private def fail(message: String): Nothing = {
    System.err.println(s"error: $message\n$Usage")
    sys.exit(2)
  }
}
